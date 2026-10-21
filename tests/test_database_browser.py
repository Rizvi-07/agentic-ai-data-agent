import datetime
import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import psycopg2
from psycopg2 import sql

from utils.database_browser import DatabaseBrowser, DatabaseNotConfigured, InvalidDatabaseRequest, display_value


class DatabaseBrowserTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "host": "localhost", "port": "5432", "user": "test_user",
            "password": "test_password", "database": "test_database",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.connect_patch = patch("utils.database_browser.psycopg2.connect", return_value=self.connection)
        self.connect = self.connect_patch.start()
        self.addCleanup(self.connect_patch.stop)
        self.browser = DatabaseBrowser()

    def test_list_uses_database_settings_and_read_only_connection(self):
        self.cursor.fetchall.return_value = [("payments", 4)]
        result = self.browser.list_tables()
        self.assertEqual(result["tables"], [{"schema": "public", "name": "payments", "column_count": 4}])
        self.assertEqual(result["database"], "test_database")
        self.assertNotIn("test_password", str(result))
        self.connect.assert_called_once_with(
            host="localhost", port="5432", user="test_user", password="test_password",
            dbname="test_database", connect_timeout=5, options="-c statement_timeout=5000",
        )
        self.connection.set_session.assert_called_once_with(readonly=True, autocommit=False)
        self.connection.close.assert_called_once()

    def test_preview_quotes_identifiers_caps_rows_and_preserves_values(self):
        name = 'odd " name; --'
        self.cursor.fetchall.side_effect = [
            [(name, 2)], [(Decimal("12345678901234567890.01"), None)] * 51,
        ]
        self.cursor.description = [SimpleNamespace(name="amount"), SimpleNamespace(name="optional")]
        result = self.browser.preview_table(name)
        query, params = self.cursor.execute.call_args.args
        identifiers = [part for part in query if isinstance(part, sql.Identifier)]
        self.assertEqual(identifiers[0].strings, ("public", name))
        self.assertEqual(params, (51,))
        self.assertEqual(len(result["rows"]), 50)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["rows"][0], ["12345678901234567890.01", None])
        self.connection.close.assert_called_once()

    def test_unknown_table_does_not_execute_select(self):
        self.cursor.fetchall.return_value = [("payments", 2)]
        with self.assertRaises(LookupError):
            self.browser.preview_table('payments"; DROP TABLE payments; --')
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.connection.close.assert_called_once()

    def test_empty_table_still_returns_columns(self):
        self.cursor.fetchall.side_effect = [[("payments", 1)], []]
        self.cursor.description = [SimpleNamespace(name="id")]
        result = self.browser.preview_table("payments")
        self.assertEqual(result["columns"], ["id"])
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["has_more"])

    def test_connection_closes_when_query_fails(self):
        self.cursor.execute.side_effect = psycopg2.OperationalError()
        with self.assertRaises(psycopg2.OperationalError):
            self.browser.list_tables()
        self.connection.close.assert_called_once()

    def test_missing_settings_do_not_connect(self):
        with patch.dict(os.environ, {"database": ""}):
            with self.assertRaises(DatabaseNotConfigured):
                self.browser.list_tables()
        self.connect.assert_not_called()

    def test_non_json_postgresql_values_are_displayable(self):
        self.assertEqual(display_value(datetime.date(2026, 10, 20)), "2026-10-20")
        self.assertEqual(display_value(UUID(int=0)), "00000000-0000-0000-0000-000000000000")
        self.assertEqual(display_value(memoryview(b"\x00\xff")), "\\x00ff")
        self.assertEqual(display_value({"tags": ["one", "two"]}), '{"tags": ["one", "two"]}')
        self.assertEqual(display_value(9007199254740993), "9007199254740993")

    def test_table_export_defaults_to_primary_key_and_bounds_rows(self):
        self.cursor.fetchall.side_effect = [[("ratings", 2)], [("id",), ("score",)], [("id",)]]
        self.cursor.description = [SimpleNamespace(name="id"), SimpleNamespace(name="score")]
        self.cursor.fetchmany.return_value = [(i, Decimal("4.50")) for i in range(1, 7)]
        result = self.browser.read_table("ratings", limit=5)
        self.assertEqual(len(result["rows"]), 5)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["order_by"], ["id"])
        self.assertEqual(self.cursor.execute.call_args.args[1], (6,))
        self.connection.set_session.assert_called_once_with(readonly=True, autocommit=False)

    def test_table_export_rejects_unknown_sort_column(self):
        self.cursor.fetchall.side_effect = [[("ratings", 1)], [("id",)]]
        with self.assertRaises(InvalidDatabaseRequest):
            self.browser.read_table("ratings", order_by='id; DROP TABLE ratings')
        self.assertEqual(self.cursor.execute.call_count, 2)

    def test_ranked_export_uses_confirmed_column_with_key_tiebreaker(self):
        self.cursor.fetchall.side_effect = [[("ratings", 2)], [("id",), ("score",)], [("id",)]]
        self.cursor.description = [SimpleNamespace(name="id"), SimpleNamespace(name="score")]
        self.cursor.fetchmany.return_value = []
        result = self.browser.read_table("ratings", limit=5, order_by="score", descending=True)
        self.assertEqual(result["order_by"], ["score", "id"])
        self.assertTrue(result["descending"])

    def test_query_rejects_export_commands_and_batches_before_connecting(self):
        for statement in ["COPY ratings TO STDOUT", "DELETE FROM ratings", "SELECT 1; DROP TABLE ratings", ""]:
            with self.assertRaises(InvalidDatabaseRequest):
                self.browser.query(statement)
        self.connect.assert_not_called()

    def test_query_result_has_columns_and_limit_information(self):
        self.cursor.description = [SimpleNamespace(name="total")]
        self.cursor.fetchmany.return_value = [(Decimal("123456789.01"),)]
        result = self.browser.query("SELECT SUM(amount) AS total FROM public.payments;", limit=5)
        self.assertEqual(result["columns"], ["total"])
        self.assertEqual(result["rows"], [["123456789.01"]])
        self.assertFalse(result["has_more"])
        self.connection.set_session.assert_called_once_with(readonly=True, autocommit=False)


if __name__ == "__main__":
    unittest.main()
