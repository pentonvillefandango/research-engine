# Step 0: Public-repo hygiene

> Part of `docs/plans/2026-10-04-research-engine-v1.md`. Read that file first for the Global Constraints and the interface contract.

**Branch:** `main` (B5). **Feature refs:** §8 Public repository, V1-19 (workspace skeleton so CI has something to run).

**Outcome:**
- Nothing secret can be committed from here on.
- CI runs ruff, pyright, pytest and gitleaks.
- GitHub secret scanning, push protection and Dependabot are on.
- An empty-but-valid uv workspace exists.

---

### Task 0.1: Ignore rules, licence and standard project files

**Goal:** Add `.gitignore`, `.env.example` (seed), `LICENSE`, `README.md` skeleton, `CONTRIBUTING.md` and `SECURITY.md`, so the repo is safe and legible to outsiders.

**Files:**
- Create: `.gitignore`, `.env.example`, `LICENSE`, `README.md`, `CONTRIBUTING.md`, `SECURITY.md`

**Acceptance Criteria:**
- [ ] `git check-ignore .env data/x.sqlite backups/x deploys.jsonl logs/x.log .env.local` prints all six paths.
- [ ] `git check-ignore .env.example` prints nothing (exit 1), so the example stays tracked.
- [ ] `LICENSE` is MIT with `Copyright (c) 2026 pentonvillefandango`.
- [ ] `.env.example` contains only placeholder values (`change-me`, `research.localhost`).

**Verify:** `git check-ignore -v .env data/x.sqlite backups/x deploys.jsonl logs/x.log .env.local; git check-ignore .env.example; echo "example-ignored-exit=$?"` prints 6 matches, then `example-ignored-exit=1`.

**Steps:**

- [ ] **Step 1: Write the failing check.** Run the Verify command first. Expected: no matches, because `.gitignore` doesn't exist yet.

- [ ] **Step 2: Create `.gitignore`**

```gitignore
# Secrets and local config: never commit
.env
.env.*
!.env.example
*.pem
*.key
secrets/

# Runtime data
data/
backups/
logs/
*.log
deploys.jsonl
*.sqlite
*.sqlite-wal
*.sqlite-shm
*.db

# Python / uv
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.ruff_cache/
.coverage
coverage.xml
htmlcov/
dist/
build/
*.egg-info/

# Editors / OS
.idea/
.vscode/
.DS_Store

# Claude Code: keep shared settings, ignore personal ones
.claude/settings.local.json
```

- [ ] **Step 3: Create `.env.example`.** Later steps append to this file. Keep each section commented.

```dotenv
# Research Engine configuration. Copy to .env and edit. Never commit .env.
# ---- Core ----
# API key required on REST, MCP and the GUI login. Generate with: openssl rand -hex 32
API_KEY=change-me
# Public hostname served by Caddy (example deployment uses research.toolbox)
SITE_HOST=research.localhost
```

- [ ] **Step 4: Create `LICENSE`.** Use the standard MIT text with the line `Copyright (c) 2026 pentonvillefandango`.

- [ ] **Step 5: Create `README.md` (skeleton).** Include these sections:
  - *What it is*: one paragraph from REQUIREMENTS §1.
  - *Status*: "V1 in development".
  - *Quick start*: placeholder sentence "Docker Compose quick start lands in build step 8", plus `uv sync && uv run pytest` for development.
  - *Licence*.
  - *Security*: link to SECURITY.md.

  No hostnames or IPs other than `research.localhost`.

- [ ] **Step 6: Create `CONTRIBUTING.md`.** Keep it short:
  - Prerequisites: uv, Docker.
  - `uv sync`, then `uv run pre-commit install`.
  - Run `uv run ruff check`, `uv run pyright` and `uv run pytest`.
  - TDD expected; conventional commits with feature IDs.
  - Never commit `.env`.

- [ ] **Step 7: Create `SECURITY.md`.** Cover:
  - Report vulnerabilities privately via GitHub Security Advisories ("Report a vulnerability" on the repo's Security tab).
  - Don't open public issues for security problems.
  - Scope note: the service fetches untrusted web content, so SSRF, sandbox escapes and injection are in scope.

- [ ] **Step 8: Run Verify.** Expected: 6 matches and `example-ignored-exit=1`.

- [ ] **Step 9: Commit**

```bash
git add .gitignore .env.example LICENSE README.md CONTRIBUTING.md SECURITY.md
git commit -m "chore: public-repo hygiene files (§8)"
```

---

### Task 0.2: gitleaks pre-commit hook

**Goal:** Every commit is scanned by gitleaks via pre-commit, and a planted fake secret is demonstrably blocked.

**Files:**
- Create: `.pre-commit-config.yaml`, `.gitleaks.toml`

**Acceptance Criteria:**
- [ ] `uv tool run pre-commit run gitleaks --all-files` exits 0 on the clean repo.
- [ ] Staging a file containing a fake AWS-style key makes `git commit` fail with gitleaks output. The file is removed afterwards and never committed.
- [ ] `.git/hooks/pre-commit` exists after `pre-commit install`.

**Verify:** `uv tool run pre-commit run gitleaks --all-files` → `Detect hardcoded secrets...Passed`

**Steps:**

- [ ] **Step 1: Create `.pre-commit-config.yaml`**

```yaml
repos:
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.30.1
    hooks:
      - id: gitleaks
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0   # verify the latest tag with: gh release list -R pre-commit/pre-commit-hooks -L 1
    hooks:
      - id: check-added-large-files
        args: ["--maxkb=2048"]
      - id: check-merge-conflict
      - id: end-of-file-fixer
      - id: trailing-whitespace
      - id: detect-private-key
```

- [ ] **Step 2: Create `.gitleaks.toml`.** It extends the defaults and allow-lists only the example placeholders.

```toml
[extend]
useDefault = true

[allowlist]
description = "Documented placeholders only"
regexes = ['''change-me''']
paths = ['''\.env\.example$''']
```

- [ ] **Step 3: Install the hook.** Run `uv tool run pre-commit install`. Expected: `pre-commit installed at .git/hooks/pre-commit`. The first run downloads Go for the gitleaks hook, which takes about a minute.

- [ ] **Step 4: Prove it blocks a secret (red).**

```bash
printf 'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"\naws_access_key_id = AKIAZ7Q3VLRDXK4PM2TB\n' > leak-test.txt
git add leak-test.txt
git commit -m "should fail" ; echo "commit-exit=$?"
git reset -q leak-test.txt && rm leak-test.txt
```

Expected: gitleaks reports `leaks found`, then `commit-exit=1`. If the commit succeeds, **stop** and run `git reset --soft HEAD~1`, then investigate with superpowers-extended-cc:systematic-debugging.

- [ ] **Step 5: Run Verify** (green on the clean tree).

- [ ] **Step 6: Commit**

```bash
git add .pre-commit-config.yaml .gitleaks.toml
git commit -m "chore: gitleaks pre-commit hook (§8)"
```

---

### Task 0.3: uv workspace skeleton and tooling config

**Goal:** Create the uv workspace with the two member packages and the ruff, pyright and pytest configuration, so CI has real targets.

**Files:**
- Create: `pyproject.toml`, `.python-version`, `uv.lock` (generated)
- Create: `packages/research_engine_client/pyproject.toml`, `packages/research_engine_client/src/research_engine_client/__init__.py`, `packages/research_engine_client/src/research_engine_client/py.typed`
- Create: `packages/research_engine/pyproject.toml`, `packages/research_engine/src/research_engine/__init__.py`, `packages/research_engine/src/research_engine/py.typed`
- Test: `tests/test_workspace.py`, `tests/conftest.py`

**Acceptance Criteria:**
- [ ] `uv sync` succeeds and `uv.lock` is created.
- [ ] `uv run pytest` passes: `tests/test_workspace.py` (2 tests) passes and integration tests are deselected by default.
- [ ] `uv run ruff check`, `uv run ruff format --check` and `uv run pyright` exit 0.
- [ ] Both packages expose `__version__ == "0.1.0"`.

**Verify:** `uv run pytest -q && uv run ruff check && uv run ruff format --check && uv run pyright` → `2 passed`, then `All checks passed!`, then `0 errors`.

**Steps:**

- [ ] **Step 1: Write the failing test**, `tests/test_workspace.py`:

```python
import research_engine
import research_engine_client


def test_client_package_version() -> None:
    assert research_engine_client.__version__ == "0.1.0"


def test_service_package_version() -> None:
    assert research_engine.__version__ == "0.1.0"
```

`tests/conftest.py` starts empty, apart from a module docstring `"""Shared pytest fixtures."""`.

- [ ] **Step 2: Run it.** `uv run pytest` fails because there's no pyproject yet. Expected: an error.

- [ ] **Step 3: Create the root `pyproject.toml`**

```toml
[project]
name = "research-engine-workspace"
version = "0.1.0"
requires-python = ">=3.12"
description = "Workspace root for the Research Engine"
license = "MIT"

[tool.uv]
package = false

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
research-engine = { workspace = true }
research-engine-client = { workspace = true }

[dependency-groups]
dev = [
  "research-engine",
  "research-engine-client",
  "pytest>=9.1.1",
  "pytest-asyncio>=1.4.0",
  "pytest-cov>=7.1.0",
  "respx>=0.23.1",
  "ruff>=0.16.10",
  "pyright>=1.1.414",
  "jsonschema>=4.26.0",
  "pre-commit>=4.6.2",
]

[tool.pytest]
minversion = "9.0"
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
markers = ["integration: needs the live compose stack (run with -m integration)"]
addopts = ["-m", "not integration", "--strict-markers", "-ra"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "ASYNC", "S", "SIM", "RUF"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "S105", "S106"]
"scripts/**" = ["S603", "S607"]

[tool.pyright]
pythonVersion = "3.12"
typeCheckingMode = "standard"
include = ["packages", "tests", "scripts", "examples"]
venvPath = "."
venv = ".venv"
strict = ["packages/research_engine_client/src"]
```

Write `.python-version` containing `3.13`.

- [ ] **Step 4: Create the member packages.** `packages/research_engine_client/pyproject.toml`:

```toml
[project]
name = "research-engine-client"
version = "0.1.0"
description = "Shared Pydantic models and async client for the Research Engine"
requires-python = ">=3.12"
license = "MIT"
dependencies = ["pydantic>=2.13.5", "httpx>=0.28.1"]

[build-system]
requires = ["uv_build>=0.12,<0.13"]
build-backend = "uv_build"
```

`packages/research_engine/pyproject.toml`:

```toml
[project]
name = "research-engine"
version = "0.1.0"
description = "Self-hosted web search and scraping service for research agents"
requires-python = ">=3.12"
license = "MIT"
dependencies = ["research-engine-client"]

[project.scripts]
research-engine = "research_engine.cli:main"

[tool.uv.sources]
research-engine-client = { workspace = true }

[build-system]
requires = ["uv_build>=0.12,<0.13"]
build-backend = "uv_build"
```

Each package's `__init__.py` contains `"""<one-line description>."""` followed by `__version__ = "0.1.0"`. Add an empty `py.typed` to each.

`research_engine/cli.py` is added in step 1. Until then, leave `[project.scripts]` out and add it in Task 1.4.

- [ ] **Step 5: Sync and run.** `uv sync` then `uv run pytest -q`. Expected: `2 passed`.

- [ ] **Step 6: Lint and type-check.** `uv run ruff check && uv run ruff format --check && uv run pyright`. Expected: clean. Fix anything reported; don't silence rules.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .python-version uv.lock packages tests
git commit -m "chore: uv workspace skeleton and tooling config (V1-19)"
```

---

### Task 0.4: CI workflow, Dependabot and Claude Code guardrails

**Goal:** Add a GitHub Actions CI workflow (ruff, pyright, pytest, gitleaks), the Dependabot config, and a project `.claude/settings.json` that makes live-stack commands prompt and denies destructive ones.

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/dependabot.yml`, `.claude/settings.json`

**Acceptance Criteria:**
- [ ] `ci.yml` has two jobs: `lint-test` (uv sync --locked, ruff check, ruff format --check, pyright, pytest with coverage) and `gitleaks` (fetch-depth 0, `gitleaks/gitleaks-action@v3`).
- [ ] Every action is pinned to a major or exact version, and uv is pinned to `0.12.23`.
- [ ] `dependabot.yml` covers the `uv`, `github-actions` and `docker` ecosystems weekly. The docker entries for `/` (Dockerfile, compose) and `/deploy/caddy` are added in step 8 when those files exist.
- [ ] `.claude/settings.json` sets the following:
  - It **asks** before `make deploy`, `make rollback`, `make restore` and `make bootstrap`.
  - It **denies** `docker compose down -v`, `docker volume rm`, `docker system prune`, `git push --force` and `git push -f`.
  - It validates as JSON.
- [ ] `actionlint` is clean, if it's available. Otherwise `python -c "import yaml,sys;yaml.safe_load(open('.github/workflows/ci.yml'))"` must exit 0.

**Verify:** `python3 -c "import json,yaml;yaml.safe_load(open('.github/workflows/ci.yml'));yaml.safe_load(open('.github/dependabot.yml'));json.load(open('.claude/settings.json'));print('ok')"` → `ok`

**Steps:**

- [ ] **Step 1: Create `.github/workflows/ci.yml`**

```yaml
name: CI
on:
  push:
  pull_request:
permissions:
  contents: read
jobs:
  lint-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v7
        with:
          version: "0.12.23"
          enable-cache: true
      - run: uv sync --locked
      - run: uv run ruff check
      - run: uv run ruff format --check
      - run: uv run pyright
      - run: uv run pytest --cov=research_engine --cov=research_engine_client --cov-report=term-missing
  gitleaks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - uses: gitleaks/gitleaks-action@v3
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

Before committing, check the current major versions with `gh release list -R actions/checkout -L 1` and `gh release list -R astral-sh/setup-uv -L 1`, and adjust the `@vN` pins to match.

- [ ] **Step 2: Create `.github/dependabot.yml`**

```yaml
version: 2
updates:
  - package-ecosystem: "uv"
    directory: "/"
    schedule: { interval: "weekly" }
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule: { interval: "weekly" }
```

- [ ] **Step 3: Create `.claude/settings.json`**

```json
{
  "permissions": {
    "ask": [
      "Bash(make deploy:*)",
      "Bash(make rollback:*)",
      "Bash(make restore:*)",
      "Bash(make bootstrap:*)",
      "Bash(./ops/deploy.sh:*)",
      "Bash(./ops/rollback.sh:*)",
      "Bash(./ops/restore.sh:*)",
      "Bash(./ops/bootstrap.sh:*)"
    ],
    "deny": [
      "Bash(docker compose down -v:*)",
      "Bash(docker compose down --volumes:*)",
      "Bash(docker volume rm:*)",
      "Bash(docker system prune:*)",
      "Bash(git push --force:*)",
      "Bash(git push -f:*)"
    ]
  }
}
```

- [ ] **Step 4: Run Verify.** Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
git add .github .claude/settings.json
git commit -m "ci: lint/type/test/gitleaks workflow, dependabot, Claude guardrails (§8, §10)"
```

---

### Task 0.5: Enable GitHub protections and push (after owner approval)

**Goal:** Turn on secret scanning, push protection and Dependabot alerts and security updates for the public repo, push `main`, and confirm CI goes green.

**Files:** none (repository settings and push only)

**Acceptance Criteria:**
- [ ] `gh api repos/{owner}/{repo} --jq .security_and_analysis` shows `secret_scanning.status=enabled` and `secret_scanning_push_protection.status=enabled`.
- [ ] `gh api repos/{owner}/{repo}/vulnerability-alerts -i` returns `HTTP/2 204`.
- [ ] `gh run list -L 1 --branch main` shows the CI run concluded `success`.

**Verify:** `gh api repos/pentonvillefandango/research-engine --jq '.security_and_analysis | {ss: .secret_scanning.status, pp: .secret_scanning_push_protection.status}'` → `{"pp":"enabled","ss":"enabled"}`

**Steps:**

- [ ] **Step 1: Ask the owner to approve step 0 before pushing.** This is the step-0 pause.

- [ ] **Step 2: Enable the protections**

```bash
gh api -X PATCH repos/pentonvillefandango/research-engine \
  -F 'security_and_analysis[secret_scanning][status]=enabled' \
  -F 'security_and_analysis[secret_scanning_push_protection][status]=enabled'
gh api -X PUT repos/pentonvillefandango/research-engine/vulnerability-alerts
gh api -X PUT repos/pentonvillefandango/research-engine/automated-security-fixes
```

- [ ] **Step 3: Push.** Run `git push origin main`.

- [ ] **Step 4: Check CI.** Run `gh run watch --exit-status $(gh run list -L 1 --branch main --json databaseId --jq '.[0].databaseId')`. Expected: exit 0. If it fails, use superpowers-extended-cc:systematic-debugging; don't patch blindly.

- [ ] **Step 5: Run Verify** and paste the outputs in the step report.

- [ ] **Step 6: Create the `v1` branch for steps 1–9.** Run `git switch -c v1 && git push -u origin v1`.
