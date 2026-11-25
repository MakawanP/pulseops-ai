"""PulseOps AI HTTP API & Static Frontend Server (Python 3 stdlib only)."""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import socket
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.agent import ask_copilot, get_llm_provider_status
from backend.analytics import get_overview
from backend.database import (
    PROJECT_ROOT,
    execute_readonly_sql,
    get_table_counts,
    init_db,
    reset_db,
    resolve_db_path,
)
from backend.etl import get_data_summary, ingest_csv_text
from backend.evals import get_latest_eval, run_eval_suite
from backend.workflows import list_workflows_and_history, run_workflow

FRONTEND_DIR = PROJECT_ROOT / "frontend"
SAMPLE_CSV_PATH = PROJECT_ROOT / "sample_data" / "q4_upload_sample.csv"
MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MB
SOCKET_TIMEOUT_SECONDS = 15.0

ALLOWED_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

POST_ROUTES = frozenset(
    {
        "/api/data/upload",
        "/api/data/reset",
        "/api/sql/query",
        "/api/copilot/ask",
        "/api/workflows/run",
        "/api/evals/run",
    }
)

SECURITY_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

EXTRA_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".csv": "text/csv; charset=utf-8",
}

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_dotenv(env_path: Path | None = None) -> dict[str, str]:
    """Minimal stdlib .env loader using os.environ.setdefault (shell env vars take precedence)."""
    target = env_path or (PROJECT_ROOT / ".env")
    loaded: dict[str, str] = {}
    if not target.is_file():
        return loaded
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return loaded

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            continue
        val = raw_val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()
        os.environ.setdefault(key, val)
        loaded[key] = val
    return loaded


def _sanitize_json_value(obj: Any) -> Any:
    """Recursively normalize bytes -> hex strings and non-finite floats -> None for strict JSON."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return bytes(obj).hex()
    if isinstance(obj, dict):
        return {str(k): _sanitize_json_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json_value(item) for item in obj]
    return str(obj)


class _HTTPRequestError(Exception):
    """Structured HTTP client/protocol error carrying explicit HTTPStatus and safe error message."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class PulseOpsHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying resolved DB and static asset paths."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        db_path: Path,
        frontend_dir: Path = FRONTEND_DIR,
        sample_csv_path: Path = SAMPLE_CSV_PATH,
    ) -> None:
        self.db_path = db_path
        self.frontend_dir = frontend_dir.resolve()
        self.sample_csv_path = sample_csv_path.resolve()
        super().__init__(server_address, request_handler_class)


class PulseOpsHandler(BaseHTTPRequestHandler):
    """REST JSON API + static SPA handler for PulseOps AI."""

    server: PulseOpsHTTPServer  # type: ignore[assignment]
    timeout = SOCKET_TIMEOUT_SECONDS

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.timeout)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        try:
            sys.stderr.write(f"[PulseOps] {self.address_string()} - {format % args}\n")
        except OSError:
            pass

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", SECURITY_CSP)
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        safe_payload = _sanitize_json_value(payload)
        encoded = json.dumps(safe_payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send_bytes(status, encoded, "application/json; charset=utf-8")

    def _validate_origin(self) -> None:
        """Reject cross-origin browser POST requests while allowing loopback and same-origin proxy hosts."""
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return
        try:
            parsed = urlparse(origin)
        except ValueError as exc:
            raise _HTTPRequestError(
                HTTPStatus.FORBIDDEN, "Malformed Origin header."
            ) from exc
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not hostname:
            raise _HTTPRequestError(
                HTTPStatus.FORBIDDEN,
                "Cross-origin POST requests are forbidden.",
            )
        if hostname in ALLOWED_LOOPBACK_HOSTS:
            return
        # Allow strict same-origin requests behind reverse proxies / tunnels (e.g. Cloudflare Tunnel)
        origin_netloc = parsed.netloc.lower()
        req_host = (self.headers.get("Host") or "").strip().lower()
        if req_host and origin_netloc == req_host:
            return
        extra_origins = {
            item.strip().rstrip("/").lower()
            for item in os.environ.get("PULSEOPS_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        }
        if origin.rstrip("/").lower() in extra_origins:
            return
        raise _HTTPRequestError(
            HTTPStatus.FORBIDDEN,
            "Cross-origin POST requests are forbidden.",
        )

    def _validate_json_content_type(self) -> None:
        """Require Content-Type: application/json on JSON POST routes to prevent simple-POST CSRF."""
        raw_ct = (self.headers.get("Content-Type") or "").strip()
        media_type = raw_ct.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise _HTTPRequestError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json.",
            )

    def _read_json_body(self) -> dict[str, Any]:
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if transfer_encoding:
            raise _HTTPRequestError(
                HTTPStatus.NOT_IMPLEMENTED,
                "Transfer-Encoding is not supported; send a Content-Length header.",
            )

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}

        length_header = raw_length.strip()
        try:
            content_length = int(length_header)
        except ValueError as exc:
            raise _HTTPRequestError(
                HTTPStatus.BAD_REQUEST, "Invalid Content-Length header."
            ) from exc

        if content_length < 0:
            raise _HTTPRequestError(
                HTTPStatus.BAD_REQUEST, "Invalid negative Content-Length."
            )
        if content_length > MAX_BODY_BYTES:
            raise _HTTPRequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"Request body exceeds maximum size of {MAX_BODY_BYTES} bytes.",
            )
        if content_length == 0:
            return {}

        try:
            raw = self.rfile.read(content_length)
        except (TimeoutError, socket.timeout) as exc:
            raise _HTTPRequestError(
                HTTPStatus.REQUEST_TIMEOUT, "Timed out reading request body."
            ) from exc

        if len(raw) < content_length:
            raise _HTTPRequestError(
                HTTPStatus.BAD_REQUEST, "Incomplete request body."
            )

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _HTTPRequestError(
                HTTPStatus.BAD_REQUEST, f"Invalid JSON payload: {exc}"
            ) from exc
        if not isinstance(decoded, dict):
            raise _HTTPRequestError(
                HTTPStatus.BAD_REQUEST, "JSON request body must be an object."
            )
        return decoded

    def _serve_static(self, rel_path: str) -> None:
        if "\x00" in rel_path:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid static path."})
            return

        clean_rel = "index.html" if rel_path in ("", "/") else rel_path.lstrip("/")
        try:
            target = (self.server.frontend_dir / clean_rel).resolve()
        except (OSError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid static path."})
            return

        # Prevent path traversal outside frontend/
        if not target.is_relative_to(self.server.frontend_dir):
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Forbidden path."})
            return

        try:
            is_file = target.is_file()
        except OSError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid static path."})
            return

        if not is_file:
            if clean_rel == "favicon.ico":
                self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": f"Static asset not found: /{clean_rel}"},
            )
            return

        suffix = target.suffix.lower()
        content_type = EXTRA_MIME_TYPES.get(suffix)
        if not content_type:
            guessed, _ = mimetypes.guess_type(str(target))
            content_type = guessed or "application/octet-stream"

        try:
            content = target.read_bytes()
        except OSError:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": f"Static asset unavailable: /{clean_rel}"},
            )
            return

        self._send_bytes(HTTPStatus.OK, content, content_type)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        db_path = self.server.db_path

        try:
            if path == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "db_path": str(db_path),
                        "tables": get_table_counts(db_path),
                        "llm_providers": get_llm_provider_status(),
                    },
                )
                return

            if path == "/api/overview":
                self._send_json(HTTPStatus.OK, get_overview(db_path))
                return

            if path == "/api/data/summary":
                self._send_json(HTTPStatus.OK, get_data_summary(db_path))
                return

            if path == "/api/sample-csv":
                if not self.server.sample_csv_path.is_file():
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "Sample Q4 CSV file not found."},
                    )
                    return
                csv_bytes = self.server.sample_csv_path.read_bytes()
                self._send_bytes(
                    HTTPStatus.OK,
                    csv_bytes,
                    "text/csv; charset=utf-8",
                    extra_headers={
                        "Content-Disposition": 'inline; filename="q4_upload_sample.csv"'
                    },
                )
                return

            if path == "/api/workflows":
                self._send_json(HTTPStatus.OK, list_workflows_and_history(db_path))
                return

            if path == "/api/evals/latest":
                self._send_json(HTTPStatus.OK, get_latest_eval(db_path))
                return

            if path.startswith("/api/"):
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": f"Unknown API endpoint: {path}"},
                )
                return

            self._serve_static(path)
        except _HTTPRequestError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.message})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "Internal server error."},
            )

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        db_path = self.server.db_path

        try:
            # 1. Verify Origin (CSRF guard) before any route/body processing
            self._validate_origin()

            # 2. Validate route BEFORE reading body
            if path not in POST_ROUTES:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": f"Unknown API endpoint: {path}"},
                )
                return

            # 3. Require application/json Content-Type (prevents preflight-free text/plain CSRF)
            self._validate_json_content_type()

            # 4. Read and validate bounded JSON body
            body = self._read_json_body()

            if path == "/api/data/upload":
                filename = str(body.get("filename") or "upload.csv")
                csv_text = body.get("csv_text")
                if not isinstance(csv_text, str):
                    raise ValueError("Field 'csv_text' must be a CSV string.")
                result = ingest_csv_text(db_path, filename, csv_text)
                self._send_json(HTTPStatus.OK, result)
                return

            if path == "/api/data/reset":
                reset_db(db_path)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "message": "Database atomically cleared and reseeded cleanly with FY2025 enterprise demo data.",
                    },
                )
                return

            if path == "/api/sql/query":
                sql = body.get("sql")
                if not isinstance(sql, str):
                    raise ValueError("Field 'sql' must be a string.")
                result = execute_readonly_sql(db_path, sql)
                self._send_json(HTTPStatus.OK, result)
                return

            if path == "/api/copilot/ask":
                question = body.get("question")
                if not isinstance(question, str) or not question.strip():
                    raise ValueError("Field 'question' is required.")
                provider = str(body.get("provider") or "local")
                api_key = str(body.get("api_key") or "")
                result = ask_copilot(db_path, question, provider=provider, api_key=api_key)
                self._send_json(HTTPStatus.OK, result)
                return

            if path == "/api/workflows/run":
                workflow_id = str(body.get("workflow_id") or "")
                params = body.get("params") or {}
                if not isinstance(params, dict):
                    raise ValueError("Field 'params' must be a JSON object.")
                result = run_workflow(db_path, workflow_id, params=params, persist=True)
                self._send_json(HTTPStatus.OK, result)
                return

            if path == "/api/evals/run":
                result = run_eval_suite(db_path, persist=True)
                self._send_json(HTTPStatus.OK, result)
                return
        except _HTTPRequestError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.message})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except Exception:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "Internal server error."},
            )


def create_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    db_path: str | Path | None = None,
) -> PulseOpsHTTPServer:
    """Initialize SQLite DB and return a configured PulseOpsHTTPServer (supports port 0 for tests)."""
    resolved_db = init_db(resolve_db_path(db_path))
    return PulseOpsHTTPServer(
        (host, port),
        PulseOpsHandler,
        db_path=resolved_db,
        frontend_dir=FRONTEND_DIR,
        sample_csv_path=SAMPLE_CSV_PATH,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PulseOps AI — Autonomous Financial & Operational Data Automation Workbench"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database (overrides PULSEOPS_DB_PATH; default: data/pulseops.db)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    args = parse_args(argv)
    server = create_server(host=args.host, port=args.port, db_path=args.db)
    bound_host, bound_port = server.server_address[:2]
    print(f"PulseOps AI listening on http://{bound_host}:{bound_port} (db: {server.db_path})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down PulseOps AI server...", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
