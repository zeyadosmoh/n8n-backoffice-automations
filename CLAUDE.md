# Working rules for this repo (P8, public portfolio project)

- **Plan first.** For any non-trivial change, show the plan (stack choices, ports, files) and wait for an OK.
  Ask before anything that writes to a real external system (e.g. the Shopify dev store).
- **Synthetic data, labelled everywhere.** Every product, order, buyer, supplier and price list is generated;
  no real customer data. README, emails, Excel exports and form pages say "synthetic". Emails use `example.com`.
- **Tests + CI.** Every change comes with tests. CI (GitHub Actions) must stay green on Python 3.12 and 3.14 and
  stays mock-only: the Shopify live test (`pytest -m shopify_live tests/live`) never runs in CI.
- **Secrets only in `.env`** (gitignored); `.env.example` documents every variable with empty or placeholder
  values. n8n gets secrets only as credentials via `scripts/import_workflows.sh`; workflow JSON references
  credentials by id and name. Never print, log or echo a secret or token (scan output is `file:line` only).
- **Ports bound to 127.0.0.1** (dev 5680/5438/8780/1026/8026, e2e 5681/5439/8781/1027/8027).
- **Ask before installing** anything system-wide; Python deps go into the project `.venv`.
- **Commit in small steps, never push** (and never create the GitHub repo) without the owner's explicit OK.
- **Run the secret scan before reporting ready:** `bash scripts/secret-scan.sh` (includes
  `scripts/check_workflows_clean.py`), plus `python3 scripts/build_workflows.py --check` and the unit tests.
- Workflow JSON is generated: edit `scripts/build_workflows.py` / `workflows/src/*.js`, then rebuild.
