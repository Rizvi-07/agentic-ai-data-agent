# Run the Data Agent website

The website calls the existing router, SQL analyst and ETL analyst in this project.
It uses the same `.env`, PostgreSQL connection and models as the command-line
version. The **PostgreSQL tables** panel reads your live database directly.
The source datasets in `data/` remain available for sharing on GitHub. The website
only lists generated extracts under `data/extract/`, in the **Extracted data** panel.

## Start on Windows

From PowerShell in this project folder:

```powershell
uv sync
.\.venv\Scripts\python.exe -m uvicorn web_app:app --host 127.0.0.1 --port 8000
```

Or, after dependencies are installed, run `./run_website.ps1`.
Open **http://127.0.0.1:8000**. Keep the terminal open; Ctrl+C stops the server.
The existing CLI entry points continue to work.

For a pip-managed virtual environment, install the pinned requirements with
`python -m pip install -r requirements.txt`, then run the same Uvicorn command.

## Use the website

1. Type a question or choose an example. Ctrl+Enter also submits a request.
2. Click **Run Request**. The router chooses the SQL or ETL agent. **Clear** beside
   it resets the prompt and selected table, hides the result, and returns focus to
   the prompt. It is disabled while a request is running.
3. Read or copy the final answer. **Your result** appears when you run a request.
   Use its download links or the **Extracted data** panel on the right to download
   saved files. The panel refreshes automatically after a request finishes.
   Each extract has a circular **×** icon that deletes it immediately without a dialog. Deleting an extract
   removes that file from `data/extract/` and its download links from browser history.
4. Under **PostgreSQL tables**, select a table to preview its columns and up to
   50 rows under **Your result** when you run a request. The selected table is
   included as context in your next agent question.
   Clear the selection to ask about the whole database.
5. Use the refresh button in that panel to reload the table list and preview.

Browsing tables runs read-only PostgreSQL queries and does not call an AI model.
The panel shows tables in the `public` schema that your configured database user
can read, matching the schema used by the SQL agent. The connection badge reports
whether the database table list could be loaded. PostgreSQL must be running;
pgAdmin does not need to be open.

Examples:

- "What are the different payment methods available in the database?"
- "Show completed payment totals, grouped by payment method."
- "Extract top 5 data from ratings table and save it as CSV in data/extract."
- "Export the 5 highest ratings, ordered by the rating score, as CSV in data/extract."
- "Extract data from https://pokeapi.co/api/v2/pokemon and save it as CSV in data/extract."
- Select `payments`, then ask "How many rows are in this table?"

For a named PostgreSQL table, the ETL agent first checks the database table list,
then calls the FastAPI export endpoint. It saves a uniquely named CSV such as
`data/extract/ratings_<timestamp>_<id>.csv`. CSV writing happens in Python after
retrieving rows with SELECT, so the agent does not need a COPY command or an
external dataset URL. Unrequested external URLs are rejected by the ETL tool runner.

"Top 5" without a ranking column means the first five rows ordered by the table's
primary key (or first column if no primary key exists). To request highest scores
or newest records, specify that ranking in your question. Table/query exports
default to 1,000 rows and allow up to 10,000 rows per request.

**Extracted data** lists CSV, JSON and Parquet files under `data/extract/` only,
including existing extracts. Source CSVs directly under `data/` are not served.

Use a SQL-only or ETL-only request at a time: the original router selects one
specialist per request. The original extraction tool expects a JSON response
with a `results` array, as returned by the Pokémon example.

## Configuration

The website reads `.env` on the server. Your current agent code uses:

```dotenv
ANTHROPIC_API_KEY=your-anthropic-key
OPENAI_API_KEY=your-openai-key
host=localhost
port=your-postgresql-port
user=your-postgresql-user
password=your-postgresql-password
database=your-database-name
```

Model selection remains in `utils/llm_pick.py`. Restart the server after changing
`.env` or Python code. The setup notice checks whether agent settings are present;
the PostgreSQL badge reflects an actual database query. Model requests can incur charges.

Database operations from both agents use FastAPI. Keep the website server running
when calling the agents from the CLI too. CLI tools default to
`http://127.0.0.1:8000`; for another port, set `DATA_AGENT_API_URL` to that local
server URL. Website requests automatically use their server's actual port.

## How the web adapter works

- `web_app.py` serves the page and accepts `POST /api/requests`.
- A single worker calls `data_agent.invoke(...)` without blocking the page.
- The browser polls `GET /api/requests/{id}` until the request finishes.
- `format_result()` extracts the user-facing answer from the graph state.
- `utils/database_browser.py` connects directly to PostgreSQL using the same
  lowercase `.env` database settings as the SQL agent.
- `GET /api/database/tables` lists readable tables in `public`.
- `GET /api/database/preview?table=payments` returns column names and up to 50 rows.
  Preview queries use quoted identifiers, a read-only transaction and a timeout.
- `GET /api/database/schema` supplies actual columns, keys and sample values to agents.
- `POST /api/database/query` runs a bounded SELECT in a read-only transaction.
- `POST /api/database/export` accepts either a table with a row limit and optional
  sort column, or a SELECT query. It saves the returned rows as CSV in `data/extract/`.
- `utils/database_api.py` calls these endpoints; `utils/database_tools.py` exposes
  the table, schema, query and export tools to the ETL agent. The SQL agent uses
  the same API for schema discovery and queries.
- `GET /api/extractions` lists up to 100 recent extracted files;
  `GET /api/extractions/download?path=<filename>` downloads an extract.
- `DELETE /api/extractions?path=<filename>` deletes a single extracted file.
  File type and folder checks prevent deleting source datasets outside `data/extract/`.
- `POST /api/requests` accepts `question` and an optional `table` name, checked
  against the readable table list before it is passed to the agent.
- The former `/api/files` and file download endpoints have been removed.

Requests run one at a time because the ETL tools can overwrite a shared filename.
Use a single Uvicorn process. Jobs are kept in memory (up to 100); restarting the
server clears job records. The browser stores the eight most recent completed
answers locally. **Clear** removes that browser history. A page refresh resumes
polling the current request; closing the page does not cancel it.

This is a local website for a trusted user. The original ETL agent executes generated
Python, and the SQL agent uses model-based checks plus read-only API queries. Public hosting
requires separate authentication, execution isolation and stronger database controls.
The server binds to `127.0.0.1`; it is not published as a hosted Site.

## Checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Web tests use stub agent and database responses. They make no live model calls
and do not modify your PostgreSQL data.
