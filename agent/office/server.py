"""Agent Office web server (read-only, loopback only, stdlib).

    python -m agent.office                      # http://127.0.0.1:18787
    python -m agent.office --port 9000

Open it from your laptop through an SSH tunnel (no port is opened on the VPS):
    ssh -L 18787:127.0.0.1:18787 ubuntu@<vps>      then browse http://localhost:18787
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

import structlog

from agent.config import load_settings
from agent.office.state import build_state

log = structlog.get_logger(__name__)

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self' 'unsafe-inline'; "
                               "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_handler(db_path: str, settings):
    page = resources.files("agent.office").joinpath("index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentOffice"
        sys_version = ""

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/state":
                try:
                    body = json.dumps(build_state(db_path, settings), default=str).encode()
                    self._send(200, body, "application/json")
                except Exception as e:  # noqa: BLE001
                    log.warning("office_state_failed", error=f"{type(e).__name__}: {e}"[:200])
                    self._send(503, json.dumps({"error": f"{type(e).__name__}"}).encode(), "application/json")
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._send(404, b"not found", "text/plain")

        do_HEAD = do_GET

        def do_POST(self):  # noqa: N802 - read-only dashboard
            self._send(405, b"read-only", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_POST

        def log_message(self, fmt, *args):  # keep the journal quiet
            pass

    return Handler


def serve(host: str, port: int, db_path: str, settings) -> ThreadingHTTPServer:
    if not is_loopback(host):
        raise ValueError(f"refusing to listen on {host!r}: the office is loopback-only (use an SSH tunnel)")
    return ThreadingHTTPServer((host, port), make_handler(db_path, settings))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18787)
    ap.add_argument("--settings", default=os.environ.get("AGENT_SETTINGS", "config/settings.yaml"))
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    s = load_settings(args.settings)          # non-secret settings only; .env is never read
    db_path = args.db or s.storage.db_path
    if not Path(db_path).exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2
    try:
        httpd = serve(args.host, args.port, db_path, s)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"port {args.port} on {args.host} is already used by another program; pick another with "
                  f"--port (and change ExecStart in deploy/indodax-office.service)", file=sys.stderr)
            return 3          # systemd: RestartPreventExitStatus=3 -> no restart loop
        raise
    print(f"Agent Office on http://{args.host}:{args.port} (read-only)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
