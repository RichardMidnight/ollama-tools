# ollama-tools

An enhanced drop-in wrapper around the Ollama CLI. One Python file (standard
library only, no dependencies) plus a small Windows shim. You keep typing
`ollama ...` on your command line — the wrapper adds what the stock CLI
lacks:

- **Wildcards** on `list`, `rm`, and `stop` (`ollama rm 'gemma*'`)
- **Smart `create`** — derive a new model from a Modelfile where the base
  model is matched by pattern, with the tag preserved
  (`nemotron3:33b` + `-m my-sys` → `nemotron3-my-sys:33b`)
- **`test`** — run a prompt against several models at once, optionally
  through a Modelfile, capture results to files, and get an LLM-judge rating
- **`doctor`** — when a model hangs or stops responding: figures out what's
  wrong, shows you, and only fixes it after you say yes
- **`keepawake`** (Windows) — holds the PC back from sleeping while Ollama
  has models loaded, and lets it sleep when nobody is using them
- **`set_sleep`** (Windows) — one interactive command that sets up the
  sleep story: keepawake logon task, wake-on-LAN, and the
  "may sleep after inactivity" time (AC and battery)
- **`ctx`** — context-length tool for a loaded model: read its native max
  (from the GGUF) vs the live server/runner context, estimate the KV-cache
  memory a window costs, persist `OLLAMA_CONTEXT_LENGTH`, and re-apply it by
  restarting the live server — every mutating command is asked-for, never silent
- **Passthrough** for `ps`, `show`, `pull`

## Requirements

- **Ollama** installed and on your `PATH`
- **Python 3** (any reasonably recent version — the script is stdlib-only)
- Windows: nothing else (the `ollama.cmd` shim finds `py` or `python` itself)

## Installation

No build step and no dependencies — it's two files. Grab them and put
them in a directory that's on your `PATH`.

**Windows** (from PowerShell; `curl.exe` ships with Windows 10+):

```powershell
mkdir "$env:USERPROFILE\bin"
curl.exe -o "$env:USERPROFILE\bin\ollama.py"  https://raw.githubusercontent.com/RichardMidnight/ollama-tools/main/ollama.py
curl.exe -o "$env:USERPROFILE\bin\ollama.cmd" https://raw.githubusercontent.com/RichardMidnight/ollama-tools/main/ollama.cmd
```

**Unix / macOS:**

```sh
mkdir -p ~/.local/bin
curl -fLo ~/.local/bin/ollama.py https://raw.githubusercontent.com/RichardMidnight/ollama-tools/main/ollama.py
chmod +x ~/.local/bin/ollama.py
```

Then, as long as that directory is on your `PATH` (`%USERPROFILE%\bin` on
Windows, `~/.local/bin` is on PATH by default on most Linux distros), you
can just type:

```
ollama list 'gemma*'
```

(If you downloaded to a different folder, just run it from there, or install
it into a PATH bin dir with the script's own installer:
`ollama.cmd install [TARGET_DIR]` on Windows / `./ollama.py install [TARGET_DIR]`
on Unix — it copies the script and shim into `%USERPROFILE%\bin` or
`~/.local/bin` by default and tells you if the target isn't on PATH.)

### Updating

Re-run the download commands — they overwrite cleanly — or `git clone` the
repo for a working copy and use `install` to push it into your bin dir.
`ollama -V` shows you which copy (and version) is actually running.

> **Windows note:** run `ollama.cmd`, not `ollama.py` directly — the `.py`
> file association opens a detached console that flashes and closes.
> (Double-clicking is detected and the output is kept on screen with a
> "Press Enter to close" prompt.)

## Usage

```
ollama list PATTERN [PATTERN ...]        # list matching models + type (base/derived)
ollama rm PATTERN [PATTERN ...]          # remove matches (asks for 'yes')
ollama stop PATTERN [PATTERN ...]        # unload matching running models
ollama create NEWNAME -f MODELFILE -m BASE_PAT
ollama test -m MODELS -p PROMPT [-f MODELFILE] [-o] [--rate] [--judge M]
ollama doctor [MODEL] [--url URL] [--first-token-window N] [--stop-wait N] [-y|-n]
ollama keepawake [--url URL] [--interval N]            # Windows: hold sleep while models loaded
ollama set_sleep [--ac H] [--dc H] [--task-only] [--check-only] [-y] [--no-task] [--no-wol] [--no-power]
ollama ctx get [MODEL]                    # native max vs live server/runner context
ollama ctx kv [N] [MODEL]                 # KV-cache memory cost for N tokens
ollama ctx set N [MODEL] [-y] [--restart] # persist OLLAMA_CONTEXT_LENGTH
ollama ps | ollama show MODEL | ollama pull MODEL   # passthrough to real ollama
ollama -V                                    # version + which copy is running
```

### Wildcards

Patterns use shell-style wildcards (fnmatch). Multiple patterns are allowed;
`rm`/`stop` always show the resolved list and wait for a `yes` before
deleting anything.

**Quote your patterns** — `ollama rm 'gemma*'`, not `ollama rm gemma*`.
zsh (and fish) expand unquoted `*` against files in the current directory
before the script runs, and if nothing matches zsh refuses to run the
command at all: `zsh: no matches found: gemma*`.

```
ollama list 'nemotron*'
ollama stop 'qwen*'
ollama rm '*-unbound'          # derived copies
```

### Create with a Modelfile

The `FROM` line in your Modelfile is rewritten automatically to the resolved
base model — so one Modelfile works across all your model families:

```
ollama create my-sys -f SYSTEM -m 'nemotron3*'
#   base: nemotron3:33b  →  nemotron3-my-sys:33b
```

### Test harness

Runs a prompt against one or more (pattern-matched) models, optionally
applying a Modelfile on the fly (creates a throwaway model that is stopped
and removed afterwards). Other loaded models are unloaded first for a clean
run.

```
# Base models, output to console
ollama test -m 'nemotron3*' -p ./prompts/continuity.txt

# Through a modelfile, results to auto-named files:
#   nemotron3_33b_mod_continuity_response.txt
ollama test -m 'nemotron3:33b,qwen3*' -f ./SYSTEM -p ./prompts/continuity.txt -o

# + LLM judge rating (prompt must contain a rubric under
#   "POST-GENERATION EVALUATION CRITERIA"; the judge is told to use only it)
ollama test -m 'nemotron3*' -f ./SYSTEM -p prompt.txt --rate --judge qwen3-unbound:30b
```

Exit code is `1` if any model failed, so it chains cleanly in scripts.

### Context length (ctx)

A *usable* context window is capped by the lowest of three layers — and only
the middle one is something you can actually tune:

1. **the model's native max** — fixed in the GGUF (readable, not changeable)
2. **the Ollama server / runner size** — `OLLAMA_CONTEXT_LENGTH`: the real knob
3. **the client's own prompt window** (e.g. OpenCode's `limit.context`) —
   invisible to Ollama; you set it in the client, not here

`ctx get` and `ctx kv` are **read-only and consent-free**; `ctx set` and
`ctx restart` change the live setup, and both ask before they do (`ctx restart`
only re-applies what's already persisted — you never pick a number there).

```
ollama ctx get [MODEL]              # native max vs live server/runner context
ollama ctx kv [N] [MODEL]           # KV-cache memory for N tokens + free-RAM headroom
ollama ctx set N [MODEL] [-y] [--restart]   # persist OLLAMA_CONTEXT_LENGTH
ollama ctx restart [-y]                     # re-apply the persisted value (Windows)
```

- **`ctx get`** reads the native max from the model's GGUF and reports what you
  actually have right now — the server context (`/api/ps`) and the runner's
  `-c` — so you can see your working cap versus the ceiling.
- **`ctx kv`** estimates the KV-cache cost (GiB) for a given window from the
  GGUF's layer / kv-head geometry, and shows your free-RAM headroom — so you
  can grow the window with your eyes open.
- **`ctx set`** on Windows persists the value with `setx` (for *future*
  `ollama serve` processes) and **asks before changing anything** (and declines
  on non-interactive stdin, so it can never act by surprise). `--restart` also
  restarts the live server, which **drops any live sessions** — opt-in, not the
  default. On non-Windows it doesn't change anything; it just prints the
  `export OLLAMA_CONTEXT_LENGTH=N` line and how to restart your Ollama service.
- **`ctx restart`** (Windows) re-applies whatever context value is already
  persisted (`HKCU\Environment\OLLAMA_CONTEXT_LENGTH`) without asking you to
  re-enter a number: it kills the tray app (`ollama app`), the runner(s), and
  the live `ollama serve`, then starts a fresh server with that value — and
  bakes in the `0.0.0.0:11434` LAN bind the tray app used to provide. It
  **drops live sessions**, so it asks first (and non-interactive stdin
  declines). Use it to flip a running server to its persisted value, or to claw
  back a session if the tray app re-imposed a lower cap.

#### Durable 256k on Windows (bypass the tray app)

The Ollama Windows **tray app** (`ollama app.exe`) pins the server it launches
to a smaller context (128k on this machine) *and ignores* the persisted
`OLLAMA_CONTEXT_LENGTH` — so `ctx set 262144` alone won't hold once the tray
app has started the server. The tray app also normally owns the
`0.0.0.0:11434` LAN bind, so bypassing it must re-supply that or LAN / OpenWebUI
access breaks.

Give a plain `ollama serve` the win on the port instead. Two fully-reversible
changes in your per-user **Startup** folder (both only take effect at next
logon):

- the tray-app shortcut is renamed `Ollama.lnk` → `Ollama.lnk.off` (disabled,
  not deleted — rename it back to restore); and
- `Ollama-serve-256k.cmd` is added, which launches the real `ollama.exe serve`
  with `OLLAMA_CONTEXT_LENGTH=262144` and `OLLAMA_HOST=http://0.0.0.0:11434`
  baked in, and skips starting if `:11434` is already serving.

> `ollama` on `PATH` resolves to a shim (`ollama.cmd`) that runs `ollama.py`,
> so both `ctx restart` and the launcher resolve the **real** `ollama.exe`
> explicitly to avoid launching the Python tool by mistake.

**Activate now (no reboot):** `ollama ctx restart -y` — it drops every live
session (including the assistant if it runs on Ollama), so run it last. **Or**
just reboot once: the launcher brings up a 256k server and OpenWebUI reconnects.
Verify with `ollama ctx get`.

**Rollback:** delete `Startup\Ollama-serve-256k.cmd`; rename
`Ollama.lnk.off` → `Ollama.lnk`; start the tray app; optionally
`ollama ctx set 131072`.

### Stuck-runner doctor

`doctor` is the heavy one. It:

1. Checks whether a newer Ollama release exists (informational, online)
2. Saves diagnostics (`ollama -v`, `ps`, process list, `nvidia-smi`) to a
   timestamped `ollama_doctor_*.log` next to the script
3. Streams a tiny chat request — **first token within the window = healthy**,
   in which case it touches nothing and exits 0
4. If wedged: prints what's wrong and **asks** before doing anything —
   `-y` pre-answers yes (unattended), `-n`/`--check-only` never attempts
   recovery, and non-interactive stdin auto-declines, so a bare `doctor`
   can never act by surprise
5. On yes: graceful `ollama stop`, waits for unload, then — if still stuck —
   kills *only* the `llama-server` identified as serving that model (matched
   via the model manifest's layer digest) — other models' runners left alone
6. Re-tests inference and reports the outcome

```
ollama doctor                        # default: whichever model is loaded
ollama doctor nemotron3:33b --first-token-window 30 --stop-wait 30
ollama doctor -y                     # unattended: answer yes without asking
ollama doctor -n                     # probe/report only; nothing is touched
```

Exit codes: `0` healthy or recovered · `1` stuck and could not be recovered ·
`3` stuck but recovery declined (or `--check-only`).

Status lines are colored on a real terminal — **green** for healthy/pass,
**red** for stuck/failed; piped output and the log files stay plain, and
`NO_COLOR` is honored.

Logs are written next to the running copy (so dev runs log to the dev
folder, installed runs to `~/bin`); override with `--log-dir`.

### Sleep and keepawake (Windows)

For a local Ollama box that also serves other people on your network:
`set_sleep` is one interactive command that sets up the sleep story —
keep the PC awake while models are in use, let it sleep when idle, and
let other machines wake it. Each piece also works on its own.

**`keepawake`** polls Ollama's `/api/ps` and holds a system-wide
"do not sleep" flag for as long as at least one model is loaded. The
moment the last model unloads, the flag is released and the PC can
sleep again according to your normal power settings.

```
ollama keepawake               # run it right now (Ctrl-C to stop)
ollama keepawake --interval 5  # poll every 5s instead of 10
```

**`set_sleep`** asks three questions and shows what it will change
(green/red diff, confirm before touching anything):

1. Create an `OllamaKeepAwake` logon task that starts `keepawake`
   automatically when you log on?
2. Enable wake-on-LAN on the active network adapters (so other
   machines can wake the PC)?
3. How long may the PC stay idle before it may sleep — on AC power and
   on battery? (Enter = 2 hours, `never`, or a number like `30m`)

Declining one question never cancels the others.

```
ollama set_sleep                  # interactive: 3 questions, then a confirm
ollama set_sleep --check-only     # pure report: current sleep/task/WoL state
ollama set_sleep --task-only      # just make sure the logon task exists
ollama set_sleep --ac 2 --dc 4    # only change "sleep after idle" times
ollama set_sleep -y               # unattended: task + wake-on-LAN + 2h idle
ollama set_sleep -y --no-wol      # same, except wake-on-LAN
```

How it works:

- `keepawake` uses the Windows `SetThreadExecutionState` API
  (`ES_CONTINUOUS | ES_SYSTEM_REQUIRED`), which resets the system idle
  timer while any model is loaded. It checks every `--interval` seconds
  and releases the flag when no models are loaded.
- The sleep-after-idle time is the standby timeout, set with
  `powercfg /change standby-timeout-ac` / `-dc` (no admin needed) and
  read back from `powercfg /query`; `0` means "never".
- Wake-on-LAN: active physical adapters are listed, the ones already
  allowed to wake (`powercfg /devicequery wake_armed`) are left alone,
  and the rest get `powercfg /deviceenablewake`. That one often needs
  an admin prompt — when it's denied, the exact elevated one-liner is
  printed. WoL also needs the NIC driver's own wake setting and a cable
  or router that supports it; the tool prints those notes.
- The logon task is created with `schtasks /Create /F /SC ONLOGON /TN
  OllamaKeepAwake` and its action points at *the running copy* of
  `ollama.py` (a no-window Python), so re-running `set_sleep` after an
  update keeps the task in sync. On systems that block non-elevated
  task creation, the exact elevated one-liner is printed instead.
- LAN note: for other machines to reach the API at all, Ollama must
  bind to the LAN (`OLLAMA_HOST=0.0.0.0`). That exposes it to anyone on
  the network with no authentication — set it yourself if that's what
  you want; this tool never changes that for you.

Exit codes: `0` success · `1` some steps failed (usually: needs admin —
see the printed one-liners) · `3` you declined at the confirm.

## Files

| File        | Purpose                                              |
| ----------- | ---------------------------------------------------- |
| `ollama.py` | The whole tool (single file, stdlib-only)            |
| `ollama.cmd`| Windows shim: runs the script without the flash-and-close console |
| `.gitignore`| Keeps `__pycache__`, response captures, and doctor/recovery logs out of git |
