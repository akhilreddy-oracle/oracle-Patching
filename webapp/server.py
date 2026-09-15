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
from urllib.parse import parse_qs, unquote, urlparse
import os
import time

import auth
import assistant
import local_llm
import company_auth
import fleet
import fleet_metadata
import agent_queue
import evidence
import extjobctl
import itsm
import lockctl
import notifications
import pipeline_runner
import pipeline_steps
import planctl
import production
import procedure_hints
import recoveryctl
import remote
import runtime_paths

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
HOSTS_FILE = runtime_paths.hosts_file()
SSH_TIMEOUT_SECONDS = 45
DISCOVERY_TIMEOUT_SECONDS = pipeline_steps.DISCOVERY_TIMEOUT_SECONDS

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
    return pipeline_steps.step_discovery(host["id"], host, {})


def _cluster_is_standalone_no_crs(cluster: dict) -> bool:
    """True when discovery found no Clusterware (expected for single-instance)."""
    return (
        str(cluster.get("status") or "").lower() == "unavailable"
        and not (cluster.get("nodes") or [])
        and not cluster.get("grid_home")
    )


def summarize_discovery(host: dict, payload: dict | None, error: remote.RemoteError | None) -> dict:
    summary = {"id": host["id"], "label": host["label"]}
    if error is not None:
        summary["status"] = "error"
        summary["error"] = error.to_json()
        return summary

    cluster = payload.get("cluster") or {}
    runtime = cluster.get("runtime") or {}
    databases = payload.get("databases") or []
    if not isinstance(databases, list):
        databases = []

    # Keep raw collector value (recovery gates still expect unavailable for SI),
    # but expose an operator-facing cluster_status that does not look like "DB down".
    raw_cluster = cluster.get("status")
    summary["status"] = "ok"
    summary["collected_at"] = payload.get("collected_at")
    summary["host_name"] = (payload.get("host") or {}).get("name")
    summary["cluster_status_raw"] = raw_cluster
    summary["cluster_status"] = "not_applicable" if _cluster_is_standalone_no_crs(cluster) else raw_cluster
    summary["active_version"] = runtime.get("active_version")
    summary["upgrade_state"] = runtime.get("upgrade_state")
    summary["node_count"] = len(cluster.get("nodes") or [])
    summary["oracle_home_count"] = len(payload.get("oracle_homes") or [])
    summary["database_count"] = len(databases)
    summary["warning_count"] = len(payload.get("warnings") or [])

    db_summaries = []
    for db in databases:
        if not isinstance(db, dict):
            continue
        db_runtime = db.get("runtime") or {}
        db_summaries.append({
            "db_unique_name": db.get("db_unique_name"),
            "runtime_status": db_runtime.get("status"),
            "instance_state": db_runtime.get("instance_state"),
            "open_mode": db_runtime.get("open_mode"),
            "database_role": db_runtime.get("database_role"),
        })
    summary["databases"] = db_summaries
    return summary


def build_estate(*, live: bool = False) -> list[dict]:
    """Estate cards from last live discovery evidence, or fresh SSH when live=True.

    Cached payloads are always from a prior live SSH run (never fixtures).
    live=True re-probes every configured host over SSH (can take minutes).
    """
    hosts = list(load_hosts().values())
    if not hosts:
        return []

    if not live:
        out = []
        for host in hosts:
            cached = evidence.read_evidence(host["id"], "snapshot")
            if cached is None:
                out.append({
                    "id": host["id"],
                    "label": host["label"],
                    "status": "pending",
                    "message": "No live discovery yet — open the host to run SSH topology discovery.",
                })
            else:
                summary = summarize_discovery(host, cached, None)
                summary["evidence_source"] = "live_cache"
                out.append(summary)
        return out

    def probe(host: dict) -> dict:
        try:
            payload = run_discovery(host)
            summary = summarize_discovery(host, payload, None)
            summary["evidence_source"] = "live_ssh"
            return summary
        except remote.RemoteError as exc:
            return summarize_discovery(host, None, exc)
        except Exception as exc:  # noqa: BLE001 - estate must never drop the HTTP connection
            return {
                "id": host["id"],
                "label": host["label"],
                "status": "error",
                "error": {"error": "discovery_failed", "message": str(exc)},
            }

    with ThreadPoolExecutor(max_workers=max(len(hosts), 1)) as pool:
        return list(pool.map(probe, hosts))


class Handler(BaseHTTPRequestHandler):
    server_version = "opu-webapp/0.1"

    def log_message(self, fmt, *args):  # keep default access logging, just tagged
        if urlparse(self.path).path.startswith("/auth/"):
            print(f"[webapp] {self.address_string()} {self.command} {urlparse(self.path).path}", flush=True)
        else:
            print(f"[webapp] {self.address_string()} {fmt % args}", flush=True)

    def _send_json(self, status: int, payload) -> None:
        if getattr(self, "_response_sink", None) is not None:
            self._response_sink.append((status, payload))
            return
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_run_conflict(self, exc: pipeline_runner.RunConflict) -> None:
        payload = {"error": "run_in_progress", "message": str(exc)}
        if exc.run_id:
            payload["run_id"] = exc.run_id
        self._send_json(409, payload)

    def _read_json_body(self) -> dict:
        if hasattr(self, "_parsed_body"):
            return self._parsed_body
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is not supported")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length must be a nonnegative integer") from None
        if length < 0 or length > 1024 * 1024:
            raise ValueError("JSON body must be at most 1 MiB")
        if length == 0:
            self._parsed_body = {}
            return self._parsed_body
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("Incomplete request body")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        self._parsed_body = body
        return body

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
        # Lab UI iterates quickly; never let browsers keep stale ES modules.
        if candidate.suffix in {".js", ".css", ".html"}:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _resolved_host(self, host_id: str) -> dict | None:
        hosts = load_hosts()
        return hosts.get(host_id)

    def _require_api_auth(self) -> bool:
        try:
            self._company_session = None
            if company_auth.configured() and (company_auth.cookie_value(self.headers.get("Cookie"), company_auth.SESSION_COOKIE)
                                               or not self.headers.get("Authorization")):
                self._company_session = company_auth.authenticate(self.headers.get("Cookie"), method=self.command,
                    csrf=self.headers.get("X-CSRF-Token"), origin=self.headers.get("Origin"))
                self._principal = self._company_session["actor"]
            else:
                self._principal = auth.require_api_auth(self.headers.get("Authorization"))
            asserted = self.headers.get("X-OPU-Actor")
            if self._principal and asserted and asserted != self._principal:
                raise auth.AuthError("X-OPU-Actor does not match the authenticated principal", status=403)
            return True
        except auth.AuthError as exc:
            self.send_response(exc.status)
            self.send_header("WWW-Authenticate", 'Bearer realm="opu-webapp"')
            body = json.dumps(exc.to_json()).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return False

    def _require_role(self, actor: str | None, action: str) -> bool:
        try:
            principal = getattr(self, "_principal", None)
            if principal and actor and actor != principal:
                raise auth.AuthError("actor does not match the authenticated principal", status=403)
            actor = principal or actor
            if getattr(self, "_company_session", None):
                roles = set(self._company_session["roles"])
                if roles.isdisjoint(auth.ACTION_ROLES.get(action, set())):
                    raise auth.AuthError("Company role does not permit this action", status=403)
            else:
                auth.require_role(actor, action)
            return True
        except auth.AuthError as exc:
            self._send_json(exc.status, exc.to_json())
            return False

    def _authorize_path(self, method: str, path: str) -> bool:
        """Default-deny role selection shared by every API route family."""
        actor = getattr(self, "_principal", None) or self.headers.get("X-OPU-Actor")
        if method == "GET":
            action = "agent" if path == "/api/agent/jobs" else "read"
        elif path.startswith("/api/assistant/"):
            action = "read"
        elif path.startswith("/api/agent/"):
            action = "agent"
        elif path.startswith("/api/fleet/hosts/") and path.endswith("/metadata"):
            action = "manage_fleet"
        elif "/pipeline/" in path or path.startswith("/api/runs/"):
            action = "execute"
        elif path == "/api/auth/logout":
            action = "read"
        elif path in {"/api/plans", "/api/recovery", "/api/plans/testmode-demo", "/api/recovery/testmode-demo"} or path.endswith("/create-rollback"):
            action = "create"
        elif path.endswith("/approve"):
            action = "approve"
        elif path.endswith("/authorize"):
            action = "authorize"
        elif path.endswith(("/dispatch", "/publish-agent-queue")):
            action = "dispatch"
        elif path.endswith("/analyze"):
            action = "read"
        else:
            action = "execute"
        return self._require_role(actor, action)

    def _has_role(self, action: str) -> bool:
        """Presentation hint only; every write still passes _authorize_path."""
        company = getattr(self, "_company_session", None)
        if company:
            return not set(company["roles"]).isdisjoint(auth.ACTION_ROLES.get(action, set()))
        actor = getattr(self, "_principal", None) or self.headers.get("X-OPU-Actor")
        try:
            auth.require_role(actor, action)
            return True
        except auth.AuthError:
            return False

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        path = urlparse(self.path).path

        if path in {"/api/auth/config", "/auth/login", "/auth/callback"}:
            try:
                if path == "/api/auth/config":
                    self._send_json(200, company_auth.status())
                    return
                if path == "/auth/login":
                    location, cookie_header = company_auth.login()
                    cookies = [cookie_header]
                else:
                    cookies = company_auth.callback(parse_qs(urlparse(self.path).query, keep_blank_values=True), self.headers.get("Cookie"))
                    location = "/"
                self.send_response(303)
                self.send_header("Location", location)
                self.send_header("Cache-Control", "no-store")
                for value in cookies:
                    self.send_header("Set-Cookie", value)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except auth.AuthError as exc:
                self._send_json(exc.status, exc.to_json())
            return

        if path == "/api/health":
            # Unauthenticated liveness probe for monitoring.
            self._send_json(200, {"status": "ok", "time": time.time()})
            return

        if path.startswith("/api/") and not self._require_api_auth():
            return

        if path.startswith("/api/") and not self._authorize_path("GET", path):
            return

        if path.startswith("/api/assistant/"):
            self._assistant_route("GET", path, {})
            return

        if path in {"/api/auth/whoami", "/api/session"}:
            actor = getattr(self, "_principal", None) or self.headers.get("X-OPU-Actor")
            company = getattr(self, "_company_session", None)
            self._send_json(200, {key: value for key, value in company.items() if key != "groups"} if company else auth.whoami(actor))
            return

        if path == "/api/fleet":
            self._send_json(200, fleet.build(load_hosts(), can_manage_metadata=self._has_role("manage_fleet")))
            return

        if path == "/api/validation":
            import release_status
            self._send_json(200, release_status.status())
            return

        if path == "/api/approvals":
            actor = getattr(self, "_principal", None) or self.headers.get("X-OPU-Actor")
            items = []
            for kind, records in (("plan", planctl.list_plans()), ("recovery", recoveryctl.list_requests())):
                for item in records:
                    if item.get("state") not in {"awaiting_approval", "approved"}:
                        continue
                    item_id = item.get("plan_id" if kind == "plan" else "request_id")
                    items.append({"kind": kind, "id": item_id, "state": item["state"], "requester": item.get("requester"),
                        "target": item.get("target"), "host_id": item.get("host_id"), "window": item.get("maintenance_window", item.get("window")),
                        "next_action": "review_approval" if item["state"] == "awaiting_approval" else "review_authorization",
                        "self_requested": bool(actor and item.get("requester") == actor)})
            self._send_json(200, {"items": items, "actor": actor})
            return

        if path.startswith("/api/plans/") and path.endswith(("/execution", "/report")):
            import execution_console
            import evidence_reports
            suffix = "/report" if path.endswith("/report") else "/execution"
            plan_id = path[len("/api/plans/"):-len(suffix)]
            try:
                if suffix == "/execution":
                    self._send_json(200, execution_console.snapshot(plan_id))
                else:
                    report = evidence_reports.build(plan_id)
                    format_name = parse_qs(urlparse(self.path).query).get("format", ["json"])[0]
                    if format_name == "json":
                        self._send_json(200, report)
                    else:
                        content, content_type, filename = evidence_reports.export(report, format_name)
                        payload = content.encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                        self.send_header("Content-Length", str(len(payload)))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        self.wfile.write(payload)
            except (planctl.PlanError, ValueError) as exc:
                self._send_json(400, {"error": "report_unavailable", "message": str(exc)})
            return

        if path == "/api/production/status":
            self._send_json(200, production.status())
            return

        if path == "/api/itsm/tickets":
            if not self._require_role(self.headers.get("X-OPU-Actor"), "read"):
                return
            self._send_json(200, {
                "enabled": itsm.itsm_enabled(),
                "tickets_file": str(itsm.tickets_path()),
                "tickets": itsm.list_tickets(),
            })
            return

        if path == "/api/events":
            if not self._require_role(self.headers.get("X-OPU-Actor"), "read"):
                return
            limit = 100
            for part in urlparse(self.path).query.split("&"):
                if part.startswith("limit="):
                    try:
                        limit = max(1, min(int(part.split("=", 1)[1]), 1000))
                    except ValueError:
                        pass
            self._send_json(200, {"events": notifications.tail_events(limit)})
            return

        if path == "/api/metrics":
            if not self._require_role(self.headers.get("X-OPU-Actor"), "read"):
                return
            self._send_json(200, {
                "runs_by_status": pipeline_runner.status_counts(),
                "events": notifications.event_counts(),
                "deadletter_count": notifications.deadletter_count(),
                "itsm_enabled": itsm.itsm_enabled(),
            })
            return

        if path == "/api/agent/jobs":
            if not self._require_role(self.headers.get("X-OPU-Actor"), "agent"):
                return
            status_filter = None
            query = urlparse(self.path).query
            if "status=" in query:
                for part in query.split("&"):
                    if part.startswith("status="):
                        status_filter = part.split("=", 1)[1]
            self._send_json(200, {"jobs": agent_queue.list_jobs(status_filter)})
            return

        if path == "/api/hosts":
            if not self._require_role(self.headers.get("X-OPU-Actor"), "read"):
                return
            hosts = load_hosts()
            self._send_json(200, {"hosts": [{"id": h["id"], "label": h["label"]} for h in hosts.values()]})
            return

        if path == "/api/estate":
            if not self._require_role(self.headers.get("X-OPU-Actor"), "read"):
                return
            query = urlparse(self.path).query
            live = any(
                part.split("=", 1)[0] == "live" and part.split("=", 1)[-1] == "1"
                for part in query.split("&")
                if part
            )
            try:
                self._send_json(200, {"hosts": build_estate(live=live), "live": live})
            except Exception as exc:  # noqa: BLE001 - never abort the socket mid-response
                self._send_json(500, {"error": "estate_failed", "message": str(exc)})
            return

        if path.startswith("/api/hosts/") and path.endswith("/discovery"):
            host_id = path[len("/api/hosts/"):-len("/discovery")]
            host = self._resolved_host(host_id)
            if host is None:
                self._send_json(404, {"error": "unknown_host", "message": f"No configured host: {host_id}"})
                return
            # Prefer async POST .../pipeline/discovery from the UI (long SSH).
            # GET remains for curl/ops and always runs live SSH — never fixtures.
            print(f"[webapp] live discovery start host={host_id} timeout={DISCOVERY_TIMEOUT_SECONDS}s", flush=True)
            try:
                payload = run_discovery(host)
                print(f"[webapp] live discovery ok host={host_id}", flush=True)
                self._send_json(200, payload)
            except remote.RemoteError as exc:
                print(f"[webapp] live discovery remote error host={host_id}: {exc.error}", flush=True)
                status = 504 if exc.error == "ssh_timeout" else 502
                self._send_json(status, exc.to_json())
            except Exception as exc:  # noqa: BLE001 - uncaught errors become Failed to fetch in the browser
                print(f"[webapp] live discovery crashed host={host_id}: {exc}", flush=True)
                self._send_json(500, {"error": "discovery_failed", "message": str(exc)})
            return

        if path.startswith("/api/hosts/") and path.endswith("/pipeline"):
            host_id = path[len("/api/hosts/"):-len("/pipeline")]
            if self._resolved_host(host_id) is None:
                self._send_json(404, {"error": "unknown_host", "message": f"No configured host: {host_id}"})
                return
            self._send_json(200, {"steps": pipeline_steps.pipeline_state(host_id)})
            return

        if path.startswith("/api/hosts/") and path.endswith("/procedure-hints"):
            host_id = path[len("/api/hosts/"):-len("/procedure-hints")]
            host = self._resolved_host(host_id)
            if host is None:
                self._send_json(404, {"error": "unknown_host", "message": f"No configured host: {host_id}"})
                return
            identifiers = parse_qs(urlparse(self.path).query).get("readme_identifier", [""])
            if len(identifiers) != 1:
                self._send_json(400, {"error": "invalid_readme_selection", "message": "Select one README identifier"})
                return
            try:
                self._send_json(200, procedure_hints.get_hints(host_id, host, identifiers[0]))
            except remote.RemoteError as exc:
                status = 504 if exc.error == "ssh_timeout" else 502 if exc.error == "pull_file_failed" else 400
                self._send_json(status, exc.to_json())
            return

        if path.startswith("/api/hosts/") and path.endswith("/artifact-sources"):
            host_id = path[len("/api/hosts/"):-len("/artifact-sources")]
            hosts = load_hosts()
            host = hosts.get(host_id)
            if host is None:
                self._send_json(404, {"error": "unknown_host", "message": f"No configured host: {host_id}"})
                return
            artifact_dir = ""
            for part in urlparse(self.path).query.split("&"):
                if part.startswith("artifact_dir="):
                    artifact_dir = unquote(part.split("=", 1)[1])
            try:
                self._send_json(200, pipeline_steps.artifact_sources(host_id, host, hosts, artifact_dir))
            except remote.RemoteError as exc:
                self._send_json(400 if exc.error == "invalid_input" else 502, exc.to_json())
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
            host_id = parse_qs(urlparse(self.path).query).get("host_id", [None])[0]
            plans = planctl.list_plans()
            self._send_json(200, {"plans": [p for p in plans if host_id is None or p.get("host_id") == host_id]})
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
                plan = planctl.status(plan_id)
                plan["sod"] = planctl.sod_summary(plan_id, plan)
                plan["viability"] = planctl.viability(plan_id, plan)
                self._send_json(200, plan)
            except planctl.PlanError as exc:
                self._send_json(404, exc.to_json())
            return

        if path == "/api/recovery":
            host_values = parse_qs(urlparse(self.path).query, keep_blank_values=True).get("host_id", [])
            if len(host_values) > 1 or (host_values and not host_values[0]):
                self._send_json(400, {"error": "invalid_host", "message": "Provide one nonempty host_id"})
                return
            host_id = host_values[0] if host_values else None
            if host_id is not None and self._resolved_host(host_id) is None:
                self._send_json(404, {"error": "unknown_host", "message": "Unknown configured host"})
                return
            try:
                targets = recoveryctl.target_capabilities(host_id) if host_id is not None else {}
                self._send_json(200, {"requests": recoveryctl.list_requests(host_id=host_id), **recoveryctl.capability(), **targets})
            except ValueError as exc:
                self._send_json(400, {"error": "invalid_host", "message": str(exc)})
            return

        if path.startswith("/api/recovery/"):
            request_id = path[len("/api/recovery/"):]
            try:
                recovery = recoveryctl.status(request_id)
                runs = pipeline_runner.list_runs(key_prefix=f"recovery:{request_id}:")
                recovery["latest_run"] = runs[0] if runs else None
                recovery["active_run"] = next((run for run in runs if run.get("status") in {"queued", "running", "unknown", "reconciling"}), None)
                self._send_json(200, recovery)
            except recoveryctl.RecoveryError as exc:
                self._send_json(404, exc.to_json())
            except remote.RemoteError as exc:
                self._send_json(502, exc.to_json())
            return

        if path == "/":
            self._send_static("index.html")
            return

        self._send_static(path.lstrip("/"))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        self.__dict__.pop("_parsed_body", None)

        if path.startswith("/api/") and not self._require_api_auth():
            return

        if path.startswith("/api/") and not self._authorize_path("POST", path):
            return
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeError) as exc:
            self._send_json(400, {"error": "invalid_body", "message": str(exc)})
            return
        self._dispatch_post(path, body)

    def _dispatch_post(self, path: str, body: dict) -> None:
        """Shared native command dispatcher for API and confirmed assistant actions."""
        self._parsed_body = body
        submitted_body_fields = set(body)
        for field in ("actor", "requester", "plan_id", "request_id", "task_id", "run_id", "host_id", "node", "agent_id", "approval_ticket", "source_plan_id", "window_start", "window_end", "adapter", "artifact_dir", "agent_token", "claim_token"):
            if field in body and not isinstance(body[field], str):
                self._send_json(400, {"error": "invalid_body", "message": f"{field} must be a string"})
                return
        for field in ("max_tasks", "lease_seconds", "seconds"):
            if field in body:
                try:
                    value = body[field]
                    if isinstance(value, bool) or not isinstance(value, (int, str)):
                        raise ValueError()
                    body[field] = int(value)
                except ValueError:
                    self._send_json(400, {"error": "invalid_body", "message": f"{field} must be an integer"})
                    return
        principal = getattr(self, "_principal", None)
        if principal:
            for field in ("actor", "requester"):
                if field in body and body[field] != principal:
                    self._send_json(403, {"error": "unauthorized", "message": f"{field} does not match the authenticated principal"})
                    return
            body.setdefault("actor", principal)
            body.setdefault("requester", principal)

        if path.startswith("/api/assistant/"):
            if submitted_body_fields & {"actor", "requester"}:
                self._send_json(400, {"error": "invalid_body", "message": "Assistant identity comes only from the authenticated session"})
                return
            # Remove only the server-injected identity fields.
            self._assistant_route("POST", path, {key: value for key, value in body.items() if key not in {"actor", "requester"}})
            return

        if path in {"/api/plans/testmode-demo", "/api/recovery/testmode-demo"}:
            try:
                runtime_paths.require_fixtures_allowed()
            except ValueError as exc:
                self._send_json(403, {"error": "fixtures_disabled", "message": str(exc)})
                return

        if path.startswith("/api/fleet/hosts/") and path.endswith("/metadata"):
            host_id = unquote(path[len("/api/fleet/hosts/"):-len("/metadata")])
            fields = {"expected_version", "environment", "desired_patch_baseline"}
            if submitted_body_fields != fields:
                self._send_json(400, {"error": "invalid_body", "message": "Only expected_version, environment and desired_patch_baseline are accepted"})
                return
            actor = principal or self.headers.get("X-OPU-Actor")
            try:
                result = fleet_metadata.update(load_hosts(), host_id, {key: body[key] for key in fields}, actor=actor)
                self._send_json(200, result)
            except fleet_metadata.MetadataError as exc:
                self._send_json(exc.status, exc.to_json())
            return

        if path == "/api/auth/logout":
            if not getattr(self, "_company_session", None):
                self._send_json(400, {"error": "no_company_session"})
                return
            cookie_header = company_auth.logout(self.headers.get("Cookie"))
            self.send_response(204)
            self.send_header("Set-Cookie", cookie_header)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/recovery":
            allowed = {"request_id", "requester", "actor", "host_id", "database", "backup_parent", "window_start", "window_end", "policy"}
            if set(body) - allowed:
                self._send_json(400, {"error": "invalid_body", "message": "Unexpected live recovery fields"})
                return
            try:
                for field in ("request_id", "requester", "host_id", "database", "backup_parent", "window_start", "window_end"):
                    if not isinstance(body.get(field), str) or not body[field].strip():
                        raise ValueError(f"{field} is required")
                host = self._resolved_host(body["host_id"])
                if host is None:
                    raise ValueError("Unknown configured recovery host")
                recoveryctl._identifier(body["request_id"], "request_id")
                def create(_record):
                    return recoveryctl.create_live(body["request_id"], body["requester"], host=host, host_id=body["host_id"],
                        database=body["database"], backup_parent=body["backup_parent"], window_start=body["window_start"],
                        window_end=body["window_end"], policy=body.get("policy"))
                record = pipeline_runner.start_run("recovery", f"recovery:{body['request_id']}:create", create)
                self._send_json(202, {"run_id": record.run_id})
            except pipeline_runner.RunConflict as exc:
                self._send_run_conflict(exc)
            except (ValueError, recoveryctl.RecoveryError) as exc:
                self._send_json(400, {"error": "invalid_recovery", "message": str(exc)})
            return

        if path.startswith("/api/plans/") and path.endswith("/execution-observe"):
            import execution_console
            plan_id = path[len("/api/plans/"):-len("/execution-observe")]
            try:
                planctl.validate_plan_id(plan_id)
                run_id = body.get("run_id")
                if not isinstance(run_id, str) or not run_id:
                    raise ValueError("run_id is required")
                record = pipeline_runner.start_run("execution_observe", f"plan:{plan_id}:observe",
                    lambda _record: execution_console.observe(plan_id, run_id))
                self._send_json(202, {"run_id": record.run_id})
            except pipeline_runner.RunConflict as exc:
                self._send_run_conflict(exc)
            except (ValueError, planctl.PlanError) as exc:
                self._send_json(400, {"error": "invalid_observation", "message": str(exc)})
            return

        if path.startswith("/api/runs/") and path.endswith("/reconcile"):
            run_id = path[len("/api/runs/"):-len("/reconcile")]
            try:
                result = pipeline_runner.reconcile_run(
                    run_id,
                    actor=principal or body.get("actor"),
                    inspect=lambda record: (
                        recoveryctl.reconcile_detached_run(record)
                        if (record.get("context") or {}).get("recovery_request_id")
                        else lockctl.reconcile_detached_run(record)
                        if (record.get("context") or {}).get("lock_recovery") is True
                        else lockctl.reconcile_execution_run(record)
                    ),
                    confirm_no_active_execution=body.get("confirm_no_active_execution") is True,
                    note=body.get("note"),
                )
                self._send_json(200, result)
            except (pipeline_runner.RunConflict, ValueError) as exc:
                self._send_json(409, {"error": "reconciliation_required", "message": str(exc)})
            return

        if path in {"/api/agent/renew", "/api/agent/reconcile"}:
            try:
                if path.endswith("/renew"):
                    if not self._require_role(body.get("agent_id"), "agent"):
                        return
                    job = agent_queue.extend_lease(
                        str(body.get("job_id") or ""), str(body.get("agent_id") or ""),
                        int(body.get("seconds", 120)), agent_token=body.get("agent_token"),
                        claim_token=body.get("claim_token"),
                    )
                else:
                    job = agent_queue.reconcile(str(body.get("job_id") or ""), str(principal or body.get("actor") or ""))
            except (agent_queue.QueueError, ValueError) as exc:
                self._send_json(getattr(exc, "status", 400), exc.to_json() if hasattr(exc, "to_json") else {"error": "invalid_body", "message": str(exc)})
                return
            self._send_json(200, {"job": job})
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
            # Server-side host inventory for cross-host actions; never trust the client's copy.
            body.pop("_hosts", None)
            body.pop("_record", None)
            if step == "stage-artifact":
                body["_hosts"] = load_hosts()

            def run(record, host=host, body=body, step_fn=step_fn):
                if step == "readiness-chain":
                    body["_record"] = record
                return step_fn(host_id, host, body)

            # One pipeline run per host at a time: steps share the host's SSH
            # scratch dir and evidence files, and the chain wraps all of them.
            try:
                record = pipeline_runner.start_run("pipeline", f"host:{host_id}:pipeline", run)
            except pipeline_runner.RunConflict as exc:
                self._send_run_conflict(exc)
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
            adapter = str(body.get("adapter") or "standalone")
            create_fn = {
                "standalone": planctl.create_testmode_demo,
                "rac": planctl.create_testmode_demo_rac,
                "grid": planctl.create_testmode_demo_grid,
            }.get(adapter)
            if create_fn is None:
                self._send_json(400, {"error": "invalid_adapter", "message": f"Unsupported demo adapter: {adapter!r} (expected standalone, rac, or grid)"})
                return
            if not self._require_role(requester, "create"):
                return

            def run(_record, create_fn=create_fn, plan_id=plan_id, requester=requester, window_start=window_start, window_end=window_end):
                return create_fn(plan_id, requester, window_start, window_end)

            try:
                record = pipeline_runner.start_run("plan", f"plan:{plan_id}:testmode-demo", run)
            except pipeline_runner.RunConflict as exc:
                self._send_run_conflict(exc)
                return
            self._send_json(202, {"run_id": record.run_id})
            return

        if path.startswith("/api/plans/") and path.endswith("/extjob-inspect"):
            plan_id = path[len("/api/plans/"):-len("/extjob-inspect")]
            actor = body.get("actor")
            try:
                if submitted_body_fields - {"actor"}:
                    raise extjobctl.ExtjobError("extjob inspection accepts only actor")
                extjobctl.validate_input(plan_id, actor)
            except extjobctl.ExtjobError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            if not self._require_role(actor, "execute"):
                return

            def run(record, plan_id=plan_id, actor=actor):
                return extjobctl.inspect(plan_id, actor, inspection_run_id=record.run_id)

            try:
                # Reserve execution while synchronizing and inspecting. The
                # read-only worker never claims a task or detaches execution.
                record = pipeline_runner.start_run("extjob_inspect", f"plan:{plan_id}:execute", run)
            except pipeline_runner.RunConflict as exc:
                self._send_run_conflict(exc)
                return
            self._send_json(202, {"run_id": record.run_id})
            return

        if path.startswith("/api/plans/") and path.endswith(("/lock-inspect", "/lock-recover")):
            operation = "recover" if path.endswith("/lock-recover") else "inspect"
            suffix = f"/lock-{operation}"
            plan_id = path[len("/api/plans/"):-len(suffix)]
            actor = body.get("actor")
            if not isinstance(actor, str) or not actor.strip():
                self._send_json(400, {"error": "missing_field", "message": "actor is required"})
                return
            run_id = body.get("run_id")
            if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
                self._send_json(400, {"error": "invalid_body", "message": "run_id must be a nonempty string"})
                return
            if not self._require_role(actor, "execute"):
                return

            def run(record, plan_id=plan_id, actor=actor, run_id=run_id, operation=operation):
                if operation == "recover":
                    return lockctl.recover(plan_id, actor, run_id, maintenance_run_id=record.run_id)
                return lockctl.inspect(plan_id, actor, run_id)

            kind = "lock_recovery" if operation == "recover" else "lock_inspect"
            key = f"plan:{plan_id}:lock-recovery" if operation == "recover" else f"plan:{plan_id}:lock-inspect"
            try:
                record = pipeline_runner.start_run(kind, key, run)
            except pipeline_runner.RunConflict as exc:
                self._send_run_conflict(exc)
                return
            self._send_json(202, {"run_id": record.run_id})
            return

        if path.startswith("/api/plans/") and path.endswith("/retry-task"):
            plan_id = path[len("/api/plans/"):-len("/retry-task")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            try:
                actor = body["actor"]
                task_id = body["task_id"]
            except KeyError as exc:
                self._send_json(400, {"error": "missing_field", "message": f"Missing required field: {exc}"})
                return
            if not self._require_role(actor, "execute"):
                return

            def run(_record, plan_id=plan_id, task_id=task_id, actor=actor):
                return planctl.retry_task(plan_id, task_id, actor)

            try:
                record = pipeline_runner.start_run("plan", f"plan:{plan_id}:retry-task", run)
            except pipeline_runner.RunConflict as exc:
                self._send_run_conflict(exc)
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
            if not self._require_role(actor, "execute"):
                return

            def run(_record, plan_id=plan_id, actor=actor):
                try:
                    result = planctl.execute_next_task(plan_id, actor)
                except Exception:
                    notifications.emit("plan.execute.failed", {"plan_id": plan_id, "actor": actor})
                    raise
                notifications.emit("plan.execute.succeeded", {"plan_id": plan_id, "actor": actor, "no_pending_task": result is None})
                return {"task_result": result, "no_pending_task": result is None}

            try:
                record = pipeline_runner.start_run("plan", f"plan:{plan_id}:execute", run)
            except pipeline_runner.RunConflict as exc:
                self._send_run_conflict(exc)
                return
            self._send_json(202, {"run_id": record.run_id})
            return

        if path.startswith("/api/plans/") and path.endswith("/execute-remaining"):
            plan_id = path[len("/api/plans/"):-len("/execute-remaining")]
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
            if not self._require_role(actor, "execute"):
                return
            max_tasks = int(body.get("max_tasks", 200))

            def run(_record, plan_id=plan_id, actor=actor, max_tasks=max_tasks):
                try:
                    result = planctl.execute_remaining_tasks(plan_id, actor, max_tasks=max_tasks)
                except Exception:
                    notifications.emit("plan.execute.failed", {"plan_id": plan_id, "actor": actor})
                    raise
                notifications.emit("plan.execute.succeeded", {"plan_id": plan_id, "actor": actor})
                return result

            try:
                record = pipeline_runner.start_run("plan", f"plan:{plan_id}:execute", run)
            except pipeline_runner.RunConflict as exc:
                self._send_run_conflict(exc)
                return
            self._send_json(202, {"run_id": record.run_id})
            return

        if path.startswith("/api/plans/") and path.endswith("/publish-agent-queue"):
            plan_id = path[len("/api/plans/"):-len("/publish-agent-queue")]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            actor = body.get("actor")
            if not self._require_role(actor, "dispatch"):
                return
            try:
                jobs = planctl.publish_agent_queue(plan_id)
            except planctl.PlanError as exc:
                self._send_json(400, exc.to_json())
                return
            self._send_json(200, {"published": len(jobs), "jobs": jobs})
            return

        if path == "/api/agent/claim":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            agent_id = body.get("agent_id") or body.get("actor")
            if not self._require_role(agent_id, "agent"):
                return
            try:
                job = agent_queue.claim(
                    str(body.get("node") or ""),
                    str(agent_id or ""),
                    lease_seconds=int(body.get("lease_seconds", 120)),
                    agent_token=body.get("agent_token"),
                )
            except agent_queue.QueueError as exc:
                self._send_json(exc.status, exc.to_json())
                return
            self._send_json(200, {"job": job, "idle": job is None})
            return

        if path == "/api/agent/complete":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": "invalid_body", "message": str(exc)})
                return
            agent_id = body.get("agent_id") or body.get("actor")
            if not self._require_role(agent_id, "agent"):
                return
            try:
                job = agent_queue.complete(
                    str(body.get("job_id") or ""),
                    str(agent_id or ""),
                    result=body.get("result"),
                    agent_token=body.get("agent_token"),
                    claim_token=body.get("claim_token"),
                )
            except agent_queue.QueueError as exc:
                self._send_json(exc.status, exc.to_json())
                return
            self._send_json(200, {"job": job})
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

    def _submit_assistant_action(self, path, body):
        """Reuse normal role checks, native guards, dedupe keys and notifications."""
        responses = []
        previous_body = getattr(self, "_parsed_body", None)
        self._response_sink = responses
        try:
            if self._authorize_path("POST", path):
                self._dispatch_post(path, dict(body))
        finally:
            self._response_sink = None
            self._parsed_body = previous_body
        if len(responses) != 1:
            raise assistant.AssistantError("Native command did not return one verified response", 502)
        return responses[0]

    def _assistant_route(self, method, path, body):
        owner = getattr(self, "_principal", None)
        try:
            if path == "/api/assistant/config" and method == "GET":
                self._send_json(200, {**local_llm.config_status(), "can_chat": bool(owner)})
                return
            if not owner:
                raise assistant.AssistantError("Sign in with an individual identity to use the assistant", 403)
            allowed = {action for action in auth.ACTION_ROLES if self._has_role(action)}
            parts = path.strip("/").split("/")
            if parts[:3] != ["api", "assistant", "conversations"]:
                raise assistant.AssistantError("Unknown assistant route", 404)
            if len(parts) == 3:
                if method == "GET":
                    self._send_json(200, {"conversations": assistant.list_conversations(owner)})
                elif not body:
                    self._send_json(201, {"conversation": assistant.create(owner)})
                else:
                    raise assistant.AssistantError("Conversation creation accepts an empty object")
                return
            conversation_id = parts[3]
            if len(parts) == 4 and method == "GET":
                self._send_json(200, {"conversation": assistant.get(owner, conversation_id)})
                return
            if method == "POST" and len(parts) == 5 and parts[4] == "messages":
                if set(body) != {"content"}:
                    raise assistant.AssistantError("Messages accept only content")
                run_id = assistant.send(owner, conversation_id, body["content"], allowed, load_hosts)
                self._send_json(202, {"run_id": run_id})
                return
            if method == "POST" and len(parts) == 7 and parts[4] == "actions" and parts[6] in {"execute", "dismiss"}:
                dismiss = parts[6] == "dismiss"
                if set(body) != (set() if dismiss else {"digest"}):
                    raise assistant.AssistantError("Unexpected action fields")
                result = assistant.action(owner, conversation_id, parts[5], dismiss=dismiss, digest=body.get("digest"),
                    allowed=allowed, load_hosts=load_hosts, submit=self._submit_assistant_action)
                self._send_json(200 if dismiss else 202, {"conversation": result} if dismiss else {"run_id": result})
                return
            raise assistant.AssistantError("Unknown assistant route", 404)
        except pipeline_runner.RunConflict as exc:
            self._send_run_conflict(exc)
        except (assistant.AssistantError, assistant.capabilities.ToolError, local_llm.LLMError, planctl.PlanError, recoveryctl.RecoveryError) as exc:
            self._send_json(getattr(exc, "status", 400), {"error": "assistant_error", "message": str(exc)})

    def _post_plan_action(self, action: str, plan_id: str | None, body: dict) -> None:
        try:
            if action == "create":
                plan_id = body["plan_id"]
                requester = body["requester"]
                host_id = body["host_id"]
                window_start = body["window_start"]
                window_end = body["window_end"]
                if not self._require_role(requester, "create"):
                    return
                if self._resolved_host(host_id) is None:
                    self._send_json(404, {"error": "unknown_host", "message": f"No configured host: {host_id}"})
                    return
                key = f"plan:{plan_id}:create"

                def run(_record, plan_id=plan_id, requester=requester, host_id=host_id, window_start=window_start, window_end=window_end):
                    return planctl.create(plan_id, requester, host_id, window_start, window_end)

            elif action == "approve":
                actor = body["actor"]
                approval_ticket = body["approval_ticket"]
                if not self._require_role(actor, "approve"):
                    return
                if itsm.itsm_enabled():
                    # Fail-closed gate: an invalid ticket or an unusable
                    # registry rejects before any run is started.
                    try:
                        itsm.validate_ticket(approval_ticket)
                    except itsm.ItsmError as exc:
                        self._send_json(exc.status, exc.to_json())
                        return
                key = f"plan:{plan_id}:approve"

                def run(_record, plan_id=plan_id, actor=actor, approval_ticket=approval_ticket):
                    return planctl.approve(plan_id, actor, approval_ticket)

            elif action == "authorize":
                actor = body["actor"]
                if not self._require_role(actor, "authorize"):
                    return
                key = f"plan:{plan_id}:authorize"

                def run(_record, plan_id=plan_id, actor=actor):
                    return planctl.authorize(plan_id, actor)

            elif action == "dispatch":
                actor = body["actor"]
                if not self._require_role(actor, "dispatch"):
                    return
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
                if not self._require_role(requester, "create"):
                    return
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

        event = {
            "create": "plan.created",
            "approve": "plan.approved",
            "authorize": "plan.authorized",
            "dispatch": "plan.dispatched",
            "create-rollback": "plan.created",
        }.get(action)
        notify_plan_id = new_plan_id if action == "create-rollback" else plan_id

        def notifying_run(record, inner=run, event=event, notify_plan_id=notify_plan_id, action=action):
            result = inner(record)
            # Emit only after the plan action has completed and persisted;
            # emit() never raises, so it cannot fail the run.
            if event:
                notifications.emit(event, {"plan_id": notify_plan_id, "action": action})
            return result

        try:
            record = pipeline_runner.start_run("plan", key, notifying_run)
        except pipeline_runner.RunConflict as exc:
            self._send_run_conflict(exc)
            return
        self._send_json(202, {"run_id": record.run_id})

    def _post_recovery_action(self, action: str, request_id: str | None, body: dict) -> None:
        try:
            if action == "create":
                request_id = body["request_id"]
                key = f"recovery:{request_id}:create"

                def run(_record, body=body):
                    host_id = body.get("host_id")
                    if host_id and self._resolved_host(host_id) is None:
                        raise recoveryctl.RecoveryError("Unknown recovery host_id")
                    return recoveryctl.create_testmode_demo(body["request_id"], body["requester"], host_id=host_id)

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
            self._send_run_conflict(exc)
            return
        self._send_json(202, {"run_id": record.run_id})


def main() -> None:
    port = int(os.environ.get("OPU_WEBAPP_PORT") or "8765")
    if company_auth.configured():
        company_auth.config()
    elif auth.rbac_enabled():
        auth.validate_configuration()
    else:
        auth.ensure_token()
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

    print(f"opu webapp listening on {scheme}://127.0.0.1:{port}", flush=True)
    print(f"API token file: {auth.TOKEN_FILE}", flush=True)
    print(f"authentication mode: {'principal credentials' if auth.rbac_enabled() else 'local lab token file'}", flush=True)
    print(
        "production mode: "
        f"{'enabled' if production_state['production_mode'] else 'disabled'}; "
        f"certified={production_state['certified']}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
