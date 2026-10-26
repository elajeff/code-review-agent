"""Bounded, rule-based retrieval of code context for plausible findings.

The first review sees only the diff. This module expands context only for accepted
candidates, using Python AST locations, direct call sites and relevant test snippets.
It deliberately does not dump the whole repository into the prompt.
"""

from __future__ import annotations

import ast
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .git_diff import ChangedFile, HUNK
from .source import EXCLUDED_DIRS, get_head_commit, read_reviewed_source, safe_source_path

DEFAULT_CONTEXT_BUDGET = 12_000
MAX_FILES_TO_SCAN = 250
MAX_FILE_BYTES = 150_000
MAX_SCOPE_CHARS = 2_600
MAX_CALL_SITES = 3
MAX_TEST_SNIPPETS = 2
COMMON_CALL_NAMES = {"run", "get", "set", "main", "build", "parse", "load", "save", "close", "open", "init"}


@dataclass(frozen=True)
class ContextBundle:
    text: str
    chars_used: int
    files_scanned: int
    truncated: bool
    blocks_added: int


def _tracked_python_files(repo: Path) -> tuple[list[str], bool]:
    """Prefer tracked Python files; fall back to files in temporary fixture repos."""
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "--name-only", "HEAD"], cwd=repo,
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    is_git_repo = result is not None and result.returncode == 0
    if is_git_repo:
        paths = [item.decode("utf-8", errors="replace") for item in result.stdout.split(b"\0") if item]
    else:
        paths = []

    if not is_git_repo:
        # The fallback is mainly for small test fixtures. Prune dependency/cache
        # directories during traversal rather than walking them and filtering later.
        paths = []
        for root, directories, filenames in os.walk(repo):
            directories[:] = [
                name for name in directories
                if name not in EXCLUDED_DIRS and not (Path(root) / name).is_symlink()
            ]
            for filename in filenames:
                if filename.endswith(".py"):
                    rel = (Path(root) / filename).relative_to(repo).as_posix()
                    paths.append(rel)
                    if len(paths) > MAX_FILES_TO_SCAN:
                        break
            if len(paths) > MAX_FILES_TO_SCAN:
                break
    safe = []
    for rel in sorted(set(paths)):
        if not rel.endswith(".py") or any(part in EXCLUDED_DIRS for part in rel.split("/")):
            continue
        if is_git_repo or safe_source_path(repo, rel) is not None:
            safe.append(rel)
    limited = len(safe) > MAX_FILES_TO_SCAN
    return safe[:MAX_FILES_TO_SCAN], limited


def _line_excerpt(
    source: str,
    start: int,
    end: int,
    max_chars: int,
    focus_line: int | None = None,
) -> str:
    """Render a bounded excerpt while always preserving its focus line."""
    lines = source.splitlines()
    start = max(1, start)
    end = min(len(lines), end)
    if start > end or max_chars < 80:
        return ""

    focus = min(max(focus_line or start, start), end)
    selected = {start, focus}

    def render(indices: set[int]) -> str:
        output: list[str] = []
        previous = None
        for index in sorted(indices):
            if previous is not None and index > previous + 1:
                output.append("    ... [lines omitted] ...")
            prefix = f"{index:>4}: "
            content = lines[index - 1]
            line_limit = max(20, max_chars - len(prefix) - 24)
            if len(content) > line_limit:
                content = content[:line_limit] + " ... [line truncated]"
            output.append(prefix + content)
            previous = index
        return "\n".join(output)

    full = "\n".join(f"{index:>4}: {lines[index - 1]}" for index in range(start, end + 1))
    if len(full) <= max_chars:
        return full

    candidates = []
    for distance in range(1, 5):
        candidates.extend((focus - distance, focus + distance))
    candidates.extend((start + 1, start + 2, start + 3))
    for index in candidates:
        if start <= index <= end and index not in selected:
            trial = set(selected)
            trial.add(index)
            if len(render(trial)) <= max_chars:
                selected = trial

    result = render(selected)
    if len(result) <= max_chars:
        return result + ("\n    ... [context excerpt truncated] ..." if len(selected) < end - start + 1 and len(result) + 42 <= max_chars else "")

    # Mandatory header/focus lines may themselves be unusually long. Keep both
    # visible and hard-cap the final string, with the focus excerpt last.
    focus_text = f"{focus:>4}: {lines[focus - 1]}"
    focus_text = focus_text[:max_chars - 20] + " ... [truncated]"
    return focus_text[:max_chars]


def _enclosing_scopes(source: str, line: int) -> list[ast.AST]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    scopes: list[ast.AST] = []

    def walk(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= line <= end:
                scopes.append(node)
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    scopes.sort(key=lambda item: getattr(item, "end_lineno", item.lineno) - item.lineno)
    return scopes


def _related_calls(source: str, target_names: set[str]) -> tuple[set[str], list[tuple[int, int, str]]]:
    """Parse a file once to find direct call sites and the functions that contain them."""
    if not target_names:
        return set(), []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set(), []
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    callers: set[str] = set()
    sites: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called_name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
        if called_name not in target_names:
            continue
        line = getattr(node, "lineno", None)
        if not isinstance(line, int):
            continue
        start, end = max(1, line - 3), line + 3
        sites.append((start, end, f"call to {called_name}"))
        containing = [fn for fn in functions if fn.lineno <= line <= getattr(fn, "end_lineno", fn.lineno)]
        if containing:
            containing.sort(key=lambda fn: getattr(fn, "end_lineno", fn.lineno) - fn.lineno)
            callers.add(containing[0].name)
    sites.sort(key=lambda item: (item[0], item[2]))
    return callers, sites


def _test_snippets(source: str, target_names: set[str]) -> list[tuple[int, int, str]]:
    targets = {name.casefold() for name in target_names if len(name) >= 4}
    if not targets:
        return []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    snippets: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test"):
            continue
        segment = (ast.get_source_segment(source, node) or "").casefold()
        if any(target in segment or target in node.name.casefold() for target in targets):
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            snippets.append((start, end, f"test {node.name}"))
    snippets.sort(key=lambda item: (item[0], item[2]))
    return snippets


def _relevant_diff_hunk(changed: ChangedFile, target_line: int) -> tuple[int, int, str] | None:
    """Return the diff hunk whose new-file range contains the finding line."""
    lines = changed.diff.splitlines()
    for index, line in enumerate(lines):
        match = HUNK.match(line)
        if not match:
            continue
        hunk_new_start = int(match.group(1))
        hunk_new_count = int(match.group(2) or 1)
        # Range lines extend to the next hunk (or the end of this changed file).
        next_index = len(lines)
        for candidate_index in range(index + 1, len(lines)):
            if HUNK.match(lines[candidate_index]):
                next_index = candidate_index
                break
        if hunk_new_start <= target_line <= hunk_new_start + max(0, hunk_new_count - 1):
            excerpt = "\n".join(lines[index:next_index])
            return hunk_new_start, hunk_new_start + max(0, hunk_new_count - 1), excerpt
    return None


def build_context_bundle(
    repo: Path,
    findings: Iterable[object],
    changed_files: list[ChangedFile] | None = None,
    max_chars: int = DEFAULT_CONTEXT_BUDGET,
) -> ContextBundle:
    """Retrieve candidate-specific context within a single character budget.

    Order of retrieval: relevant diff hunk, enclosing scope, module imports, direct
    callers, then matching tests. Lower-priority blocks are omitted at the budget.
    """
    if max_chars < 500:
        raise ValueError("Context budget must be at least 500 characters.")
    findings = list(findings)
    if not findings:
        return ContextBundle(text="", chars_used=0, files_scanned=0, truncated=False, blocks_added=0)
    repo = repo.resolve()
    head_commit = get_head_commit(repo)
    files, file_scan_limited = _tracked_python_files(repo)
    blocks: list[str] = []
    seen: set[tuple[str, int, int, str]] = set()
    used = 0
    truncated = file_scan_limited
    scanned_paths: set[str] = set()

    def add_raw_block(path: str, start: int, end: int, kind: str, raw_text: str) -> None:
        nonlocal used, truncated
        key = (path, start, end, kind)
        if key in seen or not raw_text.strip():
            return
        block = f"[{kind}: {path}:{start}-{end}]\n{raw_text.strip()}"
        remaining = max_chars - used
        if remaining <= 0:
            truncated = True
            return
        if len(block) > remaining:
            if remaining < 200:
                truncated = True
                return
            suffix = "\n... [context budget reached]"
            block = block[:max(0, remaining - len(suffix))] + suffix
            truncated = True
        blocks.append(block)
        seen.add(key)
        used += len(block) + 2

    def add_block(path: str, start: int, end: int, kind: str, source: str, max_snippet_chars: int = MAX_SCOPE_CHARS, focus_line: int | None = None) -> None:
        actual_end = min(end, len(source.splitlines()))
        excerpt = _line_excerpt(source, start, actual_end, max_snippet_chars, focus_line)
        add_raw_block(path, start, actual_end, kind, excerpt)

    # The relevant before/after hunk is important: the contextual pass must be able
    # to distinguish a new regression from a pre-existing issue.
    changed_by_path = {item.path: item for item in (changed_files or [])}
    candidate_names: set[str] = set()
    for finding in findings:
        path = str(getattr(finding, "file", ""))
        line = int(getattr(finding, "line", 0) or 0)
        source = read_reviewed_source(repo, path, MAX_FILE_BYTES, head_commit=head_commit)
        if source is None or not line:
            continue
        scanned_paths.add(path)
        lines = source.splitlines()
        if not 1 <= line <= len(lines):
            continue
        changed = changed_by_path.get(path)
        if changed is not None:
            hunk = _relevant_diff_hunk(changed, line)
            if hunk is not None:
                add_raw_block(path, hunk[0], hunk[1], "before/after diff hunk", hunk[2])
        scopes = _enclosing_scopes(source, line)
        if scopes:
            scope = scopes[0]
            start = int(getattr(scope, "lineno", line))
            end = int(getattr(scope, "end_lineno", line))
            name = str(getattr(scope, "name", ""))
            add_block(path, start, end, "enclosing scope", source, focus_line=line)
        else:
            start, end = max(1, line - 12), min(len(lines), line + 12)
            name = ""
            add_block(path, start, end, "nearby code", source, focus_line=line)

        # Imports can explain aliases and existing helpers without sending a whole module.
        import_lines = [
            index for index, text in enumerate(lines[:80], start=1)
            if text.lstrip().startswith(("import ", "from "))
        ]
        if import_lines:
            import_excerpt = "\n".join(f"{index:>4}: {lines[index - 1]}" for index in import_lines)
            add_raw_block(path, import_lines[0], import_lines[-1], "module imports", import_excerpt[:1300])
        if name and name not in COMMON_CALL_NAMES and len(name) >= 4:
            candidate_names.add(name)

    if used < max_chars and candidate_names:
        test_paths: list[str] = []
        source_paths: list[str] = []
        for path in files:
            is_test = any(part.startswith("test") for part in PurePosixPath(path).parts) or PurePosixPath(path).name.startswith("test_")
            (test_paths if is_test else source_paths).append(path)

        caller_names: set[str] = set()
        call_count = 0
        # First inspect ordinary source files so caller names are known before we look for tests.
        for path in source_paths:
            # Call-site snippets are a small context aid, not a repository-wide index.
            # Stop searching once the useful examples are found; tests are searched next.
            if used >= max_chars or call_count >= MAX_CALL_SITES:
                break
            source = read_reviewed_source(repo, path, MAX_FILE_BYTES, head_commit=head_commit)
            if source is None:
                continue
            scanned_paths.add(path)
            callers, sites = _related_calls(source, candidate_names)
            caller_names.update(callers)
            for start, end, label in sites:
                if call_count >= MAX_CALL_SITES or used >= max_chars:
                    break
                before = len(blocks)
                add_block(path, start, end, label, source, 900, focus_line=(start + end) // 2)
                if len(blocks) > before:
                    call_count += 1

        # Only tests that reference the reviewed symbol or one of its direct callers are included.
        test_count = 0
        for path in test_paths:
            if used >= max_chars or test_count >= MAX_TEST_SNIPPETS:
                break
            source = read_reviewed_source(repo, path, MAX_FILE_BYTES, head_commit=head_commit)
            if source is None:
                continue
            scanned_paths.add(path)
            for start, end, label in _test_snippets(source, candidate_names | caller_names):
                if test_count >= MAX_TEST_SNIPPETS or used >= max_chars:
                    break
                before = len(blocks)
                add_block(path, start, end, label, source, 1900, focus_line=start)
                if len(blocks) > before:
                    test_count += 1

    if truncated:
        tail = "[Context policy: additional source omitted because a safety budget was reached.]"
        if used + len(tail) <= max_chars:
            blocks.append(tail)
            used += len(tail)

    text = "\n\n".join(blocks)
    return ContextBundle(text=text, chars_used=len(text), files_scanned=len(scanned_paths), truncated=truncated, blocks_added=len(blocks))
