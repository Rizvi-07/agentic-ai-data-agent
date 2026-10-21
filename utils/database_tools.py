"""Tools used by the ETL agent for the connected PostgreSQL database."""
import json

from langchain.tools import tool

from utils.database_api import DatabaseAPI, DatabaseAPIError


def _call(operation):
    try:
        return json.dumps(operation(DatabaseAPI()), ensure_ascii=False)
    except DatabaseAPIError as error:
        return json.dumps({"error": str(error)})


@tool
def list_database_tables() -> str:
    """List the actual PostgreSQL tables using the local FastAPI endpoint.

    Call before extracting a named database table. Never substitute an Internet
    dataset if a table is missing or the local API is unavailable.
    """
    return _call(lambda api: api.tables())


@tool
def get_database_schema() -> str:
    """Get real PostgreSQL columns, keys and sample rows through FastAPI.

    Use this before writing a query, choosing a ranking column, joining tables
    or filtering database rows. Do not invent table or column names.
    """
    return _call(lambda api: api.schema())


@tool
def extract_database_table(table: str, limit: int = 1000, order_by: str | None = None,
                           descending: bool = False, output_folder: str = "data/extract") -> str:
    """Export a PostgreSQL table to CSV via FastAPI and save it in data/extract.

    table: Exact unqualified table name from list_database_tables (e.g. ratings).
    limit: Requested number of rows, 1 to 10000. Use 5 for 'top 5 data'.
    order_by: Real column name when the user specifies a ranking. If omitted,
        rows are ordered by primary key, or the first column if no key exists.
    descending: True for highest values first; False for lowest/first rows.
    output_folder: Must be data/extract. Each export gets a unique filename.

    This tool needs no external URL and does not use COPY. A success response
    includes the saved filename, row count and whether more rows exist.
    """
    return _call(lambda api: api.export(table=table, limit=limit, order_by=order_by,
                                     descending=descending, output_folder=output_folder))


@tool
def query_database(query: str, limit: int = 1000, save_csv: bool = False) -> str:
    """Run a single PostgreSQL SELECT via FastAPI; optionally export it as CSV.

    Read get_database_schema first. Use for filters, joins, aggregates or ranked
    results. Set save_csv=True when the user wants these results as a CSV in
    data/extract. Never use COPY or commands that modify data. Maximum 10000 rows.
    """
    return _call(lambda api: api.export(query=query, limit=limit) if save_csv else api.query(query, limit))
