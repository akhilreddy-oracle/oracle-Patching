"""Strict contracts for the first migrated API route group.

Domain services remain framework-independent. Additional session fields are
preserved for existing principal/company clients; known fields cannot coerce
strings or numbers into roles, booleans or evidence status.
"""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, RootModel


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)


class JsonObject(RootModel[dict[str, Any]]):
    model_config = ConfigDict(strict=True)


class Health(Contract):
    status: Literal["ok"]
    time: FiniteFloat


class Permissions(Contract):
    live_discovery: bool


class Session(Contract):
    actor: str | None
    mode: Literal["principal", "company", "lab"]
    rbac_enabled: bool
    roles: list[str]
    permissions: Permissions


class FixtureResult(Contract):
    status: Literal["unknown", "passed", "failed"]
    scopes: list[Literal["check", "browser"]]
    reason: str
    source_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    completed_at: FiniteFloat | None = None


class AcceptanceResult(Contract):
    # Current application has no mechanism to certify live/production evidence.
    status: Literal["unverified"]
    reason: str


class Validation(Contract):
    fixture_tested: FixtureResult
    live_lab_verified: AcceptanceResult
    production_approved: AcceptanceResult


class Approval(Contract):
    kind: Literal["plan", "recovery"]
    id: str
    state: Literal["awaiting_approval", "approved"]
    requester: str | None
    target: Any
    host_id: str | None
    window: dict[str, Any] | None
    next_action: Literal["review_approval", "review_authorization"]
    self_requested: bool


class ApprovalInbox(Contract):
    items: list[Approval]
    actor: str | None
