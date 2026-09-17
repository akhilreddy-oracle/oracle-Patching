"""Stable controller paths independent of a versioned application release."""
from __future__ import annotations

import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent


def deployment_root(code_root: Path | None = None) -> Path:
    """Keep mutable state outside an immutable, content-addressed tool tree.

    Callers derive code_root from their own resolved __file__, never from a
    deployment-root environment override. Legacy installations keep their
    existing paths; the reserved runtime layout must be exact and canonical.
    """
    root = ROOT.parent if code_root is None else Path(code_root)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("Code root must be an absolute normalized path")
    if ".opu-runtimes" not in root.parts:
        return root
    if (root.parts.count(".opu-runtimes") != 1 or root.parent.name != ".opu-runtimes"
            or not re.fullmatch(r"[a-f0-9]{64}", root.name) or root != root.resolve()):
        raise ValueError("Invalid immutable runtime generation layout")
    return root.parent.parent


def _absolute_config(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if value is None:
        return default
    path = Path(value)
    if not value or not path.is_absolute() or path != path.resolve():
        raise ValueError(f"{name} must be a canonical absolute path (no symlinks)")
    return path


def state_dir() -> Path:
    return _absolute_config("OPU_WEBAPP_STATE_DIR", deployment_root() / "webapp/var")


def hosts_file() -> Path:
    return _absolute_config("OPU_WEBAPP_HOSTS_FILE", deployment_root() / "webapp/hosts.json")


def require_fixtures_allowed() -> None:
    value = os.environ.get("OPU_WEBAPP_ALLOW_FIXTURES", "1")
    if value != "1":
        raise ValueError("Fixture operations are disabled on this controller")
