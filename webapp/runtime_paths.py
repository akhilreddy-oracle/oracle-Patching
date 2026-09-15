"""Stable controller paths independent of a versioned application release."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _absolute_config(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if value is None:
        return default
    path = Path(value)
    if not value or not path.is_absolute() or path != path.resolve():
        raise ValueError(f"{name} must be a canonical absolute path (no symlinks)")
    return path


def state_dir() -> Path:
    return _absolute_config("OPU_WEBAPP_STATE_DIR", ROOT / "var")


def hosts_file() -> Path:
    return _absolute_config("OPU_WEBAPP_HOSTS_FILE", ROOT / "hosts.json")


def require_fixtures_allowed() -> None:
    value = os.environ.get("OPU_WEBAPP_ALLOW_FIXTURES", "1")
    if value != "1":
        raise ValueError("Fixture operations are disabled on this controller")
