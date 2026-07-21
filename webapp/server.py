#!/usr/bin/env python3
"""Minimal control-plane API for the Oracle Patching Utility discovery view.

Wraps the existing read-only bin/opu-topology-discover operation over SSH.
Stdlib only — no new dependencies. This is a first slice, not the pull-based
agent/control-plane model described in docs/ARCHITECTURE.md; it exists to
give the frontend real data to render while that model is built out.
"""
from __future__ import annotations

import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
HOSTS_FILE = ROOT / "hosts.json"
SSH_TIMEOUT_SECONDS = 45

STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def load_hosts() -> dict[str, dict]:
    data = json.loads(HOSTS_FILE.read_text())
    return {host["id"]: host for host in data["hosts"]}


def run_discovery(host: dict) -> tuple[int, dict]:
    remote_command = f"{host['remote_root']}/bin/opu-topology-discover --pretty"
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host["ssh_alias"], remote_command],
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 504, {"error": "ssh_timeout", "message": f"No response from {host['ssh_alias']} within {SSH_TIMEOUT_SECONDS}s"}

    if result.returncode != 0:
        return 502, {
            "error": "discovery_failed",
            "message": f"opu-topology-discover exited {result.returncode} on {host['ssh_alias']}",
            "stderr": result.stderr.strip(),
        }

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return 502, {
            "error": "invalid_json",
            "message": f"opu-topology-discover produced unparsable output: {exc}",
            "stderr": result.stderr.strip(),
        }

    return 200, payload


class Handler(BaseHTTPRequestHandler):
    server_version = "opu-webapp/0.1"

    def log_message(self, fmt, *args):  # keep default access logging, just tagged
        print(f"[webapp] {self.address_string()} {fmt % args}")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel_path: str) -> None:
        candidate = (STATIC_DIR / rel_path).resolve()
        if STATIC_DIR not in candidate.parents and candidate != STATIC_DIR:
            self.send_error(404)
            return
        if not candidate.is_file():
            self.send_error(404)
            return
        content_type = STATIC_CONTENT_TYPES.get(candidate.suffix, "application/octet-stream")
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        path = urlparse(self.path).path

        if path == "/api/hosts":
            hosts = load_hosts()
            self._send_json(200, {"hosts": [{"id": h["id"], "label": h["label"]} for h in hosts.values()]})
            return

        if path.startswith("/api/hosts/") and path.endswith("/discovery"):
            host_id = path[len("/api/hosts/"):-len("/discovery")]
            hosts = load_hosts()
            host = hosts.get(host_id)
            if host is None:
                self._send_json(404, {"error": "unknown_host", "message": f"No configured host: {host_id}"})
                return
            status, payload = run_discovery(host)
            self._send_json(status, payload)
            return

        if path == "/":
            self._send_static("index.html")
            return

        self._send_static(path.lstrip("/"))


def main() -> None:
    port = 8765
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"opu webapp listening on http://127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
