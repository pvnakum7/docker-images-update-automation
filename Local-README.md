# Run it on your laptop (step by step)

This guide gets the Docker Image Update automation running on **your own computer** (Windows first; macOS/Linux notes at the end).
Nothing is installed globally: everything lives in a `.venv` folder inside the project.

By default a local run is **safe**: it only *reads* your repositories and writes reports. It does **not** change any repository,
create pull requests or send e-mail, until you switch those on yourself (section 7).

---

## 1. What you need

| Requirement | Needed for | Notes |
|---|---|---|
| **Windows 10/11** (or macOS / Linux) | everything | PowerShell 5.1 or 7 |
| **Python 3.15** (3.11 or newer also works) | running the tool | `winget install Python.Python.3.15`, or python.org. Tick *Add python.exe to PATH* / install the *py launcher* |
| **Git** | cloning the repositories | `winget install Git.Git` - check with `git --version` |
| **Internet access** to `github.com` and to the image registries (Docker Hub, ghcr.io, mcr.microsoft.com, quay.io ...) | cloning + looking up newer image versions | corporate proxy: set `HTTPS_PROXY` (section 9) |
| **Excel file** `config/repositories.xlsx` | the list of repositories | edit with Excel or LibreOffice |
| **GitHub token(s)** | private repositories, branches, pull requests | see section 4. Public repositories work without one |
| Docker Desktop *(optional)* | only the optional `docker build` validation, and running the workflow locally with `act` | without Docker, Dockerfile checks fall back to a static syntax check |
| SMTP account *(optional)* | only when you want the e-mail | e.g. Gmail needs an *app password* |

Disk/RAM: small. Repositories are shallow-cloned one by one into `repos/<repo-name>/` and emptied at the end.

Check your tools in PowerShell:
```powershell
py -3.15 --version      # or: python --version     -> Python 3.15.x (3.11+ is fine)
git --version
```

---

## 2. Get the project

The project folder is `C:\Users\Imagine\Documents\Automation\Docker-Image-automation`.
```powershell
cd C:\Users\Imagine\Documents\Automation\Docker-Image-automation
```

---

## 3. First run (creates everything for you)

If PowerShell blocks scripts, allow them for this window only:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```
Then:
```powershell
.\scripts\run_local.ps1
```
The script creates `.venv`, installs `requirements.txt`, loads your `.env` file (section 4) and runs the tool in **safe mode**
(report-only, test mode, no e-mail). The first run takes a few minutes because of the downloads.

Optional - prove the installation works before using real data (runs the self-tests on temporary local git repositories, no network/token needed):
```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

---

## 4. Configure

### 4.1 The repository list - `config/repositories.xlsx`
One row per repository. Header row exactly:

| Repo_Name | Repo_Link | POC | Email | CC_Email | Token_Name |
|---|---|---|---|---|---|
| inventory-system | https://github.com/VikramBabariya/inventory-system | Vikram | owner1@company.com, owner2@company.com | dev1@company.com; dev2@company.com | GIT_TOKEN |

* `Email` = application owners (become **TO**), `CC_Email` = developers (become **CC**). Several addresses can be separated by comma, semicolon or space. Duplicates are removed everywhere; an address in both columns stays in TO.
* `Token_Name` (optional) = the **name** of the token to use for that repository: `GIT_TOKEN`, `GIT_TOKEN2`, `GIT_TOKEN3` ... Empty = every configured token is tried until one works.
* If your list is a Google Sheet: *File -> Download -> Microsoft Excel (.xlsx)* and save it over `config/repositories.xlsx`.
* Every Dockerfile in the repository is found automatically (`Dockerfile`, `Dockerfile.dev`, `Dockerfile.stage`, `*.Dockerfile` ... in the root and up to 4 folder levels).
* The branch analysed is the first that exists: `dev`, `development`, `develop`, `main` (else the default branch).

### 4.2 Secrets - the `.env` file
```powershell
copy .env.example .env
notepad .env
```
Fill in only what you need (the file is git-ignored; never commit or share it):
```ini
GIT_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx        # token 1
GIT_TOKEN2=ghp_yyyyyyyyyyyyyyyyyyyy       # token 2 (optional) ... GIT_TOKEN3, GIT_TOKEN4 ...
DIU_TEST_MODE=true                        # true = never send e-mail, never create pull requests
```
**Creating a GitHub token**: GitHub -> Settings -> Developer settings -> Personal access tokens -> *Fine-grained tokens* ->
repository access = the repositories in your list; permissions: **Contents: Read** (reports only), plus **Contents: Read and write** and
**Pull requests: Read and write** if you later want branches and pull requests. For organisation repositories using SSO, authorize the token for the organisation.

E-mail settings (only for section 7): `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `EMAIL_FROM`.
Recipients are **not** configured here - they come from the Excel file.

### 4.3 Behaviour - `config/settings.yaml`
Every key is commented. The defaults are safe. Useful overrides can also be given as environment variables (below).

---

## 5. Run

```powershell
.\scripts\run_local.ps1                                   # all repositories in the Excel file, safe mode

$env:DIU_REPOSITORIES = "inventory-system"                # only these Repo_Name values (comma separated)
$env:DIU_STRATEGY = "both"                                # same_major (default) | new_major | both
.\scripts\run_local.ps1

.\scripts\run_local.ps1 --repos-file C:\path\other.xlsx   # another Excel file
```
Environment variables you set with `$env:NAME = "value"` last only for that PowerShell window.

---

## 6. Where to look afterwards

| What | Where |
|---|---|
| **One report per repository** (all its Dockerfiles in one table) | `dockerfile-reports\<repo-name>-<DD-Mon-YYYY>.txt` |
| Consolidated CSV for all repositories | `reports\Dockerfile-update-report-<date>.csv` |
| Overall summary (also printed in the console) | `reports\overall-summary.txt` |
| ZIP of all repository reports | `reports\Docker-repository-reports-<date>.zip` |
| E-mail recipient summary / e-mail preview (test mode) | `reports\email-recipient-summary.txt`, `reports\email-preview-NOT-SENT.eml` |
| Logs (one per repository) | `reports\logs\` |

Example reports with realistic content: `docs\dockerfile-reports\`.
Status meanings: `UPDATE_AVAILABLE` (newer stable version, nothing changed), `UPDATED`, `UP_TO_DATE`, `SKIPPED` (with reason),
`MAJOR_UPGRADE_RECOMMENDED` / `REQUIRES_VALIDATION` (a new major exists, never applied automatically).
The code cloned into `repos\<repo-name>\` is deleted at the end of the run; the reports stay.

---

## 7. Going further (only when you are ready)

1. **Edit Dockerfiles and push a branch** - in `config\settings.yaml`: `upgrade.report_only: false` and `git.commit_changes: true`.
   A new branch `docker-image-update/<timestamp>` is pushed from the analysed branch; your original branches are never touched.
2. **Open pull requests** - `git.create_pull_requests: true` and set `DIU_TEST_MODE=false` in `.env` (test mode blocks PRs).
3. **Send the e-mail** - fill the SMTP values in `.env`, set `DIU_TEST_MODE=false` and `DIU_EMAIL_ENABLED=true`.
   Exactly **one** consolidated e-mail is sent at the end (summary + CSV + ZIP of the reports) to the TO/CC addresses taken from the Excel file.
4. Start small: use `DIU_REPOSITORIES=<one repo>` first and review the pull request before running on everything.

---

## 8. Run the GitHub Actions workflow on your laptop (optional)

Uses [`act`](https://github.com/nektos/act) and **Docker Desktop** (must be running).
```powershell
winget install nektos.act
copy .act-secrets.example .act-secrets        # fill GIT_TOKEN ... (git-ignored)
act workflow_dispatch -W .github/workflows/docker-image-update.yml -e tests/act/workflow_dispatch.json --secret-file .act-secrets --artifact-server-path .\.act-artifacts
```
The sample event runs a dry run with test mode and no e-mail. The first run downloads a large Docker image. Some marketplace steps
(buildx, artifact upload) can misbehave under `act` - if so, the normal local run in section 5 executes the same Python step.
*This `act` procedure is documented but has not been verified on a real machine.*

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `running scripts is disabled on this system` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, run the script again |
| `py` / `python` not found | install Python (section 1) and open a **new** PowerShell window |
| `git` not found | install Git, open a new window |
| `token GIT_TOKENn is not configured` | the Excel `Token_Name` names a token that is missing/empty in `.env` |
| `authentication failed` / `repository not found` | token expired, no access to that repo, or not SSO-authorized; try another `Token_Name` |
| Everything `SKIPPED` with a registry error | no internet, a proxy, or Docker Hub rate limit: set `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` in `.env`; behind a proxy set `$env:HTTPS_PROXY="http://proxy:port"` |
| `no valid TO recipient` | no valid address in the `Email` column; reports are still produced |
| No e-mail arrived | test mode is on (`DIU_TEST_MODE=true`) or `DIU_EMAIL_ENABLED=false`; see the `Email:` line at the end of the run |
| `PermissionError` while cleaning `repos\` | close programs that have files open there (VS Code, Explorer) - cleanup only warns, the run still succeeds |
| Reports from an earlier run are still in `dockerfile-reports\` | delete the old files; each run only adds its own |
| Want a fresh install | delete the `.venv` folder and run `.\scripts\run_local.ps1` again |

More detail: `README.md` (full documentation), `docs/QUICKSTART.md` (10-minute version).

---

## macOS / Linux
```bash
cd Docker-Image-automation
./scripts/run_local.sh          # same steps; needs python3 (3.11+; 3.15 preferred) and git
```
Environment variables: `export DIU_REPOSITORIES=inventory-system`.
