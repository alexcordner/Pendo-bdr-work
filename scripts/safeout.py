"""Safe output files for the helper scripts (review O2): they hold real addresses or ids, so they may only be written
under the repository's ``output/`` directory (gitignored), never over an existing symlink, with mode 0600.

``APAC_OUTPUT_ROOT`` overrides the allowed root (tests only). Local files only; no network.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class UnsafeOutput(ValueError):
    """The requested output path is refused. Nothing was written."""


def output_root() -> Path:
    return Path(os.environ.get("APAC_OUTPUT_ROOT") or (ROOT / "output")).resolve()


def _git_ignored(path: Path) -> bool | None:
    """True / False from ``git check-ignore``; None when git is unavailable or the path is not in a repository."""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", "--", str(path)], cwd=str(ROOT), capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(r.returncode)


def safe_out_path(path) -> Path:
    """Resolve ``path`` (symlinks included) and require it to sit under ``output/``; refuse it if git says it is NOT
    ignored. Returns the resolved path."""
    p = Path(path).expanduser()
    if p.is_symlink():
        raise UnsafeOutput(f"{p} is a symlink: refused")
    resolved = p.resolve()
    root = output_root()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise UnsafeOutput(f"output file must be under {root} (got {resolved})") from None
    if resolved == root:
        raise UnsafeOutput("output file must be a file inside output/, not output/ itself")
    if _git_ignored(resolved) is False:
        raise UnsafeOutput(f"{resolved} is not ignored by git: refused (real data must never be committable)")
    return resolved


def write_private(path, text: str) -> Path:
    """Write ``text`` to a NEW 0600 file at a safe path. An earlier regular file there (this tool's own scratch file
    from a previous round) is replaced; a symlink or directory is refused. The file is created with O_EXCL and
    O_NOFOLLOW and its mode is set explicitly, so the umask cannot make it more open."""
    p = safe_out_path(path)
    if p.exists():
        if p.is_symlink() or not p.is_file():
            raise UnsafeOutput(f"{p} exists and is not a regular file: refused")
        p.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(p, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return p
