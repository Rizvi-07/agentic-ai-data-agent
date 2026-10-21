"""Agent access to the local FastAPI database endpoints."""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import requests

_api_url = ContextVar("data_agent_api_url", default=None)


class DatabaseAPIError(Exception):
    pass


@contextmanager
def database_api_url(url):
    token = _api_url.set(url)
    try:
        yield
    finally:
        _api_url.reset(token)


class DatabaseAPI:
    def __init__(self):
        self.base_url = (_api_url.get() or os.getenv("DATA_AGENT_API_URL", "http://127.0.0.1:8000")).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
            raise DatabaseAPIError("DATA_AGENT_API_URL must be the local FastAPI server URL.")

    def request(self, method, path, **kwargs):
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.request(method, self.base_url + path, timeout=45,
                                           allow_redirects=False, **kwargs)
            if not 200 <= response.status_code < 300:
                try:
                    detail = response.json().get("detail")
                except ValueError:
                    detail = None
                raise DatabaseAPIError(detail if isinstance(detail, str) else "The database API could not complete this request.")
            return response.json()
        except (requests.RequestException, ValueError):
            raise DatabaseAPIError("Cannot reach the local database API. Start the FastAPI website server and check DATA_AGENT_API_URL.") from None

    def tables(self):
        return self.request("GET", "/api/database/tables")

    def schema(self):
        return self.request("GET", "/api/database/schema")

    def query(self, query, limit=1000):
        return self.request("POST", "/api/database/query", json={"query": query, "limit": limit})

    def export(self, **options):
        return self.request("POST", "/api/database/export", json=options)
