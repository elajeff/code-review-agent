# Code Review Agent

> **Status: Unfinished learning project.** The idea is to make debugging easier by identifying potential defects in changed Python code and showing the source evidence behind each finding. Development and larger-scale evaluation are limited by the quota available on Gemini's free API tier, so this remains a prototype rather than a finished tool.

## What it does

This command-line tool reviews committed Python changes between a base Git ref and `HEAD`. Gemini suggests possible defects; local checks validate source locations and quoted evidence; a bounded context pass can then inspect related code before a report is generated.

The aim is to surface useful, explainable findings—not to prove every claim or guarantee that a change is bug-free.

## Review process

1. Read the Python diff for `base...HEAD`.
2. Ask Gemini for concrete potential defects.
3. Check that each finding points to a changed line and its quoted evidence matches the source.
4. For candidates that pass, gather relevant context: the diff hunk, enclosing function or class, imports, up to three direct call sites, and up to two matching tests.
5. Make at most one batched context-check request. It uses the same model, so it is not independent proof. If the request fails, first-pass candidates are retained as unverified.
6. Write Markdown and JSON reports.

Context is capped at 12,000 characters by default and scans at most 250 tracked Python files. Set `REVIEW_MAX_CONTEXT_CHARS` in `.env` to a value between 1,000 and 30,000. The Git diff is capped at 40,000 characters by default; set `REVIEW_MAX_DIFF_CHARS` to a value between 5,000 and 100,000 to change that limit. An oversized diff is stopped while Git output is being read and rejected rather than split into chunks. Relevant code can still be missed when dependencies are distant or the context budget is reached.

## Setup

Requirements: Python 3.11+, Git, and a Gemini API key.

In Windows PowerShell, from this project directory:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
notepad .env
```

Set `GEMINI_API_KEY` in `.env` and choose a `GEMINI_MODEL` available to your account. Create a key at <https://aistudio.google.com/apikey>. Keep `.env` out of Git. The reviewed diff and selected source context are sent to Gemini; only use the tool on code you are allowed to share with the provider. Free-tier quotas may limit repeated reviews and evaluations.

## Run a review

Commit the changes you want reviewed first; uncommitted edits are not included.

```powershell
.\.venv\Scripts\python.exe -m review_agent.cli review --repo "C:\path\to\target-repo" --base main
```

Reports are written to `.code-review-output\review.md` and `.code-review-output\review.json` in the target repository by default. Use `--output` to choose another directory.

Optional flags:

- `--run-tests` asks for confirmation before running the target repository's pytest suite once. This executes that repository's code; only use it on repositories you trust.
- `--draft-repros` asks Gemini to draft focused pytest tests for suitable findings. Drafts are saved under `.code-review-output\reproductions\` and are **not executed**. Inspect them before running; they are not sandboxed.

## Evaluation

Ten small, labeled Python diffs are included. Run a subset with:

```powershell
.\.venv\Scripts\python.exe -m review_agent.cli evaluate --limit 4
```

Add `--no-context` to use one model request per case and skip the optional context pass. Otherwise, each case uses one initial request and may use one additional batched context request. Automatic SDK retries are disabled to avoid silently consuming more quota. If a provider request fails, completed cases are preserved and the report is marked incomplete. Reports are written to `.code-review-output\evaluation.md` and `.code-review-output\evaluation.json` by default.

The examples are a small synthetic development benchmark with a simple label matcher. Results are useful for comparing changes to the tool, not for claiming general accuracy; inspect findings and labels manually.

## Design and limitations

- `git_diff.py` reads and parses a size-bounded Git diff; `source.py` reads committed source safely; `gemini_review.py` handles model calls; `evidence.py` checks findings and maps severity to review order; `context.py` retrieves limited related code; `evaluation.py` scores labeled examples; `report.py` writes the results.
- P1/P2/P3 is a simple mapping from model-estimated severity, not a probability of correctness. Severity and evidence status are reported separately.
- Context retrieval is bounded and syntax-aware, but it scans only a limited number of tracked Python files and selects up to a few direct-call examples and tests. It cannot reliably capture every dynamic call, external service or cross-language dependency. The same model performs the context check; it is not an independent judge.
- Diffs above `REVIEW_MAX_DIFF_CHARS` are rejected while Git output is being read, rather than being sent to Gemini. This version does not automatically split large changes into chunks; narrow the review or raise the limit cautiously.
- Repository tests execute project code. Generated reproduction tests are drafts and are never run automatically.
- A finding is a candidate for human review. An empty report does not mean the code is bug-free.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The test suite runs offline and does not consume Gemini quota. GitHub Actions runs the same suite on Python 3.11 and 3.13.
