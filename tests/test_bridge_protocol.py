"""Pruebas multiproceso del protocolo del bridge, sin Codex ni daemon real."""
import multiprocessing
import os
import argparse
import contextlib
import io
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bridge


class SyntheticDaemon(bridge.Daemon):
    def __init__(self, effects):
        self.started_at = "synthetic"
        threading = __import__("threading")
        self.request_locks = __import__("collections").defaultdict(threading.Lock)
        self.request_locks_guard = threading.Lock()
        self.running = {}
        self.workers = {}
        self.inflight = __import__("collections").defaultdict(int)
        self.cycle_locks = __import__("collections").defaultdict(threading.RLock)
        self.lock = threading.Lock()
        self.effects = Path(effects)

    def _dispatch(self, method, params, timeout, deadline=None):
        if method == "bridge/clock-probe":
            return {"result": {"timeout": timeout, "hasDeadline": "_deadline" in params}}
        if method == "turn/start":
            with self.effects.open("a") as f:
                f.write((getattr(self, "_active_request_id", None) or "no-id") + "\n")
            text = ((params.get("input") or [{}])[0] or {}).get("text")
            if text == "slow synthetic turn":
                time.sleep(0.25)
            return {"result": {"turn": {"id": "synthetic-turn"}}}
        if method in ("turn/steer", "turn/interrupt", "thread/queue/add"):
            with self.effects.open("a") as f:
                f.write(method + "\n")
            return {"result": {"accepted": True}}
        return {"result": {"method": method}}


def synthetic_server(home, effects):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import bridge as child_bridge
    child_bridge.HOME = Path(home)
    child_bridge.SOCK = child_bridge.HOME / "bridge.sock"
    child_bridge.THREADS = child_bridge.HOME / "threads.json"
    child_bridge.LOG = child_bridge.HOME / "events.jsonl"
    child_bridge.REQUEST_JOURNAL = child_bridge.HOME / "requests.json"
    child_bridge.HOME.mkdir(parents=True, exist_ok=True)
    real_monotonic = time.monotonic
    time.monotonic = lambda: real_monotonic() + 700_000
    daemon = SyntheticDaemon(effects)
    server = child_bridge.make_server(daemon)
    try:
        server.serve_forever(poll_interval=0.02)
    finally:
        server.server_close()
        child_bridge.SOCK.unlink(missing_ok=True)


def separate_client(home, request_id, offset, output):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import bridge as client_bridge
    client_bridge.HOME = Path(home)
    client_bridge.SOCK = client_bridge.HOME / "bridge.sock"
    original_monotonic = time.monotonic
    time.monotonic = lambda: original_monotonic() + offset
    client = client_bridge.Client()
    client.response_grace = 1.2
    deadline = time.monotonic() + 5
    result = client.call("turn/start", {"_deadline": deadline, "_request_id": request_id,
                                         "input": [{"type": "text", "text": "slow synthetic turn"}]}, timeout=5)
    output.put(result)
    client.f.close()
    client.c.close()


class BridgeProtocol(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-protocol-")
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.effects = self.home / "effects.txt"
        bridge.HOME = self.home
        bridge.SOCK = self.home / "bridge.sock"
        bridge.THREADS = self.home / "threads.json"
        bridge.LOG = self.home / "events.jsonl"
        bridge.REQUEST_JOURNAL = self.home / "requests.json"
        self.ctx = multiprocessing.get_context("spawn")
        self.process = None
        self.clients = []
        self.start_server()

    def tearDown(self):
        for client in self.clients:
            client.f.close()
            client.c.close()
        self.stop_server()
        self.temp.cleanup()

    def start_server(self):
        self.process = self.ctx.Process(target=synthetic_server, args=(str(self.home), str(self.effects)))
        self.process.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if bridge.ping():
                return
            if not self.process.is_alive():
                self.fail("el daemon sintético terminó durante el arranque")
            time.sleep(0.02)
        self.fail("el socket temporal no quedó disponible")

    def stop_server(self):
        if self.process and self.process.is_alive():
            self.process.terminate()
            self.process.join(3)
        bridge.SOCK.unlink(missing_ok=True)

    def client(self, response_grace=0.2):
        client = bridge.Client()
        client.response_grace = response_grace
        self.clients.append(client)
        return client

    def test_version_receipt_is_available_without_a_model_turn(self):
        receipt = self.client().call("bridge/version")["result"]
        self.assertEqual(receipt["daemonVersion"], bridge.BRIDGE_VERSION)
        self.assertEqual(receipt["codeHash"], bridge.DAEMON_CODE_HASH)
        self.assertEqual(receipt["startedAt"], "synthetic")
        self.assertFalse(self.effects.exists())

    def test_relative_timeout_survives_shifted_process_clocks(self):
        c = self.client()
        with patch.object(bridge.time, "monotonic", return_value=900_000.0):
            response = c.call("bridge/clock-probe", {"_deadline": 900_004.0}, timeout=20)
        self.assertAlmostEqual(response["result"]["timeout"], 4.0, places=4)
        self.assertFalse(response["result"]["hasDeadline"])

    def test_timeout_late_reply_and_retry_have_one_effect_across_daemon_restart(self):
        request_id = "stable-request-001"
        first = self.client(response_grace=0.02).call(
            "turn/start", {"_request_id": request_id,
                           "input": [{"type": "text", "text": "slow synthetic turn"}]}, timeout=0.02)
        self.assertIn("error", first)
        self.assertEqual(first["error"]["requestId"], request_id)
        time.sleep(0.35)  # permite que llegue la respuesta tardía al daemon
        self.stop_server()
        self.start_server()
        replay = self.client().call(
            "turn/start", {"_request_id": request_id,
                           "input": [{"type": "text", "text": "slow synthetic turn"}]}, timeout=1)
        self.assertEqual(replay["result"]["turn"]["id"], "synthetic-turn")
        self.assertEqual(self.effects.read_text().splitlines(), [request_id])

    def test_two_process_clients_with_shifted_clocks_share_one_mutation(self):
        request_id = "concurrent-request-003"
        output = self.ctx.Queue()
        clients = [self.ctx.Process(target=separate_client,
                                    args=(str(self.home), request_id, offset, output))
                   for offset in (12_000, 840_000)]
        for client in clients:
            client.start()
        for client in clients:
            client.join(3)
            self.assertEqual(client.exitcode, 0)
        replies = [output.get(timeout=1) for _ in clients]
        self.assertTrue(all("result" in reply for reply in replies))
        self.assertEqual(self.effects.read_text().splitlines(), [request_id])

    def test_uncertain_request_after_restart_requires_reconciliation(self):
        request_id = "possibly-applied-002"
        bridge.write_request_record(request_id, {"status": "started", "method": "turn/start",
                                                  "threadId": "thread-synthetic"})
        response = self.client().call("turn/start", {"_request_id": request_id,
                                                     "threadId": "thread-synthetic"}, timeout=1)
        self.assertIn("error", response)
        self.assertIn("reconcilia", response["error"]["message"])
        status = self.client().call("bridge/request/status", {"requestId": request_id})["result"]
        self.assertEqual(status["status"], "started")
        self.assertFalse(self.effects.exists())

    def test_corrupt_request_journal_fails_closed(self):
        bridge.REQUEST_JOURNAL.write_text("{")
        response = self.client().call("turn/start", {"_request_id": "journal-corrupt-004",
                                                     "threadId": "thread-synthetic"}, timeout=1)
        self.assertIn("error", response)
        self.assertFalse(self.effects.exists())

    def test_app_call_rejects_a_different_inner_thread_id(self):
        self.bridge_thread("allowed")
        response = self.client().call("bridge/app-call", {
            "threadId": "allowed", "owner": "owner-a", "generation": 1,
            "appMethod": "turn/interrupt",
            "appParams": {"threadId": "revoked", "turnId": "turn-x"},
        })
        self.assertIn("error", response)
        self.assertIn("threadId", response["error"]["message"])
        self.assertFalse(self.effects.exists())

    def test_corrupt_thread_registry_blocks_mutation_and_lease_claim(self):
        bridge.THREADS.write_text('{"thread-synthetic":')
        response = self.client().call("turn/start", {
            "threadId": "thread-synthetic", "owner": "old-owner", "generation": 1,
            "_request_id": "registry-corrupt-001", "input": [{"text": "must not run"}],
        })
        self.assertIn("error", response)
        self.assertFalse(self.effects.exists())
        self.assertFalse(bridge.REQUEST_JOURNAL.exists())

    def test_handoff_waits_for_registry_lock_without_holding_cycle_lock(self):
        self.bridge_thread("thread-synthetic", owner="owner-a", owner_generation=1)
        registry_lock = bridge._thread_lock()
        result = {}

        def offer_handoff():
            result["handoff"] = self.client().call("bridge/handoff/offer", {
                "threadId": "thread-synthetic", "owner": "owner-a", "generation": 1,
                "nextOwner": "owner-b",
            }, timeout=2)

        try:
            worker = threading.Thread(target=offer_handoff)
            worker.start()
            time.sleep(0.08)
            running = self.client().call("bridge/running", {"threadId": "thread-synthetic"}, timeout=0.5)
            self.assertIn("result", running, "handoff retuvo cycle_lock mientras esperaba threads.lock")
        finally:
            import fcntl
            fcntl.flock(registry_lock, fcntl.LOCK_UN)
            os.close(registry_lock)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIn("result", result["handoff"])

    def test_request_id_rejects_a_different_parameter_fingerprint(self):
        self.bridge_thread("thread-synthetic", owner="owner-a", owner_generation=1)
        c = self.client()
        first = c.call("turn/start", {"threadId": "thread-synthetic", "owner": "owner-a",
            "generation": 1, "_request_id": "fingerprint-001",
            "input": [{"text": "first payload"}]})
        second = c.call("turn/start", {"threadId": "thread-synthetic", "owner": "owner-a",
            "generation": 1, "_request_id": "fingerprint-001",
            "input": [{"text": "different payload"}]})
        self.assertIn("result", first)
        self.assertIn("error", second)
        self.assertEqual(second["error"].get("requestId"), "fingerprint-001")
        self.assertEqual(self.effects.read_text().splitlines(), ["fingerprint-001"])

    def test_app_steer_and_stop_errors_return_request_id(self):
        class FailedClient:
            def call(self, *_args, **_kwargs):
                return {"error": {"message": "timed out", "requestId": "app-error-001"}}

        a = argparse.Namespace(thread="thread-synthetic", text="steer", owner="owner-a",
                               generation=1, request_id="app-error-001")
        for method, verb in (("turn/steer", "STEERED"), ("turn/interrupt", "STOPPED")):
            output = io.StringIO()
            with patch.object(bridge, "held_by_app", return_value=True), \
                    patch.object(bridge, "app_active_turn", return_value="turn-x"), \
                    contextlib.redirect_stdout(output):
                result = bridge.app_turn_op(FailedClient(), a, method, verb)
            self.assertEqual(result, 5)
            self.assertIn("requestId=app-error-001", output.getvalue())

    def test_start_from_is_used_as_effective_mutation_owner(self):
        a = argparse.Namespace(owner=None, sender="session-from", generation=1, request_id=None)
        context = bridge.mutation_context(a)
        self.assertEqual(context["owner"], "session-from")
        self.assertEqual(context["generation"], 1)

    def test_request_journal_and_temp_are_private_and_symlinks_rejected(self):
        self.home.chmod(0o755)
        bridge.write_request_record("private-001", {"status": "started", "method": "turn/start"})
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(bridge.REQUEST_JOURNAL.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.home / "requests.lock").stat().st_mode), 0o600)
        self.assertEqual(list(self.home.glob("requests.*.tmp")), [])
        unsafe_journal = self.home / "unsafe.json"
        unsafe_journal.symlink_to(bridge.REQUEST_JOURNAL)
        bridge.REQUEST_JOURNAL = unsafe_journal
        with self.assertRaises(OSError):
            bridge.request_record("private-001")

    def test_handoff_acceptance_revokes_previous_generation_for_mutations(self):
        bridge.save_thread("thread-synthetic", **{"from": "supervisor-old"})
        c = self.client()
        migrated = c.call("bridge/owner/check", {"threadId": "thread-synthetic",
                                                  "owner": "supervisor-old", "generation": 1})
        self.assertEqual(migrated["result"]["generation"], 1)
        offered = c.call("bridge/handoff/offer", {"threadId": "thread-synthetic",
                            "owner": "supervisor-old", "generation": 1,
                            "nextOwner": "supervisor-new"})["result"]
        accepted = c.call("bridge/handoff/accept", {"threadId": "thread-synthetic",
                             "owner": "supervisor-new", "generation": 1,
                             "handoffId": offered["handoffId"]})["result"]
        self.assertEqual(accepted["generation"], 2)
        for method in ("turn/start", "turn/steer", "turn/interrupt", "thread/queue/add"):
            response = c.call(method, {"threadId": "thread-synthetic", "owner": "supervisor-old",
                                       "generation": 1, "input": []})
            self.assertIn("error", response, method)
        allowed = c.call("turn/start", {"threadId": "thread-synthetic", "owner": "supervisor-new",
                                         "generation": 2, "input": [{"text": "current owner"}],
                                         "_request_id": "new-owner-action"})
        self.assertIn("result", allowed)
        self.assertEqual(self.effects.read_text().splitlines(), ["new-owner-action"])

    @staticmethod
    def bridge_thread(tid, **metadata):
        bridge.save_thread(tid, **metadata)


if __name__ == "__main__":
    unittest.main()
