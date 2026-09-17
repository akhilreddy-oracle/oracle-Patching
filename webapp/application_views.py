"""Read-side application services, independent of HTTP and framework models."""
from __future__ import annotations

import time

import auth
import planctl
import recoveryctl
import release_status


def health() -> dict:
    return {"status": "ok", "time": time.time()}


def session(actor: str | None, company: dict | None, *, can_discover: bool) -> dict:
    value = {key: item for key, item in company.items() if key != "groups"} if company else auth.whoami(actor)
    # A UI hint: native operations always perform their own current role checks.
    return {**value, "permissions": {"live_discovery": can_discover}}


def validation() -> dict:
    return release_status.status()


def approvals(actor: str | None) -> dict:
    items = []
    for kind, records in (("plan", planctl.list_plans()), ("recovery", recoveryctl.list_requests())):
        for item in records:
            if item.get("state") not in {"awaiting_approval", "approved"}:
                continue
            items.append({
                "kind": kind, "id": item.get("plan_id" if kind == "plan" else "request_id"),
                "state": item["state"], "requester": item.get("requester"),
                "target": item.get("target"), "host_id": item.get("host_id"),
                "window": item.get("maintenance_window", item.get("window")),
                "next_action": "review_approval" if item["state"] == "awaiting_approval" else "review_authorization",
                "self_requested": bool(actor and item.get("requester") == actor),
            })
    return {"items": items, "actor": actor}
