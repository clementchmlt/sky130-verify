"""Read-only git probes shared by doctor.py and check.py.

No writes and no network access (no ``fetch`` or ``clone``): only
``rev-parse``, ``config``, ``show`` and ``status`` on a local repository.
"""

from __future__ import annotations

from pathlib import Path
import subprocess


def _run(args: list[str]) -> str | None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def head(path: Path) -> str | None:
    return _run(["git", "-C", str(path), "rev-parse", "HEAD"])


def toplevel(path: Path) -> Path | None:
    out = _run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    return Path(out) if out else None


def remote_url(path: Path) -> str | None:
    return _run(["git", "-C", str(path), "config", "--get", "remote.origin.url"])


def blob_at_commit(repo_root: Path, relative_path: str, commit: str) -> bytes | None:
    """Content of ``relative_path`` at ``commit``, or ``None`` if the path
    does not exist in that commit (for example, a file added later)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{commit}:{relative_path}"],
            capture_output=True, timeout=10,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def is_repo_dirty(path: Path) -> bool | None:
    """``True``/``False`` when known, ``None`` when ``path`` is not in a git
    repository (not an error: the question does not apply)."""
    out = _run(["git", "-C", str(path), "status", "--porcelain"])
    if out is None:
        # `_run` returns None both outside a repository and for a clean tree
        # (empty `status --porcelain` output); tell them apart by checking
        # for a repository explicitly.
        return None if toplevel(path) is None else False
    return True
