#!/usr/bin/env python3
"""Control-plane API for the Oracle Patching Utility estate frontend.

Wraps existing bin/opu-* operations over SSH. Stdlib only — no new
dependencies. This is a first slice, not the pull-based agent/control-plane
model described in docs/ARCHITECTURE.md; it exists to give the frontend real
data and a real (evidence-gated) execution path while that model is built
out. See /Users/akhilreddy/.claude/plans/nested-humming-eagle.md for the
phased plan this file implements.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import os

import auth
import evidence
import pipeline_runner
import pipeline_steps
import planctl
import production
import recoveryctl
import remote

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


def run_discovery(host: dict) -> dict:
    """Run opu-topology-discover on a host over SSH. Raises remote.RemoteError."""
    argv = [f"{host['remote_root']}/bin/opu-topology-discover", "--pretty"]
    payload = remote.run_remote_json(host["ssh_alias"], argv, timeout=SSH_TIMEOUT_SECONDS)
    evidence.write_evidence(host["id"], "snapshot", payload)
    return payload


def summarize_discovery(host: dict, payload: dict | None, error: remote.RemoteError | None) -> dict:
    summary = {"id": host["id"], "label": host["label"]}
    if error is not None:
        summary["status"] = "error"
        summary["error"] = error.to_json()
        return summary

    cluster = payload.get("cluster") or {}
    runtime = cluster.get("runtime") or {}
    summary["status"] = "ok"
    summary["collected_at"] = payload.get("collected_at")
    summary["host_name"] = (payload.get("host") or {}).get("name")
    summary["cluster_status"] = cluster.get("status")
    summary["active_version"] = runtime.get("active_version")
    summary["upgrade_state"] = runtime.get("upgrade_state")
    summary["node_count"] = len(cluster.get("nodes") or [])
    summary["oracle_home_count"] = len(payload.get("oracle_homes") or [])
    summary["database_count"] = len(payload.get("databases") or [])
    summary["warning_count"] = len(payload.get("warnings") or [])
    return summary


def build_estate() -> list[dict]:
    hosts = list(load_hosts().values())

    def probe(host: dict) -> dict:
        try:
            payload = run_discovery(host)
            return summarize_discovery(host, payload, None)
        except remote.RemoteError as exc:
            return summarize_discovery(host, None, exc)

    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=max(len(hosts), 1)) as pool:
        return list(pool.map(probe, hosts))


class Handler(BaseHTTPRequestHandler):
    server_version = "opu-webapp/0.1"

    def log_message(self, fmt, *args):  # keep default access logging, just tagged
        print(f"[webapp] {self.address_string()} {fmt % args}")

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

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

    def _resolved_host(self, host_id: str) -> dict | None:
        hosts = load_hosts()
        return hosts.get(host_id)

    def _require_api_auth(self) -> bool:
        try:
            auth.require_api_auth(self.headers.get("Authorization"))
            return True
        except auth.AuthError as exc:
            self.send_response(exc.status)
            self.send_header("WWW-Authenticate", 'Bearer realm="opu-webapp"')
            body = json.dumps(exc.to_json()).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        path = urlparse(self.path).path

        if path.startswith("/api/") and not self._require_api_auth():
            return

        if path == "/api/hosts":
            hosts = load_hosts()
            self._send_json(200, {"hosts": [{"id": h["id"], "label": h["label"]} for h in hosts.values()]})
            return

        if path == "/api/estate":
            self._send_json(200, {"hosts": build_estate()})
            return

        if path.startswith("/api/hosts/") and path.endswith("/discovery"):
            host_id = path[len("/api/hosts/"):-len("/discovery")]
            host = self._resolved_host(host_id)
            if host is None:
                self._send_json(404, {"error": "unknown_host", "message": f"No configured host: {host_id}"})
                return
            try:
                payload = run_discovery(host)
                self._send_json(200, payload)
            except remote.RemoteError as exc:
                status = 504 if exc.error == "ssh_timeout" else 502
                self._send_json(status, exc.to_json())
            return

        if path.startswith("/api/hosts/") and path.endswith("/pipeline"):
            host_id = path[len("/api/hosts/"):-len("/pipeline")]
            if self._resolved_host(host_id) is None:
                self._send_json(404, {"error": "unknown_host", "message": f"No configured host: {host_id}"})
                return
            self._send_json(200, {"steps": pipeline_steps.pipeline_state(host_id)})
            return

        if path.startswith("/api/runs/"):
            run_id = path[len("/api/runs/"):]
            record = pipeline_runner.get_run(run_id)
            if record is None:
                self._send_json(404, {"error": "unknown_run", "message": f"No such run: {run_id}"})
                return
            self._send_json(200, record.to_json())
            return

        if path == "/api/plans":
            self._send_json(200, {"plans": planctl.list_plans()})
            return

        if path.startswith("/api/plans/") and path.endswith("/tasks"):
            plan_id = path[len("/api/plans/"):-len("/tasks")]
            try:
                self._send_json(200, {"tasks": planctl.list_tasks(plan_id)})
            except planctl.PlanError as exc:
                self._send_json(400, exc.to_json())
            return

        if path.startswith("/api/plans/"):
            plan_id = path[len("/api/plans/"):]
            try:
                self._send_json(200, planctl.status(plan_id))
            except planctl.PlanError as exc:
                self._send_json(404, exc.to_json())
            return

        if path == "/api/recovery":
            self._send_json(200, {"requests": recoveryctl.list_requests()})
            return

        if path.startswith("/api/recovery/"):
            request_id = path[len("/api/recovery/"):]
            try:
                self._send_json(200, recoveryctl.status(request_id))
            except recoveryctl.RecoveryError as exc:
                self._send_json(404, exc.to_json())
            return

        if path == "/":
            self._send_static("index.html")
            return

        self._send_static(path.lstrip("/"))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path.startswith("/api/") and not self._require_api_auth():
            return

        if path.startswith("/api/hosts/") and "/pipeline/" in path:
            host_id, _, step = path[len("/api/hosts/"):].partition("/pipeline/")
            host = self._resolved_host(host_id)
            if host is None:
                self._send_json(404, {"error": "unknown_host", "message": f"No configured host: {host_id}"})
                return
            step_fn = pipeline_steps.STEPS.get(step)
            if step_fn is None:
                self._send_json(404, {"error": "unknown_step", "message": f"No such pipeline step: {step}"})
                return

            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return

            def run(_record, host=host, body=body, step_fn=step_fn):
                return step_fn(host_id, host, body)

            try:
                record = pipeline_runner.start_run("pipeline", f"host:{host_id}:pipeline:{step}", run)
            except pipeline_runner.RunConflict as exc:
                self._send_json(409, {"error": "run_in_progress", "message": str(exc)})
                return

            self._send_json(202, {"run_id": record.run_id})
            return

        if path == "/api/plans":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            self._post_plan_action("create", None, body)
            return

        if path == "/api/plans/testmode-demo":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            try:
                plan_id = body["plan_id"]
                requester = body["requester"]
                window_start = body["window_start"]
                window_end = body["window_end"]
            except KeyError as exc:
                self._send_json(400, {"error": "missing_field", "message": f"Missing required field: {exc}"})
                return

            def run(_record, plan_id=plan_id, requester=requester, window_start=window_start, window_end=window_end):
                return planctl.create_testmode_demo(plan_id, requester, window_start, window_end)

            try:
                record = pipeline_runner.start_run("plan", f"plan:{plan_id}:testmode-demo", run)
            except pipeline_runner.RunConflict as exc:
                self._send_json(409, {"error": "run_in_progress", "message": str(exc)})
                return
            self._send_json(202, {"run_id": record.run_id})
            return

        if path.startswith("/api/plans/") and path.endswith("/execute-next"):
            plan_id = path[len("/api/plans/"):-len("/execute-next")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            try:
                actor = body["actor"]
            except KeyError as exc:
                self._send_json(400, {"error": "missing_field", "message": f"Missing required field: {exc}"})
                return

            def run(_record, plan_id=plan_id, actor=actor):
                result = planctl.execute_next_task(plan_id, actor)
                return {"task_result": result, "no_pending_task": result is None}

            try:
                record = pipeline_runner.start_run("plan", f"plan:{plan_id}:execute-next", run)
            except pipeline_runner.RunConflict as exc:
                self._send_json(409, {"error": "run_in_progress", "message": str(exc)})
                return
            self._send_json(202, {"run_id": record.run_id})
            return

        if path.startswith("/api/plans/") and path.endswith("/approve"):
            plan_id = path[len("/api/plans/"):-len("/approve")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            self._post_plan_action("approve", plan_id, body)
            return

        if path.startswith("/api/plans/") and path.endswith("/authorize"):
            plan_id = path[len("/api/plans/"):-len("/authorize")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            self._post_plan_action("authorize", plan_id, body)
            return

        if path.startswith("/api/plans/") and path.endswith("/dispatch"):
            plan_id = path[len("/api/plans/"):-len("/dispatch")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            self._post_plan_action("dispatch", plan_id, body)
            return

        if path.startswith("/api/plans/") and path.endswith("/create-rollback"):
            plan_id = path[len("/api/plans/"):-len("/create-rollback")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            self._post_plan_action("create-rollback", plan_id, body)
            return

        if path == "/api/recovery/testmode-demo":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            self._post_recovery_action("create", None, body)
            return

        if path.startswith("/api/recovery/") and path.endswith("/analyze"):
            request_id = path[len("/api/recovery/"):-len("/analyze")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            self._post_recovery_action("analyze", request_id, body)
            return

        if path.startswith("/api/recovery/") and path.endswith("/approve"):
            request_id = path[len("/api/recovery/"):-len("/approve")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            self._post_recovery_action("approve", request_id, body)
            return

        if path.startswith("/api/recovery/") and path.endswith("/authorize"):
            request_id = path[len("/api/recovery/"):-len("/authorize")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            self._post_recovery_action("authorize", request_id, body)
            return

        if path.startswith("/api/recovery/") and path.endswith("/execute"):
            request_id = path[len("/api/recovery/"):-len("/execute")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            self._post_recovery_action("execute", request_id, body)
            return

        if path.startswith("/api/recovery/") and path.endswith("/reconcile"):
            request_id = path[len("/api/recovery/"):-len("/reconcile")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            self._post_recovery_action("reconcile", request_id, body)
            return

        self.send_error(404)

    def _post_plan_action(self, action: str, plan_id: str | None, body: dict) -> None:
        try:
            if action == "create":
                plan_id = body["plan_id"]
                requester = body["requester"]
                host_id = body["host_id"]
                window_start = body["window_start"]
                window_end = body["window_end"]
                if self._resolved_host(host_id) is None:
                    self._send_json(404, {"error": "unknown_host", "message": f"No configured host: {host_id}"})
                    return
                key = f"plan:{plan_id}:create"

                def run(_record, plan_id=plan_id, requester=requester, host_id=host_id, window_start=window_start, window_end=window_end):
                    return planctl.create(plan_id, requester, host_id, window_start, window_end)

            elif action == "approve":
                actor = body["actor"]
                approval_ticket = body["approval_ticket"]
                key = f"plan:{plan_id}:approve"

                def run(_record, plan_id=plan_id, actor=actor, approval_ticket=approval_ticket):
                    return planctl.approve(plan_id, actor, approval_ticket)

            elif action == "authorize":
                actor = body["actor"]
                key = f"plan:{plan_id}:authorize"

                def run(_record, plan_id=plan_id, actor=actor):
                    return planctl.authorize(plan_id, actor)

            elif action == "dispatch":
                actor = body["actor"]
                key = f"plan:{plan_id}:dispatch"

                def run(_record, plan_id=plan_id, actor=actor):
                    return planctl.dispatch(plan_id, actor)

            elif action == "create-rollback":
                # Path plan_id is the source apply plan; body must agree when present.
                source_plan_id = body.get("source_plan_id", plan_id)
                if source_plan_id != plan_id:
                    self._send_json(400, {
                        "error": "source_plan_mismatch",
                        "message": f"URL source plan {plan_id!r} does not match body source_plan_id {source_plan_id!r}",
                    })
                    return
                new_plan_id = body["plan_id"]
                requester = body["requester"]
                window_start = body["window_start"]
                window_end = body["window_end"]
                key = f"plan:{plan_id}:create-rollback"

                def run(_record, new_plan_id=new_plan_id, requester=requester, source_plan_id=source_plan_id, window_start=window_start, window_end=window_end):
                    return planctl.create_rollback(new_plan_id, requester, source_plan_id, window_start, window_end)

            else:
                self._send_json(404, {"error": "unknown_action", "message": action})
                return
        except KeyError as exc:
            self._send_json(400, {"error": "missing_field", "message": f"Missing required field: {exc}"})
            return
        except (planctl.PlanError, evidence.EvidenceError) as exc:
            self._send_json(400, exc.to_json())
            return

        try:
            record = pipeline_runner.start_run("plan", key, run)
        except pipeline_runner.RunConflict as exc:
            self._send_json(409, {"error": "run_in_progress", "message": str(exc)})
            return
        self._send_json(202, {"run_id": record.run_id})

    def _post_recovery_action(self, action: str, request_id: str | None, body: dict) -> None:
        try:
            if action == "create":
                request_id = body["request_id"]
                key = f"recovery:{request_id}:create"

                def run(_record, body=body):
                    return recoveryctl.create_testmode_demo(body["request_id"], body["requester"])

            elif action == "analyze":
                key = f"recovery:{request_id}:analyze"

                def run(_record, request_id=request_id):
                    return recoveryctl.analyze(request_id)

            elif action == "approve":
                key = f"recovery:{request_id}:approve"

                def run(_record, request_id=request_id, body=body):
                    return recoveryctl.approve(request_id, body["actor"], body["approval_ticket"])

            elif action == "authorize":
                key = f"recovery:{request_id}:authorize"

                def run(_record, request_id=request_id, body=body):
                    return recoveryctl.authorize(request_id, body["actor"])

            elif action == "execute":
                key = f"recovery:{request_id}:execute"

                def run(_record, request_id=request_id, body=body):
                    return recoveryctl.execute(request_id, body["actor"])

            elif action == "reconcile":
                key = f"recovery:{request_id}:reconcile"

                def run(_record, request_id=request_id, body=body):
                    return recoveryctl.reconcile(request_id, body["actor"])

            else:
                self._send_json(404, {"error": "unknown_action", "message": action})
                return
        except KeyError as exc:
            self._send_json(400, {"error": "missing_field", "message": f"Missing required field: {exc}"})
            return

        try:
            record = pipeline_runner.start_run("recovery", key, run)
        except pipeline_runner.RunConflict as exc:
            self._send_json(409, {"error": "run_in_progress", "message": str(exc)})
            return
        self._send_json(202, {"run_id": record.run_id})


def main() -> None:
    port = int(os.environ.get("OPU_WEBAPP_PORT") or "8765")
    token = auth.ensure_token()
    production_state = production.status()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    cert = (os.environ.get("OPU_WEBAPP_TLS_CERT") or "").strip()
    key = (os.environ.get("OPU_WEBAPP_TLS_KEY") or "").strip()
    scheme = "http"
    if cert or key:
        if not cert or not key:
            raise SystemExit("OPU_WEBAPP_TLS_CERT and OPU_WEBAPP_TLS_KEY must both be set")
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=cert, keyfile=key)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"

    print(f"opu webapp listening on {scheme}://127.0.0.1:{port}")
    print(f"API token file: {auth.TOKEN_FILE}")
    print(f"API token (dev): {token}")
    print(
        "production mode: "
        f"{'enabled' if production_state['production_mode'] else 'disabled'}; "
        f"certified={production_state['certified']}"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
