"""Read-only table browsing using the same PostgreSQL settings as the agents."""
import json
import os
import re
from contextlib import contextmanager

import psycopg2
from psycopg2 import sql

SCHEMA = "public"
PREVIEW_LIMIT = 50


class DatabaseNotConfigured(Exception):
    pass


class InvalidDatabaseRequest(Exception):
    pass


def display_value(value):
    """Preserve numeric precision and render PostgreSQL values as plain text."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


class DatabaseBrowser:
    @contextmanager
    def cursor(self):
        names = ("host", "port", "user", "password", "database")
        config = {name: os.getenv(name, "") for name in names}
        if any(not value.strip() for value in config.values()):
            raise DatabaseNotConfigured()
        config["dbname"] = config.pop("database")
        connection = psycopg2.connect(
            **config, connect_timeout=5, options="-c statement_timeout=5000"
        )
        try:
            connection.set_session(readonly=True, autocommit=False)
            with connection:
                with connection.cursor() as cursor:
                    yield cursor
        finally:
            connection.close()

    @staticmethod
    def _tables(cursor):
        cursor.execute(
            """
            SELECT t.table_name,
                   (SELECT count(*) FROM information_schema.columns c
                    WHERE c.table_schema = t.table_schema
                      AND c.table_name = t.table_name) AS column_count
            FROM information_schema.tables t
            WHERE t.table_schema = %s AND t.table_type = 'BASE TABLE'
              AND has_table_privilege(
                  format('%%I.%%I', t.table_schema, t.table_name), 'SELECT')
            ORDER BY t.table_name
            """,
            (SCHEMA,),
        )
        return [
            {"schema": SCHEMA, "name": name, "column_count": count}
            for name, count in cursor.fetchall()
        ]

    def list_tables(self):
        with self.cursor() as cursor:
            tables = self._tables(cursor)
        return {"database": os.getenv("database", ""), "schema": SCHEMA, "tables": tables}

    def preview_table(self, name):
        with self.cursor() as cursor:
            if not any(table["name"] == name for table in self._tables(cursor)):
                raise LookupError("Table not found.")
            cursor.execute(
                sql.SQL("SELECT * FROM {} LIMIT %s").format(sql.Identifier(SCHEMA, name)),
                (PREVIEW_LIMIT + 1,),
            )
            columns = [column.name for column in cursor.description]
            rows = cursor.fetchall()
        return {
            "schema": SCHEMA,
            "name": name,
            "columns": columns,
            "rows": [[display_value(value) for value in row] for row in rows[:PREVIEW_LIMIT]],
            "limit": PREVIEW_LIMIT,
            "has_more": len(rows) > PREVIEW_LIMIT,
        }

    def schema_details(self):
        """Give agents actual column names, keys and sample values via the API."""
        with self.cursor() as cursor:
            tables = self._tables(cursor)
            for table in tables:
                cursor.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                    (SCHEMA, table["name"]),
                )
                table["columns"] = [{"name": name, "type": kind} for name, kind in cursor.fetchall()]
                cursor.execute(sql.SQL("SELECT * FROM {} LIMIT 3").format(sql.Identifier(SCHEMA, table["name"])))
                table["sample_rows"] = [[display_value(value) for value in row] for row in cursor.fetchall()]
            cursor.execute(
                """SELECT c.relname, con.conname, pg_get_constraintdef(con.oid)
                   FROM pg_constraint con
                   JOIN pg_class c ON c.oid = con.conrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = %s AND con.contype IN ('p', 'f')""", (SCHEMA,)
            )
            keys = [{"table": table, "name": name, "definition": definition}
                    for table, name, definition in cursor.fetchall()
                    if any(item["name"] == table for item in tables)]
        return {"schema": SCHEMA, "tables": tables, "keys": keys}

    @staticmethod
    def _result(cursor, limit):
        columns = [column.name for column in cursor.description]
        rows = cursor.fetchmany(limit + 1)
        return {"columns": columns,
                "rows": [[display_value(value) for value in row] for row in rows[:limit]],
                "limit": limit, "has_more": len(rows) > limit}

    def read_table(self, name, limit=1000, order_by=None, descending=False):
        with self.cursor() as cursor:
            if not any(table["name"] == name for table in self._tables(cursor)):
                raise LookupError()
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position", (SCHEMA, name)
            )
            columns = [row[0] for row in cursor.fetchall()]
            if order_by is not None and order_by not in columns:
                raise InvalidDatabaseRequest("The sort column does not exist in this table.")
            cursor.execute(
                """SELECT a.attname FROM pg_index i
                   JOIN pg_class c ON c.oid = i.indrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, position) ON true
                   JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
                   WHERE n.nspname = %s AND c.relname = %s AND i.indisprimary ORDER BY k.position""",
                (SCHEMA, name),
            )
            primary_key = [row[0] for row in cursor.fetchall()]
            ordering = ([order_by] + [key for key in primary_key if key != order_by]
                        if order_by else primary_key or columns[:1])
            if not ordering:
                raise InvalidDatabaseRequest("This table has no sortable columns.")
            query = sql.SQL("SELECT * FROM {} ORDER BY {} LIMIT %s").format(
                sql.Identifier(SCHEMA, name),
                sql.SQL(", ").join(
                    sql.SQL("{} {}").format(sql.Identifier(column), sql.SQL("DESC" if descending else "ASC"))
                    for column in ordering
                ),
            )
            cursor.execute(query, (limit + 1,))
            result = self._result(cursor, limit)
        return {**result, "schema": SCHEMA, "name": name,
                "order_by": ordering, "descending": descending}

    def query(self, query, limit=1000):
        statement = query.strip().removesuffix(";").rstrip()
        # Reject batches and export commands before reaching PostgreSQL.
        if not re.match(r"^(SELECT|WITH)\b", statement, re.IGNORECASE) or ";" in statement:
            raise InvalidDatabaseRequest("Use a single SELECT query without embedded semicolons. Use the export endpoint to create CSV files.")
        with self.cursor() as cursor:
            # The subquery rejects non-retrieval syntax; the transaction also
            # enforces read-only behavior independently of the agent's judge.
            cursor.execute(sql.SQL("SELECT * FROM (\n{}\n) AS agent_result LIMIT {}").format(
                sql.SQL(statement), sql.Literal(limit + 1)
            ))
            return self._result(cursor, limit)
