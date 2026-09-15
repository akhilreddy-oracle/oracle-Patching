"""Expose fixture evidence without elevating it to live or production approval."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def status():
    path = ROOT / 'scripts/release_validation.py'
    spec = importlib.util.spec_from_file_location('opu_release_validation', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validation_status(ROOT)
