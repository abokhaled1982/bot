# Repository instructions

Keep context and edits minimal.

- Begin with the file, test, or traceback named by the user. Read only its direct implementation path and one nearby test or call site.
- Do not perform repository-wide scans or read all tests unless explicitly requested.
- Exclude runtime/secrets data from context and edits: `.env`, `*.db`, `*.db-*`, `*.bak`, `positions.json`, `watchlist.json`, `established_coins.json`, `nohup.out`, and logs.
- Exclude generated files everywhere: `*.out`, `*.bak`, `*.backup`, `*.tmp`, `*.temp`, `*.log`, `*.pid`, `*.lock`, `*.dump`, `*.sql`, `*.sqlite*`, `*.coverage`, `*.ini`, `*.toml`, `*.json`, and coverage reports.
- Never search or open generated/tool directories: `.venv/`, `venv/`, `env/`, `node_modules/`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.git/`, and `.claude/`.
- Code ownership: `src/` bot/execution/adapters; `dashboard/` UI; `tests/` unit tests. Keep public APIs stable and make the smallest focused change.
- Validate Python edits with the narrowest relevant `pytest` test or `python3 -m py_compile`. Never run live/API/integration tests without an explicit request.
- Never install packages, call external services, access credentials, change trading configuration, or place orders unless explicitly requested.
- Answer in concise German and report changed files plus the validation command.