# Quick start (10 minutes)

1. `py -3 -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -r requirements.txt ; python -m pytest -q`  -> all tests pass
2. Edit `config/repositories.xlsx` (public repos need no token).
3. `.\scripts\run_local.ps1`  -> open `dockerfile-reports\*.txt` and `reports\Dockerfile-update-report-*.csv`
4. Private repos: `copy .env.example .env`, put a fine-grained PAT in `GIT_TOKEN` (more tokens: `GIT_TOKEN2`..., named per repo in the Excel column `Token_Name`), re-run.
5. GitHub Actions: push to a private repo, add secrets (`GH_PAT`, `SMTP_*`, `EMAIL_*`), run *Docker Image Update* with `dry_run=true`.
Step-by-step local guide: ../LOCAL-README.md - full guide: ../README.md
