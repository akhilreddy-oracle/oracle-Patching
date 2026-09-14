"""Validate complete patch trees and extract only preflighted regular media.

Never use archive extraction APIs that can create links, devices, or traverse
outside the private destination. All headers are checked before writing data.
"""
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile
import zipfile


def validate_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("artifact root must be a real directory")
    device = root.stat().st_dev
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if info.st_dev != device or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError(f"artifact contains a link, special file, or mount: {path}")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError(f"artifact contains a hard-linked file: {path}")
            if any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise ValueError("artifact filenames cannot contain control characters")


def member_path(name: str, expected: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or ".." in path.parts
            or not path.parts or path.parts[0] != expected
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ValueError(f"unsafe or unexpected archive path: {name!r}")
    return path


def extract(kind: str, source: Path, destination: Path, expected: str) -> None:
    max_bytes = int(os.environ.get("OPU_ARTIFACT_MAX_BYTES", str(100 * 1024**3)))
    max_files = int(os.environ.get("OPU_ARTIFACT_MAX_ENTRIES", "1000000"))
    if max_bytes <= 0 or max_files <= 0:
        raise ValueError("artifact extraction limits must be positive")
    archive = tarfile.open(source, "r:*") if kind == "tar" else zipfile.ZipFile(source)
    with archive:
        entries, seen, total = [], set(), 0
        members = archive if kind == "tar" else archive.infolist()
        for item in members:
            name = item.name if kind == "tar" else item.filename
            rel = member_path(name, expected)
            if kind == "tar":
                directory, regular, mode, size = item.isdir(), item.isreg(), item.mode, item.size
            else:
                mode = item.external_attr >> 16
                directory = item.is_dir()
                regular = stat.S_IFMT(mode) in (0, stat.S_IFREG)
                if directory and stat.S_IFMT(mode) not in (0, stat.S_IFDIR):
                    raise ValueError(f"unsafe archive directory: {name}")
                size = item.file_size
            if not directory and not regular:
                raise ValueError(f"archive links and special files are forbidden: {name}")
            if rel in seen or size < 0:
                raise ValueError(f"duplicate or invalid archive entry: {name}")
            seen.add(rel); total += size
            if len(seen) > max_files or total > max_bytes:
                raise ValueError("archive exceeds extraction limits")
            entries.append((item, rel, directory, mode, size))
        if not entries:
            raise ValueError("archive is empty")
        for item, rel, directory, mode, size in entries:
            target = destination.joinpath(*rel.parts)
            if directory:
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stream = archive.extractfile(item) if kind == "tar" else archive.open(item)
            if stream is None:
                raise ValueError(f"archive entry has no payload: {rel}")
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with stream, os.fdopen(fd, "wb") as output:
                shutil.copyfileobj(stream, output, length=1024 * 1024)
            if target.stat().st_size != size:
                raise ValueError(f"archive payload size mismatch: {rel}")
            target.chmod(0o755 if mode & 0o111 else 0o644)
        validate_tree(destination / expected)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "validate-tree":
            validate_tree(Path(sys.argv[2]))
        elif len(sys.argv) == 5 and sys.argv[1] in {"tar", "zip"}:
            extract(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
        else:
            raise ValueError("usage: artifact_media.py validate-tree DIR | tar|zip ARCHIVE PRIVATE_DIR PATCH_ID")
    except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"unsafe patch media: {exc}", file=sys.stderr)
        raise SystemExit(65)
