#!/usr/bin/env python3
"""addon.delegation-bench local-service entry (http-json on 127.0.0.1:4890).

ResonantOS add-on contract: protocol http-json, healthCommand
delegationbench.status. Wraps the FROZEN vendored delegation-bench runner
in-process. Modes: mock (deterministic self-test, no network) and http
(loopback-only endpoint, opt-in env for anything else). The service layer
spawns nothing and never accepts adapter configs from requests; the vendored
hidden-test oracle does execute submitted code under python as its grading
function (fixture-controlled, never request-controlled).

All persisted output is home-path-redacted; absolute workspace paths are
stripped from rows before anything is acknowledged (PRD R2/R3).

Exit codes: 0 normal stop; 78 port bind failure.
"""

import json
import os
import re
import socket
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ADDON_ROOT = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(ADDON_ROOT, "vendor")
sys.path.insert(0, os.path.join(VENDOR, "bench"))

PORT = int(os.environ.get("DELEGATIONBENCH_PORT", "4890"))  # dev override; manifest 4890 is the contract
ALLOW_REMOTE = os.environ.get("DELEGATIONBENCH_ALLOW_REMOTE", "") == "1"
CALL_TIMEOUT = int(os.environ.get("DELEGATIONBENCH_CALL_TIMEOUT", "600"))
JOB_CAP = int(os.environ.get("DELEGATIONBENCH_JOB_CAP", "3600"))
MAX_BODY = 64 * 1024
MAX_STR = 2048
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

OUT_BASE = os.path.join(ADDON_ROOT, "var")


def _load_cells():
    cells = {}
    for sub in ("templates", "dev"):
        d = os.path.join(VENDOR, "fixtures", sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".json"):
                with open(os.path.join(d, name)) as f:
                    spec = json.load(f)
                cells[spec.get("cell", name[:-5])] = spec
    return cells


CELLS = _load_cells()
# workspace-template dirs under fixtures/ — currently the empty set (PRD R3);
# enumerated so a future upstream template can never become a traversal path
TEMPLATE_DIRS = set()
_td = os.path.join(VENDOR, "fixtures", "templates")
if os.path.isdir(_td):
    TEMPLATE_DIRS = {n for n in os.listdir(_td) if os.path.isdir(os.path.join(_td, n))}

import runner as bench_runner  # noqa: E402  (vendored, byte-identical, hash-pinned by tests)

_state = {"busy": False, "last_run_id": None, "runs": {}}
_lock = threading.Lock()


def _validate_run_params(params):
    if not isinstance(params, dict):
        return None, "params must be an object"
    cell = params.get("cell")
    if not isinstance(cell, str) or cell not in CELLS:
        return None, "cell must be one of the fixtures listed by delegationbench.status"
    mode = params.get("mode", "mock")
    if mode not in ("mock", "http"):
        return None, "mode must be mock or http"
    url = params.get("endpoint_url")
    if mode == "http":
        if not isinstance(url, str) or not (0 < len(url) <= MAX_STR):
            return None, "endpoint_url is required for mode http"
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
            return None, "endpoint_url contains control characters"
        try:
            parts = urlsplit(url)
        except ValueError:
            return None, "endpoint_url is not a valid URL"
        if parts.scheme not in ("http", "https"):
            return None, "endpoint_url must be http(s)"
        if (parts.hostname or "") not in LOOPBACK_HOSTS and not ALLOW_REMOTE:
            return None, "endpoint_url must be loopback (127.0.0.1/localhost); set DELEGATIONBENCH_ALLOW_REMOTE=1 to override"
    elif url is not None:
        return None, "endpoint_url is only valid with mode http"
    trials = params.get("trials", 5)
    if not isinstance(trials, int) or isinstance(trials, bool) or not (1 <= trials <= 30):
        return None, "trials must be an integer in 1..30"
    run_id = params.get("run_id")
    if run_id is None:
        run_id = "run-" + uuid.uuid4().hex[:12]
    if not isinstance(run_id, str) or len(run_id) > 64 or not RUN_ID_RE.match(run_id) or run_id.startswith("."):
        return None, "run_id may only contain ASCII letters, digits, dot, underscore, hyphen"
    for key in params:
        if key not in ("cell", "mode", "endpoint_url", "trials", "run_id"):
            return None, f"unknown field: {key}"
    return {"cell": cell, "mode": mode, "url": url, "trials": trials, "run_id": run_id}, None


def _redact_text(text):
    home = os.path.expanduser("~")
    return text.replace(home, "~") if home and home != "~" else text


def _sanitize_rows(rows):
    clean = []
    for row in rows:
        row = dict(row)
        row.pop("workspace", None)  # absolute tmp path never leaves the machine
        clean.append(row)
    return clean


def _compose_task(spec):
    """Merge grading keys into the task dict (review C1): dev fixtures carry
    hidden_tests/expect/fact_spec/safety_cell as siblings of task, and the
    template nests them under expect — machine_judge reads them from task."""
    task = dict(spec["task"])
    for key in ("hidden_tests", "expect", "fact_spec", "safety_cell"):
        if key in spec and key not in task:
            task[key] = spec[key]
    expect = task.get("expect")
    if isinstance(expect, dict):
        nested = expect.get("hidden_tests")
        if nested is not None:
            task["hidden_tests"] = nested
            task.pop("expect", None)
    return task


def _execute_run(job):
    run_id = job["run_id"]
    spec = dict(CELLS[job["cell"]])
    cell = {
        "id": spec.get("cell", job["cell"]),
        "n": job["trials"],
        "task": _compose_task(spec),
        "adapter": "http",
        "adapter_cfg": {"url": job["url"], "timeout": CALL_TIMEOUT} if job["mode"] == "http" else {},
    }
    watchdog = threading.Timer(JOB_CAP, _cap_flip, args=(run_id,))
    watchdog.daemon = True
    old_tmpdir = None
    try:
        run_dir = os.path.join(OUT_BASE, run_id)
        tmp_dir = os.path.join(OUT_BASE, "run-" + run_id)
        os.makedirs(run_dir, exist_ok=True)
        os.makedirs(tmp_dir, exist_ok=True)
        old_tmpdir = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = tmp_dir  # upstream mkdtemp lands inside var/, never /tmp
        watchdog.start()
        rows = bench_runner.run_cell(cell, dry=(job["mode"] == "mock"))
        verdict = bench_runner.decide([bool(r["ok"]) for r in rows])
        rows = _sanitize_rows(rows)
        card_path = os.path.join(run_dir, "card.md")
        bench_runner.emit_card(
            [{"id": cell["id"], "results": rows, "verdict": verdict}], card_path)
        with open(os.path.join(run_dir, "rows.json"), "w") as f:
            json.dump(rows, f, indent=1)
        with open(os.path.join(run_dir, "decision.json"), "w") as f:
            json.dump({"run_id": run_id, "cell": cell["id"], "mode": job["mode"],
                       "trials": job["trials"], "verdict": verdict}, f, indent=1)
        # home-path redaction pass over everything we persist
        for name in ("card.md", "rows.json", "decision.json"):
            p = os.path.join(run_dir, name)
            with open(p) as f:
                text = f.read()
            if _redact_text(text) != text:
                with open(p, "w") as f:
                    f.write(_redact_text(text))
        with _lock:
            record = _state["runs"].get(run_id)
            if record and record["state"] == "running":  # review I3: a capped run stays failed
                _state["runs"][run_id] = {
                    "state": "done",
                    "verdict": list(verdict),
                    "card_path": os.path.relpath(card_path, ADDON_ROOT),
                    "rows": rows,
                    "summary_path": os.path.relpath(os.path.join(run_dir, "decision.json"), ADDON_ROOT),
                }
    except Exception as exc:  # bench errors surface as job failure, never server crash
        with _lock:
            _state["runs"][run_id] = {"state": "failed", "verdict": None, "card_path": None,
                                      "rows": None, "summary_path": None,
                                      "error": _redact_text(str(exc))[:300]}  # review I2
    finally:
        watchdog.cancel()
        if old_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_tmpdir
        with _lock:
            _state["busy"] = False


def _cap_flip(run_id):
    # wall-clock cap (PRD R2): flip the visible state; per-call timeouts bound
    # the thread, so busy drains without overlap
    with _lock:
        record = _state["runs"].get(run_id)
        if record and record["state"] == "running":
            _state["runs"][run_id] = dict(record, state="failed",
                                          error=f"job exceeded wall-clock cap ({JOB_CAP}s)")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30

    def _reply(self, code, payload, close=False):
        if close:
            self.close_connection = True
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/health"):
            self._reply(200, self._status())
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/":
            self._reply(404, {"error": "not found"}, close=True)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, {"error": "bad content-length"}, close=True)
            return
        if length <= 0 or length > MAX_BODY:
            self._reply(413 if length > MAX_BODY else 400, {"error": "body must be 1..65536 bytes"}, close=True)
            return
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except (TimeoutError, socket.timeout, OSError):
            self._reply(408, {"error": "request body incomplete (timeout)"}, close=True)
            return
        except (ValueError, UnicodeDecodeError):
            self._reply(400, {"error": "body must be valid JSON"}, close=True)
            return
        if not isinstance(req, dict):
            self._reply(400, {"error": "body must be a JSON object"}, close=True)
            return
        method = req.get("method")
        params = req.get("params", {})
        for key in req:
            if key not in ("method", "params"):
                self._reply(400, {"error": f"unknown field: {key}"}, close=True)
                return
        if method == "delegationbench.status":
            self._reply(200, self._status())
        elif method == "delegationbench.run":
            self._run(params)
        elif method == "delegationbench.results":
            self._results(params)
        else:
            self._reply(404, {"error": f"unknown method: {method}"})

    def _status(self):
        with _lock:
            return {
                "ok": True,
                "version": "0.1.0",
                "busy": _state["busy"],
                "cells": sorted(CELLS),
                "last_run_id": _state["last_run_id"],
            }

    def _run(self, params):
        job, err = _validate_run_params(params)
        if err:
            self._reply(400, {"error": err})
            return
        with _lock:
            if _state["busy"]:
                self._reply(409, {"error": "a run is already in progress", "run_id": _state["last_run_id"]})
                return
            _state["busy"] = True
            _state["runs"][job["run_id"]] = {"state": "running", "verdict": None, "card_path": None,
                                             "rows": None, "summary_path": None}
            _state["last_run_id"] = job["run_id"]
        try:
            threading.Thread(target=_execute_run, args=(job,), daemon=True).start()
        except Exception:
            with _lock:
                _state["busy"] = False
                _state["runs"][job["run_id"]] = {"state": "failed", "error": "worker thread failed to start"}
            self._reply(500, {"error": "worker thread failed to start"})
            return
        self._reply(200, {"run_id": job["run_id"], "state": "running"})

    def _results(self, params):
        if not isinstance(params, dict):
            self._reply(400, {"error": "params must be an object"})
            return
        run_id = params.get("run_id")
        if run_id is None:
            with _lock:
                run_id = _state["last_run_id"]
        if not isinstance(run_id, str) or len(run_id) > 64:
            self._reply(400, {"error": "run_id must be a string of at most 64 characters"})
            return
        with _lock:
            record = _state["runs"].get(run_id)
        if record is None:
            self._reply(404, {"error": "unknown run_id"})
            return
        payload = {"run_id": run_id, "state": record["state"], "card_path": record.get("card_path")}
        if record.get("error"):
            payload["error"] = record["error"]
        if record.get("verdict") is not None:
            payload["verdict"] = record["verdict"]
        if record.get("rows") is not None:
            payload["rows"] = record["rows"]
        self._reply(200, payload)

    def log_message(self, fmt, *args):
        sys.stderr.write("delegationbench-service: " + (fmt % args) + "\n")


def main():
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        sys.stderr.write(f"delegationbench-service: cannot bind 127.0.0.1:{PORT} ({exc}); manifest entrypoint expects this port\n")
        return 78
    sys.stderr.write(f"delegationbench-service: listening on http://127.0.0.1:{PORT}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
