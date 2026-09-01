# Agent instructions

Keep context and edits minimal.

- Start from the file, failing test, or traceback named by the user. Read only the directly related module and one relevant test/call site.
- Do not scan the repository, read all tests, or summarize architecture unless explicitly asked.
- Never read or modify runtime/secrets data: `.env`, `*.db`, `*.db-*`, `*.bak`, `positions.json`, `watchlist.json`, `established_coins.json`, `nohup.out`, or logs.
- Exclude generated files everywhere: `*.out`, `*.bak`, `*.backup`, `*.tmp`, `*.temp`, `*.log`, `*.pid`, `*.lock`, `*.dump`, `*.sql`, `*.sqlite*`, `*.coverage`, and coverage reports.
- Exclude generated and tool directories from context: `.venv/`, `venv/`, `env/`, `node_modules/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.git/`, and `.claude/`.
- Core code is in `src/`; dashboard UI is in `dashboard/`; tests are in `tests/`. Prefer the smallest local change and preserve existing APIs.
- For Python changes, run the narrowest relevant `pytest` test or `python3 -m py_compile` for changed modules. Do not run live/API/integration tests unless requested.
- Do not install dependencies, change trading configuration, place orders, access credentials, or make network calls unless explicitly requested.
- Respond concisely in German. State changed files and the exact validation run.