#!/usr/bin/env python3
# Single-container supervisor: API, web, and independent periodic collectors.
# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
from contextlib import ExitStack, suppress
import os
import signal
import subprocess
import sys
import threading
import time

APP = "/app"
VENV_PYTHON = "/opt/venv/bin/python"
CONFIG = os.environ.get("TRACKER_CONFIG", "/config/tracker.toml")
DB = os.environ.get("TRACKER_DB", "/data/state/tracker.sqlite3")
WEB_PORT = os.environ.get("PORT", "8080")
API_PORT = os.environ.get("API_PORT", "18731")

sys.path.insert(0, f"{APP}/backend")
from tracker.monitors import runner as monitor
from tracker.monitors.build import obs
from tracker.monitors.github import model as github
from tracker.monitors.source import git as spec_git
from tracker.monitors.version import nvchecker as nv
from tracker.runtime_checks import runtime_session

_stop = threading.Event()
_procs = {}
_procs_lock = threading.Lock()


def log(message):
    print(f"[entrypoint] {message}", flush=True)


def run_child(name, cmd, *, cwd, env=None, timeout=None):
    """Track services AND collectors so stop also terminates git/nvchecker children."""
    with _procs_lock:
        if _stop.is_set():
            return None
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, start_new_session=True)
        _procs[name] = proc
    try:
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"{name}: timeout after {timeout}s; terminating process group")
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # The grace period expired; kill the process group below.
                pass
            # Also remove descendants if their parent exited before they did.
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return 124
    finally:
        with _procs_lock:
            _procs.pop(name, None)


def run_collector(phase, extra=()):
    # Exit 2 (stale observations) and 75 (already running) are recorded, not fatal.
    cmd = [VENV_PYTHON, "-m", "tracker.collector", "--only", phase,
           "--config", CONFIG, "--db", DB, *extra]
    if phase == "upstreams":
        cmd.append("--due")
    result = run_child(f"collect-{phase}", cmd, cwd=f"{APP}/backend")
    log(f"collect {phase}: exit {result}")
    return result


def periodic(phase, schedule):
    failures = 0
    while not _stop.is_set():
        try:
            # Keep the configured bounded OBS batch, including on restart. An existing
            # snapshot must not trigger another expensive full source fill at every boot.
            result = run_collector(phase)
        except OSError as error:
            log(f"collect {phase}: {type(error).__name__}: {error}")
            result = 1
        failures = 0 if result in (0, 75) else failures + 1
        _stop.wait(schedule.delay(failures))


def specs(spec):
    # Full initial cloning can be slow or fail offline: never block the API/web or
    # the other collectors. Retry initialization separately; later fetches are owned
    # by the SPEC collector and run outside the snapshot writer lock.
    env = {**os.environ, "SPEC_REPO_DIR": spec["repo"],
           "SPEC_REPO_URL": spec["url"], "SPEC_REPO_BRANCH": spec["branch"]}
    while not _stop.is_set():
        try:
            result = run_child("spec-init", ["sh", f"{APP}/deploy/init-spec-repo.sh"],
                               cwd=APP, env=env, timeout=spec["fetch_timeout_seconds"])
            log(f"SPEC init: exit {result}")
            if result == 0:
                periodic("specs", spec_git.polling(spec))
                return
        except OSError as error:
            log(f"SPEC init: {type(error).__name__}: {error}")
        _stop.wait(60)


def service(name, cmd, env, cwd):
    while not _stop.is_set():
        try:
            result = run_child(name, cmd, env=env, cwd=cwd)
            log(f"{name}: exit {result}")
        except OSError as error:
            log(f"{name}: {type(error).__name__}: {error}")
        if not _stop.is_set():
            log(f"{name}: restarting in 3s")
            _stop.wait(3)


def shutdown(*_):
    # Signal handlers must not acquire locks that an interrupted thread may hold.
    _stop.set()


def terminate_children():
    with _procs_lock:
        processes = list(_procs.values())
    for proc in processes:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    for proc in processes:
        try:
            proc.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    with ExitStack() as stack:
        try:
            config = stack.enter_context(runtime_session(CONFIG, DB))
            policies = {**obs.polling(config), "upstreams": nv.polling(config)}
            if github.settings(config).repositories:
                policies['github'] = github.polling(config, authenticated=bool(os.environ.get('LOOKOUT_GITHUB_TOKEN')))
            from tracker.monitors.commits import model as commits
            if commits.package_policies(config):
                policies['commits'] = commits.polling(config)
            monitors = monitor.settings(config)
            if monitors["enabled"]:
                policies["monitors"] = monitor.polling(config)
        except Exception as error:
            log(f"runtime preflight failed: {error}")
            return 2
        base_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        api_env = {**base_env, "TRACKER_DB": DB}
        web_env = {**base_env, "HOST": os.environ.get("HOST", "0.0.0.0"), "PORT": WEB_PORT,
                   "TRACKER_API_URL": f"http://127.0.0.1:{API_PORT}",
                   "ASTRO_TELEMETRY_DISABLED": "1"}
        tasks = [
            (service, ("api", [VENV_PYTHON, "-m", "uvicorn", "tracker.api:app",
                              "--host", "127.0.0.1", "--port", API_PORT, "--no-access-log"],
                       api_env, f"{APP}/backend")),
            (service, ("web", ["node", f"{APP}/frontend/server.mjs"],
                       web_env, f"{APP}/frontend")),
        ]
        tasks.extend((periodic, (phase, policy)) for phase, policy in policies.items())
        if config["spec"]["repo"]:
            tasks.append((specs, (config["spec"],)))
        threads = [threading.Thread(target=fn, args=args, daemon=True,
                                    name="specs" if fn is specs else args[0]) for fn, args in tasks]
        for thread in threads:
            thread.start()
        log(f"supervising {len(threads)} tasks; web on :{WEB_PORT}, api on 127.0.0.1:{API_PORT}")
        try:
            _stop.wait()
        finally:
            _stop.set()
            log("shutting down")
            terminate_children()
            for thread in threads:
                thread.join(timeout=1)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
