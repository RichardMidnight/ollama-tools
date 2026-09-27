# ollama-tools

An enhanced drop-in wrapper around the Ollama CLI. One Python file (standard
library only, no dependencies) plus a small Windows shim. You keep typing
`ollama ...` on your command line — the wrapper adds what the stock CLI
lacks:

- **Wildcards** on `list`, `rm`, and `stop` (`ollama rm gemma*`)
- **Smart `create`** — derive a new model from a Modelfile where the base
  model is matched by pattern, with the tag preserved
  (`nemotron3:33b` + `-m my-sys` → `nemotron3-my-sys:33b`)
- **`test`** — run a prompt against several models at once, optionally
  through a Modelfile, capture results to files, and get an LLM-judge rating
- **`recover`** — diagnose (and fix) a stuck Ollama model runner: inference
  probe with a first-token timeout, graceful unload, targeted kill of only
  *your* model's `llama-server`, re-test — all with a diagnostic log
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

(If you downloaded to some other folder instead, drop the command's output
wherever you like, or run the script's own installer from that folder:
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
ollama recover [MODEL] [--url URL] [--first-token-window N] [--stop-wait N]
ollama ps | ollama show MODEL | ollama pull MODEL   # passthrough to real ollama
ollama -V                                    # version + which copy is running
```

### Wildcards

Patterns use shell-style wildcards (fnmatch). Multiple patterns are allowed;
`rm`/`stop` always show the resolved list and wait for a `yes` before
deleting anything.

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

### Stuck-runner recovery

`recover` is the heavy one. It:

1. Checks whether a newer Ollama release exists (informational, online)
2. Saves diagnostics (`ollama -v`, `ps`, process list, `nvidia-smi`) to a
   timestamped `ollama_recover_*.log` next to the script
3. Streams a tiny chat request — **first token within the window = healthy**,
   in which case it touches nothing
4. On failure: graceful `ollama stop`, waits for unload
5. If still stuck: kills *only* the `llama-server` identified as serving that
   model (matched via the model manifest's layer digest) — other models'
   runners are left alone
6. Re-tests inference and reports the outcome

```
ollama recover                       # default model qwen3.8:27b
ollama recover nemotron3:33b --first-token-window 30 --stop-wait 30
```

Logs are written next to the running copy (so dev runs log to the dev
folder, installed runs to `~/bin`); override with `--log-dir`.

## Files

| File        | Purpose                                              |
| ----------- | ---------------------------------------------------- |
| `ollama.py` | The whole tool (single file, stdlib-only)            |
| `ollama.cmd`| Windows shim: runs the script without the flash-and-close console |
| `.gitignore`| Keeps `__pycache__`, response captures, and recovery logs out of git |
