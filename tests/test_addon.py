"""addon.delegation-bench wrapper tests — A2,A3,A4,A6,A7,A9,A10.

Run:  python3 -m unittest discover -s tests -v   (from the add-on root)
"""
import hashlib
import inspect
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
UPSTREAM = os.path.expanduser("~/workspaces/delegation-bench")
sys.path.insert(0, ADDON_ROOT)

import server  # noqa: E402

TEST_PORT = 4919
BASE = f"http://127.0.0.1:{TEST_PORT}"


def post(payload, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    req = urllib.request.Request(BASE + "/", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode())


def post_err(payload, raw=None):
    try:
        return post(payload, raw)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Service:
    def __enter__(self):
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", TEST_PORT), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        server._state.update({"busy": False, "last_run_id": None, "runs": {}})


def wait_run(run_id, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, res = post({"method": "delegationbench.results", "params": {"run_id": run_id}})
        if res["state"] != "running":
            return res
        time.sleep(0.2)
    raise AssertionError("run did not finish in time")


class TestVendorPin(unittest.TestCase):  # A4 — every vendored file hash-pinned
    """Vendor pin law: the pack is pinned absolutely by the recorded sha256
    hashes in vendor/VENDOR-MANIFEST.json (frozen at vendor time from the
    upstream committed tree — fixtures included, per file; verifiable on any
    machine, no clone needed). Byte-comparison against a live upstream clone
    is opportunistic: it runs only when the clone is present AND at the
    pinned commit, and skips otherwise — a stale clone would verify the
    wrong tree."""

    MANIFEST = os.path.join(ADDON_ROOT, "vendor", "VENDOR-MANIFEST.json")

    @classmethod
    def setUpClass(cls):
        with open(cls.MANIFEST) as f:
            cls.meta = json.load(f)

    def _upstream_head_at_pin(self):
        """True when a local upstream clone sits at the pinned commit."""
        try:
            head = subprocess.run(
                ["git", "-C", UPSTREAM, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return False
        return head == self.meta["upstream"]["commit"]

    def test_vendor_manifest_pins_expected_upstream(self):
        self.assertEqual(self.meta["upstream"]["name"], "delegation-bench")
        self.assertEqual(self.meta["upstream"]["license"], "MIT")
        self.assertRegex(self.meta["upstream"]["commit"], r"^[0-9a-f]{40}$")
        self.assertGreater(len(self.meta["files"]), 30)

    def test_vendored_files_hash_identical_to_upstream(self):
        """Absolute pin: every vendored byte matches its recorded upstream
        hash — enforced on every machine, no upstream clone required."""
        checked = 0
        for rel, expected in self.meta["files"].items():
            self.assertEqual(sha256(os.path.join(ADDON_ROOT, "vendor", rel)),
                             expected, f"vendor drift: {rel}")
            checked += 1
        self.assertGreater(checked, 30)

    def test_no_unlisted_files_in_vendor(self):
        """The pin must be complete: a file dropped under vendor/ but absent
        from the manifest would ship without any hash check."""
        unlisted = []
        for root, dirs, names in os.walk(os.path.join(ADDON_ROOT, "vendor")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for n in names:
                if n.endswith((".pyc", ".DS_Store")):
                    continue
                rel = os.path.relpath(os.path.join(root, n), os.path.join(ADDON_ROOT, "vendor"))
                if rel != "VENDOR-MANIFEST.json" and rel not in self.meta["files"]:
                    unlisted.append(rel)
        self.assertEqual(unlisted, [])

    def test_vendored_bytes_identical_to_upstream_git_head(self):
        """Opportunistic live check: byte-identity against the upstream clone
        (fixtures included), only when it sits at the pinned commit (skipped
        when the clone is absent or stale; the recorded pins above still
        enforce integrity)."""
        if not self._upstream_head_at_pin():
            self.skipTest(
                "upstream clone not present at pinned commit "
                f"{self.meta['upstream']['commit'][:12]} ({UPSTREAM}); "
                "recorded manifest pins still enforce pack integrity")
        for rel in self.meta["files"]:
            theirs = os.path.join(UPSTREAM, rel)
            self.assertTrue(os.path.exists(theirs), f"upstream missing: {rel}")
            self.assertEqual(
                sha256(os.path.join(ADDON_ROOT, "vendor", rel)), sha256(theirs),
                f"vendor drift: {rel}")


class TestInternalApiPin(unittest.TestCase):  # A9
    def test_runner_signatures(self):
        for fn, params in (
            (server.bench_runner.run_cell, ["cell", "dry"]),
            (server.bench_runner.decide, ["trial_passes", "confirmation_passes"]),
            (server.bench_runner.emit_card, ["all_cells", "out_path"]),
            (server.bench_runner.mock_submit, ["task"]),
        ):
            got = list(inspect.signature(fn).parameters)
            self.assertEqual(got[: len(params)], params, fn.__name__)


class TestStatus(unittest.TestCase):  # A2
    def test_status_roundtrip_lists_cells(self):
        with Service():
            code, body = post({"method": "delegationbench.status"})
            self.assertEqual(code, 200)
            self.assertTrue(body["ok"])
            self.assertFalse(body["busy"])
            self.assertIn("C1-S", body["cells"])
            self.assertGreater(len(body["cells"]), 10)

    def test_get_health(self):
        with Service():
            with urllib.request.urlopen(BASE + "/health", timeout=10) as resp:
                self.assertTrue(json.loads(resp.read().decode())["ok"])


class TestRunSemantics(unittest.TestCase):  # A3 + A10
    def test_mock_run_lifecycle_with_verdict_and_card(self):
        with Service():
            code, body = post({"method": "delegationbench.run", "params": {
                "cell": "C1-S", "mode": "mock", "trials": 5}})
            self.assertEqual(code, 200)
            res = wait_run(body["run_id"])
            self.assertEqual(res["state"], "done", res.get("error", ""))
            verdict = res["verdict"]
            self.assertTrue(verdict[0] in ("ESCALATE", "YELLOW", "RED", "CAPABILITY-GREEN-CANDIDATE"))
            self.assertEqual(len(res["rows"]), 5)
            for row in res["rows"]:
                self.assertNotIn("workspace", row)  # redaction (R3)
            card = os.path.join(ADDON_ROOT, res["card_path"])
            self.assertTrue(os.path.exists(card))
            with open(card) as f:
                card_text = f.read()
            self.assertIn("DELEGATION CARD", card_text)
            self.assertNotIn(os.path.expanduser("~"), card_text)
            decision = os.path.join(ADDON_ROOT, "var", body["run_id"], "decision.json")
            with open(decision) as f:
                self.assertNotIn(os.path.expanduser("~"), f.read())

    def test_garbage_http_endpoint_fails_honestly(self):
        with Service():
            code, body = post({"method": "delegationbench.run", "params": {
                "cell": "C1-S", "mode": "http", "endpoint_url": "http://127.0.0.1:1", "trials": 1}})
            self.assertEqual(code, 200)
            res = wait_run(body["run_id"])
            self.assertEqual(res["state"], "failed")

    def test_single_flight_and_immediate_poll(self):
        with Service():
            code, first = post({"method": "delegationbench.run", "params": {
                "cell": "C1-S", "mode": "mock", "trials": 5}})
            code, res = post_err({"method": "delegationbench.results", "params": {"run_id": first["run_id"]}})
            self.assertEqual(code, 200)
            self.assertEqual(res["state"], "running")  # I2: never 404
            code, status = post({"method": "delegationbench.status"})
            self.assertTrue(status["busy"])
            code, second = post_err({"method": "delegationbench.run", "params": {"cell": "C1-S", "mode": "mock"}})
            self.assertEqual(code, 409)
            wait_run(first["run_id"])


class TestAdversarial(unittest.TestCase):  # A6
    def test_remote_url_refused(self):
        with Service():
            for url in ("http://example.com:8080", "https://10.0.0.1"):
                code, body = post_err({"method": "delegationbench.run", "params": {
                    "cell": "C1-S", "mode": "http", "endpoint_url": url}})
                self.assertEqual(code, 400, url)
                self.assertIn("loopback", body["error"])

    def test_unknown_cell_rejected(self):
        with Service():
            code, _ = post_err({"method": "delegationbench.run", "params": {
                "cell": "../../../etc/passwd", "mode": "mock"}})
            self.assertEqual(code, 400)

    def test_adapter_cfg_never_accepted(self):
        with Service():
            code, _ = post_err({"method": "delegationbench.run", "params": {
                "cell": "C1-S", "mode": "mock", "adapter_cfg": {"cmd": ["rm", "-rf", "/"]}}})
            self.assertEqual(code, 400)

    def test_endpoint_url_rejected_in_mock_mode(self):
        with Service():
            code, _ = post_err({"method": "delegationbench.run", "params": {
                "cell": "C1-S", "mode": "mock", "endpoint_url": "http://127.0.0.1:9"}})
            self.assertEqual(code, 400)

    def test_control_chars_rejected(self):
        with Service():
            code, _ = post_err({"method": "delegationbench.run", "params": {
                "cell": "C1-S", "mode": "http",
                "endpoint_url": "http://127.0.0.1:9/\u0000x"}})
            self.assertEqual(code, 400)

    def test_unknown_fields_rejected(self):
        with Service():
            code, _ = post_err({"method": "delegationbench.status", "params": {}, "extra": 1})
            self.assertEqual(code, 400)

    def test_oversized_body_rejected(self):
        with Service():
            big = json.dumps({"method": "delegationbench.run", "params": {
                "cell": "C1-S", "run_id": "x" * 100000}}).encode()
            try:
                post(None, raw=big)
                self.fail("oversized body must not succeed")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 413)
                self.assertEqual(exc.headers.get("Connection"), "close")  # advertised, not silent

    def test_unknown_run_id_404(self):
        with Service():
            code, _ = post_err({"method": "delegationbench.results", "params": {"run_id": "nope"}})
            self.assertEqual(code, 404)

    def test_no_home_paths_in_tree(self):  # A7
        needle = (os.sep + "Users" + os.sep).encode()
        for root, dirs, files in os.walk(ADDON_ROOT):
            dirs[:] = [d for d in dirs if d not in ("var", "__pycache__")]
            for name in files:
                path = os.path.join(root, name)
                if path.endswith(".pyc"):
                    continue
                with open(path, "rb") as f:
                    self.assertNotIn(needle, f.read(), f"home path leaked in {path}")


class TestGradingWired(unittest.TestCase):  # review C1 — verdicts must actually judge
    def test_cells_grade_beyond_no_check(self):
        with Service():
            for cell in ("C1-S", "C1-L1-0", "C3-L2000-9"):
                code, body = post({"method": "delegationbench.run", "params": {
                    "cell": cell, "mode": "mock", "trials": 1}})
                self.assertEqual(code, 200, cell)
                res = wait_run(body["run_id"])
                self.assertEqual(res["state"], "done", cell)
                for row in res["rows"]:
                    self.assertNotEqual(row["judge"], "no-check",
                                        f"{cell} graded as no-check — grading keys lost")


if __name__ == "__main__":
    unittest.main()
