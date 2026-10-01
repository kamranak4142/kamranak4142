"""A loopback-only dashboard server, with no arbitrary file-serving routes."""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import Columns, MAX_BYTES, parse_csv, run_pipeline
from .export import csv_bytes, geojson_bytes, geopackage_bytes, report_bytes

STATIC = Path(__file__).parent / "static"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/demo.csv": ("demo.csv", "text/csv; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "GeoFlow/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        # Uploaded rows, filenames and credentials are never written to logs.
        return

    def allowed_host(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def respond(self, body: bytes, content_type: str, status: int = 200, filename: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def json(self, payload: dict, status: int = 200) -> None:
        self.respond(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(), "application/json", status)

    def do_GET(self) -> None:
        if not self.allowed_host():
            self.json({"error": "Use the loopback dashboard address."}, 403)
            return
        route = STATIC_ROUTES.get(self.path)
        if not route:
            self.json({"error": "Not found."}, 404)
            return
        filename, content_type = route
        self.respond((STATIC / filename).read_bytes(), content_type)

    def do_POST(self) -> None:
        port = self.server.server_address[1]
        origin = self.headers.get("Origin")
        if (not self.allowed_host() or self.headers.get("X-GeoFlow") != "1"
                or (origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"})):
            self.json({"error": "Only the local dashboard may submit data."}, 403)
            return
        if self.path not in {"/api/inspect", "/api/analyze", "/api/export"}:
            self.json({"error": "Not found."}, 404)
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.json({"error": "Use application/json."}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BYTES + 1024 * 1024:
                self.json({"error": "Request size is outside the supported limit."}, 413)
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Request must be a JSON object.")
            table = parse_csv(payload.get("csv", ""))
            if self.path == "/api/inspect":
                self.json({"headers": table.headers, "total": len(table.records)})
                return
            selection = payload.get("columns", {})
            if not isinstance(selection, dict) or not isinstance(selection.get("groups", []), list):
                raise ValueError("Supply a valid column mapping.")
            columns = Columns(selection.get("latitude", ""), selection.get("longitude", ""),
                              selection.get("identifier", ""), selection.get("filename", ""), tuple(selection.get("groups", [])))
            if any(not isinstance(name, str) for name in [columns.latitude, columns.longitude, columns.identifier, columns.filename, *columns.groups]):
                raise ValueError("Column selections must be names.")
            zero_missing, repair = payload.get("zero_missing", True), payload.get("repair", False)
            if not isinstance(zero_missing, bool) or not isinstance(repair, bool):
                raise ValueError("Use boolean processing settings.")
            max_gap = payload.get("max_gap", 3)
            max_distance = payload.get("max_distance", 250)
            if not isinstance(max_distance, (int, float)) or isinstance(max_distance, bool):
                raise ValueError("Maximum distance must be numeric.")
            table, report = run_pipeline(table, columns, zero_missing, repair, max_gap, max_distance)
            if self.path == "/api/analyze":
                self.json(report)
                return
            format_name = payload.get("format")
            exporters = {
                "geojson": (lambda: geojson_bytes(report), "application/geo+json", "geoflow_points.geojson"),
                "gpkg": (lambda: geopackage_bytes(table, report), "application/geopackage+sqlite3", "geoflow_points.gpkg"),
                "csv": (lambda: csv_bytes(table, report), "text/csv; charset=utf-8", "geoflow_audited.csv"),
                "report": (lambda: report_bytes(report), "application/json", "geoflow_qa_report.json"),
            }
            if format_name not in exporters:
                raise ValueError("Choose GeoJSON, GeoPackage, CSV or QA report.")
            export, mime, filename = exporters[format_name]
            self.respond(export(), mime, filename=filename)
        except (ValueError, TypeError, UnicodeError, KeyError) as error:
            self.json({"error": str(error) or "Invalid request."}, 400)


def create_server(port: int = 8765) -> ThreadingHTTPServer:
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535.")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve(port: int = 8765, open_browser: bool = True) -> int:
    server = create_server(port)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"GeoFlow dashboard: {url}\nPress Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
