"""Read bounded Python diffs and track the new line numbers in changed lines."""

from __future__ import annotations

import codecs
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
DIFF_TIMEOUT_SECONDS = 30
READ_CHUNK_BYTES = 4096


@dataclass
class ChangedFile:
    path: str
    diff: str
    changed_lines: set[int] = field(default_factory=set)
    deleted: bool = False


def _run_bounded_diff(repo: Path, command: list[str], max_chars: int) -> str:
    """Read Git output in small chunks and stop if the character limit is exceeded."""
    output: list[str] = []
    too_large = threading.Event()
    read_errors: list[OSError] = []

    try:
        with tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
            )
            if process.stdout is None:  # Defensive: stdout is configured as a pipe above.
                process.kill()
                process.wait()
                raise ValueError("Could not read Git diff output.")

            # Drain stdout on a reader thread so the main thread can enforce a timeout.
            def collect_output() -> None:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                char_count = 0
                try:
                    while True:
                        chunk = process.stdout.read(READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        text = decoder.decode(chunk)
                        char_count += len(text)
                        if char_count > max_chars:
                            too_large.set()
                            process.kill()
                            return
                        output.append(text)

                    final_text = decoder.decode(b"", final=True)
                    char_count += len(final_text)
                    if char_count > max_chars:
                        too_large.set()
                        process.kill()
                        return
                    if final_text:
                        output.append(final_text)
                except OSError as exc:
                    read_errors.append(exc)
                    process.kill()
                finally:
                    process.stdout.close()

            reader = threading.Thread(target=collect_output, daemon=True)
            reader.start()
            try:
                return_code = process.wait(timeout=DIFF_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                reader.join()
                raise ValueError("Git diff timed out after 30 seconds.") from exc
            reader.join()

            if too_large.is_set():
                raise ValueError(
                    f"Python diff exceeds the {max_chars:,}-character limit. "
                    "Review a smaller set of commits or raise REVIEW_MAX_DIFF_CHARS cautiously."
                )
            if read_errors:
                raise ValueError("Could not read Git diff output.") from read_errors[0]
            if return_code != 0:
                stderr_file.seek(0, os.SEEK_END)
                size = stderr_file.tell()
                stderr_file.seek(max(0, size - 1000))
                reason = stderr_file.read().decode("utf-8", errors="replace").strip()
                reason = reason[-500:] if reason else "git diff failed"
                raise ValueError(f"Could not compare against the requested base ref: {reason}")

            return "".join(output)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Git diff timed out or could not be started.") from exc


def get_python_diff(
    repo: Path,
    base_ref: str,
    max_chars: int,
) -> tuple[str, list[ChangedFile]]:
    if not isinstance(base_ref, str) or not base_ref.strip() or base_ref.startswith("-") or "\x00" in base_ref:
        raise ValueError("Base ref is empty or invalid.")
    if not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("Diff character limit must be a positive integer.")

    try:
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Could not inspect the target Git repository.") from exc
    if check.returncode != 0 or check.stdout.strip() != "true":
        raise ValueError(f"{repo} is not a Git working tree.")

    command = [
        "git", "diff", "--no-ext-diff", "--no-textconv", "--unified=3",
        f"{base_ref}...HEAD", "--", "*.py",
    ]
    diff_text = _run_bounded_diff(repo, command, max_chars)
    return diff_text, parse_diff(diff_text)


def parse_diff(diff_text: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    current: ChangedFile | None = None
    current_new_line: int | None = None
    current_diff: list[str] = []

    def finish() -> None:
        nonlocal current, current_diff
        if current is not None:
            current.diff = "\n".join(current_diff)
            files.append(current)
        current = None
        current_diff = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            finish()
            current_new_line = None
            continue
        if line.startswith("+++ b/"):
            path = line[6:]
            if path == "/dev/null":
                continue
            current = ChangedFile(path=path, diff="")
            current_diff = [line]
            continue
        if current is None:
            continue
        current_diff.append(line)
        if line.startswith("deleted file mode "):
            current.deleted = True
        match = HUNK.match(line)
        if match:
            current_new_line = int(match.group(1))
            continue
        if current_new_line is None or line.startswith("\\ No newline"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current.changed_lines.add(current_new_line)
            current_new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            # Deleted lines do not have a location in the new file.
            continue
        elif line.startswith(" "):
            current_new_line += 1
    finish()
    return [item for item in files if not item.deleted and item.changed_lines]
