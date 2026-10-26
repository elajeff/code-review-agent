"""Read reviewed Python source safely from a Git commit or test fixture."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath

EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "site-packages", "dist", "build",
}


def _relative_parts(relative_path: str) -> tuple[str, ...] | None:
    """Reject absolute paths, drives, traversal, NULs and ambiguous path segments."""
    if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
        return None
    if PureWindowsPath(relative_path).drive:
        return None
    normalized = relative_path.replace("\\", "/")
    if normalized.startswith("/"):
        return None
    parts = tuple(normalized.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        return None
    parsed = PurePosixPath(normalized)
    if parsed.is_absolute() or parsed.suffix != ".py":
        return None
    if any(part in EXCLUDED_DIRS for part in parts):
        return None
    return parts


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    # Path.is_junction exists on newer Python versions; keep compatibility with 3.11.
    junction_check = getattr(path, "is_junction", None)
    return bool(junction_check()) if callable(junction_check) else False


def safe_source_path(repo: Path, relative_path: str) -> Path | None:
    parts = _relative_parts(relative_path)
    if parts is None:
        return None

    root = repo.resolve()
    path = root
    try:
        for part in parts:
            path = path / part
            if _is_link_or_junction(path):
                return None
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return None
    if not resolved.is_file():
        return None
    return resolved


@lru_cache(maxsize=64)
def _is_git_repo(repo_text: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_text,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def get_head_commit(repo: Path) -> str | None:
    """Return the current commit ID so cached source is invalidated after a new commit."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


@lru_cache(maxsize=512)
def _read_head_file(repo_text: str, commit: str, relative_path: str, max_bytes: int) -> str | None:
    """Read one committed blob, cached by commit so later changes cannot go stale."""
    try:
        size_result = subprocess.run(
            ["git", "--literal-pathspecs", "ls-tree", "-l", commit, "--", relative_path],
            cwd=repo_text,
            capture_output=True,
            text=True,
            timeout=3,
        )
        metadata = size_result.stdout.split("\t", 1)[0].split()
        if (size_result.returncode != 0 or len(metadata) != 4
                or metadata[0] not in {"100644", "100755"}
                or metadata[1] != "blob" or not metadata[3].isdigit()):
            return None
        if int(metadata[3]) > max_bytes:
            return None
        result = subprocess.run(
            ["git", "show", f"{commit}:{relative_path}"],
            cwd=repo_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=5,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > max_bytes:
        return None
    return result.stdout


def read_reviewed_source(
    repo: Path,
    relative_path: str,
    max_bytes: int = 150_000,
    head_commit: str | None = None,
) -> str | None:
    """Read the reviewed commit in Git repos, or disk files in test fixtures."""
    if _relative_parts(relative_path) is None:
        return None
    root = repo.resolve()
    normalized_relative = relative_path.replace("\\", "/")

    if _is_git_repo(str(root)):
        commit = head_commit or get_head_commit(root)
        if not commit:
            return None
        return _read_head_file(str(root), commit, normalized_relative, max_bytes)

    path = safe_source_path(repo, relative_path)
    if path is None:
        return None
    try:
        if path.stat().st_size > max_bytes:
            return None
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if len(content.encode("utf-8")) > max_bytes:
        return None
    return content
