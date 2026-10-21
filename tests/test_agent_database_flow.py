import csv
import importlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from Models.schema import AgentSchema, ETLAgentSchema, RouterSchema
from utils.database_api import DatabaseAPI, DatabaseAPIError, database_api_url
from web_app import create_app


class RatingsDatabase:
    def list_tables(self):
        return {"schema": "public", "database": "test", "tables": [
            {"schema": "public", "name": "ratings", "column_count": 2}]}

    def read_table(self, name, limit, order_by, descending):
        if name != "ratings":
            raise LookupError()
        return {"columns": ["rating_id", "score"], "rows": [[str(i), "5"] for i in range(1, limit + 1)],
                "has_more": True, "order_by": ["rating_id"], "descending": False}

    def schema_details(self):
        return {"schema": "public", "tables": [{"name": "ratings", "columns": [{"name": "score", "type": "integer"}]}], "keys": []}

    def query(self, query, limit):
        return {"columns": ["score"], "rows": [["5"]], "limit": limit, "has_more": False}


class AgentDatabaseFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import real graphs without constructing live model clients.
        with patch("utils.llm_pick.pick_llm", return_value=MagicMock()):
            cls.router = importlib.import_module("agents.data_agent")
            cls.etl = importlib.import_module("agents.etl_analyst")
            cls.sql = importlib.import_module("agents.sql_analyst")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = TestClient(create_app(database_browser=RatingsDatabase(), extraction_dir=Path(self.temp.name)),
                                 base_url="http://127.0.0.1:8765")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.calls = []
        self.ports = []

    def transport(self, method, url, **kwargs):
        parsed = urlsplit(url)
        self.assertIn(parsed.hostname, {"localhost", "127.0.0.1"})
        self.calls.append((method, parsed.path))
        self.ports.append(parsed.port)
        return self.client.request(method, parsed.path, json=kwargs.get("json"))

    def test_exact_request_runs_graph_tools_api_and_saves_five_rows(self):
        question = "Extract top 5 data from ratings table and save it as CSV in data/extract."
        model = MagicMock()
        model.invoke.side_effect = [
            AIMessage(content="", tool_calls=[{"name": "list_database_tables", "args": {}, "id": "list"}]),
            AIMessage(content="", tool_calls=[{"name": "extract_database_table", "args": {"table": "ratings", "limit": 5}, "id": "export"}]),
            AIMessage(content="Saved 5 ratings rows as CSV in data/extract, ordered by rating_id."),
        ]
        with patch.object(self.router.llm_router, "invoke", return_value=RouterSchema(answer="etl", comments="Export table")), \
                patch.object(self.etl, "llm_bind", model), \
                patch("requests.Session.request", side_effect=self.transport):
            accepted = self.client.post("/api/requests", json={"question": question})
            self.assertEqual(accepted.status_code, 202)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = self.client.get('/api/requests/' + accepted.json()['id']).json()
                if job['status'] in {'completed', 'failed'}:
                    break
                time.sleep(.01)
            self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['result']["agent"], "etl")
        self.assertEqual(len(job['result']['files']), 1)
        self.assertEqual(self.calls, [("GET", "/api/database/tables"), ("POST", "/api/database/export")])
        self.assertEqual(self.ports, [8765, 8765])
        files = self.client.get("/api/extractions").json()["files"]
        self.assertEqual(len(files), 1)
        content = self.client.get("/api/extractions/download", params={"path": files[0]["path"]}).text
        rows = list(csv.reader(io.StringIO(content)))
        self.assertEqual(rows[0], ["rating_id", "score"])
        self.assertEqual(len(rows) - 1, 5)
        # Only newly produced messages are appended; no duplicated graph history.
        self.assertEqual(len(model.invoke.call_args.args[0]), 6)

    def test_invented_external_url_is_blocked_before_network_access(self):
        state = ETLAgentSchema(messages=[HumanMessage(content="Extract ratings as CSV"), AIMessage(
            content="", tool_calls=[{"name": "extract_load_tool", "id": "bad-url", "args": {
                "url": "https://example.com/ratings.csv", "output_folder": "data/extract", "format": "csv"}}])])
        with patch.object(self.etl.ETLTools, "extract_load") as extract:
            result = self.etl.tool_node(state)
        extract.assert_not_called()
        self.assertIn("not supplied by the user", result["messages"][0].content)

    def test_user_supplied_api_url_still_works(self):
        url = "https://pokeapi.co/api/v2/pokemon"
        state = ETLAgentSchema(messages=[HumanMessage(content=f"Extract '{url}' as CSV"), AIMessage(
            content="", tool_calls=[{"name": "extract_load_tool", "id": "supplied-url", "args": {
                "url": url, "output_folder": "data/extract", "format": "csv"}}])])
        with patch.object(self.etl.ETLTools, "extract_load", return_value="Saved CSV") as extract:
            self.etl.tool_node(state)
        extract.assert_called_once_with(url, "data/extract", "csv")

    def test_sql_agent_uses_api_for_schema_and_execution(self):
        state = AgentSchema(messages=[], user_question="Show ratings", curated_ques="Show ratings",
                            prompt_query_context="", generated_sql_query="SELECT score FROM ratings",
                            is_safe="Yes", comments="", sql_query_execution_result="", final_answer="")
        with patch("requests.Session.request", side_effect=self.transport):
            self.sql.prompt_query_context(state)
            self.sql.execute_sql(state)
        self.assertEqual(self.calls, [("GET", "/api/database/schema"), ("POST", "/api/database/query")])
        self.assertEqual(json.loads(state.sql_query_execution_result)["rows"], [["5"]])

    def test_local_api_address_context_is_scoped_and_validated(self):
        with database_api_url("http://127.0.0.1:8765"):
            self.assertEqual(DatabaseAPI().base_url, "http://127.0.0.1:8765")
            with database_api_url("http://127.0.0.1:8766"):
                self.assertEqual(DatabaseAPI().base_url, "http://127.0.0.1:8766")
            self.assertEqual(DatabaseAPI().base_url, "http://127.0.0.1:8765")
        for url in ["https://external.example", "http://localhost@external.example", "http://127.0.0.1/api"]:
            with database_api_url(url), self.assertRaises(DatabaseAPIError):
                DatabaseAPI()


if __name__ == "__main__":
    unittest.main()
