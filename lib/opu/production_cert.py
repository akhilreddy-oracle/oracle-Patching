"""Read the local production switch once, from a protected regular file.

This is an admission switch, not a cryptographic release certification.
Shared by the native mutation guard and the controller; stdlib only.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

CHECKLIST_KEYS = (
    'OPU_PRODUCTION_CERTIFIED=1',
    'OPU_SBOM_VERIFIED=1',
    'OPU_RELEASE_SIGNED=1',
    'OPU_THREAT_MODEL_SIGNED=1',
)
MAX_BYTES = 16384


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_text(path: Path) -> str | None:
    """Reject links, shared writers, special files and changes during the read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid not in {0, os.geteuid()}
                or before.st_mode & 0o022 or before.st_size > MAX_BYTES):
            return None
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(MAX_BYTES + 1)
        after = os.fstat(fd)
        if (len(data) > MAX_BYTES or len(data) != before.st_size
                or _identity(before) != _identity(after)
                or _identity(after) != _identity(path.lstat())):
            return None
        return data.decode('utf-8')
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(fd)


def checklist(text: str | None) -> dict[str, bool]:
    """Recognized switches must have exactly one unambiguous, exact value."""
    lines = (text or '').splitlines()
    result = {}
    for key in CHECKLIST_KEYS:
        name = key.split('=', 1)[0]
        # Whitespace cannot hide a second, conflicting assignment.
        assignments = [line.strip() for line in lines
                       if line.strip().split('=', 1)[0].strip() == name]
        result[key] = assignments == [key] and key in lines
    return result


def certified(values: dict[str, bool], require_checklist: bool) -> bool:
    return values[CHECKLIST_KEYS[0]] and (not require_checklist or all(values.values()))


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in {'0', '1'}:
        return 64
    values = checklist(read_text(Path(sys.argv[1])))
    return 0 if certified(values, sys.argv[2] == '1') else 77


if __name__ == '__main__':
    raise SystemExit(main())
