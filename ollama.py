#!/usr/bin/env python3

# v0.9.5
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


def _log(logfile, msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
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
        _log(logfile, "Inference test FAILED (probe error): %s" % e)
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
            _log(logfile, "Inference test PASSED - first token at %ss, answered 'OK'." % sec)
        else:
            _log(logfile, "Inference test PASSED - first token at %ss; visible answer: '%s'" % (sec, answer))
            if answer == "":
                _log(logfile, "   (tokens went to thinking - normal for Qwen thinking models)")
        return True

    if error_text is not None:
        _log(logfile, "Inference test FAILED - server error: %s" % error_text)
        return False

    _log(logfile, "Inference test FAILED - no token produced within %ss (runner likely wedged)." % window)
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
        _log(logfile, "Ollama is healthy (probe saw live generation). No recovery action required.")
        _log(logfile, "=== Finished -- no changes made ===")
        return 0

    _save_diagnostics(logfile, "Inference failed BEFORE recovery")

    if args.check_only:
        _log(logfile, "Stuck. --check-only given: no recovery attempted.")
        print("\nStuck. No recovery attempted (--check-only).")
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
        _log(logfile, "=== RECOVERY SUCCESSFUL ===")
        _log(logfile, "Ollama is generating normally again.")
        return 0
    else:
        _save_diagnostics(logfile, "Recovery failed")
        _log(logfile, "=== RECOVERY FAILED ===")
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
    p_list.add_argument("patterns", nargs="*")
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
    p_rm.add_argument("patterns", nargs="+")
    p_rm.set_defaults(func=cmd_rm)

    p_stop = sub.add_parser("stop",
        help="Stop running models (supports wildcards)",
        usage="ollama.py stop PATTERN [PATTERN ...]"
    )
    p_stop.add_argument("patterns", nargs="+")
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
