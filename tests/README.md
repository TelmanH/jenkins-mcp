# Local testing (developers)

Everything in `tests/` is for development and testing. None of it is installed or needed by people who
only use the server at work (they follow [setup/windows/README.md](../setup/windows/README.md)).

You can develop and test on either platform:

| | macOS + Claude Code | Windows + VS Code |
|---|---|---|
| Setup | `tests/macos/setup.sh` | `setup\windows\setup.ps1 -Dev` |
| Tokens | `config/.env` | VS Code token prompts (`.vscode/mcp.json`, generated) or `-TokensInEnvFile` |
| Offline tests | `.venv/bin/pytest -q` | `.venv\Scripts\pytest -q` or the VS Code **Testing** panel |
| Live test (Docker) | `tests/live/run_live.sh` | `tests\live\run_live.ps1` |
| Try the tools by hand | Claude Code | Copilot Chat (Agent mode) |

| Path | What |
|---|---|
| `test_*.py`, `conftest.py`, `fake_jenkins.py` | Offline tests (pytest) against a fake Jenkins - no network, ~2 s |
| `fixtures/` | Sample log and example knowledge base used by the tests (not your real `knowledge/`) |
| `live/` | A real Jenkins in Docker with sample jobs, and a live check of every tool |
| `macos/setup.sh` | macOS setup (the Windows one is `setup\windows\setup.ps1 -Dev`) |

The offline tests create their own temporary config and knowledge base: your `config/` and `knowledge/`
are never read or changed.

---

## macOS + Claude Code

**Needs:** Python 3.10+ (`python3 --version`), [Claude Code](https://claude.com/claude-code) (`claude --version`),
Docker Desktop for the live test.

### 1. Set up (once)

```bash
cd jenkins-mcp
tests/macos/setup.sh                            # registers 'jenkins' in Claude Code for this folder
tests/macos/setup.sh "$HOME/some/other/folder"  # ... or for the folder where you start claude
```

Check: `claude mcp list` shows `jenkins: ... ✔ Connected`.

### 2. Offline tests

```bash
.venv/bin/pytest -q                            # all tests
.venv/bin/pytest -q tests/test_log_tools.py    # one file
.venv/bin/pytest -q -k search                  # by name
.venv/bin/pytest -q --cov=jenkins_mcp          # with line coverage
.venv/bin/mypy src                             # type check
```

### 3. Live test: real Jenkins in Docker

```bash
open -a Docker                 # start Docker Desktop, wait until it runs
tests/live/run_live.sh         # build + start Jenkins, create sample jobs, check every tool
```

### 4. Try it by hand in Claude Code

Point your config at the Docker Jenkins (see [Use the test Jenkins](#use-the-test-jenkins-by-hand) below), start
`claude` in the registered folder (`/mcp` shows the server) and ask the questions listed there.
Prompts: `/mcp__jenkins__analyse_build`, `/mcp__jenkins__document_session`.

---

## Windows + VS Code

**Needs:** Python 3.10+, VS Code with the **Python** extension (recommended automatically when you open the
folder) and GitHub Copilot, Docker Desktop for the live test.

### 1. Set up (once)

In PowerShell, in the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File setup\windows\setup.ps1 -Dev
# with a specific Python / proxy:
powershell -ExecutionPolicy Bypass -File setup\windows\setup.ps1 -Dev -Python "C:\Program Files\Python311\python.exe" -Proxy "http://proxy.example.com:80"
```

`-Dev` installs the server in editable mode (your code changes apply immediately) together with the
test tools, and runs the offline tests at the end. It is the same script the company users run, plus
that switch.

### 2. Offline tests

**In VS Code:** open the project folder, click the **Testing** icon (beaker) in the left bar. All tests are
listed per file; run or debug any of them with the buttons next to it. `.vscode/settings.json` enables
pytest; the Python extension picks up `.venv` automatically (otherwise: `Ctrl+Shift+P` ->
*Python: Select Interpreter* -> `.venv`).

**In the terminal:**

```powershell
.venv\Scripts\pytest -q                          # all tests
.venv\Scripts\pytest -q tests\test_log_tools.py  # one file
.venv\Scripts\pytest -q -k search                # by name
.venv\Scripts\pytest -q --cov=jenkins_mcp        # with line coverage
.venv\Scripts\mypy src                           # type check
```

### 3. Live test: real Jenkins in Docker

Start Docker Desktop, then:

```powershell
powershell -ExecutionPolicy Bypass -File tests\live\run_live.ps1
```

### 4. Try it by hand in Copilot

Point your config at the Docker Jenkins (next section), then in VS Code: `Ctrl+Shift+P` ->
*MCP: List Servers* -> `jenkins` -> **Restart**, open Copilot Chat in **Agent** mode and ask the questions below.
Prompts: type `/` and pick `mcp.jenkins.analyse_build`.

---

## Live test details

The first run downloads Jenkins and plugins (5-10 min). Jenkins UI: http://localhost:8080 (admin / admin).
Sample jobs and the full checklist: [live/README.md](live/README.md).

Stop and delete it: `docker compose -f tests/live/docker-compose.yml down -v`

## Use the test Jenkins by hand

`config/jenkins_instances.yaml`
```yaml
default: local
instances:
  local:
    url: http://localhost:8080/
```

`config/.env`
```
JENKINS_LOCAL_USER=admin
JENKINS_LOCAL_TOKEN=<contents of tests/live/.token>
```

Then ask:

- *"list_instances with check_connection"*
- *"Why did cd/master/app-deploy #2 fail?"*
- *"What failed in the last 24 hours?"*
- *"Compare the good and bad builds of cd/master/app-deploy"*

Every run of `run_live.sh` / `run_live.ps1` writes a new token to `tests/live/.token` (the old one stays valid
until Jenkins is deleted with `down -v`). After a fresh Jenkins, copy the new token into `config/.env` and ask
*"list_instances reload_config=true"*.
