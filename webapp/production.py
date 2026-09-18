"""Production certification gate (S13 starter).

Mutation through the lab webapp live-execute path refuses to run when
OPU_PRODUCTION_MODE=1 unless a local certification marker is present. This is
not a substitute for the full S13 pilot checklist — it is the explicit
fail-closed switch so uncertified builds cannot pretend to be production.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import runtime_paths

CERT_FILE_ENV = "OPU_PRODUCTION_CERT_FILE"
MODE_ENV = "OPU_PRODUCTION_MODE"
DEFAULT_CERT_FILE = runtime_paths.state_dir() / "production.cert"

_spec = importlib.util.spec_from_file_location(
    'opu_production_cert', Path(__file__).resolve().parents[1] / 'lib/opu/production_cert.py')
_marker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_marker)
CHECKLIST_KEYS = _marker.CHECKLIST_KEYS


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


def _cert_text() -> str | None:
    return _marker.read_text(cert_file())


def checklist_required() -> bool:
    return _enabled("OPU_PRODUCTION_REQUIRE_CHECKLIST")


def checklist_status() -> dict[str, bool]:
    return _marker.checklist(_cert_text())


def is_certified() -> bool:
    return _marker.certified(checklist_status(), checklist_required())


def status() -> dict:
    mode = production_mode_enabled()
    required = checklist_required()
    values = checklist_status()
    return {
        "production_mode": mode,
        "certified": _marker.certified(values, required),
        "cert_file": str(cert_file()),
        "checklist_required": required,
        "checklist": values,
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
