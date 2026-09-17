"""Production certification gate (S13 starter).

Mutation through the lab webapp live-execute path refuses to run when
OPU_PRODUCTION_MODE=1 unless a local certification marker is present. This is
not a substitute for the full S13 pilot checklist — it is the explicit
fail-closed switch so uncertified builds cannot pretend to be production.
"""
from __future__ import annotations

import os
from pathlib import Path
import runtime_paths

CERT_FILE_ENV = "OPU_PRODUCTION_CERT_FILE"
MODE_ENV = "OPU_PRODUCTION_MODE"
DEFAULT_CERT_FILE = runtime_paths.state_dir() / "production.cert"


class ProductionError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.error = "production_not_certified"
        self.message = message

    def to_json(self) -> dict:
        return {"error": self.error, "message": self.message}


def production_mode_enabled() -> bool:
    return _enabled(MODE_ENV)


def _enabled(name: str) -> bool:
    value = (os.environ.get(name) or "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise ProductionError(f"{name} must be a boolean value; refusing ambiguous production configuration")


def cert_file() -> Path:
    override = (os.environ.get(CERT_FILE_ENV) or "").strip()
    return Path(override) if override else DEFAULT_CERT_FILE


CHECKLIST_KEYS = (
    "OPU_PRODUCTION_CERTIFIED=1",
    "OPU_SBOM_VERIFIED=1",
    "OPU_RELEASE_SIGNED=1",
    "OPU_THREAT_MODEL_SIGNED=1",
)


def _cert_text() -> str | None:
    path = cert_file()
    if not path.is_file() or path.is_symlink():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def checklist_required() -> bool:
    return _enabled("OPU_PRODUCTION_REQUIRE_CHECKLIST")


def _has_exact_marker(text: str, key: str) -> bool:
    return any(line.strip() == key for line in text.splitlines())


def checklist_status() -> dict[str, bool]:
    text = _cert_text() or ""
    return {key: _has_exact_marker(text, key) for key in CHECKLIST_KEYS}


def is_certified() -> bool:
    text = _cert_text()
    if text is None:
        return False
    if not _has_exact_marker(text, "OPU_PRODUCTION_CERTIFIED=1"):
        return False
    if checklist_required():
        return all(_has_exact_marker(text, key) for key in CHECKLIST_KEYS)
    return True


def status() -> dict:
    return {
        "production_mode": production_mode_enabled(),
        "certified": is_certified(),
        "cert_file": str(cert_file()),
        "checklist_required": checklist_required(),
        "checklist": checklist_status(),
    }


def require_live_mutation_allowed() -> None:
    if not production_mode_enabled():
        return
    if is_certified():
        return
    raise ProductionError(
        "OPU_PRODUCTION_MODE is enabled but production certification marker "
        f"is missing or invalid at {cert_file()}. Refusing live mutation."
    )
