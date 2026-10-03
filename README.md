# windows-ai-agent

A Claude-powered agent that controls a Windows PC directly — running commands,
launching/closing apps, managing files, and operating the mouse/keyboard/screen
for GUI-only tasks.

## How it works

- **Direct scripting first**: `commands.py` runs PowerShell, launches/closes
  apps, and does file operations. Fast and reliable — the agent prefers this.
- **UI Automation second**: `ui_automation.py` targets named controls (e.g.
  a "Save" button, a "File name" text box) inside a window via Windows UI
  Automation (`pywinauto`), instead of pixel coordinates. Survives window
  moves, resizes, and DPI changes that would break coordinate clicking.
- **Screen/GUI coordinates as last resort**: `computer.py` takes
  screenshots and drives the mouse/keyboard via `pyautogui`, only for apps
  with no accessible controls (games, custom-rendered UIs).
- **Action verification**: MODIFY-class actions on `computer`,
  `ui_click_control`, `ui_type_into_control`, `launch_app`, and `close_app`
  automatically get a post-action screenshot and the active window title
  attached to their result. The system prompt tells Claude to check this
  before assuming an action worked — a tool call returning "success"
  doesn't guarantee the intended visible change actually happened.
- **Permission-based safety gate**: every tool call passes through
  `safety.py` before it runs. Every action resolves to one of three classes:
  - `OBSERVE` — read-only (screenshots, listing files, reading files, safe
    cmdlets like `Get-Process`). Never confirmed.
  - `MODIFY` — reversible/bounded side effects (moving files, clicking,
    typing, launching apps). Confirmed if `require_confirmation` is on.
  - `DANGEROUS` — irreversible, broad blast-radius, **or unrecognized**.
    Always confirmed, hardcoded in `safety.py`, regardless of what
    `require_confirmation` is set to. This floor cannot be turned off by
    config, and no tool exposed to the agent can edit `config.yaml` at
    runtime — the only way to weaken it is a human editing the file by hand.
  - `run_command` specifically is **not** a blacklist check, and its
    enforcement lives INSIDE `CommandRunner.run_command()` itself, not
    just in the dispatcher that calls it — so a future code path calling
    `CommandRunner` directly still can't skip the check. Its leading
    PowerShell cmdlet is matched against explicit `safe_cmdlets` /
    `confirm_cmdlets` allowlists in `config.yaml`. Anything not on either
    list — not just anything on a blocklist — defaults to `DANGEROUS`
    (fail closed). Obfuscation indicators (`-EncodedCommand`, `Invoke-Expression`,
    `DownloadString`, piping to `iex`, etc.) are hard-blocked outright,
    independent of which cmdlet appears in the command text.
  - **`OBSERVE` requires a single, uncomposed command, not just a safe
    leading cmdlet.** `Get-Date; Start-Process evil.exe` has a safe-looking
    first token but chains a second command after a semicolon — checking
    only the first word would auto-trust the whole line. Any of `;`, `|`,
    `&`, `` ` ``, `$(`, `{`, `}`, or a newline anywhere in the command
    forces `DANGEROUS` regardless of the leading cmdlet, so composition,
    piping, subexpressions, and script blocks can't ride in on a
    trusted-looking prefix. `Where-Object`/`Select-Object`/`Select-String`
    are kept out of `safe_cmdlets` for the same reason — their normal use
    involves a script block (`{ }`), which the composition check would
    catch anyway, so they live in `confirm_cmdlets` instead.
  - **File operations check every path involved, not just one.**
    `move_file`'s `dst` used to go unchecked — only `src` was validated
    against `allowed_paths`. Now both are checked independently; either
    one falling outside the allowlist escalates the action to `DANGEROUS`.
    This matters because a move can smuggle a file *into* an unapproved
    location just as easily as reading one *out of* one.
- **Emergency kill switch**: `kill_switch.py` runs a global hotkey listener
  (default `Ctrl+Alt+Shift+Q`) in a background thread, independent of the
  agent loop. Triggering it terminates the process immediately
  (`os._exit`) rather than just setting a flag the agent might not check
  in time — it works even if the agent is stuck mid-action.
- **Structured action log**: every tool call — goal, iteration, tool,
  arguments, resolved permission class, and result summary (screenshots
  excluded) — is appended to `logs/actions.jsonl` as one JSON object per
  line by `action_logger.py`. Useful for debugging a run after the fact or
  auditing exactly what a session did, without scrolling back through
  console output.
- **Bounded recovery on UI Automation lookups**: `ui_click_control` and
  `ui_type_into_control` retry up to `limits.ui_max_retries` times (each
  with a fresh window lookup and a short pause) if a control isn't found —
  apps are sometimes still rendering when the first action lands. This is
  a config-driven cap, not an unbounded loop. Both tools also accept an
  optional `control_type` (e.g. `"Button"`, `"MenuItem"`) to disambiguate
  when two controls share a name.
- **Explicit timeouts everywhere an action could hang**: `run_command`'s
  subprocess timeout and UI Automation's control-wait timeout both come
  from `config.yaml`'s `limits` section rather than being scattered magic
  numbers, so they're visible and tunable in one place.
- **`launch_app` is allowlist-based and never uses a shell.** A name on
  `config.yaml`'s `app_permissions.allowlist` (notepad, calculator,
  explorer, paint, wordpad, cmd) resolves and launches with a normal
  `MODIFY` confirmation. Anything else — an arbitrary path or an
  unrecognized name — still launches, but only after an explicit
  `DANGEROUS`-level confirmation naming exactly what will run. Execution
  always uses `subprocess.Popen(args, shell=False)` with `shlex`-split
  argv, never `shell=True` on a raw string, so there's no shell
  metacharacter interpretation to worry about regardless of what the
  model supplies.
- **`close_app` scales its confirmation to what it would actually affect.**
  Process names aren't unique, so before acting it counts every matching
  process: zero matches needs no confirmation (nothing to do), exactly one
  match is a normal `MODIFY` confirmation, and two or more matches is
  escalated to a `DANGEROUS`-level confirmation that explicitly lists
  every PID that would be terminated. `ui_close_window` (UI Automation)
  is the preferred alternative when you mean to close one specific window
  — it targets that window via a close request, not a process-name match.
- **Both `launch_app` and `close_app` enforce safety inside their own
  `CommandRunner` methods**, the same pattern as `run_command` — not just
  in the dispatcher that calls them, so a future direct caller can't skip
  the check.
- **Path checks resolve symlinks/junctions, not just string-normalize.**
  `path_is_allowed` uses `os.path.realpath` on both the candidate and each
  allowed path before comparing, resolved fresh on every call. A plain
  `normpath` comparison can be fooled by an NTFS junction inside an
  allowed folder that actually redirects elsewhere; `realpath` follows
  that redirect before the comparison happens.
- **Loop**: `agent.py` sends your goal to Claude with all tools attached,
  executes whatever tool Claude asks for, feeds the result back, and repeats
  until Claude gives a final answer, the iteration cap is hit, or the kill
  switch fires.

## Setup

1. **Python 3.10+ on Windows.**

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Set your Anthropic API key as an environment variable:
   ```
   setx ANTHROPIC_API_KEY "your-key-here"
   ```
   (restart your terminal after this)

4. Review `config/config.yaml` — especially `safety.allowed_paths` and
   `safety.blocked_patterns`. Adjust `allowed_paths` to folders you're
   actually comfortable letting the agent touch.

## Running

```
python main.py "Open Notepad, write a short note, save it to Desktop\note.txt"
```

Or run with no arguments to be prompted for a goal interactively.

## Running the test suite

```
python run_tests.py            # basic suite: files, folders, app open/close (8 cases)
python run_tests.py --full     # + Calculator, window switching, failure recovery (11 cases)
python run_tests.py --quick    # first 3 basic cases only, fast smoke test
```

Each test sends a goal through the real `Agent.run()` loop, then verifies
the outcome **deterministically** — reading actual file contents, checking
real process lists, and checking the real active window title via
`dispatcher.commands`/`dispatcher.ui` directly — rather than trusting the
agent's own summary of what it did. A screenshot or narration saying
"success" doesn't count; the file has to actually exist with the right
content.

Results are written to `logs/test_reports/report_<timestamp>.{json,md}`.

**This runs unattended** — it uses a `confirm_callback` hook (see
`safety.py`'s docstring) so confirmations don't block on a console prompt
mid-suite. That hook is constructor-only and invisible to the agent's own
tools; `main.py` never passes one, so normal interactive runs still
confirm exactly as before.

Importantly, the test callback (`_scoped_test_confirm` in `tests/runner.py`)
is **scope-aware, not a blanket approval**. It only approves actions that
match what the suite is actually meant to do — file operations under a
Desktop path containing `agent_test`, launching/closing Notepad or
Calculator specifically, UI actions targeting Notepad/Calculator windows —
and denies everything else, including `run_command` (which the suite
never intentionally uses). This matters because an unconditional "approve
everything" test callback would itself become a safety bypass if a test
goal were ever worded ambiguously or the agent's behavior drifted; scoping
it means the harness can't approve something outside its own intent even
running unattended. Read `tests/cases.py` and the scope lists at the top
of `tests/runner.py` before running so you know exactly what's in scope.

## Safety notes — read before use

- **`require_confirmation: true` only affects `MODIFY`-class actions.**
  `DANGEROUS`-class actions (file deletion, unrecognized/high-risk
  PowerShell) always ask for confirmation no matter what — that floor is
  hardcoded in `safety.py`, not a config toggle.
- **The kill switch (`Ctrl+Alt+Shift+Q` by default) terminates the process
  outright.** Test it once at the start of a session so you know it works
  in your environment before you need it for real. On Windows, global
  hotkey hooks via the `keyboard` package sometimes need the terminal
  running as administrator to register — check this explicitly, since
  running the *agent* as admin is something you want to avoid, but you
  still need the kill switch's hook to actually register.
- **`pyautogui.FAILSAFE` is on** — slam your mouse cursor to any screen
  corner to immediately abort whatever the agent is doing. This and the
  kill switch are independent fallbacks; use whichever is faster in the
  moment.
- **Run as a non-admin Windows user** while you're building trust in the
  agent's behavior. Don't grant it an admin account.
- **`command_permissions` in config.yaml is an allowlist, not a
  blocklist.** Unrecognized PowerShell cmdlets default to `DANGEROUS`
  rather than auto-running — extend `safe_cmdlets`/`confirm_cmdlets`
  deliberately as you find commands you actually need, rather than trying
  to enumerate everything dangerous up front.
- The agent can only act as fast and safely as the tools you give it — the
  scaffold intentionally does *not* include tools for sending money,
  deleting system files, or modifying user accounts. Don't add those
  without serious thought about blast radius.

## Known limitations (next steps)

- **Verification is visual/positional, not semantic.** The agent gets a
  post-action screenshot and active-window title, but it's still Claude's
  judgment call whether that screenshot shows the intended result — there's
  no pixel-diffing or assertion framework checking this automatically. The
  test suite (`tests/cases.py`) is the deterministic layer that fills this
  gap for known tasks; it doesn't run automatically during a live session.
- **Recovery is basic, not a full strategy ladder.** `ui_click_control`/
  `ui_type_into_control` retry once with a fresh window lookup on failure,
  and `control_type` disambiguates same-named controls, but there's no
  automatic "try UI Automation, then coordinates, then re-open the app"
  escalation — that's still the model's judgment, guided by the system
  prompt and the error text/hints returned on failure.
- **Memory isn't wired into the agent loop yet.** `memory/memory.py` exists
  but `agent.py` doesn't call it — goals and outcomes aren't currently
  retrieved or stored automatically. Fine to defer until PC control itself
  is reliable (this was a deliberate ordering choice, not an oversight).
- **UI Automation control-matching is name-based and can be brittle** for
  apps with duplicate or dynamically-generated control names. `ui_list_controls`
  exists specifically so the agent can inspect what's actually available
  before guessing a name, and `control_type` narrows ambiguous matches.
- **Not yet tested against a real battery of tasks.** The code compiles,
  and the safety/dispatch/logging logic has been unit-tested in isolation
  (including the run_command hardening and the result-mutation fix), but
  the actual PowerShell/UI Automation/coordinate-control paths need real
  Windows testing — `pyautogui`, `pywinauto`, and `keyboard` don't fully
  function in the Linux sandbox this was built in.

## Project structure

```
windows-ai-agent/
├── agent/
│   ├── agent.py         # Main reasoning/action loop
│   ├── tools.py          # Tool schemas + dispatcher
│   ├── computer.py       # Mouse, keyboard, screenshots (coordinate fallback)
│   ├── ui_automation.py  # Windows UI Automation (named controls, preferred)
│   ├── commands.py       # Windows command execution, apps, files
│   ├── safety.py         # Permission classes + confirmation rules
│   ├── action_logger.py  # Structured JSONL action log
│   └── kill_switch.py    # Global emergency-stop hotkey
├── memory/
│   └── memory.py          # Persistent JSON history (basic, not yet wired in)
├── tests/
│   ├── cases.py            # Test case definitions with deterministic checks
│   └── runner.py           # Executes cases, writes JSON/Markdown reports
├── config/
│   └── config.yaml         # Agent configuration incl. command allowlists
├── logs/
│   └── test_reports/        # Output of run_tests.py
├── main.py                  # Entry point
├── run_tests.py               # Test suite entry point
└── requirements.txt
```

## Extending

- **Browser control**: add a `browser.py` module using Playwright, wire its
  actions into `tools.py` the same way `commands.py` is wired.
- **Scheduling**: wrap `Agent.run()` in APScheduler or a cron-triggered
  script for recurring tasks.
- **Real memory**: swap `memory/memory.py`'s JSON file for a vector store or
  SQLite DB, keeping the same `add_entry` / `recent` interface.
- **Action logging**: `safety.py` is the natural place to also write a
  structured log line per action to `logs/` for later audit.


## V2: Windows + Browser + Memory

This release combines the three layers into one agent:

### Windows hardening
- Command/app safety is enforced inside the command runner.
- Unknown PowerShell commands fail closed to confirmation.
- Shell composition and common obfuscation constructs are blocked.
- Application launching uses `shell=False`.
- File path checks use canonical real paths.
- Source and destination paths are both checked.
- Dangerous actions always require confirmation.
- Emergency kill switch: `Ctrl+Alt+Shift+Q`.
- Action results are logged as JSONL.

### Browser control
Browser automation uses Playwright and a fresh non-persistent Chromium context by default.
Install the browser runtime once:

```powershell
pip install -r requirements.txt
python -m playwright install chromium
```

Browser reading/navigation is available without confirmation. Clicks, form filling,
selection, and other modifying browser actions pass through the same safety gate.
Do not store credentials, API keys, cookies, or secrets in memory.

### Local memory
Memory is stored locally in `memory/memory.db` using SQLite.

The agent can:
- search relevant memories before a task
- remember explicit user preferences
- keep concise task history
- list recent memories
- forget individual memories

Memory is advisory only and can never override the safety layer. Website/file
instructions are treated as untrusted data and are not automatically saved as trusted
preferences.

### V2.1 hardening

- `browser_navigate` accepts only `http://` and `https://` URLs. Embedded URL credentials and localhost/private/reserved targets are blocked by default. `browser.allow_private_network` must be explicitly enabled to relax the network restriction.
- `browser_screenshot` is a `MODIFY` action and its destination is checked against `safety.safe_output_paths` and the normal allowed user paths. The controller repeats the output-path check as defense in depth.
- Browser tools use the same `SafetyGate` instance as Windows tools; there is no parallel browser permission system.
- The model-facing `memory_save` tool has been removed. Trusted code is the only persistence path for explicit user preferences and task history.
- `MemoryStore` accepts only trusted source labels (`explicit_user_request`, `task_history`, `agent_experience`) and rejects obvious credential/secret material.
- Browser/dispatcher exceptions are converted into tool results rather than crashing the agent loop.

## V2 examples

```powershell
python main.py "Open Chrome and research three good Python IDEs, then summarize them."
python main.py "Remember that my work folder is D:\Work."
python main.py "Find my previous preferences about file organization."
```

For unattended automation, keep confirmations enabled and expand the app/path allowlists
only after testing.
