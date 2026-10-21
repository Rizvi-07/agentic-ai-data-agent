import os
import csv
import io
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

import psycopg2
from fastapi.testclient import TestClient

from web_app import create_app
from utils.database_browser import DatabaseNotConfigured, InvalidDatabaseRequest


class StubDatabase:
    def schema_details(self):
        return {"schema": "public", "tables": [{"name": "payments", "columns": [{"name": "id", "type": "integer"}]}], "keys": []}

    def read_table(self, name, limit, order_by, descending):
        if name != "payments":
            raise LookupError()
        if order_by not in (None, "id"):
            raise InvalidDatabaseRequest("The sort column does not exist in this table.")
        return {"columns": ["id", "note"], "rows": [[str(i), 'line one, comma\nline two "quoted"'] for i in range(1, min(limit, 8) + 1)],
                "has_more": limit < 8, "order_by": ["id"], "descending": descending}

    def query(self, query, limit):
        return {"columns": ["total"], "rows": [["30.00"]], "limit": limit, "has_more": False}

    def list_tables(self):
        return {"database": "test_database", "schema": "public",
                "tables": [{"schema": "public", "name": "payments", "column_count": 2}]}

    def preview_table(self, name):
        if name != "payments":
            raise LookupError()
        return {"schema": "public", "name": name, "columns": ["id", "amount"],
                "rows": [["1", "30.00"], ["2", None]], "limit": 50, "has_more": False}


class WebsiteTests(unittest.TestCase):
    def setUp(self):
        self.questions = []
        self.database = StubDatabase()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.extracts = Path(self.temporary.name) / "data" / "extract"

    def runner(self, question):
        self.questions.append(question)
        return {"route_response": "sql", "messages": [
            {"final_answer": "Total: 30", "generated_sql_query": "SELECT ...", "prompt_query_context": "Private schema"}]}

    def client(self, runner=None):
        client = TestClient(create_app(runner=runner or self.runner, database_browser=self.database, extraction_dir=self.extracts))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def finish(self, client, job_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = client.get(f"/api/requests/{job_id}")
            self.assertEqual(response.status_code, 200)
            job = response.json()
            if job["status"] in ("completed", "failed"):
                return job
            time.sleep(.01)
        self.fail("Job did not finish")

    def test_home_and_assets(self):
        client = self.client()
        self.assertIn("Your data.", client.get("/").text)
        self.assertIn("PostgreSQL tables", client.get("/").text)
        self.assertIn("Extracted data", client.get("/").text)
        self.assertNotIn("Workspace files", client.get("/").text)
        self.assertEqual(client.get("/static/app.js").status_code, 200)
        self.assertEqual(client.get("/static/styles.css").status_code, 200)

    def test_job_returns_only_final_answer(self):
        client = self.client()
        accepted = client.post("/api/requests", json={"question": "  Total payments?  "})
        self.assertEqual(accepted.status_code, 202)
        job = self.finish(client, accepted.json()["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["answer"], "Total: 30")
        self.assertEqual(job["result"]["agent"], "sql")
        self.assertNotIn("Private schema", str(job))
        self.assertNotIn("SELECT", str(job))
        self.assertEqual(self.questions, ["Total payments?"])

    def test_database_tables_and_preview_do_not_invoke_agent(self):
        client = self.client()
        response = client.get("/api/database/tables")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tables"][0]["name"], "payments")
        preview = client.get("/api/database/preview", params={"table": "payments"})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["columns"], ["id", "amount"])
        self.assertEqual(preview.json()["rows"], [["1", "30.00"], ["2", None]])
        self.assertEqual(self.questions, [])

    def test_selected_table_passed_to_agent(self):
        client = self.client()
        accepted = client.post("/api/requests", json={"question": "Count rows", "table": "payments"})
        self.assertEqual(accepted.status_code, 202)
        self.finish(client, accepted.json()["id"])
        self.assertIn('"schema": "public", "table": "payments"', self.questions[0])
        self.assertTrue(self.questions[0].startswith("Count rows"))

    def test_local_files_are_not_exposed(self):
        client = self.client()
        for path in ["/api/files", "/api/files/download?path=users.csv", "/data/users.csv", "/.env"]:
            self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(client.post("/api/requests", json={"question": "Read it", "file": "users.csv"}).status_code, 422)

    def test_unknown_table_rejected_before_agent_runs(self):
        client = self.client()
        for table in ["missing", 'payments"; DROP TABLE payments; --', "pg_authid"]:
            self.assertEqual(client.get("/api/database/preview", params={"table": table}).status_code, 404)
            self.assertEqual(client.post("/api/requests", json={"question": "Read it", "table": table}).status_code, 404)
        self.assertEqual(self.questions, [])
        for table in ["", "x" * 64, "invalid\x00table"]:
            self.assertEqual(client.get("/api/database/preview", params={"table": table}).status_code, 422)
            self.assertEqual(client.post("/api/requests", json={"question": "Read it", "table": table}).status_code, 422)

    def test_database_failures_are_actionable_and_do_not_leak_credentials(self):
        client = self.client()
        for error in [psycopg2.OperationalError("private-password"), DatabaseNotConfigured()]:
            with patch.object(self.database, "list_tables", side_effect=error), patch.object(self.database, "preview_table", side_effect=error):
                responses = [
                    client.get("/api/database/tables"),
                    client.get("/api/database/preview", params={"table": "payments"}),
                    client.post("/api/requests", json={"question": "Read it", "table": "payments"}),
                ]
            for response in responses:
                self.assertEqual(response.status_code, 503)
                self.assertIn("PostgreSQL", response.json()["detail"])
                self.assertNotIn("private-password", response.text)
        # Database failure must not prevent an independent ETL request.
        with patch.object(self.database, "list_tables", side_effect=psycopg2.OperationalError()):
            accepted = client.post("/api/requests", json={"question": "Extract API data"})
            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(self.finish(client, accepted.json()["id"])["status"], "completed")

    def test_empty_database(self):
        client = self.client()
        with patch.object(self.database, "list_tables", return_value={"database": "empty", "schema": "public", "tables": []}):
            response = client.get("/api/database/tables")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tables"], [])

    def test_validation_and_cross_origin(self):
        client = self.client()
        for body in [{"question": ""}, {"question": "   "}, {"question": "x" * 4001}, {"question": "Hi", "other": True}]:
            self.assertEqual(client.post("/api/requests", json=body).status_code, 422)
        self.assertEqual(client.post("/api/requests", json={"question": "Hello"}, headers={"Origin": "https://elsewhere.example"}).status_code, 403)
        self.assertEqual(client.get("/", headers={"Host": "elsewhere.example"}).status_code, 400)

    def test_busy_requests_rejected_and_slot_released(self):
        entered, release = threading.Event(), threading.Event()
        def slow(question):
            entered.set()
            release.wait(3)
            return self.runner(question)
        client = self.client(slow)
        first = client.post("/api/requests", json={"question": "First"}).json()
        self.assertTrue(entered.wait(2))
        try:
            self.assertEqual(client.post("/api/requests", json={"question": "Second"}).status_code, 429)
        finally:
            release.set()
        self.assertEqual(self.finish(client, first["id"])["status"], "completed")
        self.assertEqual(client.post("/api/requests", json={"question": "Third"}).status_code, 202)

    def test_failure_hides_internal_details_and_releases_slot(self):
        def fail(question):
            raise RuntimeError("private-provider-internals")
        client = self.client(fail)
        with self.assertLogs("web_app", level="ERROR"):
            first = client.post("/api/requests", json={"question": "Failure"}).json()
            job = self.finish(client, first["id"])
        self.assertEqual(job["status"], "failed")
        self.assertNotIn("private-provider-internals", job["error"])
        with self.assertLogs("web_app", level="ERROR"):
            second = client.post("/api/requests", json={"question": "Retry"})
            self.assertEqual(second.status_code, 202)
            self.finish(client, second.json()["id"])

    def test_configuration_does_not_return_credentials(self):
        client = self.client()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "private-key"}, clear=True):
            status = client.get("/api/status").json()
        self.assertFalse(status["configured"])
        self.assertIn("OPENAI_API_KEY", status["missing"])
        self.assertNotIn("private-key", str(status))

    def test_unknown_request(self):
        client = self.client()
        self.assertEqual(client.get("/api/requests/00000000-0000-0000-0000-000000000000").status_code, 404)
        self.assertEqual(client.get("/api/requests/not-an-id").status_code, 422)

    def test_export_five_rows_and_download_csv(self):
        client = self.client()
        response = client.post("/api/database/export", json={"table": "payments", "limit": 5})
        self.assertEqual(response.status_code, 201)
        result = response.json()
        self.assertEqual(result["row_count"], 5)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["order_by"], ["id"])
        download = client.get("/api/extractions/download", params={"path": result["file"]["path"]})
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["content-disposition"])
        rows = list(csv.reader(io.StringIO(download.text)))
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0], ["id", "note"])
        self.assertEqual(rows[1][1], 'line one, comma\nline two "quoted"')
        second = client.post("/api/database/export", json={"table": "payments", "limit": 5}).json()
        self.assertNotEqual(result["file"]["name"], second["file"]["name"])
        self.assertEqual(len(client.get("/api/extractions").json()["files"]), 2)

    def test_export_validation_and_missing_table(self):
        client = self.client()
        for payload in [{}, {"table": "payments", "query": "SELECT 1"}, {"table": "payments", "limit": 0},
                        {"table": "payments", "limit": 10001}, {"table": "payments", "output_folder": "../"},
                        {"query": "SELECT 1", "order_by": "id"}, {"table": "payments", "order_by": "missing"}]:
            self.assertEqual(client.post("/api/database/export", json=payload).status_code, 422)
        self.assertEqual(client.post("/api/database/export", json={"table": "missing"}).status_code, 404)
        self.assertEqual(client.get("/api/extractions").json()["files"], [])

    def test_only_extraction_files_are_served(self):
        self.extracts.mkdir(parents=True)
        (self.extracts / "output.csv").write_text("id\n1\n", encoding="utf-8")
        (self.extracts / ".env").write_text("private", encoding="utf-8")
        source = self.extracts.parent / "users.csv"
        source.write_text("source-data", encoding="utf-8")
        client = self.client()
        self.assertEqual([file["name"] for file in client.get("/api/extractions").json()["files"]], ["output.csv"])
        for path in ["../users.csv", "..\\users.csv", str(source), ".env", "missing.csv"]:
            self.assertEqual(client.get("/api/extractions/download", params={"path": path}).status_code, 404)

    def test_schema_query_and_query_export(self):
        client = self.client()
        self.assertEqual(client.get("/api/database/schema").status_code, 200)
        query = client.post("/api/database/query", json={"query": "SELECT SUM(amount) FROM public.payments", "limit": 5})
        self.assertEqual(query.json()["rows"], [["30.00"]])
        exported = client.post("/api/database/export", json={"query": "SELECT SUM(amount) FROM public.payments"})
        self.assertEqual(exported.status_code, 201)
        self.assertEqual(exported.json()["row_count"], 1)

    def test_generated_extract_is_in_completed_job(self):
        def extract(question):
            self.extracts.mkdir(parents=True)
            (self.extracts / "ratings.csv").write_text("id\n1\n", encoding="utf-8")
            return {"route_response": "etl", "messages": [{"type": "ai", "content": "Saved ratings.csv"}]}
        client = self.client(extract)
        accepted = client.post("/api/requests", json={"question": "Export ratings"}).json()
        result = self.finish(client, accepted["id"])["result"]
        self.assertEqual(result["files"][0]["name"], "ratings.csv")

    def test_database_export_error_does_not_create_file(self):
        client = self.client()
        with patch.object(self.database, "read_table", side_effect=psycopg2.OperationalError("private-password")):
            response = client.post("/api/database/export", json={"table": "payments"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-password", response.text)
        self.assertEqual(client.get("/api/extractions").json()["files"], [])

    def test_delete_removes_only_selected_extract(self):
        client = self.client()
        first = client.post("/api/database/export", json={"table": "payments", "limit": 5}).json()["file"]
        second = client.post("/api/database/export", json={"table": "payments", "limit": 5}).json()["file"]
        deleted = client.delete("/api/extractions", params={"path": first["path"]})
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.json()["deleted"])
        self.assertFalse((self.extracts / first["path"]).exists())
        self.assertTrue((self.extracts / second["path"]).exists())
        self.assertEqual(client.get("/api/extractions").json()["files"], [second])
        self.assertEqual(client.get("/api/extractions/download", params={"path": first["path"]}).status_code, 404)
        self.assertEqual(client.delete("/api/extractions", params={"path": first["path"]}).status_code, 404)

    def test_delete_rejects_source_files_and_unsupported_types(self):
        self.extracts.mkdir(parents=True)
        source = self.extracts.parent / "users.csv"
        source.write_text("source-data", encoding="utf-8")
        secret = self.extracts / ".env"
        secret.write_text("secret", encoding="utf-8")
        client = self.client()
        for path in ["../users.csv", "..\\users.csv", str(source), ".env", "missing.csv", "."]:
            self.assertEqual(client.delete("/api/extractions", params={"path": path}).status_code, 404)
        self.assertEqual(source.read_text(encoding="utf-8"), "source-data")
        self.assertEqual(secret.read_text(encoding="utf-8"), "secret")
        self.assertEqual(client.delete("/api/extractions").status_code, 422)

    def test_delete_rejects_cross_origin_requests(self):
        self.extracts.mkdir(parents=True)
        file = self.extracts / "keep.csv"
        file.write_text("id\n1\n", encoding="utf-8")
        client = self.client()
        response = client.delete("/api/extractions", params={"path": "keep.csv"}, headers={"Origin": "https://elsewhere.example"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(file.exists())

    def test_delete_permission_error_keeps_file_and_hides_details(self):
        self.extracts.mkdir(parents=True)
        file = self.extracts / "keep.csv"
        file.write_text("id\n1\n", encoding="utf-8")
        client = self.client()
        with patch("utils.extracted_files.Path.unlink", side_effect=PermissionError("private-server-detail")):
            response = client.delete("/api/extractions", params={"path": "keep.csv"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private-server-detail", response.text)
        self.assertTrue(file.exists())


if __name__ == "__main__":
    unittest.main()
