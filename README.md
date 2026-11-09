# Code Review Agent

A command-line tool that asks Gemini to review the Python changes on a Git branch, then checks every finding against the actual source before reporting it.

Built with Python, the Gemini API and pydantic. It doesn't take Gemini's findings at face value. A finding has to point at a line the change added, quote code that is really there, and describe a specific case with what should happen and what happens instead. Anything that doesn't is rejected, and the report says why. The tests run offline, and ten labeled example diffs are included for checking whether a change to the tool made it better or worse.

This is an unfinished learning project. Larger evaluations are limited by the quota on Gemini's free tier, so it is a prototype rather than a finished tool.

## What it does

It reviews committed Python changes between a base Git ref and `HEAD`. Gemini suggests possible defects, local checks confirm the line numbers and the quoted code, and a limited look at related code can follow before the report is written. The aim is findings you can read and check, not proof that a change is bug-free.

## How a review works

1. Read the Python diff for `base...HEAD`.
2. Ask Gemini for concrete potential defects.
3. Check that each finding points to a changed line and that its quoted evidence matches the source.
4. For the findings that pass, gather context: the diff hunk, the enclosing function or class, imports, up to three direct call sites and up to two matching tests.
5. Make at most one batched context-check request. It uses the same model, so it isn't independent proof. If the request fails, the first-pass findings are kept and marked unverified.
6. Write Markdown and JSON reports.

Context is capped at 12,000 characters by default and scans at most 250 tracked Python files. Set `REVIEW_MAX_CONTEXT_CHARS` in `.env` to a value between 1,000 and 30,000 to change that. The Git diff is capped at 40,000 characters by default; set `REVIEW_MAX_DIFF_CHARS` to a value between 5,000 and 100,000 to change it. A diff over the limit is stopped while Git's output is being read and rejected, not split into chunks. Related code can still be missed when it is far from the change or the context budget runs out.

## Setup

You need Python 3.11+, Git and a Gemini API key.

In Windows PowerShell, from this project's folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
notepad .env
```

Set `GEMINI_API_KEY` in `.env` and pick a `GEMINI_MODEL` your account can use. You can create a key at <https://aistudio.google.com/apikey>. Keep `.env` out of Git.

The reviewed diff and the selected source context are sent to Gemini, so only use the tool on code you are allowed to share with the provider. Free-tier quotas can limit repeated reviews and evaluations.

## Running a review

Commit the changes you want reviewed first, because uncommitted edits aren't included. This version reviews findings on added lines and skips changes that only delete code.

```powershell
.\.venv\Scripts\python.exe -m review_agent.cli review --repo "C:\path\to\target-repo" --base main
```

Reports go to `.code-review-output\review.md` and `.code-review-output\review.json` in the target repository. Use `--output` to choose another folder.

Optional flags:

- `--run-tests` asks for confirmation, then runs the target repository's pytest suite once. This executes that repository's code, so only use it on repositories you trust.
- `--draft-repros` asks Gemini to draft focused pytest tests for suitable findings. The drafts are saved under `.code-review-output\reproductions\` and are **not run**. Read them before running them, because they aren't sandboxed.

## Evaluation

Ten small, labeled Python diffs are included. Run some of them with:

```powershell
.\.venv\Scripts\python.exe -m review_agent.cli evaluate --limit 4
```

Add `--no-context` to use one model request per case and skip the context pass. Without it, each case uses one first request and can use one more batched context request. Automatic SDK retries are turned off so they don't quietly use up more quota. If a request fails, the cases that finished are kept and the report is marked incomplete. Reports go to `.code-review-output\evaluation.md` and `.code-review-output\evaluation.json`.

The examples are a small synthetic set with a simple label matcher. They are useful for comparing changes to the tool, not for claiming it is accurate in general, so read the findings and labels yourself.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The tests run offline and don't use any Gemini quota.

## Project layout

| Path | Contents |
| --- | --- |
| `review_agent/git_diff.py` | Reads and parses a size-bounded Git diff |
| `review_agent/source.py` | Reads committed source safely |
| `review_agent/gemini_review.py` | The Gemini calls |
| `review_agent/evidence.py` | Checks findings and maps severity to review order |
| `review_agent/context.py` | Gets limited related code |
| `review_agent/evaluation.py` | Scores the labeled examples (`review_agent/data/evaluation_cases.json`) |
| `review_agent/report.py` | Writes the Markdown and JSON reports |
| `review_agent/checks.py`, `cli.py`, `config.py`, `models.py` | The optional test run, command line, settings and data models |
| `examples/` | A small buggy file to talk through a finding |
| `tests/` | Offline pytest tests |

## Limitations

- P1, P2 and P3 come from the model's own severity estimate. They aren't the probability that a finding is right. Severity and evidence status are reported separately.
- Context lookup understands Python syntax, but it only scans a limited number of tracked Python files and picks up at most a few call examples and tests. It can't reliably find every dynamic call, external service or cross-language dependency. The same model does the context check, so it isn't an independent judge.
- Large changes aren't split up. A diff over `REVIEW_MAX_DIFF_CHARS` is rejected rather than sent to Gemini, so narrow the review or raise the limit carefully.
- Repository tests run project code. Generated reproduction tests are drafts and are never run automatically.
- A finding is something to look at, not a verdict. An empty report doesn't mean the code has no bugs.

## License

MIT. See [LICENSE](LICENSE).
