# Runbook — Set up JAT-LeadQuali in Visual Studio 2026

**Audience:** a developer sitting at a fresh Windows machine who wants to run, debug and test this
repository from Visual Studio 2026.
**Time:** about 30 minutes, most of it waiting for installers.
**Frequency:** once per machine.

You do not need to have read the implementation plan to follow this. Menu paths are in **bold**;
labels may differ slightly between VS 2026 builds, so every GUI step also has a terminal
equivalent you can use instead. All terminal commands are PowerShell, run from the repository
root unless stated otherwise.

---

## 0. Why Open Folder mode, and not a `.pyproj`

Visual Studio's Python tooling wants to own the project through its own `.pyproj` file, which
stores the interpreter, the file list and the startup item in a VS-specific XML format. That file
is invisible to everything else: GitHub Actions, `pip`, AWS Lambda packaging and any teammate on
macOS or Linux would all need a second, hand-synchronised description of the same project, and the
two descriptions drift the first time somebody adds a dependency. This repository therefore opens
in **Open Folder** mode and keeps `pyproject.toml` as the single source of truth for
dependencies, the package layout, pytest, ruff and mypy — so the build you get in Visual Studio is
byte-for-byte the build CI gets. `*.pyproj` is deliberately git-ignored. If Visual Studio ever
offers to generate a project file for you, decline it; converting this repo to a `.pyproj` is the
one change that will quietly break CI and the Lambda package.

---

## 1. Prerequisites

Work down this checklist. Each row has the command that proves it is done — run them in a normal
PowerShell window (**Start → Windows PowerShell**), not inside VS, since VS is not installed yet.

| # | Prerequisite | How to get it | Verify with |
|---|---|---|---|
| 1 | **Python 3.13** | python.org installer, tick **Add python.exe to PATH** | `py -3.13 --version` → `Python 3.13.x` |
| 2 | **Git** | Bundled with Visual Studio, or git-scm.com | `git --version` → `git version 2.x` |
| 3 | **Visual Studio 2026** with the **Python development** workload | Visual Studio Installer | Workload shows a tick in the installer |
| 4 | **Python web support** component | Same installer, right-hand pane | Component shows a tick in the installer |
| 5 | *(Optional, Phase 4 only)* **AWS Toolkit for Visual Studio** | **Extensions → Manage Extensions** | Extension listed as Installed |

```powershell
py -3.13 --version
git --version
```

Both must print a version. If `py -3.13 --version` fails, re-run the Python installer and make
sure **Add python.exe to PATH** is ticked; the `py` launcher is what every later step uses to pin
the interpreter to 3.13 rather than whatever Python happens to be first on `PATH`.

### 1a. Installing the Visual Studio workload

1. Open **Visual Studio Installer** → **Modify** on Visual Studio 2026.
2. **Workloads** tab → tick **Python development**.
3. In the right-hand **Installation details** pane, expand *Python development* and ensure
   **Python web support** is ticked. This is what supplies the web/debugging integration used in
   step 5.
4. Click **Modify** and let it install.
5. Optional, and not needed until the AWS phase: **Extensions → Manage Extensions** → search
   *AWS Toolkit* → install **AWS Toolkit for Visual Studio**.

You do not need the *Python native development tools* component, and you do not need any Python
version that Visual Studio bundles — this project uses the python.org 3.13 install from row 1.

---

## 2. Clone the repository

**GUI:**

1. Launch Visual Studio 2026 → **Clone a repository**.
2. **Repository location:** `https://github.com/vendo-aron/JAT-LeadQuali`
3. **Local path:** e.g. `C:\src\JAT-LeadQuali` → **Clone**.
4. Visual Studio opens the repo in **Folder View** in Solution Explorer. If it shows a solution
   picker, choose **Folder View** — see §0.

**Terminal equivalent:**

```powershell
git clone https://github.com/vendo-aron/JAT-LeadQuali.git C:\src\JAT-LeadQuali
```

Then in Visual Studio: **File → Open → Folder…** → select `C:\src\JAT-LeadQuali`.

Confirm the title bar shows the folder name and Solution Explorer lists `pyproject.toml`,
`src\`, `tests\` and `docs\` — that is Folder View. If Solution Explorer shows a solution node
with a project underneath it instead, close it (**File → Close Solution**) and re-open via
**File → Open → Folder…**.

---

## 3. Create and activate the virtual environment

Everything is installed into a `.venv` inside the repo. It is git-ignored, so it never leaves your
machine.

1. Open a terminal at the repo root: **View → Terminal** (Developer PowerShell).
2. Create the environment against Python 3.13 specifically, and activate it:

   ```powershell
   py -3.13 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. **If activation is blocked** with *"running scripts is disabled on this system"*, PowerShell's
   execution policy is refusing the activation script. Allow it for this terminal session only:

   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
   .\.venv\Scripts\Activate.ps1
   ```

   `-Scope Process` affects only the current terminal and resets when you close it, so it changes
   nothing machine-wide. Repeat it in each new terminal, or set `-Scope CurrentUser` once if you
   prefer.

4. Confirm the prompt is now prefixed with `(.venv)` and that the right interpreter is in front:

   ```powershell
   python --version
   where.exe python
   ```

   The first line must be `Python 3.13.x`; the first path printed by `where.exe python` must be
   inside `.venv\Scripts\`.

---

## 4. Point Visual Studio at that environment

Activating a terminal does not by itself tell the IDE which interpreter to use for IntelliSense,
debugging and Test Explorer. Do that once:

1. **View → Other Windows → Python Environments**.
2. `.venv` should be listed for this folder. If it is not, click **Add Environment… → Existing
   environment** and point it at `C:\src\JAT-LeadQuali\.venv\Scripts\python.exe`.
3. Set `.venv` as the **active environment for the folder** (right-click it → *Activate*, or use
   the checkbox in the pane header).

Then install the project and its dev tooling in editable mode:

```powershell
pip install -e ".[dev]"
```

The quotes matter in PowerShell — without them `[dev]` is parsed as a wildcard. Editable mode
(`-e`) means `import leadquali` resolves to `src\leadquali\` on disk, so your edits take effect
with no reinstall. Verify:

```powershell
pip show leadquali
python -c "import leadquali, sys; print(leadquali.__file__); print(sys.version)"
```

The printed path must be under `src\leadquali\`, and the version must be 3.13.

---

## 5. F5 debugging

> **Read this before you follow it.** In Open Folder mode Visual Studio debugs a Python **file**,
> not a uvicorn command line, so the repo root carries `run_local.py` as the F5 startup item.
> **`run_local.py` and the FastAPI app it serves do not exist yet.** The app object
> `leadquali.api.main:app` is created by issue **#17** (*FastAPI ingest endpoint*) in Phase 2,
> and `run_local.py` ships with it. Until that lands, the steps below cannot succeed as written —
> use the meanwhile-verification in §5b instead, and come back to §5a once Phase 2 is merged.
> Do not hand-write `run_local.py` to get ahead of this; the file belongs to #17 and a local copy
> will collide with it.

### 5a. Once Phase 2 (#17) has landed

`run_local.py` at the repo root looks like this:

```python
import uvicorn

if __name__ == "__main__":
    uvicorn.run("leadquali.api.main:app", host="127.0.0.1", port=8000, reload=True)
```

1. Solution Explorer → right-click `run_local.py` → **Set as Startup Item**.
2. Press **F5**.
3. Browse to <http://127.0.0.1:8000/docs> — the FastAPI interactive docs confirm the app is up.
4. Set a breakpoint in a request handler under `src\leadquali\`, send a request from the docs
   page, and confirm the debugger stops on it.

**Terminal equivalent** (runs the same server, without the debugger attached):

```powershell
python run_local.py
```

> **`reload=True` detaches the debugger.** Uvicorn's auto-reload runs the application in a *child*
> process while the debugger stays attached to the parent, so after the first reload your
> breakpoints in `src\leadquali\` silently stop being hit. If that happens, set `reload=False` in
> `run_local.py` for the duration of the debugging session (and restart with F5). Leave it as
> `reload=True` for ordinary local running, where edit-and-refresh is worth more than breakpoints.

### 5b. Meanwhile-verification (do this today)

The goal is only to prove that the debugger runs against the `.venv` interpreter. Either check is
enough:

**Option A — F5 on a trivial file.** Create a scratch file *outside* the repo, e.g.
`C:\src\scratch\hello.py`:

```python
import sys

print(sys.version)
print("debugger works")
```

Open it in Visual Studio (**File → Open → File…**), set a breakpoint on the `print` line, press
**F5**, and confirm execution stops there and the version shown is 3.13. Delete the file
afterwards — keep it out of the repository so it never gets committed.

**Option B — run the tests under the debugger.** In **Test Explorer** (§6), right-click any test →
**Debug**. A breakpoint inside that test being hit proves the same thing, and leaves nothing
behind.

---

## 6. Test Explorer

There is nothing to configure. Visual Studio reads pytest settings straight out of the committed
`pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

`testpaths` tells the discovery scan where the tests live; `pythonpath = ["src"]` puts the `src/`
layout on the import path so `import leadquali` works during collection.

1. **Test → Test Explorer**.
2. Wait for the scan. The scaffold's tests under `tests\` appear in the tree.
3. **Test → Run All Tests**. They must all pass.
4. Confirm the terminal agrees — this is exactly what CI runs:

   ```powershell
   pytest
   ```

If Test Explorer and the terminal disagree, trust the terminal: the difference is almost always
that Test Explorer is using a different interpreter (see troubleshooting below).

---

## 7. Your Anthropic API key

Never put the key in source, in `pyproject.toml`, or in anything you commit. Set it as a user
environment variable:

```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."
```

Then **restart Visual Studio**. `setx` writes the variable for *future* processes only — a running
VS (and every terminal already open inside it) keeps the environment it started with, so a
debugged process will not see the key until VS is relaunched. Verify in a **new** terminal after
the restart:

```powershell
echo $env:ANTHROPIC_API_KEY
```

If you prefer a per-project key, put it in a `.env` file at the repo root instead — the
application reads it, and it is git-ignored.

### Confirm nothing local can be committed

These entries are already in the repository's `.gitignore`, so no action is needed beyond
confirming them once:

- `.env` (and `.env.*`, except `.env.example`) — your key
- `.venv/` — the virtual environment
- `*.pyproj`, `*.pyproj.user`, `*.sln`, `.vs/` — Visual Studio's own files

```powershell
git status
git check-ignore -v .env .venv .vs test.pyproj
```

`git status` must show a clean tree (or only files you intentionally changed), and
`git check-ignore` must print a matching `.gitignore` rule for all four paths.

---

## 8. Commit from Visual Studio

**GUI:** **View → Git Changes** → stage your files → write a message → **Commit All** → **Push**.

**Terminal equivalent:**

```powershell
git add <files>
git commit -m "Your message"
git push
```

Then confirm the GitHub Actions run for the push goes green — the CI workflow runs the same
`ruff`, `mypy` and `pytest` you just ran locally, so a green local run and a red CI run means
something machine-specific leaked into your setup.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| **Test Explorer shows no tests** | Folder opened as a solution rather than a folder; or the scan ran before `pip install -e ".[dev]"` finished; or `pytest` is not installed in the active environment | Confirm Folder View (§2), run `pip install -e ".[dev]"`, then **Test → Test Explorer** → refresh. Cross-check with `pytest --collect-only` in the terminal — if that lists tests and VS does not, it is a VS-side interpreter problem, next row. |
| **Wrong interpreter selected** (VS uses a global Python, or 3.12) | `.venv` was created after the folder was opened, or was never activated for the folder | **View → Other Windows → Python Environments** → activate `.venv` for this folder. Verify with `where.exe python` (first hit under `.venv\Scripts\`) and `python --version` (3.13.x). Close and reopen the folder to force VS to re-scan. |
| **Breakpoints not hit** while the server runs | `reload=True` moved the app into a child process the debugger is not attached to | Set `reload=False` in `run_local.py` while debugging (§5a). |
| **Breakpoints not hit** in any file | Started with **Ctrl+F5** (Start Without Debugging) rather than **F5**; or the file is not the startup item | Use **F5**. Right-click the intended file → **Set as Startup Item**. |
| **`ModuleNotFoundError: No module named 'leadquali'`** | The `src/` layout is not on the import path — usually the project was never installed in editable mode, or a different interpreter is active | `pip install -e ".[dev]"` in the activated `.venv`. For test runs, `pythonpath = ["src"]` in `pyproject.toml` covers it, so this error in Test Explorer means the wrong interpreter is active. |
| **Imports resolve at runtime but are red in the editor** | IntelliSense is still indexing, or is pointed at another environment | Activate `.venv` in **Python Environments**, then **Build → Rescan Solution** / reopen the folder. |
| **`.\.venv\Scripts\Activate.ps1` cannot be loaded** | PowerShell execution policy | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` (§3.3). |
| **`pip install -e ".[dev]"` fails on the `[dev]` part** | PowerShell glob-expanded the brackets | Keep the double quotes exactly as written. |
| **App cannot find `ANTHROPIC_API_KEY`** | VS was not restarted after `setx` | Restart Visual Studio; verify with `echo $env:ANTHROPIC_API_KEY` in a new terminal (§7). |
| **VS offers to create a `.pyproj` / a solution** | Folder was opened through a solution-oriented entry point | Decline. Re-open via **File → Open → Folder…**. See §0 for why. |

---

## 10. Definition of done

You are finished when every box is true:

- [ ] `py -3.13 --version` and `git --version` both report expected versions.
- [ ] Visual Studio 2026 has the **Python development** workload with **Python web support**.
- [ ] The repo is cloned and open in **Folder View** — no `.pyproj`, no `.sln`.
- [ ] `.venv` exists, is activated for the folder in **Python Environments**, and `where.exe
      python` resolves to it.
- [ ] `pip install -e ".[dev]"` completed and `python -c "import leadquali"` succeeds.
- [ ] **Test Explorer** discovers the suite and **Run All Tests** is green, matching bare `pytest`.
- [ ] A breakpoint is hit under the debugger — via `run_local.py` + F5 once #17 has landed, or via
      §5b's meanwhile-verification until then.
- [ ] `ANTHROPIC_API_KEY` is set with `setx` and Visual Studio has been restarted since.
- [ ] `git status` is clean of `.vs/`, `.venv/`, `*.pyproj` and `.env`.
- [ ] A commit pushed from the **Git Changes** window turns the GitHub Actions run green.

---

## Related

- `docs/IMPLEMENTATION_PLAN.md` §6 (repository layout), §9 (delivery phases), §10 (the source
  material for this runbook).
- Issue #17 — *FastAPI ingest endpoint*, which creates `leadquali.api.main:app` and `run_local.py`
  and makes §5a live.
