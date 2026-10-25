"""Pruebas de archivado acotado al registro de hilos del bridge."""
import argparse
import errno
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
from unittest.mock import ANY, Mock, call, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bridge_tidy", ROOT / "scripts" / "bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class TidyClient:
    def __init__(self, threads, running=None, archive_failures=None, list_pages=None, read_threads=None,
                 pending=None, app_open=None):
        self.threads = threads
        self.read_threads = read_threads or threads
        self.running = set(running or ())
        self.archive_failures = set(archive_failures or ())
        self.pending = set(pending or ())
        self.app_open = set(app_open or ())
        self.list_pages = list_pages
        self.calls = []

    def call(self, method, params=None, timeout=60):
        params = params or {}
        self.calls.append((method, params, timeout))
        if method == "bridge/tidy-archive":
            deadline = time.monotonic() + timeout
            lock = bridge._thread_lock(deadline)
            try:
                return {"result": {"status": bridge._tidy_registered(
                    self, params["threadId"], params["cutoff"], params.get("dryRun", False), deadline)}}
            finally:
                bridge._release_thread_lock(lock)
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
        if method == "thread/queue/list":
            rows = [{"id": f"queued-{params['threadId']}"}] if params["threadId"] in self.pending else []
            return {"result": {"data": rows}}
        if method == "thread/resume" and params["threadId"] in self.app_open:
            return {"error": {"message": bridge.LOCKED}}
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
        self.environment = patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_AUTOTIDY": "1",
                                                  "CLAUDE_CODEX_BRIDGE_TIDY_HOURS": "8"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
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

    def test_tidy_respects_pending_queue(self):
        self.records(("queued", "idle"))
        client = TidyClient({"queued": self.thread("queued"), "idle": self.thread("idle")}, pending={"queued"})

        bridge.cmd_tidy(client, tidy_args())

        self.assertEqual(self.archived_ids(client), ["idle"])

    def test_tidy_respects_thread_open_in_app(self):
        self.records(("open", "idle"))
        client = TidyClient({"open": self.thread("open"), "idle": self.thread("idle")}, app_open={"open"})

        bridge.cmd_tidy(client, tidy_args())

        self.assertEqual(self.archived_ids(client), ["idle"])

    def test_tidy_respects_age_threshold(self):
        self.records(("old", "fresh"))
        client = TidyClient({"old": self.thread("old", age_hours=25), "fresh": self.thread("fresh", age_hours=23)})

        bridge.cmd_tidy(client, tidy_args(hours=24))

        self.assertEqual(self.archived_ids(client), ["old"])

    def test_tidy_default_threshold_is_8_hours(self):
        self.records(("seven-hours", "nine-hours"))
        client = TidyClient({"seven-hours": self.thread("seven-hours", age_hours=7),
                             "nine-hours": self.thread("nine-hours", age_hours=9)})

        bridge.cmd_tidy(client, tidy_args(hours=None))

        self.assertEqual(self.archived_ids(client), ["nine-hours"])

    def test_tidy_threshold_can_be_configured_by_environment(self):
        self.records(("seven-hours",))
        client = TidyClient({"seven-hours": self.thread("seven-hours", age_hours=7)})

        with patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_TIDY_HOURS": "6"}):
            bridge.cmd_tidy(client, tidy_args(hours=None))

        self.assertEqual(self.archived_ids(client), ["seven-hours"])

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

    def test_daemon_runs_periodic_tidy_without_a_new_start(self):
        daemon = object.__new__(bridge.Daemon)
        daemon._tidy_thread = None
        daemon._tidy_stop = threading.Event()
        calls = []
        swept = threading.Event()

        def fake_tidy(client, args, silent=False):
            calls.append((client, args, silent))
            if len(calls) == 2:
                swept.set()
            return 0

        with patch.object(bridge, "cmd_tidy", side_effect=fake_tidy), \
             patch.object(bridge, "TIDY_INTERVAL_SECONDS", 0.01):
            daemon.start_periodic_tidy()
            self.assertTrue(swept.wait(1))
            daemon.stop_periodic_tidy()

        self.assertIs(calls[0][0], daemon)
        self.assertEqual(calls[0][1].hours, 8)
        self.assertTrue(calls[0][2])

    def test_periodic_survives_system_exit_and_rpc_exception(self):
        for error in (SystemExit(5), RuntimeError("RPC sintético")):
            with self.subTest(error=type(error).__name__):
                daemon = object.__new__(bridge.Daemon)
                daemon._tidy_stop = Mock()
                daemon._tidy_stop.is_set.return_value = False
                daemon._tidy_stop.wait.side_effect = [False, True]
                with patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_AUTOTIDY": "1"}), \
                     patch.object(bridge, "cmd_tidy", side_effect=[error, 0]) as tidy, \
                     patch.object(bridge, "append") as log:
                    daemon._periodic_tidy()
                self.assertEqual(tidy.call_count, 2)
                self.assertEqual(log.call_args.args[1]["event"], "tidy_daemon_failed")

    def test_periodic_disabled_does_not_sweep(self):
        daemon = object.__new__(bridge.Daemon)
        daemon._tidy_stop = Mock()
        daemon._tidy_stop.is_set.return_value = False
        daemon._tidy_stop.wait.side_effect = [False, True]
        with patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_AUTOTIDY": "0"}), \
             patch.object(bridge, "cmd_tidy") as tidy:
            daemon._periodic_tidy()
        tidy.assert_not_called()
        self.assertEqual(daemon._tidy_stop.wait.call_count, 2)

    def test_resume_error_is_recoverable(self):
        client = Mock()
        client.call.return_value = {"error": {"message": "RPC sintético"}}
        with self.assertRaises(RuntimeError):
            bridge.held_by_app(client, "old")

    def test_periodic_logs_rpc_failure_and_recovers_next_iteration(self):
        self.records(("old",))
        backend = TidyClient({"old": self.thread("old")})
        original = backend.call
        resumes = []

        def call_backend(method, params=None, timeout=60):
            if method == "thread/resume":
                resumes.append(method)
                if len(resumes) == 1:
                    return {"error": {"message": "fallo RPC sintético"}}
            return original(method, params, timeout)

        backend.call = call_backend
        daemon = self.fake_daemon(backend)
        daemon._tidy_stop = Mock()
        daemon._tidy_stop.is_set.return_value = False
        daemon._tidy_stop.wait.side_effect = [False, True]
        daemon._periodic_tidy()
        self.assertEqual(len(resumes), 2)
        self.assertEqual(self.archived_ids(backend), ["old"])
        events = [json.loads(line)["event"] for line in self.log_path.read_text().splitlines()]
        self.assertEqual(events, ["tidy_failed", "tidy_daemon_failed", "tidy"])

    def fake_daemon(self, backend):
        # El daemon real enruta RPC; únicamente el app-server y el reaper son dobles.
        with patch.object(bridge, "AppServer"), patch.object(bridge.threading.Thread, "start"):
            daemon = bridge.Daemon()
        daemon._main_call = lambda method, params, timeout: backend.call(method, params, timeout)
        worker = Mock()
        worker.call.side_effect = backend.call
        daemon._worker = Mock(return_value=(worker, None))
        return daemon

    def late_archive_daemon(self):
        """AppServer.call real; solo se sustituye el transporte, sin procesos reales."""
        server = object.__new__(bridge.AppServer)
        server.n, server.waiting, server.wlock = 0, {}, threading.Lock()
        server.pending_archives = {}
        server.p = Mock()
        server.p.poll.return_value = None
        row = self.thread("old", archived=False)
        sent = []

        def send(request, proc=None):
            sent.append(request)
            method = request["method"]
            if method == "thread/archive":
                return  # La respuesta llegará después de agotar el timeout real.
            if method == "thread/read":
                result = {"thread": dict(row)}
            elif method == "thread/list":
                result = {"data": [dict(row)]}
            else:
                result = {"data": []}
            server.waiting.pop(request["id"]).put({"result": result})

        server._send = Mock(side_effect=send)
        daemon = self.fake_daemon(server)
        daemon.workers["old"] = server
        daemon._worker = Mock(return_value=(server, None))

        def finish(archived=True):
            row["archived"] = archived
            request = next(r for r in sent if r["method"] == "thread/archive")
            response = server.waiting.pop(request["id"], None)
            if response is not None:
                response.put({"result": {}} if archived else {"error": {"message": "rechazado"}})

        return daemon, server, sent, finish

    def expire_archive(self, daemon):
        with self.assertRaises(RuntimeError):
            daemon.call("bridge/tidy-archive", {"threadId": "old", "cutoff": time.time()}, timeout=0.03)

    def test_sent_archive_timeout_blocks_keep_until_late_response_and_readback(self):
        self.records(("old",))
        daemon, server, sent, finish = self.late_archive_daemon()
        self.expire_archive(daemon)
        with patch.object(bridge, "Client", return_value=daemon):
            # Una lectura prematura devolvería archived=False: no basta mientras siga en vuelo.
            self.assertEqual(bridge.cmd_keep(argparse.Namespace(thread="old"), True), 5)
            self.assertTrue(bridge.load_threads()["old"]["archive_uncertain"])
            self.assertNotIn("keep", bridge.load_threads()["old"])
            finish()
            with patch.object(bridge, "_archived_rollout_ids", return_value={"old"}):
                self.assertEqual(bridge.cmd_keep(argparse.Namespace(thread="old"), True), 5)
        record = bridge.load_threads()["old"]
        self.assertIn("archived_at", record)
        self.assertNotIn("archive_uncertain", record)
        self.assertNotIn("keep", record)
        self.assertEqual(sent[-1]["method"], "thread/read")
        self.assertEqual(sum(r["method"] == "thread/archive" for r in sent), 1)

    def test_keep_reconciles_uncertain_archive_that_did_not_happen(self):
        self.records(("old",))
        daemon, server, sent, finish = self.late_archive_daemon()
        self.expire_archive(daemon)
        self.assertTrue(bridge.load_threads()["old"].get("archive_uncertain"))
        finish(archived=False)
        with patch.object(bridge, "Client", return_value=daemon):
            self.assertEqual(bridge.cmd_keep(argparse.Namespace(thread="old"), True), 0)
        self.assertEqual(bridge.load_threads()["old"], {"keep": True})
        self.assertEqual(sum(r["method"] == "thread/archive" for r in sent), 1)

    def test_tidy_reconciles_late_archive_without_retrying(self):
        for archived in (True, False):
            with self.subTest(archived=archived):
                self.records(("old",))
                daemon, server, sent, finish = self.late_archive_daemon()
                self.expire_archive(daemon)
                self.assertTrue(bridge.load_threads()["old"].get("archive_uncertain"))
                self.assertEqual(bridge.cmd_tidy(daemon, tidy_args(), silent=True), 1)
                self.assertTrue(bridge.load_threads()["old"]["archive_uncertain"])
                finish(archived)
                with patch.object(bridge, "_archived_rollout_ids", return_value={"old"} if archived else set()):
                    self.assertEqual(bridge.cmd_tidy(daemon, tidy_args(), silent=True), 0)
                record = bridge.load_threads()["old"]
                self.assertEqual("archived_at" in record, archived)
                self.assertNotIn("archive_uncertain", record)
                self.assertEqual(sum(r["method"] == "thread/archive" for r in sent), 1)

    def test_uncertain_archive_read_error_keeps_marker_and_rejects_keep(self):
        for response in ({"error": {"message": "lectura fallida"}}, {"result": {"thread": {"id": "old"}}}):
            with self.subTest(response=response):
                self.threads_path.write_text(json.dumps({"old": {"archive_uncertain": True}}))
                client = Mock()
                client.call.return_value = response
                with patch.object(bridge, "Client", return_value=client):
                    self.assertEqual(bridge.cmd_keep(argparse.Namespace(thread="old"), True), 5)
                self.assertEqual(bridge.load_threads()["old"], {"archive_uncertain": True})

    def test_keep_reconciles_disk_and_read_without_trusting_stale_read(self):
        self.check_uncertain_disk_and_read("keep")

    def test_tidy_reconciles_disk_and_read_without_retrying_archive(self):
        self.check_uncertain_disk_and_read("tidy")

    def check_uncertain_disk_and_read(self, command):
        for disk_archived, read_archived in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(disk=disk_archived, read=read_archived):
                tid = f"00000000-0000-0000-0000-0000000000{int(disk_archived)}{int(read_archived)}"
                self.threads_path.write_text(json.dumps({tid: {"archive_uncertain": True}}))
                if disk_archived:
                    (self.archived_dir / f"rollout-2026-10-24T13-00-00-{tid}.jsonl").write_text("")
                backend = TidyClient({tid: self.thread(tid, archived=read_archived)})
                if command == "keep":
                    with patch.object(bridge, "Client", return_value=backend):
                        result = bridge.cmd_keep(argparse.Namespace(thread=tid), True)
                    self.assertEqual(result, 5 if disk_archived or read_archived else 0)
                else:
                    self.assertEqual(bridge.cmd_tidy(backend, tidy_args(), silent=True), 0)
                record = bridge.load_threads()[tid]
                self.assertEqual("archived_at" in record, disk_archived or read_archived)
                self.assertEqual(bool(record.get("archive_uncertain")), disk_archived != read_archived)
                self.assertEqual(bool(record.get("keep")), command == "keep" and not (disk_archived or read_archived))
                self.assertEqual(self.archived_ids(backend), [])

    def test_keep_resolves_rejected_archive_without_archive_directory(self):
        self.check_rejected_archive_without_directory("keep")

    def test_tidy_resolves_rejected_archive_without_archive_directory(self):
        self.check_rejected_archive_without_directory("tidy")

    def check_rejected_archive_without_directory(self, command):
        self.records(("old",))
        missing = Path(self.temp.name) / "archived_sessions_inexistente"
        self.assertFalse(missing.exists())
        daemon, server, sent, finish = self.late_archive_daemon()
        with patch.object(bridge, "ARCHIVED_SESSIONS", missing):
            self.expire_archive(daemon)
            self.assertTrue(bridge.load_threads()["old"]["archive_uncertain"])
            finish(archived=False)
            if command == "keep":
                with patch.object(bridge, "Client", return_value=daemon):
                    self.assertEqual(bridge.cmd_keep(argparse.Namespace(thread="old"), True), 0)
            else:
                self.assertEqual(bridge.cmd_tidy(daemon, tidy_args(), silent=True), 0)
        self.assertEqual(bridge.load_threads()["old"], {"keep": True} if command == "keep" else {})
        self.assertEqual(sum(r["method"] == "thread/archive" for r in sent), 1)
        self.assertFalse(missing.exists())

    def test_unreadable_archive_directory_preserves_uncertainty(self):
        cases = [(command, error) for command in ("keep", "tidy") for error in (errno.EACCES, errno.EIO)]
        for command, error in cases:
            with self.subTest(command=command, errno=error):
                self.threads_path.write_text(json.dumps({"old": {"archive_uncertain": True}}))
                backend = TidyClient({"old": self.thread("old", archived=False)})
                with patch.object(bridge.os, "listdir", side_effect=OSError(error, "disco no verificable", str(self.archived_dir))):
                    if command == "keep":
                        with patch.object(bridge, "Client", return_value=backend):
                            self.assertEqual(bridge.cmd_keep(argparse.Namespace(thread="old"), True), 5)
                    else:
                        self.assertEqual(bridge.cmd_tidy(backend, tidy_args(), silent=True), 1)
                self.assertEqual(bridge.load_threads()["old"], {"archive_uncertain": True})
                self.assertEqual(self.archived_ids(backend), [])

    def test_reaper_preserves_uncertain_worker_until_readback(self):
        self.records(("old",))
        daemon, server, sent, finish = self.late_archive_daemon()
        self.expire_archive(daemon)
        server.close = Mock()
        for completed in (False, True):
            if completed:
                finish()
            daemon.touched["old"] = time.time() - 60
            with patch.object(bridge.time, "sleep", side_effect=[None, RuntimeError("fin del bucle sintético")]):
                with self.assertRaises(RuntimeError):
                    daemon._reap()
            server.close.assert_not_called()
            self.assertIs(daemon.workers["old"], server)
        bridge.cmd_tidy(daemon, tidy_args(), silent=True)
        self.assertFalse(server.pending_archives)
        daemon.touched["old"] = time.time() - 60
        with patch.object(bridge.time, "sleep", side_effect=[None, RuntimeError("fin del bucle sintético")]):
            with self.assertRaises(RuntimeError):
                daemon._reap()
        server.close.assert_called_once()

    def test_dry_run_does_not_clear_uncertain_archive(self):
        for archived in (True, False):
            with self.subTest(archived=archived):
                self.threads_path.write_text(json.dumps({"old": {"archive_uncertain": True}}))
                backend = TidyClient({"old": self.thread("old", archived=archived)})
                self.assertEqual(bridge.cmd_tidy(backend, tidy_args(dry_run=True), silent=True), 0)
                self.assertEqual(bridge.load_threads()["old"], {"archive_uncertain": True})
                self.assertEqual(self.archived_ids(backend), [])

    def test_direct_rpc_cannot_start_or_queue_inside_tidy_decision(self):
        cases = [(method, phase) for method in ("turn/start", "thread/queue/add")
                 for phase in ("thread/queue/list", "thread/archive")]
        for method, phase in cases:
            with self.subTest(method=method, phase=phase):
                self.records(("old",))
                backend = TidyClient({"old": self.thread("old")})
                checked, release, attempted, mutated = [threading.Event() for _ in range(4)]
                order, errors = [], []
                original = backend.call

                def call_backend(name, params=None, timeout=60):
                    if name == phase:
                        self.assertGreater(daemon.inflight["old"], 0)
                        checked.set()
                        if not release.wait(2):
                            raise RuntimeError("barrera de prueba agotada")
                    if name == method:
                        order.append("mutation")
                        mutated.set()
                        return {"result": {"turn": {"id": "synthetic-turn"}}}
                    if name == "thread/archive":
                        order.append("archive")
                    return original(name, params, timeout)

                backend.call = call_backend
                daemon = self.fake_daemon(backend)

                def sweep():
                    try:
                        bridge.cmd_tidy(daemon, tidy_args(), silent=True)
                    except BaseException as exc:
                        errors.append(exc)

                def mutate():
                    attempted.set()
                    try:
                        daemon.rpc(method, {"threadId": "old"})
                    except BaseException as exc:
                        errors.append(exc)

                tidier, caller = threading.Thread(target=sweep), threading.Thread(target=mutate)
                tidier.start()
                try:
                    self.assertTrue(checked.wait(1))
                    caller.start()
                    self.assertTrue(attempted.wait(1))
                    self.assertFalse(mutated.wait(0.1), "RPC se coló entre comprobación y archive")
                    self.assertIn("result", daemon.rpc("bridge/ping", {}))
                finally:
                    release.set()
                    tidier.join(2)
                    if caller.ident:
                        caller.join(2)
                self.assertFalse(tidier.is_alive())
                self.assertFalse(caller.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(order, ["archive", "mutation"])

    def test_mutation_already_in_flight_wins_before_tidy(self):
        for method in ("turn/start", "thread/queue/add"):
            with self.subTest(method=method):
                self.records(("old",))
                backend = TidyClient({"old": self.thread("old")})
                entered, release, sweeping, inspected = [threading.Event() for _ in range(4)]
                errors = []
                original = backend.call

                def call_backend(name, params=None, timeout=60):
                    if name == method:
                        entered.set()
                        if not release.wait(2):
                            raise RuntimeError("barrera de prueba agotada")
                        if name == "thread/queue/add":
                            backend.pending.add("old")
                        return {"result": {"turn": {"id": "synthetic-turn"}}}
                    if name == "thread/read":
                        inspected.set()
                    return original(name, params, timeout)

                backend.call = call_backend
                daemon = self.fake_daemon(backend)

                def mutate():
                    try:
                        daemon.rpc(method, {"threadId": "old"})
                    except BaseException as exc:
                        errors.append(exc)

                def sweep():
                    sweeping.set()
                    try:
                        bridge.cmd_tidy(daemon, tidy_args(), silent=True)
                    except BaseException as exc:
                        errors.append(exc)

                caller, tidier = threading.Thread(target=mutate), threading.Thread(target=sweep)
                caller.start()
                try:
                    self.assertTrue(entered.wait(1))
                    tidier.start()
                    self.assertTrue(sweeping.wait(1))
                    self.assertFalse(inspected.wait(0.1))
                finally:
                    release.set()
                    caller.join(2)
                    if tidier.ident:
                        tidier.join(2)
                self.assertFalse(caller.is_alive())
                self.assertFalse(tidier.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(self.archived_ids(backend), [])

    def test_old_daemon_without_atomic_operation_fails_closed(self):
        self.records(("old",))
        backend = TidyClient({"old": self.thread("old")})
        original = backend.call

        def call_backend(method, params=None, timeout=60):
            if method == "bridge/tidy-archive":
                return {"error": {"message": "método no disponible"}}
            return original(method, params, timeout)

        backend.call = call_backend
        self.assertEqual(bridge.cmd_tidy(backend, tidy_args(), silent=True), 1)
        self.assertEqual(self.archived_ids(backend), [])
        self.assertNotIn("archived_at", bridge.load_threads()["old"])

    def test_keep_concurrent_with_archive_does_not_report_false_ok(self):
        self.records(("old",))
        client = TidyClient({"old": self.thread("old")})
        archiving, release, keeping = [threading.Event() for _ in range(3)]
        result = []
        original = client.call

        def call_backend(method, params=None, timeout=60):
            if method == "thread/archive":
                archiving.set()
                if not release.wait(2):
                    raise RuntimeError("barrera de prueba agotada")
            return original(method, params, timeout)

        client.call = call_backend

        def keep():
            keeping.set()
            result.append(bridge.cmd_keep(argparse.Namespace(thread="old"), True))

        tidier = threading.Thread(target=lambda: bridge.cmd_tidy(client, tidy_args(), silent=True))
        keeper = threading.Thread(target=keep)
        tidier.start()
        try:
            self.assertTrue(archiving.wait(1))
            keeper.start()
            self.assertTrue(keeping.wait(1))
        finally:
            release.set()
            tidier.join(2)
            if keeper.ident:
                keeper.join(2)
        self.assertFalse(keeper.is_alive())
        self.assertEqual(result, [5])
        self.assertNotEqual(bridge.load_threads()["old"].get("keep"), True)

    def test_timed_out_tidy_cannot_archive_after_successful_keep(self):
        self.records(("old",))
        backend = TidyClient({"old": self.thread("old")})
        daemon = self.fake_daemon(backend)
        entered, finished = threading.Event(), threading.Event()
        replies, handlers = [], []
        cycle = daemon.cycle_locks["old"]

        def timed_out_call(method, params, timeout=60):
            request = dict(params, _timeout=0.05, _deadline=time.monotonic() + 0.05)

            def handle():
                entered.set()
                try:
                    replies.append(daemon.rpc(method, request))
                except Exception as exc:
                    replies.append({"error": {"message": str(exc)}})
                finally:
                    finished.set()

            handler = threading.Thread(target=handle)
            handlers.append(handler)
            handler.start()
            self.assertTrue(entered.wait(1))
            finished.wait(0.15)
            return {"error": {"message": "timeout del cliente sintético"}}

        cycle.acquire()
        try:
            with self.assertRaises(RuntimeError):
                bridge._tidy_one(Mock(call=timed_out_call), "old", time.time() - 8 * 3600, False, True)
            self.assertEqual(bridge.cmd_keep(argparse.Namespace(thread="old"), True), 0)
        finally:
            cycle.release()
            for handler in handlers:
                handler.join(2)
        self.assertTrue(finished.is_set())
        self.assertIn("error", replies[0])
        self.assertEqual(self.archived_ids(backend), [])
        self.assertTrue(bridge.load_threads()["old"]["keep"])

    def test_expired_tidy_request_has_no_effects(self):
        self.records(("old",))
        backend = TidyClient({"old": self.thread("old")})
        daemon = self.fake_daemon(backend)
        with self.assertRaises(TimeoutError):
            daemon.rpc("bridge/tidy-archive", {"threadId": "old", "cutoff": time.time(),
                                               "_deadline": time.monotonic() - 1})
        self.assertEqual(backend.calls, [])
        self.assertNotIn("archived_at", bridge.load_threads()["old"])

    def test_deadline_is_checked_again_before_archive(self):
        self.records(("old",))
        backend = TidyClient({"old": self.thread("old")})
        daemon = self.fake_daemon(backend)
        clock = [100.0]
        original = backend.call

        def call_backend(method, params=None, timeout=60):
            if method == "thread/queue/list":
                clock[0] = 200.0
            return original(method, params, timeout)

        backend.call = call_backend
        with patch.object(bridge.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(TimeoutError):
                daemon.rpc("bridge/tidy-archive", {"threadId": "old", "cutoff": time.time(),
                                                   "_deadline": 150.0})
        self.assertEqual(self.archived_ids(backend), [])
        self.assertNotIn("archived_at", bridge.load_threads()["old"])

    def test_client_transmits_only_relative_timeout(self):
        client = object.__new__(bridge.Client)
        client.c = Mock()
        client.f = Mock()
        client.f.readline.return_value = '{"result": {}}\n'
        with patch.object(bridge.time, "monotonic", return_value=100.0):
            client.call("bridge/tidy-archive", {"threadId": "old", "_deadline": 104.0}, timeout=10)
            client.call("thread/search", {}, timeout=10)
        first, second = (json.loads(c.args[0]) for c in client.c.sendall.call_args_list)
        self.assertNotIn("_deadline", first["params"])
        self.assertEqual(first["params"]["_timeout"], 4.0)
        self.assertNotIn("_deadline", second["params"])
        self.assertEqual(second["params"]["_timeout"], 10)

    def test_daemon_accepts_client_from_another_monotonic_clock(self):
        # Un cliente recién arrancado tiene un reloj cerca de cero; el daemon, horas.
        d = object.__new__(bridge.Daemon)
        d.running = {}
        d.workers = {}
        with patch.object(bridge.time, "monotonic", return_value=50_000.0):
            r = d.rpc("bridge/ping", {"_timeout": 10})
        self.assertIn("result", r)

    def test_registry_lock_wait_respects_deadline(self):
        lock = bridge._thread_lock()
        try:
            with self.assertRaises(TimeoutError):
                bridge._thread_lock(time.monotonic() + 0.02)
        finally:
            bridge._release_thread_lock(lock)

    def test_daemon_persists_archive_without_client_readback(self):
        self.records(("old",))
        backend = TidyClient({"old": self.thread("old")})
        daemon = self.fake_daemon(backend)
        reply = daemon.rpc("bridge/tidy-archive", {"threadId": "old", "cutoff": time.time()})
        self.assertEqual(reply["result"]["status"], "archived")
        self.assertIn("archived_at", bridge.load_threads()["old"])

    def test_daemon_checks_keep_and_registration_itself(self):
        self.records(("kept",))
        bridge.save_thread("kept", keep=True)
        backend = TidyClient({tid: self.thread(tid) for tid in ("kept", "outside")})
        daemon = self.fake_daemon(backend)
        for tid in ("kept", "outside"):
            reply = daemon.rpc("bridge/tidy-archive", {"threadId": tid, "cutoff": time.time()})
            self.assertEqual(reply["result"]["status"], "skipped")
        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
