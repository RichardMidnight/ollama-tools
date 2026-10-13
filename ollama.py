#!/usr/bin/env python3

# v0.9.7
#
# ollama wrapper: wildcards, model testing, and stuck-runner recovery
# (all-in-one; replaces the separate ollama_fix.ps1).
#
# NOTE: Store this in your user bin directory (which must be on PATH):
#   - Windows: %USERPROFILE%\bin\ollama.py (e.g. C:\Users\<you>\bin\ollama.py)
#   - Unix/macOS: ~/.local/bin/ollama.py
#
# Development workflow: this folder is the dev copy. To install it into a
# PATH bin directory for daily use, run:  ollama.py install [TARGET_DIR]
# (defaults to ~/bin on Windows, ~/.local/bin on Unix/macOS)
#
# Windows: do NOT launch it as "ollama.py" directly - the .py file association
# runs it in a detached console that flashes and closes. Use the ollama.cmd
# shim that lives next to this file (e.g. "ollama.cmd list qwen*").
# Unix/macOS: chmod +x ollama.py and run it directly.

import argparse
import subprocess
import sys
import fnmatch
import shutil
import os
import tempfile
import json
import re
import time
import socket
import http.client
import urllib.request
import urllib.parse
import struct
from datetime import datetime

# -----------------------------
# Helpers
# -----------------------------

def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd, capture=True):
    try:
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True
        )
    except subprocess.CalledProcessError as e:
        die(e.stderr.strip() if e.stderr else str(e))


def ollama_exists():
    return shutil.which("ollama") is not None


def get_models():
    """
    Returns:
      {
        model_name: {
            "size": "9.0 GB",
            "modified": "7 weeks ago"
        }
      }
    """
    res = run(["ollama", "list"])
    lines = res.stdout.strip().splitlines()

    if len(lines) <= 1:
        return {}

    models = {}

    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue

        name = parts[0]
        size = f"{parts[2]} {parts[3]}"
        modified = " ".join(parts[4:])

        models[name] = {
            "size": size,
            "modified": modified
        }

    return models


def classify_model(model):
    """
    Returns:
      'derived' if model has a System section
      'base' otherwise
    """
    try:
        res = subprocess.run(
            ["ollama", "show", model],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True
        )
    except subprocess.CalledProcessError:
        return "unknown"

    for line in res.stdout.splitlines():
        if line.strip().lower() == "system":
            return "derived"

    return "base"


def match_models(patterns):
    models = get_models()
    matched = {}

    for pat in patterns:
        for name, info in models.items():
            if fnmatch.fnmatch(name, pat):
                matched[name] = info

    return dict(sorted(matched.items()))


def confirm(action, models):
    print(f"\nYou are about to {action} the following models:\n")
    for m in models:
        print(f"  - {m}")
    resp = input("\nType 'yes' to continue: ").strip().lower()
    if resp != "yes":
        print("Aborted.")
        sys.exit(0)


def passthrough(cmd, args):
    subprocess.run(
        ["ollama", cmd] + args,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False
    )


def stop_running_models(verbose=False):
    """
    Stops all running models by parsing `ollama ps`.
    Safe: ignores errors and continues.
    """
    try:
        res = subprocess.run(
            ["ollama", "ps"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False
        )
    except Exception:
        return

    out = (res.stdout or "").strip()
    if not out:
        return

    lines = out.splitlines()
    if len(lines) <= 1:
        return

    # Expect header row, then rows like:
    # NAME  ID  SIZE  PROCESSOR  UNTIL
    running = []
    for line in lines[1:]:
        parts = line.split()
        if parts:
            running.append(parts[0])

    for model in running:
        if verbose:
            print(f"Stopping {model}", file=sys.stderr)

        try:
            subprocess.run(
                ["ollama", "stop", model],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False
            )
        except Exception:
            pass


# -----------------------------
# Recovery ("doctor")
#
# Ported from ollama_fix.ps1. Probes whether inference works; if the runner
# is wedged, reports it and asks for consent before ANY recovery action.
# Standard library only:
#   1. Informational online check for a newer Ollama version.
#   2. Save diagnostics (version, ps, processes, nvidia-smi) to a log.
#   3. Real inference test via a STREAMING request: first token = healthy.
#      ("think": false so Qwen-class thinking models don't burn the whole
#       budget on invisible reasoning; thinking tokens also count as alive.)
#   4. If inference fails: try a graceful "ollama stop".
#   5. Only kill OUR model's llama-server if it is still loaded/stuck.
#   6. Re-test inference and report.
#
# Windows-only parts (process enumeration / targeted kill) shell out to
# powershell/taskkill exactly as .ps1 version did.
# -----------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _clean(text):
    """Strip ANSI escape sequences (ollama spinner etc.) before logging."""
    if not text:
        return ""
    return _ANSI_RE.sub("", text)


def _append(logfile, text):
    try:
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


_ANSI = None


def _ansi(force=None):
    """Decide once whether ANSI color codes may be used on stdout."""
    global _ANSI
    if force is not None:
        _ANSI = force
    if _ANSI is not None:
        return _ANSI
    if os.environ.get("NO_COLOR"):
        _ANSI = False
        return False
    try:
        if not sys.stdout.isatty():
            _ANSI = False
            return False
    except (AttributeError, ValueError):
        _ANSI = False
        return False
    if os.name == "nt":
        # Win10+ conhost/Windows Terminal speak VT escape sequences; on older
        # builds SetConsoleMode is refused and we fall back to plain text.
        try:
            import ctypes
            k = ctypes.windll.kernel32
            handle = k.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if k.GetConsoleMode(handle, ctypes.byref(mode)):
                k.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            _ANSI = False
            return False
    _ANSI = True
    return True


def _paint(color, text):
    """Wrap text in green/red/yellow, or return it unchanged if no color."""
    if not _ansi():
        return text
    return "\x1b[%sm%s\x1b[0m" % ({"green": 32, "red": 31, "yellow": 33}[color], text)


def _log(logfile, msg, color=None):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(_paint(color, line) if color else line)
    _append(logfile, line + "\n")


def _add_command_output(logfile, heading, cmd):
    _append(logfile, "\n===== %s =====\n" % heading)
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace"
        )
        out = r.stdout or ""
        if r.stderr:
            out += ("\n" + r.stderr) if out else r.stderr
        _append(logfile, _clean(out).rstrip() + "\n")
    except Exception as e:
        _append(logfile, "ERROR: %s\n" % e)


def _save_diagnostics(logfile, reason):
    _log(logfile, "Saving diagnostics: %s" % reason)
    _append(logfile, "\n===== DIAGNOSTIC SNAPSHOT: %s =====\n" % reason)
    _append(logfile, "Time: %s\n" % datetime.now())
    _add_command_output(logfile, "Ollama version", ["ollama", "-v"])
    _add_command_output(logfile, "ollama ps", ["ollama", "ps"])

    if os.name == "nt":
        _add_command_output(logfile, "Ollama processes", [
            "powershell", "-NoProfile", "-Command",
            "Get-Process ollama*, llama-server -ErrorAction SilentlyContinue "
            "| Select-Object Id, ProcessName, CPU, StartTime, WorkingSet64 "
            "| Format-List"
        ])
        _add_command_output(logfile, "llama-server command lines", [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter 'Name LIKE \"llama-server%\"' "
            "-ErrorAction SilentlyContinue | Select-Object ProcessId, CommandLine "
            "| Format-List"
        ])
    else:
        _add_command_output(logfile, "ollama/llama processes",
                            ["ps", "-eo", "pid,pcpu,rss,comm,args"])

    _add_command_output(logfile, "nvidia-smi", ["nvidia-smi"])


def _compare_versions(a, b):
    """Return -1 if a < b, 0 if equal, 1 if a > b (numeric component-wise)."""
    sa = [int(x) for x in re.findall(r"\d+", a)]
    sb = [int(x) for x in re.findall(r"\d+", b)]
    if not sa or not sb:
        return 0
    n = max(len(sa), len(sb))
    for i in range(n):
        x = sa[i] if i < len(sa) else 0
        y = sb[i] if i < len(sb) else 0
        if x < y:
            return -1
        if x > y:
            return 1
    return 0


def _check_ollama_update(logfile):
    installed = None
    try:
        r = subprocess.run(["ollama", "-v"], capture_output=True, text=True)
        raw = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"(\d+\.\d+\.\d+)", raw)
        if m:
            installed = m.group(1)
        else:
            _log(logfile, "Version check: could not determine installed ollama version.")
            return
    except Exception:
        _log(logfile, "Version check: could not run ollama -v.")
        return

    latest = None
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/ollama/ollama/releases/latest",
            headers={"User-Agent": "ollama.py"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        tag = data.get("tag_name")
        if tag:
            latest = re.sub(r"^[vV]", "", tag)
    except Exception:
        latest = None

    if not latest:
        _log(logfile, "Version check: could not reach GitHub (offline?) - installed v%s." % installed)
        return

    if _compare_versions(installed, latest) < 0:
        _log(logfile, "Version check: UPDATE AVAILABLE - installed v%s, latest v%s." % (installed, latest))
        _log(logfile, "   Update from https://ollama.com/download (or: winget upgrade Ollama.Ollama), then restart Ollama.")
    else:
        _log(logfile, "Version check: ollama v%s is up to date (latest v%s)." % (installed, latest))


def _test_inference(model, url, window, logfile):
    """Stream a tiny request; first content/thinking token => healthy."""
    base = url if "://" in url else "http://" + url
    scheme, hostport = base.split("://", 1)
    hostport = hostport.rstrip("/") or "localhost"
    if ":" in hostport:
        host, _, port = hostport.partition(":")
        try:
            port = int(port)
        except ValueError:
            port = 443 if scheme == "https" else 80
    else:
        host = hostport
        port = 443 if scheme == "https" else 80

    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly OK."}],
        "stream": True,
        "think": False,  # tell Qwen-class thinking models to skip reasoning
        "options": {"num_predict": 32},
    }

    token_count = 0
    thinking_count = 0
    answer = ""
    first_token = None
    error_text = None
    start = time.monotonic()
    conn = None

    try:
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = cls(host, port, timeout=window)
        conn.request("POST", "/api/chat",
                     body=json.dumps(body).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("error"):
                error_text = str(obj["error"])
                break
            msg = obj.get("message") or {}
            content = msg.get("content")
            thinking = msg.get("thinking")
            if content is not None:
                token_count += 1
                answer += content
                if first_token is None:
                    first_token = time.monotonic() - start
            if thinking:
                thinking_count += 1
                if first_token is None:
                    first_token = time.monotonic() - start
            if first_token is not None or obj.get("done"):
                break
    except (socket.timeout, TimeoutError, ConnectionError, OSError):
        # watchdog fired: no token within the window => runner likely wedged
        pass
    except Exception as e:
        _log(logfile, "Inference test FAILED (probe error): %s" % e, "red")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if first_token is not None:
        sec = round(first_token, 1)
        if re.search(r"\bOK\b", answer):
            _log(logfile, "Inference test PASSED - first token at %ss, answered 'OK'." % sec, "green")
        else:
            _log(logfile, "Inference test PASSED - first token at %ss; visible answer: '%s'" % (sec, answer), "green")
            if answer == "":
                _log(logfile, "   (tokens went to thinking - normal for Qwen thinking models)")
        return True

    if error_text is not None:
        _log(logfile, "Inference test FAILED - server error: %s" % error_text, "red")
        return False

    _log(logfile, "Inference test FAILED - no token produced within %ss (runner likely wedged)." % window, "red")
    return False


def _model_loaded(model):
    try:
        r = subprocess.run(["ollama", "ps"], capture_output=True, text=True)
        return (r.stdout or "").find(model) != -1
    except Exception:
        return False


def _list_llama_servers():
    """Return [(pid, cmdline), ...] for running llama-server processes."""
    ps = ("Get-CimInstance Win32_Process -Filter 'Name LIKE \"llama-server%\"' "
          "-ErrorAction SilentlyContinue | "
          "ForEach-Object { \"$($_.ProcessId)\t$($_.CommandLine)\" }")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True
        )
    except Exception:
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0].strip().isdigit():
            out.append((int(parts[0]), parts[1]))
    return out


def _find_model_runner(model, logfile):
    """Find only the llama-server process(es) serving our model."""
    runners = _list_llama_servers()
    if len(runners) == 0:
        return []
    if len(runners) == 1:
        return runners

    # Multiple runners: identify ours via the model manifest's layer digest.
    # "ollama ps" ID is the manifest hash; the runner's --model blob digest
    # lives inside the manifest JSON.
    digest = None
    root = os.path.join(os.path.expanduser("~"), ".ollama", "models", "manifests")
    family = model.split(":")[0]
    if os.path.isdir(root):
        for dirpath, _dirs, filenames in os.walk(root):
            for fn in filenames:
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        raw = f.read()
                except OSError:
                    continue
                if family not in raw:
                    continue
                m = re.search(r'"digest"\s*:\s*"sha256:([0-9a-f]{8,})"', raw)
                if m:
                    digest = m.group(1)
                    break
            if digest:
                break

    if not digest:
        _log(logfile, "Multiple llama-server processes and no usable manifest for %s. Not killing anything." % model)
        return []

    matched = []
    for pid, cmd in runners:
        hm = re.search(r"[0-9a-f]{40,}", cmd)
        if hm and digest.startswith(hm.group(0)):
            matched.append((pid, cmd))

    if not matched:
        _log(logfile, "Multiple llama-server processes; none matched %s's manifest digest. Not killing anything." % model)
        return []
    return matched


def _kill_pid(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)])
    else:
        import signal
        os.kill(pid, signal.SIGKILL)


def _loaded_models():
    """Return model names currently loaded, per 'ollama ps'."""
    try:
        r = subprocess.run(["ollama", "ps"], capture_output=True, text=True)
    except Exception:
        return []
    lines = (r.stdout or "").strip().splitlines()
    if len(lines) <= 1:
        return []
    return [parts[0] for parts in (line.split() for line in lines[1:]) if parts]


def _ask_recovery(model):
    """Consent prompt. Non-interactive stdin (EOF) => decline."""
    try:
        resp = input(
            "\nRecover %s? (graceful stop, then a targeted kill of its\n"
            "llama-server if it is still stuck) [y/N]: " % model
        ).strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        return False
    return resp in ("y", "yes")


def cmd_doctor(args):
    model = args.model
    if not model:
        # Default: whichever model is currently loaded (the likely stuck one).
        loaded = _loaded_models()
        if len(loaded) == 1:
            model = loaded[0]
        elif loaded:
            die("multiple models loaded - specify one: " + ", ".join(loaded))
        else:
            die("no model given and nothing is loaded (see 'ollama ps')")

    url = args.url
    window = args.first_token_window
    stop_wait = args.stop_wait

    log_dir = args.log_dir or os.path.dirname(os.path.abspath(__file__))
    if args.log_dir and args.log_dir not in (".", ""):
        os.makedirs(log_dir, exist_ok=True)
    logfile = os.path.join(
        log_dir, "ollama_doctor_%s.log" % datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    )

    _log(logfile, "=== Ollama Recovery Started ===")
    _log(logfile, "Model: %s" % model)
    _log(logfile, "Log: %s" % logfile)

    if not args.no_update_check:
        _check_ollama_update(logfile)

    _save_diagnostics(logfile, "Initial state")

    # If real inference works, DO NOT TOUCH ANYTHING.
    if _test_inference(model, url, window, logfile):
        _log(logfile, "Ollama is healthy (probe saw live generation). No recovery action required.", "green")
        _log(logfile, "=== Finished -- no changes made ===")
        return 0

    _save_diagnostics(logfile, "Inference failed BEFORE recovery")

    if args.check_only:
        _log(logfile, "Stuck. --check-only given: no recovery attempted.", "red")
        print("\n" + _paint("red", "Stuck. No recovery attempted (--check-only)."))
        print("Re-run without -n (and with -y if you don't want to be asked).")
        return 3

    if not args.yes and not _ask_recovery(model):
        _log(logfile, "Recovery declined by user - no changes made.")
        print("OK - no changes made.")
        return 3

    # Graceful recovery first.
    _log(logfile, "Inference is not working. Attempting graceful model stop...")
    try:
        r = subprocess.run(
            ["ollama", "stop", model],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        _append(logfile, _clean(r.stdout or "").rstrip() + "\n")
    except Exception as e:
        _log(logfile, "ollama stop produced an error: %s" % e)

    _log(logfile, "Waiting up to %s seconds for model to unload..." % stop_wait)
    still_loaded = True
    for i in range(1, stop_wait + 1):
        time.sleep(1)
        if not _model_loaded(model):
            _log(logfile, "Model unloaded after %ss." % i)
            still_loaded = False
            break

    if still_loaded:
        _log(logfile, "Model remains loaded/stuck after ollama stop. Escalating to process kill.")
        _save_diagnostics(logfile, "Model stuck after ollama stop")
        runners = _find_model_runner(model, logfile)
        if not runners:
            _log(logfile, "No safe llama-server process to kill (model already gone or ambiguous).")
            _log(logfile, "If the app is still hung, restart Ollama from the tray / Task Manager.")
        else:
            for pid, cmd in runners:
                _log(logfile, "Killing stuck llama-server PID %s..." % pid)
                _log(logfile, "   cmd: %s" % cmd)
                try:
                    _kill_pid(pid)
                    _log(logfile, "Killed llama-server PID %s." % pid)
                except Exception as e:
                    _log(logfile, "FAILED to kill PID %s: %s" % (pid, e))
            time.sleep(5)
    else:
        _log(logfile, "Model unloaded normally. No process kill required.")

    _save_diagnostics(logfile, "After recovery action")

    # Final inference test.
    _log(logfile, "Performing final inference test (fresh runner may need to cold-load)...")
    if _test_inference(model, url, window, logfile):
        _save_diagnostics(logfile, "Final healthy state")
        _log(logfile, "=== RECOVERY SUCCESSFUL ===", "green")
        _log(logfile, "Ollama is generating normally again.")
        return 0
    else:
        _save_diagnostics(logfile, "Recovery failed")
        _log(logfile, "=== RECOVERY FAILED ===", "red")
        _log(logfile, "Ollama still cannot complete inference.")
        _log(logfile, "Review log: %s" % logfile)
        _log(logfile, "A full Ollama restart (stop the ollama process in Task Manager, relaunch) or a Windows reboot may be required.")
        return 1


# -----------------------------
# Commands
# -----------------------------

def cmd_ps(args):
    passthrough("ps", args.args)


def cmd_show(args):
    passthrough("show", args.args)


def cmd_pull(args):
    passthrough("pull", args.args)


def cmd_list(args):
    patterns = args.patterns or ["*"]
    matched = match_models(patterns)

    if not matched:
        print("No models matched.")
        return

    print(f"{'MODEL':30} {'SIZE':>8}  {'MODIFIED':18}  TYPE")
    print("-" * 70)

    for name, info in matched.items():
        kind = classify_model(name)
        print(
            f"{name:30} "
            f"{info['size']:>8}  "
            f"{info['modified']:18}  "
            f"{kind}"
        )


def cmd_create(args):
    if not os.path.isfile(args.modelfile):
        die(f"Modelfile not found: {args.modelfile}")

    base_models = match_models([args.model])

    if not base_models:
        die(f"No base model matched: {args.model}")

    if len(base_models) > 1:
        die(
            "Base model wildcard matched multiple models:\n  "
            + "\n  ".join(base_models)
        )

    base_model = next(iter(base_models))


    if ":" in base_model:
        name, tag = base_model.split(":", 1)
        final_model = f"{name}-{args.newmodel}:{tag}"
    else:
        final_model = f"{base_model}-{args.newmodel}"

    print(f"\nCreating model:")
    print(f"  Base model : {base_model}")
    print(f"  New model  : {final_model}")
    print(f"  Modelfile  : {args.modelfile}")

    with open(args.modelfile, "r", encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = []
    from_found = False

    for line in lines:
        if line.strip().upper().startswith("FROM "):
            new_lines.append(f"FROM {base_model}\n")
            from_found = True
        else:
            new_lines.append(line)

    if not from_found:
        die("Modelfile has no FROM line to replace")

    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, encoding="utf-8", suffix=".txt"
    ) as tmp:
        tmp.write("".join(new_lines))
        temp_modelfile = tmp.name

    try:
        run(["ollama", "create", final_model, "-f", temp_modelfile],
            capture=False)
    finally:
        os.unlink(temp_modelfile)

    print(f"\nSuccessfully created model: {final_model}")


def cmd_rm(args):
    matched = match_models(args.patterns)
    if not matched:
        print("No models matched.")
        return

    confirm("REMOVE", matched)

    for m in matched:
        print(f"Removing {m}")
        run(["ollama", "rm", m], capture=False)


def cmd_stop(args):
    matched = match_models(args.patterns)
    if not matched:
        print("No models matched.")
        return

    for m in matched:
        print(f"Stopping {m}")
        run(["ollama", "stop", m], capture=False)


def get_version():
    """Read the '# v...' line from our own header, if present."""
    try:
        with open(__file__, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("# v"):
                    return line.strip().lstrip("# ").strip()
    except OSError:
        pass
    return "unknown"


def _keepawake_available():
    return (os.name == "nt")


def _hold_awake(on):
    """Hold/clear the Windows 'system required' stay-awake flag (kernel32).

    ES_CONTINUOUS | ES_SYSTEM_REQUIRED while Ollama is active keeps the
    machine from idle-sleeping; clearing it hands control back to the
    normal idle-sleep policy.
    """
    import ctypes
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    try:
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(
            ctypes.c_uint32(flags)))
    except Exception:
        return False


def _ps_model_names(url, timeout=3):
    """Return the names of models currently loaded (empty = Ollama idle/down)."""
    base = url if "://" in url else "http://" + url
    p = urllib.parse.urlparse(base)
    if not p.hostname:
        return []
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        conn = http.client.HTTPConnection(p.hostname, port, timeout=timeout)
        conn.request("GET", "/api/ps")
        r = conn.getresponse()
        data = json.loads(r.read().decode("utf-8", "replace"))
        conn.close()
        return [m.get("name") for m in data
                if isinstance(m, dict) and m.get("name")]
    except Exception:
        return []


def cmd_keepawake(args):
    if not _keepawake_available():
        die("keepawake uses the Windows power-request API (Windows only)")

    interval = max(2, int(args.interval))
    url = args.url
    held = False
    print("ollama keepawake: holding the system awake while Ollama has "
          "models loaded - %s (poll every %ss). Ctrl-C to stop." % (url, interval))
    try:
        while True:
            models = _ps_model_names(url)
            if models and not held:
                if _hold_awake(True):
                    held = True
                    print("[awake] models loaded: %s - stay-awake held"
                          % ", ".join(models))
            elif not models and held:
                _hold_awake(False)
                held = False
                print("[idle]  no models loaded - stay-awake released, "
                      "idle-sleep policy applies again")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopping ollama keepawake...")
    finally:
        _hold_awake(False)
    return 0


#
# set_sleep: one-shot Windows setup for "don't sleep while Ollama is working"
# Best-effort and non-destructive: anything that would need admin rights is
# refused and reported with the exact command to run elevated, never silently
# skipped. Declining one question never touches the others.
#

SLEEPTASK_NAME = "OllamaKeepAwake"
DEFAULT_IDLE_HOURS = 2


def _spawn(cmd, timeout=30):
    """Run a command without die(); return (rc, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except Exception as e:
        return -1, "", str(e)


def _fmt_power_secs(secs):
    if secs is None:
        return "unknown"
    if secs == 0:
        return "never"
    if secs % 3600 == 0:
        h = secs // 3600
        return "%d hour%s" % (h, "" if h == 1 else "s")
    if secs % 60 == 0:
        m = secs // 60
        return "%d minute%s" % (m, "" if m == 1 else "s")
    return "%d seconds" % secs


def _secs_from_setting(text):
    """'0x00003840' or '14400' -> int seconds (None on junk)."""
    t = (text or "").strip().lower().rstrip(".")
    if t.startswith("0x"):
        try:
            return int(t[2:] or "0", 16)
        except ValueError:
            return None
    try:
        return int(t)
    except ValueError:
        return None


def _parse_hours(value):
    """Parse a user duration into seconds (0 = never).

    Bare numbers are hours ('2', '0.5'); '30m'/'30min' is minutes;
    '90s' is seconds; 'never'/'inf'/'0' is never.
    """
    v = str(value).strip().lower()
    if v in ("never", "inf", "infinity"):
        return 0
    m = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(h(?:ours?)?|m(?:in(?:utes?)?)?|s(?:ec(?:onds?)?)?)?",
        v)
    if not m:
        die("bad duration '%s' (use hours like 2, minutes like 30m, or 'never')"
            % value)
    n = float(m.group(1))
    unit = (m.group(2) or "h")[0]
    if unit == "m":
        return int(n * 60)
    if unit == "s":
        return int(n)
    return int(n * 3600)


def _standby_limits():
    """Current standby-after-idle for AC and DC (seconds, None on failure)."""
    rc, out, err = _spawn(["powercfg", "/query", "SCHEME_CURRENT",
                           "SUB_SLEEP", "STANDBYIDLE"])
    if rc != 0:
        return None, None
    ac = dc = None
    for line in ((out or "") + "\n" + (err or "")).splitlines():
        s = line.strip()
        m = re.match(r"Current AC Power Setting Index:\s*(.+)$", s)
        if m:
            ac = _secs_from_setting(m.group(1))
        m = re.match(r"Current DC Power Setting Index:\s*(.+)$", s)
        if m:
            dc = _secs_from_setting(m.group(1))
    return ac, dc


def _set_standby(ac_secs, dc_secs):
    """Set standby-after-idle (0 = never). Returns per-source results."""
    results = []
    for label, secs in (("AC", ac_secs), ("DC", dc_secs)):
        if secs is None:
            continue
        minutes = "0" if secs == 0 else str(max(1, int(round(secs / 60.0))))
        which = "standby-timeout-ac" if label == "AC" else "standby-timeout-dc"
        rc, out, err = _spawn(["powercfg", "/change", which, minutes])
        results.append((label, rc == 0, (err or out).strip()))
    return results


def _sleeptask_state():
    """State of the OllamaKeepAwake scheduled task, or None if absent."""
    rc, out, err = _spawn(["schtasks", "/query", "/tn", SLEEPTASK_NAME,
                           "/fo", "LIST"])
    if rc != 0:
        return None
    low = (out or "") + (err or "")
    return {"exists": True, "enabled": "disabled" not in low.lower()}


def _ensure_sleeptask():
    """Create/refresh the OllamaKeepAwake ONLOGON task pointing at this script.

    Returns (ok, detail, elevated_cmd).
    """
    exe = None
    for cand in ("pyw", "pythonw", "python"):
        exe = shutil.which(cand)
        if exe:
            break
    if not exe:
        return False, "no python interpreter on PATH for the task action", None
    script = os.path.abspath(__file__)
    action = '"%s" "%s" keepawake' % (exe, script)
    cmd = ["schtasks", "/Create", "/F", "/SC", "ONLOGON",
           "/TN", SLEEPTASK_NAME, "/TR", action]
    rc, out, err = _spawn(cmd)
    if rc != 0:
        line = " ".join('"%s"' % c if (" " in c or '"' in c) else c for c in cmd)
        return False, (err or out).strip() or "schtasks failed (rc=%s)" % rc, line
    st = _sleeptask_state()
    if not st:
        return True, "created (but could not re-query the task)", None
    if not st["enabled"]:
        r2, o2, e2 = _spawn(["schtasks", "/Change", "/TN", SLEEPTASK_NAME,
                             "/ENABLE"])
        if r2 != 0:
            return False, "created but could not enable: " + (e2 or o2).strip(), \
                "schtasks /Change /TN %s /ENABLE" % SLEEPTASK_NAME
    return True, action, None


def _active_nics():
    """InterfaceDescriptions of up, physical, non-loopback, non-vEthernet
    adapters.  The description is the identifier that both
    'powercfg /devicequery wake_armed' and the PnP device node use, so
    matching against wake state works (adapter *names* e.g. 'Ethernet'
    do not match)."""
    ps = ("Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and "
          "$_.InterfaceCategory -ne 'Loopback' -and $_.Name -notlike 'vEthernet*' } "
          "| Where-Object { $_.InterfaceDescription } "
          "| Select-Object -ExpandProperty InterfaceDescription")
    rc, out, err = _spawn(["powershell", "-NoProfile", "-Command", ps],
                          timeout=60)
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def _wol_armed_text():
    """'powercfg /devicequery wake_armed' output ('' if unavailable)."""
    rc, out, err = _spawn(["powercfg", "/devicequery", "wake_armed"])
    if rc != 0:
        return ""
    return (out or "").strip()


def _wol_enable(nic):
    """Best-effort wake-enable for one NIC (may need admin).

    Returns (ok, detail, elevated_cmd).
    """
    dev = nic
    ps = ("Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -eq 'Net' } "
          "| ForEach-Object { \"$($_.Name)|$($_.DeviceID)\" }")
    rc, out, err = _spawn(["powershell", "-NoProfile", "-Command", ps],
                          timeout=60)
    if rc == 0:
        for line in out.splitlines():
            name, _, did = line.strip().partition("|")
            if name.lower() == nic.lower() and did:
                dev = did
                break
    rc2, o2, e2 = _spawn(["powercfg", "/deviceenablewake", dev])
    detail = (e2 or o2).strip()
    return rc2 == 0, detail, 'powercfg /deviceenablewake "%s"' % dev


def _wol_notes():
    lines = [
        "Wake-on-LAN notes (needed for it to actually work):",
        "  * After a WoL wake, Ollama is NOT running unless it auto-starts.",
        "    Start it at boot (Ollama app: Settings -> Start when running on",
        "    system startup, or a startup scheduled task) or LAN requests",
        "    will have nothing to talk to.",
        "  * Ollama listens on 127.0.0.1 by default. To serve the LAN it must",
        "    run with  OLLAMA_HOST=0.0.0.0  - then port 11434 is reachable by",
        "    EVERY device on your network with NO authentication. Only do this",
        "    on trusted networks; this tool never sets that for you.",
        "  * If a NIC is missing from the wake-enabled list, enable it in",
        "    Device Manager -> adapter -> Properties -> Power Management ->",
        "    'Allow this device to wake the computer', and check your BIOS/UEFI",
        "    for a Wake-on-LAN / deep-sleep option.",
    ]
    return "\n".join(lines)


def cmd_set_sleep(args):
    if os.name != "nt":
        die("set_sleep is Windows-only (it uses powercfg and schtasks)")

    ac_now, dc_now = _standby_limits()
    task = _sleeptask_state()
    nics = _active_nics()
    armed_raw = _wol_armed_text()
    armed = armed_raw.lower()
    armed_nics = [n for n in nics if armed and n.lower() in armed]

    # ---- check-only: report state, touch nothing -------------------------
    if args.check_only:
        def state(bad, text):
            return _paint("red", text) if bad else _paint("green", text)
        print("ollama set_sleep --check-only (nothing will be changed)")
        print()
        print("  Sleep after idle (AC):      %s" % _fmt_power_secs(ac_now))
        print("  Sleep after idle (DC):      %s" % _fmt_power_secs(dc_now))
        if task:
            print("  Keepawake logon task:     %s" %
                  state(not task["enabled"], SLEEPTASK_NAME +
                        (" (disabled!)" if not task["enabled"] else " - enabled")))
        else:
            print("  Keepawake logon task:     %s (run 'ollama set_sleep' to create)"
                  % state(True, "missing"))
        for n in nics:
            mark = state(n not in armed_nics, n + (" - not wake-enabled"
                        if n not in armed_nics else " - wake-enabled"))
            print("  Wake-on-LAN NIC:          %s" % mark)
        if not nics:
            print("  Wake-on-LAN NIC:          %s" % state(True,
                  "no active physical NIC found"))
        if armed_raw:
            print()
            print("  Devices currently allowed to wake the PC:")
            for line in armed_raw.splitlines():
                if line.strip():
                    print("   - " + line.strip())
        print()
        print("To apply a setup interactively:  ollama set_sleep")
        print("Non-interactive, e.g. task only:  ollama set_sleep --task-only")
        return 0

    # ---- resolve intent ---------------------------------------------------
    interactive = not (args.yes or args.task_only
                       or args.ac is not None or args.dc is not None)

    ac_target = _parse_hours(args.ac) if args.ac is not None else None
    dc_target = _parse_hours(args.dc) if args.dc is not None else None
    do_task = bool(args.task_only)
    do_wol = False

    if args.yes:
        do_task = do_task or not args.no_task
        do_wol = not (args.no_wol or args.task_only)
        if ac_target is None and not args.no_power:
            ac_target = DEFAULT_IDLE_HOURS * 3600
        if dc_target is None and not args.no_power:
            dc_target = DEFAULT_IDLE_HOURS * 3600

    if interactive:
        print("ollama set_sleep - configure sleep / keepawake / wake-on-LAN")
        print("(declining a question never undoes the others; Enter = default)")
        print()

        def ask(prompt, default_yes):
            try:
                resp = input(prompt).strip().lower()
            except (EOFError, KeyboardInterrupt, OSError):
                return None  # no tty / declined
            if resp == "":
                return default_yes
            return resp in ("y", "yes")

        if args.no_task:
            do_task = False
            print("Q1: skipped (--no-task)")
        else:
            do_task = ask(
                "Q1: Create a logon task that keeps the PC awake while Ollama\n"
                "    has models loaded (releases as soon as they unload)? [Y/n] ",
                True) or False
        if args.no_wol:
            do_wol = False
            print("Q2: skipped (--no-wol)")
        elif nics:
            do_wol = ask(
                "Q2: %s are active. Ensure wake-on-LAN is enabled on them\n"
                "    (best-effort; admin steps are printed, never applied\n"
                "    silently)? [y/N] " % ", ".join(nics), False) or False
        else:
            print("Q2: skipped - no active physical NIC found (wake-on-LAN)")
        if args.no_power:
            ac_target = dc_target = None
            print("Q3: skipped (--no-power)")
        else:
            h_raw = None
            try:
                h_raw = input("Q3: Hours of inactivity before the PC may sleep?\n"
                              "    Enter = %d, 'never' = never, or a number\n"
                              "    like 2 (also '30m' for minutes): "
                              % DEFAULT_IDLE_HOURS)
            except (EOFError, KeyboardInterrupt, OSError):
                h_raw = None
            if h_raw is not None:
                h = h_raw.strip().lower()
                if h == "":
                    ac_target = dc_target = DEFAULT_IDLE_HOURS * 3600
                elif h in ("never", "inf", "infinity"):
                    ac_target = dc_target = 0
                else:
                    try:
                        secs = _parse_hours(h)
                    except SystemExit:
                        secs = None
                    if secs is None:
                        print(_paint("red",
                             "  bad value - skipping the sleep-time change"))
                    else:
                        ac_target = dc_target = secs

    # ---- build the visible plan -------------------------------------------
    plan = []  # (line, changed)

    if ac_target is not None and ac_target != ac_now:
        plan.append((_paint("red", "Sleep after idle (AC):  %s" % _fmt_power_secs(ac_now))
                    + "  ->  " + _paint("green", _fmt_power_secs(ac_target)), True))
    if dc_target is not None and dc_target != dc_now:
        plan.append((_paint("red", "Sleep after idle (DC):  %s" % _fmt_power_secs(dc_now))
                    + "  ->  " + _paint("green", _fmt_power_secs(dc_target)), True))
    if do_task and not task:
        plan.append((_paint("yellow", "Keepawake task:       missing  ->  will create %s (ONLOGON)"
                           % SLEEPTASK_NAME), True))
    elif do_task and task and not task["enabled"]:
        plan.append((_paint("yellow", "Keepawake task:       disabled  ->  will enable"), True))
    elif do_task and task:
        plan.append((_paint("green", "Keepawake task:       already present (action will be refreshed)"), False))
    if do_wol:
        if not nics:
            plan.append((_paint("yellow", "Wake-on-LAN:          no active NIC found - will print steps only"), True))
        for n in nics:
            if n in armed_nics:
                plan.append((_paint("green", "Wake-on-LAN %-20.20s : already wake-enabled (no change)" % n),
                             False))
            else:
                plan.append((_paint("yellow", "Wake-on-LAN %-20.20s : not wake-enabled  ->  will attempt" % n),
                             True))

    will_apply = (do_task or do_wol or ac_target is not None or dc_target is not None)

    if not will_apply:
        print("Nothing to do - nothing will be changed.")
        if not (task or nics):
            print("(also: no keepawake task and no active NIC were found; "
                  "see --check-only)")
        return 0

    print()
    print(" Planned changes:")
    for line, _ in plan:
        print("  " + line)
    # lines that are explicitly no-change (still worth showing as context)
    if ac_target is not None and ac_target == ac_now:
        print("  " + _paint("green", "Sleep after idle (AC):  %s (unchanged)"
              % _fmt_power_secs(ac_now)))
    if dc_target is not None and dc_target == dc_now:
        print("  " + _paint("green", "Sleep after idle (DC):  %s (unchanged)"
              % _fmt_power_secs(dc_now)))

    # ---- final confirm (interactive only; explicit flags apply directly) --
    if interactive:
        print()
        try:
            resp = input("Apply these changes? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt, OSError):
            print("Declined (no confirmation). Nothing was changed.")
            return 3
        if resp not in ("y", "yes"):
            print("Declined. Nothing was changed.")
            return 3

    # ---- apply --------------------------------------------------------------
    errors = 0

    if do_task:
        ok, detail, elevated = _ensure_sleeptask()
        if ok:
            print(_paint("green", "  task: " + detail))
        else:
            errors += 1
            print(_paint("red", "  task: FAILED: " + detail))
            print("         Easiest fix - re-run in an ELEVATED prompt:\n"
                  "          python \"%s\" set_sleep --task-only\n"
                  "         (or create the task directly:\n          %s)"
                  % (os.path.abspath(__file__), elevated))

    if do_wol:
        if nics:
            for n in nics:
                if n in armed_nics:
                    print(_paint("green", "  wol:  %s already wake-enabled" % n))
                    continue
                ok, detail, elevated = _wol_enable(n)
                if ok:
                    print(_paint("green", "  wol:  %s wake-enabled" % n))
                else:
                    errors += 1
                    msg = " (needs admin?)" if ("access" in detail.lower()
                                                or "denied" in detail.lower()
                                                or "privileg" in detail.lower()) else ""
                    print(_paint("red", "  wol:  %s FAILED%s: %s" % (n, msg, detail)))
                    print("         Run this in an ELEVATED prompt:\n          " + elevated)
        else:
            print(_paint("yellow", "  wol:  no active NIC; nothing to enable"))
        print()
        print(_wol_notes())

    if ac_target is not None or dc_target is not None:
        for label, ok, detail in _set_standby(ac_target, dc_target):
            if ok:
                print(_paint("green", "  sleep(%s): now %s" %
                             (label, _fmt_power_secs(
                                 ac_target if label == "AC" else dc_target))))
            else:
                errors += 1
                print(_paint("red", "  sleep(%s): FAILED: %s" % (label, detail)))

    # ---- verify ------------------------------------------------------------
    print()
    ac_ver, dc_ver = _standby_limits()
    task_ver = _sleeptask_state()
    print(" Now:")
    print("  Sleep after idle:  AC %s | DC %s" %
          (_fmt_power_secs(ac_ver), _fmt_power_secs(dc_ver)))
    if task_ver:
        print("  Keepawake task:    %s %s" %
              (SLEEPTASK_NAME, "enabled" if task_ver["enabled"] else "DISABLED"))
    else:
        print("  Keepawake task:    missing")
    if do_wol and nics:
        print("  Wake test (optional):  schtasks /run /tn %s   "
              "(starts keepawake now)" % SLEEPTASK_NAME)

    if errors:
        print(_paint("red", "\nDone with %d failure(s) - see above for "
              "elevated one-liners." % errors))
        return 1
    print(_paint("green", "\nDone - all requested changes applied."))
    return 0


def cmd_install(args):
    """
    Install this script (and the Windows shim) into a bin directory
    that is (expected to be) on PATH.

    Default target:
      - Windows : %USERPROFILE%\\bin
      - Unix    : ~/.local/bin
    """
    here = os.path.dirname(os.path.abspath(__file__))

    if args.target:
        target = os.path.abspath(os.path.expanduser(args.target))
    else:
        target = (
            os.path.join(os.path.expanduser("~"), "bin")
            if os.name == "nt"
            else os.path.join(os.path.expanduser("~"), ".local", "bin")
        )

    version = get_version()

    files = ["ollama.py"] + (["ollama.cmd"] if os.name == "nt" else [])

    os.makedirs(target, exist_ok=True)

    installed_any = False

    for name in files:
        src = os.path.join(here, name)
        if not os.path.isfile(src):
            die(f"{name} not found next to this script: {src}")
        dst = os.path.join(target, name)

        # Running from the target dir? Nothing to install - a file that our
        # own process has open cannot be overwritten on Windows anyway.
        if os.path.exists(dst) and os.path.samefile(src, dst):
            print(f"Up to date (script is already running from {dst})")
            continue

        try:
            shutil.copy2(src, dst)
        except (PermissionError, BlockingIOError):
            die(
                f"Could not overwrite {dst} - it is in use by another process.\n"
                "    Close any terminal that is running it, then retry."
            )
        if os.name != "nt":
            os.chmod(dst, 0o755)
        print(f"Installed {dst}")
        installed_any = True

    # PATH sanity check
    path_dirs = [
        d for d in os.environ.get("PATH", "").split(os.pathsep) if d
    ]
    if any(os.path.normcase(d) == os.path.normcase(target) for d in path_dirs):
        print(f"\n{target} is on PATH - you can now run: ollama.py list <pattern>")
    else:
        print(f"\nWARNING: {target} is NOT on PATH.")
        print("Add it to your PATH, then use: ollama.py list <pattern>")
    print(f"\nInstalled version: {version}")


def rate_response(prompt_text, response_text, judge_model, verbose=False):
    """
    Rates a model response using a judge model.

    The evaluation rubric is embedded INSIDE the prompt_text under
    'POST-GENERATION EVALUATION CRITERIA'. The judge is explicitly instructed
    to use ONLY that rubric.

    Returns:
        dict of scores

    Raises:
        RuntimeError if no valid JSON can be extracted
    """

    judge_input = f"""
You are evaluating a model-generated response.

The ORIGINAL PROMPT below CONTAINS the evaluation rubric under the section:
"POST-GENERATION EVALUATION CRITERIA".

You MUST:
- Use ONLY the rubric defined inside the prompt
- Apply the numeric scales exactly as written
- Ignore all external or implied evaluation standards
- Not invent new criteria
- Not include explanations, thoughts, or commentary

--- PROMPT ---
{prompt_text}

--- MODEL RESPONSE ---
{response_text}

Scoring guidance:
- Scores of 5 should be rare and reserved for exceptional alignment with the rubric
- Scores of 3 represent competent, acceptable, but unremarkable performance
- Scores of 1 should be used only for clear failure
- Most responses should fall between 2 and 4
- Use the full range of the scale when appropriate

Return ONLY valid JSON in this exact format.
The FIRST character of your response MUST be '{{'
and the LAST character MUST be '}}'.

{{
  "accuracy_of_movement": number,
  "believability": number,
  "sensuality": number,
  "vividness": number,
  "consistency": number,
  "notes": "brief strengths and weaknesses"
}}
"""

    if verbose:
        print(f"Rating using judge model: {judge_model}", file=sys.stderr)

    proc = subprocess.run(
        ["ollama", "run", judge_model],
        input=judge_input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True
    )

    raw = (proc.stdout or "").strip()

    # --- defensive JSON extraction ---
    start = raw.find("{")
    end = raw.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(
            "Judge model did not return JSON.\n"
            "Raw output was:\n"
            f"{raw}"
        )

    json_text = raw[start:end + 1]

    try:
        scores = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "Judge model returned malformed JSON.\n"
            "Extracted JSON was:\n"
            f"{json_text}"
        ) from e

    return scores



def cmd_test(args):
    # --- pre-flight ---
    if not os.path.isfile(args.prompt):
        die(f"Prompt file not found: {args.prompt}")

    use_modelfile = bool(args.modelfile)

    if use_modelfile and not os.path.isfile(args.modelfile):
        die(f"Modelfile not found: {args.modelfile}")

    # --- expand model patterns ---
    patterns = [p.strip() for p in args.model.split(",") if p.strip()]
    base_models = match_models(patterns)

    if not base_models:
        die(f"No base models matched: {args.model}")

    print(
        f"\n=== Testing base models: {', '.join(base_models)} ===",
        file=sys.stderr
    )

    had_errors = False

    # --- read modelfile once if needed ---
    if use_modelfile:
        with open(args.modelfile, "r", encoding="utf-8") as f:
            original_lines = f.readlines()

    for base_model in base_models:
        print(f"\n--- {base_model} ---", file=sys.stderr)

        temp_model = base_model
        temp_modelfile = None
        out = None

        try:
            # --- create temp model if modelfile supplied ---
            if use_modelfile:
                pid = os.getpid()
                safe_model = base_model.replace(":", "_")
                temp_model = f"__test-{safe_model}-{pid}"

                # rewrite FROM
                from_found = False
                new_lines = []

                for line in original_lines:
                    if line.strip().upper().startswith("FROM "):
                        new_lines.append(f"FROM {base_model}\n")
                        from_found = True
                    else:
                        new_lines.append(line)

                if not from_found:
                    raise RuntimeError("Modelfile has no FROM line to replace")

                with tempfile.NamedTemporaryFile(
                    mode="w",
                    delete=False,
                    encoding="utf-8",
                    suffix=".txt"
                ) as tmp:
                    tmp.write("".join(new_lines))
                    temp_modelfile = tmp.name
                    
                if args.verbose:
                    print("Creating model", temp_model, "...", file=sys.stderr)
                    
                run(
                    ["ollama", "create", temp_model, "-f", temp_modelfile],
                    capture=False
                )

            # --- open output ---
            if args.output:
                model_safe = base_model.replace(":", "_")
                prompt_base = os.path.splitext(os.path.basename(args.prompt))[0]
                mode = "mod" if args.modelfile else "base"
                outfile = f"{model_safe}_{mode}_{prompt_base}_response.txt"

                out = open(outfile, "w", encoding="utf-8")

                # --- write header FIRST ---
                from datetime import datetime
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                out.write("# ollama.py test\n")
                out.write(f"# model: {base_model}\n")
                out.write(f"# prompt: {args.prompt}\n")
                if args.modelfile:
                    out.write(f"# modelfile: {args.modelfile}\n")
                else:
                    out.write("# modelfile: (none)\n")
                out.write(f"# timestamp: {ts}\n")
                out.write("#\n")
                out.flush()   # ensure header is written before model output
                
                if args.verbose:
                    print(f"Writing output to: {outfile}", file=sys.stderr)
            else:
                out = sys.stdout

            # --- ensure clean slate ---
            stop_running_models(verbose=args.verbose)

            # --- run prompt ---
            with open(args.prompt, "r", encoding="utf-8") as p:
                response_proc = subprocess.run(
                    ["ollama", "run", temp_model],
                    stdin=p,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=True
                )

            response_text = response_proc.stdout

            # --- rate the response ---
            scores = None

            if args.rate:
                try:
                    with open(args.prompt, "r", encoding="utf-8") as pf:
                        prompt_text = pf.read()

                    scores = rate_response(
                        prompt_text=prompt_text,
                        response_text=response_text,
                        judge_model=args.judge,
                        verbose=args.verbose
                    )
                except Exception as e:
                    print(f"! Rating failed: {e}", file=sys.stderr)
                    scores = None


            # --- write scores BEFORE model output ---
            if scores:
                print("\n=== RATING ===", file=sys.stderr)
                for k, v in scores.items():
                    print(f"{k}: {v}", file=sys.stderr)
                print("==============\n", file=sys.stderr)
    

            # --- NOW write model output ---
            out.write(response_text)
            out.flush()


            print(f"✓ {base_model} completed", file=sys.stderr)

        except Exception as e:
            had_errors = True
            print(f"✗ ERROR testing {base_model}: {e}", file=sys.stderr)

        finally:
            # --- cleanup running models ---
            stop_running_models(verbose=args.verbose)
            
            if out and out is not sys.stdout:
                out.close()

            if use_modelfile:
                subprocess.run(
                    ["ollama", "stop", temp_model],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                subprocess.run(
                    ["ollama", "rm", temp_model],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

                if temp_modelfile and os.path.exists(temp_modelfile):
                    os.unlink(temp_modelfile)

    if had_errors:
        sys.exit(1)



# -----------------------------
# ctx: context-length introspection & switching (128k <-> 256k)
# -----------------------------
#
# Usable context is capped by the LOWEST adjustable layer:
#   1. the model's native max     (fixed in the GGUF: <arch>.context_length)
#   2. the Ollama runner size      (OLLAMA_CONTEXT_LENGTH on 'ollama serve')
#   3. the client prompt window    (e.g. OpenCode limit.context - not Ollama's)
#
# 'ctx get' and 'ctx kv' are read-only: they read (1) straight from the GGUF,
# report (2) from the live server, and estimate KV-cache memory so switching to
# a bigger window is a considered choice. 'ctx set' persists (2) for future
# 'ollama serve' processes and, with --restart, swaps the live one (Windows).
# It asks before changing anything and degrades to printing the exact command.

_CTX_URL = "http://localhost:11434"


def _ollama_models_root():
    return os.path.join(os.path.expanduser("~"), ".ollama", "models")


def _model_blob_paths(model):
    """Best-effort GGUF blob paths for 'model', in manifest order."""
    fam = model.split(":", 1)[0]
    tag = model.split(":", 1)[1] if ":" in model else "latest"
    base = _ollama_models_root()
    mroot = os.path.join(base, "manifests")
    broot = os.path.join(base, "blobs")
    ordered = []
    if os.path.isdir(mroot):
        for dirpath, _dirs, files in os.walk(mroot):
            for fn in files:
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        raw = f.read()
                except OSError:
                    continue
                if fam not in raw:
                    continue
                if tag != "latest" and tag not in raw and tag not in fn \
                        and tag not in os.path.basename(dirpath):
                    continue
                for d in re.findall(r"sha256:([0-9a-f]{8,})", raw):
                    if d not in ordered:
                        ordered.append(d)
    out = []
    for d in ordered:
        p = os.path.join(broot, "sha256-" + d)
        if os.path.isfile(p):
            out.append(p)
    return out


def _all_gguf_blobs():
    """Fallback: every blob whose file begins with the GGUF magic."""
    broot = os.path.join(_ollama_models_root(), "blobs")
    out = []
    if not os.path.isdir(broot):
        return out
    for fn in os.listdir(broot):
        p = os.path.join(broot, fn)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as f:
                if f.read(4) == b"GGUF":
                    out.append(p)
        except OSError:
            continue
    return out


def _gguf_read_value(data, off, t):
    """Decode one GGUF KV value at 'off' with type code 't' (or None)."""
    if t == 8:  # string: u64 length, then bytes
        if off + 8 > len(data):
            return None
        ln = struct.unpack_from("<Q", data, off)[0]
        if ln and off + 8 + ln > len(data):
            return None
        return data[off + 8:off + 8 + ln].decode("utf-8", "replace")
    widths = {0: 4, 1: 4, 2: 4, 4: 4, 5: 4, 6: 4,
              7: 8, 10: 8, 11: 8, 12: 8, 3: 1}
    w = widths.get(t)
    if w is None or off + w > len(data):
        return None
    if w == 1:
        return bool(data[off])
    fmt = "<I" if w == 4 else "<Q"
    return struct.unpack_from(fmt, data, off)[0]


def _gguf_scan(data, full_key):
    """Find 'full_key' in the KV table and return its value (or None).
    The u64 key-length stored just before the key is checked, so a prefix
    match (head_count vs head_count_kv) is rejected."""
    key = full_key.encode("utf-8")
    n = len(key)
    i = data.find(key)
    while i >= 0:
        if i >= 8:
            try:
                klen = struct.unpack_from("<Q", data, i - 8)[0]
                if klen == n:
                    t = struct.unpack_from("<I", data, i + n)[0]
                    return _gguf_read_value(data, i + n + 4, t)
            except struct.error:
                pass
        i = data.find(key, i + 1)
    return None


_CTX_KEYS = (
    "context_length",
    "block_count",
    "embedding_length",
    "attention.head_count",
    "attention.head_count_kv",
    "attention.key_length",
    "attention.value_length",
    "full_attention_interval",
)


def _probe_gguf_metadata(data):
    arch = _gguf_scan(data, "general.architecture")
    if not arch or not isinstance(arch, str):
        return None, {}
    meta = {}
    for name in _CTX_KEYS:
        v = _gguf_scan(data, arch + "." + name)
        if v is not None:
            meta[name] = v
    return arch, meta


def _gguf_meta(model):
    """Read the context-relevant GGUF metadata for a model (dict or None)."""
    blobs = _model_blob_paths(model)
    if not blobs:
        blobs = _all_gguf_blobs()
    for path in blobs:
        try:
            with open(path, "rb") as f:
                if f.read(4) != b"GGUF":
                    continue
            with open(path, "rb") as f:
                data = f.read(128 << 20)  # metadata lives near the front
        except OSError:
            continue
        arch, meta = _probe_gguf_metadata(data)
        if arch and meta.get("context_length") is not None:
            meta["arch"] = arch
            meta["blob"] = path
            return meta
    return None


def _kv_estimate(meta):
    """Estimate KV bytes/token from GGUF meta. -> (bpt, detail) or (None, why)."""
    layers = meta.get("block_count")
    kv_heads = meta.get("attention.head_count_kv")
    head_dim = meta.get("attention.key_length") or meta.get("attention.value_length")
    if not layers or not kv_heads or not head_dim:
        return None, "GGUF lacked block_count / kv-heads / head-dim"
    interval = meta.get("full_attention_interval") or 0
    if interval and int(interval) > 1:
        full = int(layers) // int(interval)
        kind = "hybrid (full-attn every %d layers)" % int(interval)
    else:
        full = int(layers)
        kind = "full attention"
    bpt = 2 * full * int(kv_heads) * int(head_dim) * 2  # K+V, f16
    return bpt, ("%d full-attn layers x %d kv-heads x %d dim x f16, %s"
                 % (full, int(kv_heads), int(head_dim), kind))


def _human(nbytes):
    f = float(nbytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if f < 1024.0 or unit == "PiB":
            return ("%d B" % int(f)) if unit == "B" else ("%.1f %s" % (f, unit))
        f /= 1024.0
    return "%.1f PiB" % f


def _ctx_f(n):
    if n is None:
        return "(none / not reported)"
    return "%d  (%dk)" % (n, n // 1024)


def _free_ram_kb():
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_OperatingSystem "
                  "| ForEach-Object { [int64]$_.FreePhysicalMemory }")
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True)
            for line in (r.stdout or "").strip().splitlines():
                if line.strip().isdigit():
                    return int(line.strip())
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1])
    except Exception:
        return None
    return None


def _api_json(url, path):
    base = url if "://" in url else "http://" + url
    full = base.rstrip("/") + path
    try:
        req = urllib.request.Request(full, headers={"User-Agent": "ollama.py"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _server_ctx(url, model):
    data = _api_json(url, "/api/ps")
    if not data or not model:
        return None
    for m in data.get("models", []):
        if (m.get("name") or m.get("model")) == model:
            v = m.get("context_length")
            return int(v) if v is not None else None
    return None


def _runner_ctx(runners):
    for _pid, cmdline in runners:
        m = re.search(r"(?:^|\s)(?:-c|--context)(?:=|\s+)(\d+)", cmdline or "")
        if m:
            return int(m.group(1))
    return None


def _resolve_ctx_model(model):
    if model:
        return model
    loaded = _loaded_models()
    if len(loaded) == 1:
        return loaded[0]
    if loaded:
        die("multiple models loaded - specify which one: " + ", ".join(loaded))
    die("no model given and nothing is loaded (see 'ollama ps')")


def cmd_ctx_help(_args):
    print("Context-length tools for Ollama (128k <-> 256k). Use one of:")
    print("  ollama.py ctx get [MODEL]          native max vs current context")
    print("  ollama.py ctx kv  [N] [MODEL]      KV-cache memory for N tokens")
    print("  ollama.py ctx set N [MODEL]        persist OLLAMA_CONTEXT_LENGTH"
          " (+ optional restart)")
    print("  ollama.py ctx restart              re-apply the persisted context (kills tray app + serve, [Windows])")
    print("Run 'ollama.py ctx <get|kv|set|restart> --help' for details.")
    return 0


def cmd_ctx_get(args):
    model = _resolve_ctx_model(args.model)
    url = args.url
    meta = _gguf_meta(model)
    native = meta.get("context_length") if meta else None

    server = _server_ctx(url, model)
    runners = _list_llama_servers()
    runner = _runner_ctx(runners) if runners else None
    env = os.environ.get("OLLAMA_CONTEXT_LENGTH")

    if native is not None:
        nat = _paint("green", "%d  (%dk)" % (native, native // 1024))
    else:
        nat = _paint("yellow", "(GGUF metadata not found locally)")
    print("=== Context: %s ===" % model)
    print("  %-28s %s" % ("model native max (GGUF)", nat))
    print("  %-28s %s" % ("server context (/api/ps)", _ctx_f(server)))
    print("  %-28s %s" % ("runner -c (this host)", _ctx_f(runner)))
    print("  %-28s %s" % ("OLLAMA_CONTEXT_LENGTH", (env if env else "(unset)")))
    print("-" * 56)
    if meta:
        bpt, detail = _kv_estimate(meta)
        if bpt:
            print("  %-28s %s" % ("KV per token (est.)", _human(bpt) + "/token"))
            print("  %-28s %s" % ("", detail))
    print()
    print("Usable context = the LOWEST of: server context, the client's own")
    print("prompt window (invisible to Ollama), and the model native max.")
    if native is not None and env:
        try:
            if int(env) > int(native):
                print(_paint("yellow",
                             "  Warning: OLLAMA_CONTEXT_LENGTH=%s exceeds the "
                             "native max %s - the extra tokens won't be usable."
                             % (env, native)))
        except ValueError:
            pass
    return 0


def cmd_ctx_kv(args):
    model = _resolve_ctx_model(args.model)
    meta = _gguf_meta(model)
    if not meta:
        die("could not read GGUF metadata for %s (model not found locally?)" % model)
    bpt, detail = _kv_estimate(meta)
    if not bpt:
        die(detail)
    native = meta.get("context_length")
    n = args.n if args.n is not None else (native or 131072)
    if n <= 0:
        die("token count must be positive (you gave %d)" % n)

    print("=== KV-cache estimate: %s ===" % model)
    print("  per token (est.)   : %s   (%s)" % (_human(bpt), detail))
    print("  %10d tokens : %s   (KV only)" % (n, _human(bpt * n)))
    if native and native != n:
        print("  %10d tokens : %s   (KV only, native max)"
              % (native, _human(bpt * native)))
    free = _free_ram_kb()
    if free is not None:
        need = bpt * n
        head = free * 1024 - need
        if head >= 0:
            print(_paint("green",
                         "  free RAM %s -> ~%s headroom for a %d-token KV cache"
                         % (_human(free * 1024), _human(head), n)))
        else:
            print(_paint("red",
                         "  free RAM %s -> a %d-token KV cache needs ~%s more"
                         % (_human(free * 1024), n, _human(-head))))
    print("  (Already-resident SSM/linear-attention state is not counted here.)")
    return 0


def _confirm_change(why):
    print(why)
    try:
        resp = input("Proceed? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        print("(non-interactive: declined)")
        return False
    return resp in ("y", "yes")


def _setx(n):
    try:
        r = subprocess.run(["setx", "OLLAMA_CONTEXT_LENGTH", str(n)],
                           capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        return False


def _hostport(url):
    base = url if "://" in url else "http://" + url
    scheme, _, rest = base.partition("://")
    hostport = rest.rstrip("/")
    default = 443 if scheme == "https" else 80
    if ":" in hostport:
        host, _, port = hostport.partition(":")
        try:
            return host, int(port)
        except ValueError:
            return host, default
    return hostport, default


def _port_listener_pid(port):
    if os.name != "nt":
        return None
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
    except Exception:
        return None
    want = ":%d" % port
    for line in (r.stdout or "").splitlines():
        p = line.split()
        if len(p) >= 5 and p[3] == "LISTENING" and p[1].endswith(want):
            try:
                return int(p[4])
            except ValueError:
                return None
    return None


def _wait_server(url, port, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _api_json(url, "/api/version"):
            return True
        time.sleep(1)
    return False


def _serve_env(n):
    """Env for a fresh 'ollama serve': the inherited env + the context target,
    plus the LAN binding (OLLAMA_HOST) the tray app was providing - so bypassing
    the app doesn't silently drop the server back to 127.0.0.1."""
    env = dict(os.environ)
    env["OLLAMA_CONTEXT_LENGTH"] = str(n)
    env.setdefault("OLLAMA_HOST", "http://0.0.0.0:11434")
    return env


def _kill_image(image):
    """taskkill by image name (Windows-only; silent on any failure)."""
    if os.name != "nt":
        return
    try:
        subprocess.run(["taskkill", "/F", "/IM", image],
                       capture_output=True, text=True)
    except Exception:
        pass


def _real_ollama_bin():
    """Absolute path to the REAL 'ollama' server binary (Windows: 'ollama.exe').

    Resolving the exact binary matters: on this machine a convenience shim named
    'ollama.cmd' (which invokes ollama.py) shadows 'ollama' earlier in PATH, so a
    bare "ollama serve" would hit the Python tool, not the server. Prefer the
    standard per-user install; otherwise take the first PATH entry that is a real
    .exe; otherwise fall back to 'ollama'."""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            cand = os.path.join(local, "Programs", "Ollama", "ollama.exe")
            if os.path.isfile(cand):
                return cand
        for d in os.environ.get("PATH", "").split(os.pathsep):
            d = d.strip().strip('"')
            if not d:
                continue
            cand = os.path.join(d, "ollama.exe")
            if os.path.isfile(cand):
                return cand
        return "ollama.exe"
    return "ollama"


def _launch_serve(env):
    """Detached, windowless 'ollama serve'. Returns True on success.

    Uses the resolved real binary (_real_ollama_bin) so the shim named
    'ollama.cmd' (which runs ollama.py) is never mistaken for the server."""
    binpath = _real_ollama_bin()
    try:
        flags = 0x00000800 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
        subprocess.Popen([binpath, "serve"], env=env, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True)
        return True
    except Exception as e:
        print(_paint("red", "    relaunch failed (%s) - start Ollama yourself." % e))
        return False


def _restart_serve(n, url):
    """Best-effort swap of the live Ollama for a fresh 'ollama serve' that
    carries OLLAMA_CONTEXT_LENGTH=n. Stops the tray app FIRST (so it cannot
    respawn a 128-k server on the same port), then the runner(s) and the
    port listener, then launches a clean serve. Degrades to manual steps on
    any failure."""
    _host, port = _hostport(url)
    print(_paint("yellow", "  restarting Ollama - this disconnects live sessions ..."))
    _kill_image("ollama app.exe")    # stop the tray app before it can re-spawn 128k
    _kill_image("llama-server.exe")  # release the runner(s) + their VRAM
    listener = _port_listener_pid(port)
    if listener is not None:
        _kill_pid(listener)          # stop the 'ollama serve' on the port
        time.sleep(2)
    else:
        print("    (nothing was listening on %d; starting a fresh serve)" % port)
    if not _launch_serve(_serve_env(n)):
        return
    print("    waiting for Ollama to come back ...")
    if _wait_server(url, port, timeout=30):
        print(_paint("green", "    Ollama is back. The new serve uses context %d." % n))
        print("    Verify:  ollama.py ctx get   (runner re-inits on next model load)")
    else:
        print(_paint("yellow", "    Ollama did not answer in time - check it started."))
        print("    If it's down, start it (tray app, or 'ollama serve') to apply %d." % n)


def cmd_ctx_set(args):
    n = int(args.n)
    if n <= 0:
        die("context must be positive (you gave %d)" % n)
    model = args.model
    meta = _gguf_meta(model) if model else None
    native = meta.get("context_length") if meta else None
    env_now = os.environ.get("OLLAMA_CONTEXT_LENGTH")

    if os.name != "nt":
        print("=== ctx set %d%s ===" % (n, ("  (model: %s)" % model) if model else ""))
        if native is not None:
            print("    native max: %d" % native)
            if n > native:
                print(_paint("yellow", "    note: %d exceeds the native max %d - "
                                       "extra tokens won't be usable" % (n, native)))
        print(_paint("yellow", "  Non-Windows: set OLLAMA_CONTEXT_LENGTH in Ollama's "
                               "service env, then restart it:"))
        print("    export OLLAMA_CONTEXT_LENGTH=%d" % n)
        print("    systemctl --user restart ollama")
        return 0

    url = args.url
    cur = _server_ctx(url, model) if model else None
    print("=== ctx set %d%s ===" % (n, ("  (model: %s)" % model) if model else ""))
    if native is not None:
        if n > native:
            print(_paint("yellow", "    note: %d exceeds the native max %d "
                                   "(extra tokens won't be usable)" % (n, native)))
        print("    native max: %d" % native)
    if cur is not None:
        print("    server now: %d" % cur)
    print("    OLLAMA_CONTEXT_LENGTH (this shell): %s" % (env_now or "(unset)"))
    print("    setx applies to FUTURE 'ollama serve' processes only.")
    print()

    why = "Persist OLLAMA_CONTEXT_LENGTH=%d" % n
    if args.restart:
        why += " and restart the live Ollama now (live sessions will drop)"
    else:
        why += " (restart Ollama yourself afterwards to apply)"
    if not args.yes and not _confirm_change(why):
        print("Aborted (no change made).")
        return 0

    if not _setx(n):
        die("setx failed - set OLLAMA_CONTEXT_LENGTH=%d manually, then restart Ollama" % n)
    print(_paint("green", "    persisted OLLAMA_CONTEXT_LENGTH=%d (setx)" % n))

    if args.restart:
        _restart_serve(n, url)
    else:
        print()
        print(_paint("yellow", "  To apply, restart Ollama:"))
        print("    quit the tray app and relaunch it, OR stop 'ollama serve' "
              "and re-run it.")
        print("    Verify from a NEW session:  ollama.py ctx get%s"
              % (" %s" % model if model else ""))
    return 0


def _persisted_ctx():
    """Read the persisted OLLAMA_CONTEXT_LENGTH (setx / HKCU\\Environment),
    falling back to the current shell env, so 'restart' re-applies whatever the
    user last chose. Defaults to 262144 (256-k)."""
    val = None
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["reg", "query", r"HKCU\Environment", "/v", "OLLAMA_CONTEXT_LENGTH"],
                capture_output=True, text=True)
            for line in (r.stdout or "").splitlines():
                for tok in line.split():
                    if tok.isdigit():
                        val = tok
        except Exception:
            pass
    if val is None:
        val = os.environ.get("OLLAMA_CONTEXT_LENGTH")
    if val is not None and str(val).isdigit():
        return int(val)
    return 262144


def cmd_ctx_restart(args):
    if os.name != "nt":
        print("ctx restart is a Windows convenience (it kills 'ollama app' + the")
        print("live 'ollama serve'). On other systems: stop Ollama, set")
        print("OLLAMA_CONTEXT_LENGTH, then start it again.")
        return 0
    n = _persisted_ctx()
    url = args.url
    print("=== ctx restart ===")
    print("    re-apply OLLAMA_CONTEXT_LENGTH=%d" % n)
    print("    (kills the tray app + the live 'ollama serve', then starts a fresh one)")
    why = ("Re-apply OLLAMA_CONTEXT_LENGTH=%d - this restarts the live Ollama "
           "and DROPS live sessions.") % n
    if not args.yes and not _confirm_change(why):
        print("Aborted (no change made).")
        return 0
    _restart_serve(n, url)
    return 0


# -----------------------------
# Main
# -----------------------------

def main():
    if not ollama_exists():
        die("ollama executable not found in PATH")


    parser = argparse.ArgumentParser(
        description="Enhanced Ollama model manager"
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output"
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"ollama.py {get_version()} [{os.path.abspath(__file__)}]",
        help="Show version and path, then exit"
    )
    
    sub = parser.add_subparsers(dest="cmd")

    p_ps = sub.add_parser(
        "ps",
        help="Passthrough to ollama ps",
        usage="ollama.py ps"
    )
    p_ps.add_argument("args", nargs=argparse.REMAINDER)
    p_ps.set_defaults(func=cmd_ps)

    p_show = sub.add_parser(
        "show",
        help="Passthrough to ollama show",
        usage="ollama.py show MODEL"
    )
    p_show.add_argument("args", nargs=argparse.REMAINDER)
    p_show.set_defaults(func=cmd_show)

    p_pull = sub.add_parser(
        "pull",
        help="Passthrough to ollama pull",
        usage="ollama.py pull MODEL"
    )
    p_pull.add_argument("args", nargs=argparse.REMAINDER)
    p_pull.set_defaults(func=cmd_pull)

    p_list = sub.add_parser("list",
        help="List models (supports wildcards)",
        usage="ollama.py list [PATTERN ...]"
    )
    p_list.add_argument("patterns", nargs="*",
        help='wildcard list to filter against (quote it, e.g. "gemma*", '
             'or your shell — zsh in particular — expands it first)')
    p_list.set_defaults(func=cmd_list)

    p_create = sub.add_parser("create",
        help="Create model from modelfile",
        usage="ollama.py create NEWMODEL -f MODELFILE -m MODEL"
    )
    p_create.add_argument("newmodel")
    p_create.add_argument("-f", "--modelfile", required=True)
    p_create.add_argument("-m", "--model", required=True)
    p_create.set_defaults(func=cmd_create)

    p_rm = sub.add_parser("rm",
        help="Remove models (supports wildcards)",
        usage="ollama.py rm PATTERN [PATTERN ...]"
    )
    p_rm.add_argument("patterns", nargs="+",
        help='wildcard pattern(s) (quote them, e.g. "gemma*", or your '
             'shell — zsh in particular — expands them first)')
    p_rm.set_defaults(func=cmd_rm)

    p_stop = sub.add_parser("stop",
        help="Stop running models (supports wildcards)",
        usage="ollama.py stop PATTERN [PATTERN ...]"
    )
    p_stop.add_argument("patterns", nargs="+",
        help='wildcard pattern(s) (quote them, e.g. "gemma*", or your '
             'shell — zsh in particular — expands them first)')
    p_stop.set_defaults(func=cmd_stop)

    p_install = sub.add_parser("install",
        help="Install into a bin dir (default: ~/bin or ~/.local/bin)",
        usage="ollama.py install [TARGET_DIR]"
    )
    p_install.add_argument("target", nargs="?", default=None,
        help="Target bin directory (default: ~/bin on Windows, ~/.local/bin elsewhere)")
    p_install.set_defaults(func=cmd_install)

    p_doctor = sub.add_parser("doctor",
        help="Probe inference; recover a stuck runner (asks before acting)",
        usage="ollama.py doctor [MODEL] [--url URL] [--first-token-window N] [--stop-wait N] [-y|-n]"
    )
    p_doctor.add_argument("model", nargs="?", default=None,
                       help="Model to probe/recover (default: whichever model is currently loaded, per 'ollama ps')")
    p_doctor.add_argument("--url", default="http://localhost:11434",
                       help="Ollama server URL (default: http://localhost:11434)")
    p_doctor.add_argument("--first-token-window", type=int, default=20, dest="first_token_window",
                       help="Seconds to wait for a first token before treating "
                            "the runner as wedged (default: 20)")
    p_doctor.add_argument("--stop-wait", type=int, default=20, dest="stop_wait",
                       help="Seconds to wait for a graceful unload (default: 20)")
    p_doctor.add_argument("--no-update-check", action="store_true", dest="no_update_check",
                       help="Skip the online Ollama latest-version check")
    p_doctor.add_argument("--log-dir", default=None,
                       help="Where to write the log (default: next to this script)")
    p_doctor.add_argument("-y", "--yes", action="store_true",
                       help="Answer 'yes' to the recovery prompt automatically")
    p_doctor.add_argument("-n", "--check-only", action="store_true", dest="check_only",
                       help="Diagnose only; never attempt recovery")
    p_doctor.set_defaults(func=cmd_doctor)

    p_ka = sub.add_parser("keepawake",
        help="Windows: hold the sleep timer while Ollama has models loaded",
        usage="ollama.py keepawake [--url URL] [--interval N]")
    p_ka.add_argument("--url", default="http://localhost:11434",
                       help="Ollama server URL (default: http://localhost:11434)")
    p_ka.add_argument("--interval", type=int, default=10,
                       help="Seconds between /api/ps polls (default: 10)")
    p_ka.set_defaults(func=cmd_keepawake)

    p_sleep = sub.add_parser("set_sleep",
        help="Windows: one-shot sleep/keepawake/wake-on-LAN setup "
             "(interactive, or use the flags below)",
        usage="ollama.py set_sleep [--ac H] [--dc H] [--task-only] "
              "[--check-only] [-y] [--no-task] [--no-wol] [--no-power]"
    )
    p_sleep.add_argument("--ac", default=None,
                        help="Sleep after this much inactivity on AC power "
                             "(e.g. 2, 30m, 'never'; omit = leave unchanged)")
    p_sleep.add_argument("--dc", default=None,
                        help="Sleep after this much inactivity on battery "
                             "(e.g. 2, 30m, 'never'; omit = leave unchanged)")
    p_sleep.add_argument("--task-only", action="store_true",
                        help="Only ensure the keepawake logon task exists; "
                             "no wake-on-LAN, no sleep-timeout change")
    p_sleep.add_argument("-y", "--yes", action="store_true",
                        help="Non-interactive: create the keepawake task, "
                             "enable wake-on-LAN, and set a 2h idle timeout; "
                             "negate with --no-task, --no-wol or --no-power")
    p_sleep.add_argument("--check-only", action="store_true",
                        help="Report the current sleep/task/wake-on-LAN "
                             "state and touch nothing")
    p_sleep.add_argument("--no-task", action="store_true",
                        help="Skip the keepawake logon task question (with -y)")
    p_sleep.add_argument("--no-wol", action="store_true",
                        help="Skip wake-on-LAN (with -y)")
    p_sleep.add_argument("--no-power", action="store_true",
                        help="Skip the sleep-timeout change (with -y)")
    p_sleep.set_defaults(func=cmd_set_sleep)

    p_test = sub.add_parser("test",
        help="Create temp model, run test prompt, capture output",
        usage="ollama.py test -m MODEL -f MODELFILE -p PROMPT [-o]"
    )
    p_test.add_argument("-m", "--model", required=True)
    p_test.add_argument(
        "-f", "--modelfile",
        help="Optional modelfile to apply (if omitted, run directly on base model)"
    )

    p_test.add_argument("-p", "--prompt", required=True)
    p_test.add_argument(
        "-o", "--output",
        action="store_true",
        help="Write output to auto-named response file"
    )


#
    p_test.add_argument(
        "--rate",
        action="store_true",
        help="Rate model output using judge model"
    )

    p_test.add_argument(
        "--judge",
        default="qwen3-unbound:30b",
        help="Judge model to use for rating (default: qwen3-unbound:30b)"
    )

#

    p_test.set_defaults(func=cmd_test)

    p_ctx = sub.add_parser("ctx",
        help="Context length: inspect (get/kv), switch (set), or re-apply (restart)",
        usage="ollama.py ctx <get|kv|set|restart> [args] [--url URL]"
    )
    p_ctx.set_defaults(func=cmd_ctx_help)
    ctx_sub = p_ctx.add_subparsers(dest="ctx_cmd", metavar="{get,kv,set,restart}")

    p_ctx_get = ctx_sub.add_parser("get",
        help="Read-only: native max vs current context",
        usage="ollama.py ctx get [MODEL] [--url URL]"
    )
    p_ctx_get.add_argument("model", nargs="?", default=None,
        help="Model to inspect (default: the loaded one, per 'ollama ps')")
    p_ctx_get.add_argument("--url", default=_CTX_URL,
        help="Ollama server URL (default: %s)" % _CTX_URL)
    p_ctx_get.set_defaults(func=cmd_ctx_get)

    p_ctx_kv = ctx_sub.add_parser("kv",
        help="Read-only: KV-cache memory cost for N tokens",
        usage="ollama.py ctx kv [N] [MODEL] [--url URL]"
    )
    p_ctx_kv.add_argument("n", nargs="?", type=int, default=None,
        help="Number of tokens to size for (default: the model's native max)")
    p_ctx_kv.add_argument("model", nargs="?", default=None,
        help="Model to inspect (default: the loaded one, per 'ollama ps')")
    p_ctx_kv.add_argument("--url", default=_CTX_URL)
    p_ctx_kv.set_defaults(func=cmd_ctx_kv)

    p_ctx_set = ctx_sub.add_parser("set",
        help="Persist OLLAMA_CONTEXT_LENGTH & optionally restart Ollama",
        usage="ollama.py ctx set N [MODEL] [--url URL] [--restart] [-y]"
    )
    p_ctx_set.add_argument("n",
        help="Target context length in tokens (e.g. 131072 or 262144)")
    p_ctx_set.add_argument("model", nargs="?", default=None,
        help="Optional model to check the native max against")
    p_ctx_set.add_argument("--url", default=_CTX_URL)
    p_ctx_set.add_argument("--restart", action="store_true",
        help="(Windows) also restart the live Ollama serve now (drops live sessions)")
    p_ctx_set.add_argument("-y", "--yes", action="store_true",
        help="Skip the consent prompt")
    p_ctx_set.set_defaults(func=cmd_ctx_set)

    p_ctx_restart = ctx_sub.add_parser("restart",
        help="(Windows) re-apply the persisted context: kills tray app + serve, starts fresh",
        usage="ollama.py ctx restart [--url URL] [-y]"
    )
    p_ctx_restart.add_argument("--url", default=_CTX_URL,
        help="Ollama server URL (default: %s)" % _CTX_URL)
    p_ctx_restart.add_argument("-y", "--yes", action="store_true",
        help="Skip the consent prompt")
    p_ctx_restart.set_defaults(func=cmd_ctx_restart)

    args = parser.parse_args()

    # No subcommand: show help (exit quietly) instead of an argparse error.
    if not args.cmd:
        print(f"ollama.py {get_version()} [{os.path.abspath(__file__)}]")
        parser.print_help()
        return

    # Show which version/copy is running (stderr, so output stays clean).
    print(f"ollama.py {get_version()} [{os.path.abspath(__file__)}]",
          file=sys.stderr)
    rc = args.func(args)

    # Windows: if this was launched through the .py file association
    # (double-click, or bare "ollama.py" from a shell), the console window
    # closes the instant we exit. The ollama.cmd shim sets OLLAMA_INLINE;
    # if it's not set, give the user a chance to actually read the output.
    if os.name == "nt" and os.environ.get("OLLAMA_INLINE") != "1":
        print("\n(Tip: this window closes when this program exits. To keep output", file=sys.stderr)
        print("     in your terminal, run it inline, e.g. 'python ollama.py ...' or use the", file=sys.stderr)
        print("     ollama.cmd shim next to this file.)", file=sys.stderr)
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt, OSError):
            pass

    if rc:
        sys.exit(rc)


if __name__ == "__main__":
    main()
