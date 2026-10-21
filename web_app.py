"""Local website for the existing data_agent graph.

Run: python -m uvicorn web_app:app --host 127.0.0.1 --port 8000
"""
import logging
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

from dotenv import load_dotenv
import psycopg2
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from utils.output import format_result
from utils.database_browser import DatabaseBrowser, DatabaseNotConfigured, InvalidDatabaseRequest
from utils.database_api import database_api_url
from utils.extracted_files import ExtractedFiles

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger(__name__)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    table: str | None = Field(default=None, min_length=1, max_length=63, pattern=r"^[^\x00]+$")


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=20000)
    limit: int = Field(default=1000, ge=1, le=10000)


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str | None = Field(default=None, min_length=1, max_length=63, pattern=r"^[^\x00]+$")
    query: str | None = Field(default=None, min_length=1, max_length=20000)
    limit: int = Field(default=1000, ge=1, le=10000)
    order_by: str | None = Field(default=None, min_length=1, max_length=63, pattern=r"^[^\x00]+$")
    descending: bool = False
    output_folder: Literal["data/extract"] = "data/extract"


def invoke_agent(question):
    # Lazy import keeps the page available before API credentials are configured.
    from agents.data_agent import data_agent
    from langchain_core.messages import HumanMessage
    return data_agent.invoke({"messages": [HumanMessage(content=question)], "route_response": ""},
                             config={"recursion_limit": 20})


def create_app(runner=None, database_browser=None, extraction_dir=None):
    run = runner or invoke_agent
    database = database_browser or DatabaseBrowser()
    extracted = ExtractedFiles(extraction_dir)
    jobs = {}
    lock = Lock()
    # Existing ETL tools write fixed output filenames. Serialize requests.
    slot = BoundedSemaphore(1)

    @asynccontextmanager
    async def lifespan(application):
        application.state.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="data-agent")
        yield
        application.state.executor.shutdown(wait=True)

    application = FastAPI(title="Data Agent", lifespan=lifespan)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @application.middleware("http")
    async def browser_origin(request: Request, call_next):
        if request.method in {"POST", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Open the website locally to submit a request."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def database_call(operation, *args):
        try:
            return operation(*args)
        except DatabaseNotConfigured:
            raise HTTPException(503, "Add PostgreSQL host, port, user, password and database settings to .env, then restart the server.") from None
        except LookupError:
            raise HTTPException(404, "Table not found or not readable in the public schema.") from None
        except InvalidDatabaseRequest as error:
            raise HTTPException(422, str(error)) from None
        except psycopg2.ProgrammingError:
            raise HTTPException(422, "PostgreSQL rejected the query. Use a single read-only SELECT and check the table and column names using the schema endpoint.") from None
        except psycopg2.Error:
            raise HTTPException(503, "Could not read PostgreSQL. Check that it is running, your .env connection settings are correct, and your database user can read the tables.") from None

    def execute(job_id, question, api_url):
        started = monotonic()
        try:
            before = {item["path"]: item["modified"] for item in extracted.list()}
            with lock:
                jobs[job_id]["status"] = "running"
            with database_api_url(api_url):
                response = run(question)
            answer = format_result(response)
            if not answer:
                raise ValueError("The agent returned no final answer.")
            result = {"answer": answer, "agent": response.get("route_response", "agent"),
                      "seconds": round(monotonic() - started, 1),
                      "files": [item for item in extracted.list() if before.get(item["path"]) != item["modified"]]}
            with lock:
                jobs[job_id].update(status="completed", result=result)
        except Exception:
            logger.exception("Agent request %s failed", job_id)
            with lock:
                jobs[job_id].update(status="failed", error=(
                    "The request could not be completed. Check your API keys, model access, "
                    "and database settings in .env. The server terminal has the error details."))
        finally:
            slot.release()

    @application.get("/", include_in_schema=False)
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @application.get("/api/status")
    def status():
        required = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "host", "port", "user", "password", "database"]
        missing = [name for name in required if not os.getenv(name, "").strip()]
        return {"status": "ok", "configured": not missing, "missing": missing}

    @application.get("/api/database/tables")
    def tables():
        return database_call(database.list_tables)

    @application.get("/api/database/preview")
    def preview(table: str = Query(min_length=1, max_length=63, pattern=r"^[^\x00]+$")):
        return database_call(database.preview_table, table)

    @application.get("/api/database/schema")
    def schema():
        return database_call(database.schema_details)

    @application.post("/api/database/query")
    def query(body: QueryRequest):
        return database_call(database.query, body.query, body.limit)

    @application.post("/api/database/export", status_code=201)
    def export(body: ExportRequest):
        if bool(body.table) == bool(body.query):
            raise HTTPException(422, "Provide either a table name or a SELECT query.")
        if body.query and (body.order_by or body.descending):
            raise HTTPException(422, "Specify sorting in the SELECT query when exporting a query.")
        result = (database_call(database.read_table, body.table, body.limit, body.order_by, body.descending)
                  if body.table else database_call(database.query, body.query, body.limit))
        try:
            file = extracted.save_csv(result, body.table or "query")
        except OSError:
            raise HTTPException(500, "Could not save the CSV. Check write access to data/extract.") from None
        count = len(result["rows"])
        return {"message": f"Saved {count} rows to data/extract/{file['name']}.",
                "row_count": count, "has_more": result["has_more"], "limit": body.limit,
                "order_by": result.get("order_by"), "descending": result.get("descending"),
                "file": file}

    @application.get("/api/extractions")
    def extractions():
        return {"files": extracted.list()}

    @application.get("/api/extractions/download")
    def download(path: str = Query(min_length=1, max_length=500, pattern=r"^[^\x00]+$")):
        try:
            selected = extracted.resolve(path)
        except (FileNotFoundError, OSError, ValueError):
            raise HTTPException(404, "Extracted file not found.") from None
        return FileResponse(selected, filename=selected.name)

    @application.delete("/api/extractions")
    def delete_extraction(path: str = Query(min_length=1, max_length=500, pattern=r"^[^\x00]+$")):
        try:
            deleted = extracted.delete(path)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "Extracted file not found.") from None
        except OSError:
            raise HTTPException(500, "Could not delete the extracted file. Close any app using it and check file permissions.") from None
        return {"deleted": True, "path": deleted["path"], "name": deleted["name"]}

    @application.post("/api/requests", status_code=202)
    def ask(body: AskRequest, request: Request):
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Enter a question or data task first.")
        if body.table:
            tables = database_call(database.list_tables)["tables"]
            selected = next((table for table in tables if table["name"] == body.table), None)
            if selected is None:
                raise HTTPException(404, "Table not found or not readable in the public schema.")
            # Serialize names as data; PostgreSQL table names may contain quotes.
            question += "\nSelected PostgreSQL table (schema and table name): " + json.dumps(
                {"schema": selected["schema"], "table": selected["name"]}, ensure_ascii=False
            )
        if not slot.acquire(blocking=False):
            raise HTTPException(429, "An agent is already working. Wait for that request to finish.")
        job_id = str(uuid4())
        with lock:
            # Bound memory while retaining recent results for browser refreshes.
            while len(jobs) >= 100:
                jobs.pop(next(iter(jobs)))
            jobs[job_id] = {"id": job_id, "status": "queued"}
        try:
            # Use this server's actual local port, including when it is not 8000.
            address = request.scope.get("server") or ("127.0.0.1", 8000)
            api_url = f"http://127.0.0.1:{address[1] or 8000}"
            application.state.executor.submit(execute, job_id, question, api_url)
        except Exception:
            with lock:
                jobs.pop(job_id, None)
            slot.release()
            raise
        return {"id": job_id, "status": "queued"}

    @application.get("/api/requests/{job_id}")
    def job(job_id: UUID):
        with lock:
            result = jobs.get(str(job_id))
            if result is None:
                raise HTTPException(404, "This request is no longer available. Start a new request.")
            return dict(result)

    return application


app = create_app()
