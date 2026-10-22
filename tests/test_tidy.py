"""Pruebas de archivado acotado al registro de hilos del bridge."""
import argparse
import fcntl
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import ANY, call, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bridge_tidy", ROOT / "scripts" / "bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class TidyClient:
    def __init__(self, threads, running=None, archive_failures=None, list_pages=None, read_threads=None):
        self.threads = threads
        self.read_threads = read_threads or threads
        self.running = set(running or ())
        self.archive_failures = set(archive_failures or ())
        self.list_pages = list_pages
        self.calls = []

    def call(self, method, params=None, timeout=60):
        params = params or {}
        self.calls.append((method, params, timeout))
        if method == "thread/list":
            if self.list_pages is None:
                rows, next_cursor = list(self.threads.values()), None
            else:
                page = int(params.get("cursor", "0"))
                rows = self.list_pages[page]
                next_cursor = str(page + 1) if page + 1 < len(self.list_pages) else None
            result = {"data": [{"thread": thread} for thread in rows]}
            if next_cursor is not None:
                result["nextCursor"] = next_cursor
            return {"result": result}
        if method == "thread/read":
            thread = self.read_threads.get(params["threadId"])
            return {"result": {"thread": thread}} if thread else {"error": {"message": "missing"}}
        if method == "bridge/running":
            return {"result": {"turnId": "turn-running" if params["threadId"] in self.running else None}}
        if method == "thread/archive" and params["threadId"] in self.archive_failures:
            return {"error": {"message": "archive failed"}}
        return {"result": {}}


class InterleavingClient(TidyClient):
    def __init__(self):
        old = {"id": "shared", "recencyAt": time.time() - 48 * 3600, "turns": []}
        super().__init__({"shared": old})
        self.turn_started = threading.Event()
        self.allow_turn = threading.Event()
        self.read_count = 0
        self.archive_count = 0

    def call(self, method, params=None, timeout=60):
        if method == "thread/read":
            self.calls.append((method, params or {}, timeout))
            self.read_count += 1
            return {"result": {"thread": {
                "id": "shared", "archived": self.read_count == 1,
                "recencyAt": time.time() - 48 * 3600, "turns": [],
            }}}
        if method == "thread/unarchive":
            self.calls.append((method, params or {}, timeout))
            return {"result": {}}
        if method == "turn/start":
            self.calls.append((method, params or {}, timeout))
            self.turn_started.set()
            self.allow_turn.wait(2)
            return {"result": {"turn": {"id": "turn-1"}}}
        if method == "thread/archive":
            self.calls.append((method, params or {}, timeout))
            self.archive_count += 1
            return {"result": {}}
        return super().call(method, params, timeout)


def tidy_args(hours=24, dry_run=False):
    return argparse.Namespace(hours=hours, dry_run=dry_run)


class TidyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "bridge-home"
        self.home.mkdir()
        self.threads_path = self.home / "threads.json"
        self.log_path = self.home / "events.jsonl"
        self.archived_dir = Path(self.temp.name) / "archived_sessions"
        self.archived_dir.mkdir()
        self.globals = patch.multiple(bridge, HOME=self.home, THREADS=self.threads_path, LOG=self.log_path,
                                      ARCHIVED_SESSIONS=self.archived_dir)
        self.globals.start()

    def tearDown(self):
        self.globals.stop()
        self.temp.cleanup()

    def records(self, rows):
        self.threads_path.write_text(json.dumps({tid: {} for tid in rows}))

    def thread(self, tid, age_hours=48, **extra):
        row = {"id": tid, "recencyAt": time.time() - age_hours * 3600, "turns": []}
        row.update(extra)
        return row

    def archived_ids(self, client):
        return [params["threadId"] for method, params, _ in client.calls
                if method == "thread/archive"]

    def test_tidy_never_touches_thread_outside_registry(self):
        self.records(("registered",))
        client = TidyClient({"registered": self.thread("registered"), "manual": self.thread("manual")})

        bridge.cmd_tidy(client, tidy_args())

        self.assertEqual(self.archived_ids(client), ["registered"])

    def test_tidy_respects_keep(self):
        self.threads_path.write_text(json.dumps({"kept": {"keep": True}, "old": {}}))
        client = TidyClient({"kept": self.thread("kept"), "old": self.thread("old")})

        bridge.cmd_tidy(client, tidy_args())

        self.assertEqual(self.archived_ids(client), ["old"])
        self.assertNotIn("archived_at", json.loads(self.threads_path.read_text())["kept"])

    def test_tidy_respects_turn_in_progress(self):
        self.records(("running", "idle"))
        client = TidyClient({"running": self.thread("running"), "idle": self.thread("idle")}, running={"running"})

        bridge.cmd_tidy(client, tidy_args())

        self.assertEqual(self.archived_ids(client), ["idle"])

    def test_tidy_respects_age_threshold(self):
        self.records(("old", "fresh"))
        client = TidyClient({"old": self.thread("old", age_hours=25), "fresh": self.thread("fresh", age_hours=23)})

        bridge.cmd_tidy(client, tidy_args(hours=24))

        self.assertEqual(self.archived_ids(client), ["old"])

    def test_tidy_dry_run_does_not_archive(self):
        self.records(("old",))
        client = TidyClient({"old": self.thread("old")})

        bridge.cmd_tidy(client, tidy_args(dry_run=True))

        self.assertEqual(self.archived_ids(client), [])
        self.assertNotIn("archived_at", json.loads(self.threads_path.read_text())["old"])

    def test_tidy_archive_failure_does_not_stop_rest(self):
        self.records(("fails", "works"))
        client = TidyClient({"fails": self.thread("fails"), "works": self.thread("works")},
                            archive_failures={"fails"})

        bridge.cmd_tidy(client, tidy_args())

        self.assertEqual(self.archived_ids(client), ["fails", "works"])
        rows = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        self.assertEqual([row["thread"] for row in rows if row["event"] == "tidy"], ["works"])
        self.assertIn("archived_at", json.loads(self.threads_path.read_text())["works"])

    def test_tidy_reconciles_thread_already_archived_on_disk(self):
        tid = "01a1069a-cac3-7701-b5aa-b26226d175cb"
        self.records((tid, "works"))
        (self.archived_dir / f"rollout-2026-10-04T13-09-30-{tid}.jsonl.zst").write_text("")
        # El app-server del daemon sigue creyendo que no está archivado.
        client = TidyClient({tid: self.thread(tid), "works": self.thread("works")},
                            archive_failures={tid})

        bridge.cmd_tidy(client, tidy_args())

        self.assertEqual(self.archived_ids(client), ["works"])
        records = json.loads(self.threads_path.read_text())
        self.assertIn("archived_at", records[tid])
        rows = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        self.assertIn({"event": "tidy_reconciled", "thread": tid}, [
            {k: v for k, v in row.items() if k != "at"} for row in rows])

    def test_tidy_dry_run_does_not_reconcile(self):
        tid = "01a1069a-cac3-7701-b5aa-b26226d175cb"
        self.records((tid,))
        (self.archived_dir / f"rollout-2026-10-04T13-09-30-{tid}.jsonl").write_text("")
        client = TidyClient({tid: self.thread(tid)})

        bridge.cmd_tidy(client, tidy_args(dry_run=True))

        self.assertNotIn("archived_at", json.loads(self.threads_path.read_text())[tid])
        self.assertEqual(self.archived_ids(client), [])

    def test_tidy_logs_failures_even_when_quiet(self):
        self.records(("fails",))
        client = TidyClient({"fails": self.thread("fails")}, archive_failures={"fails"})

        bridge.cmd_tidy(client, argparse.Namespace(hours=24, dry_run=False, quiet=True))

        rows = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        failed = [row for row in rows if row["event"] == "tidy_failed"]
        self.assertEqual([row["thread"] for row in failed], ["fails"])
        self.assertIn("archive failed", failed[0]["error"])

    def test_tidy_rereads_before_archive_and_skips_when_age_changed(self):
        self.records(("changed",))
        listed = self.thread("changed", age_hours=48)
        current = self.thread("changed", age_hours=1)
        client = TidyClient({"changed": listed}, read_threads={"changed": current})

        bridge.cmd_tidy(client, tidy_args())

        self.assertEqual(self.archived_ids(client), [])
        methods = [method for method, _, _ in client.calls]
        self.assertLess(methods.index("thread/read"), len(methods))

    def test_tidy_uses_nonblocking_lock(self):
        self.records(("old",))
        lock_path = self.home / "tidy.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            client = TidyClient({"old": self.thread("old")})
            bridge.cmd_tidy(client, tidy_args())
            self.assertEqual(client.calls, [])
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_save_thread_locks_read_modify_replace(self):
        self.records(("registered",))
        with patch.object(bridge.fcntl, "flock") as flock:
            bridge.save_thread("registered", keep=True)

        self.assertEqual(flock.call_args_list, [call(ANY, fcntl.LOCK_EX), call(ANY, fcntl.LOCK_UN)])
        self.assertTrue(json.loads(self.threads_path.read_text())["registered"]["keep"])

    def test_timestamp_boundaries_and_utc_iso(self):
        naive = bridge._timestamp("1970-01-01T00:00:00")
        offset = bridge._timestamp("1970-01-01T01:00:00+01:00")
        zulu = bridge._timestamp("1970-01-01T00:00:00Z")
        seconds = bridge._timestamp(1_000_000_000_000)
        millis = bridge._timestamp(1_000_000_000_001)

        self.assertEqual(naive, datetime(1970, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(offset, 0)
        self.assertEqual(zulu, 0)
        self.assertEqual(seconds, 1_000_000_000_000)
        self.assertEqual(millis, 1_000_000_000.001)

    def test_tidy_paginates_thread_list_with_next_cursor_and_max_100(self):
        ids = [f"thread-{index}" for index in range(101)]
        self.records(ids)
        rows = [self.thread(tid) for tid in ids]
        client = TidyClient({thread["id"]: thread for thread in rows}, list_pages=[rows[:100], rows[100:]])

        bridge.cmd_tidy(client, tidy_args(), silent=True)

        list_calls = [(params.get("limit"), params.get("cursor"))
                      for method, params, _ in client.calls if method == "thread/list"]
        self.assertEqual(list_calls, [(100, None), (100, "1")])
        self.assertEqual(len(self.archived_ids(client)), 101)

    def test_send_holds_threads_lock_until_turn_and_tidy_waits(self):
        self.records(("shared",))
        client = InterleavingClient()
        send_args = argparse.Namespace(thread="shared", prompt="sigue", model=None, effort=None,
                                       no_watch=True, timeout=1)
        send_done = threading.Event()

        def send():
            bridge.cmd_send(client, send_args)
            send_done.set()

        with patch.object(bridge, "rollout_path", return_value=None), \
             patch.object(bridge, "held_by_app", return_value=False), \
             patch.object(bridge, "append"):
            sender = threading.Thread(target=send)
            sender.start()
            self.assertTrue(client.turn_started.wait(2))
            tidier = threading.Thread(target=lambda: bridge.cmd_tidy(client, tidy_args(), silent=True))
            tidier.start()
            time.sleep(0.05)
            self.assertEqual(client.archive_count, 0)
            client.allow_turn.set()
            sender.join(2)
            tidier.join(2)

        self.assertTrue(send_done.is_set())
        self.assertEqual(client.archive_count, 1)

    def test_keep_and_unkeep_only_update_registered_record(self):
        self.records(("registered",))
        args = argparse.Namespace(thread="registered")

        self.assertEqual(bridge.cmd_keep(args, True), 0)
        self.assertTrue(json.loads(self.threads_path.read_text())["registered"]["keep"])
        self.assertEqual(bridge.cmd_keep(args, False), 0)
        self.assertFalse(json.loads(self.threads_path.read_text())["registered"]["keep"])


if __name__ == "__main__":
    unittest.main()
