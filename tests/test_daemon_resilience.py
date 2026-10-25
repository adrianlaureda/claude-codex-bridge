"""Pruebas sintéticas de resiliencia del daemon y sus app-servers."""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BRIDGE_SCRIPT = SCRIPTS / "bridge.py"
sys.path.insert(0, str(SCRIPTS))
import bridge  # noqa: E402


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

state = pathlib.Path(os.environ["FAKE_CODEX_STATE"])
mode = os.environ.get("FAKE_CODEX_MODE", "normal")


def bump(name):
    values = {}
    if state.exists():
        values = json.loads(state.read_text())
    values[name] = values.get(name, 0) + 1
    temporary = state.with_suffix(".tmp")
    temporary.write_text(json.dumps(values))
    temporary.replace(state)
    return values[name]


def reply(request, result=None):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                 "result": result or {}}) + "\n")
    sys.stdout.flush()


def error_reply(request, message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                 "error": {"message": message}}) + "\n")
    sys.stdout.flush()


server_number = bump("servers")
if mode == "startup_retry" and server_number == 1:
    print("synthetic startup failure", file=sys.stderr, flush=True)
    raise SystemExit(2)
if mode == "startup_stderr_sensitive":
    print("Error: prompt-secreto " + "sk" + "-synthetic-token-12345678901234567890 "
          + "Bearer " + "abcdefghijklmnopqrstuvwxyz " + "key=clave-secreta",
          file=sys.stderr, flush=True)
    print("detalle adicional", file=sys.stderr, flush=True)
    raise SystemExit(7)
if mode == "serve_rpc_error" and server_number >= 2:
    print("synthetic replacement startup failure", file=sys.stderr, flush=True)
    raise SystemExit(2)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        if mode == "delayed_pump_retry" and server_number == 1:
            error_reply(request, "synthetic initialization failure")
            continue
        reply(request, {"server": "synthetic"})
    elif method == "initialized":
        if mode == "serve_rpc_error" and server_number == 1:
            raise SystemExit(0)
    elif method == "thread/start":
        bump("thread_start")
        reply(request, {"thread": {"id": "synthetic-thread"}})
        if mode == "delayed_thread_start":
            raise SystemExit(0)
    elif method == "turn/start":
        bump("turn_start")
        if mode == "postwrite_turn_start":
            raise SystemExit(0)
        reply(request, {"turn": {"id": "synthetic-turn"}})
        if mode == "delayed_turn_start":
            raise SystemExit(0)
    elif method == "thread/resume":
        reply(request, {})
    elif method == "thread/archive":
        bump("thread_archive")
        if mode != "hold_call":
            reply(request, {})
    else:
        reply(request, {})
'''


class DaemonResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "bridge-home"
        self.home.mkdir()
        self.fake = self.root / "fake-codex"
        self.fake.write_text(FAKE_CODEX)
        self.fake.chmod(0o700)
        self.state = self.root / "fake-state.json"
        self.env = os.environ | {
            "CLAUDE_CODEX_BRIDGE_HOME": str(self.home),
            "CODEX_BIN": str(self.fake),
            "FAKE_CODEX_STATE": str(self.state),
            "PYTHONPATH": str(SCRIPTS),
        }
        self.environ_patch = patch.dict(os.environ, self.env, clear=False)
        self.environ_patch.start()

    def tearDown(self):
        self.environ_patch.stop()
        self.temp.cleanup()

    def bridge_globals(self, mode="normal"):
        self.state.write_text("{}")
        self.env["FAKE_CODEX_MODE"] = mode
        os.environ["FAKE_CODEX_MODE"] = mode
        bridge.CODEX = str(self.fake)
        bridge.HOME = self.home
        bridge.SOCK = self.home / "bridge.sock"
        bridge.LOG = self.home / "events.jsonl"

    def state_values(self):
        return json.loads(self.state.read_text())

    def records(self):
        return [json.loads(line) for line in (self.home / "events.jsonl").read_text().splitlines()]

    def test_deleted_daemon_cwd_still_starts_app_server(self):
        gone = self.root / "deleted-cwd"
        gone.mkdir()
        driver = self.root / "deleted-cwd-driver.py"
        driver.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "import bridge\n"
            "cwd = Path(os.environ['GONE_CWD'])\n"
            "os.chdir(cwd)\n"
            "cwd.rmdir()\n"
            "real_popen = bridge.subprocess.Popen\n"
            "def checked_popen(*args, **kwargs):\n"
            "    if 'cwd' not in kwargs:\n"
            "        raise FileNotFoundError('synthetic deleted inherited cwd')\n"
            "    return real_popen(*args, **kwargs)\n"
            "bridge.subprocess.Popen = checked_popen\n"
            "server = bridge.AppServer()\n"
            "print('READY', flush=True)\n"
            "server.close()\n"
        )
        env = self.env | {"GONE_CWD": str(gone)}
        result = subprocess.run([sys.executable, str(driver)], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("READY", result.stdout)

    def test_app_server_retries_one_startup_failure_and_logs_stderr(self):
        self.bridge_globals("startup_retry")
        server = bridge.AppServer()
        try:
            self.assertEqual(self.state_values()["servers"], 2)
        finally:
            server.close()
        startup = [r for r in self.records() if r["event"] == "app_server_start_error"]
        self.assertEqual(len(startup), 1)
        self.assertEqual(startup[0]["method"], "initialize")
        self.assertNotIn("stderr_error", startup[0])
        self.assertEqual(startup[0]["stderr_lines"], 1)

    def test_up_uses_stable_home_as_daemon_cwd(self):
        self.bridge_globals()
        with patch.object(bridge, "ping", side_effect=[None, None, {"pid": 123}]), \
             patch.object(bridge.subprocess, "Popen") as popen, \
             patch.object(bridge.time, "sleep"):
            bridge.up()
        self.assertEqual(popen.call_args.kwargs["cwd"], Path.home())

    def test_rpc_retries_once_when_request_was_not_written(self):
        self.bridge_globals()
        daemon = bridge.Daemon()
        try:
            first = daemon.main
            with patch.object(first, "_send", side_effect=BrokenPipeError("synthetic")):
                result = daemon.rpc("thread/archive", {})
            self.assertEqual(result["result"], {})
            self.assertEqual(self.state_values()["servers"], 2)
        finally:
            daemon.main.close()
            for worker in list(daemon.workers.values()):
                worker.close()

    def test_turn_start_error_after_write_is_not_retried(self):
        self.bridge_globals("postwrite_turn_start")
        daemon = bridge.Daemon()
        try:
            result = daemon.rpc("turn/start", {"threadId": "synthetic-thread", "_timeout": 2})
            self.assertEqual(result["error"]["message"], "Codex app server exited")
            values = self.state_values()
            self.assertEqual(values["servers"], 2)  # main + un único worker
            self.assertEqual(values["turn_start"], 1)
        finally:
            daemon.main.close()
            for worker in list(daemon.workers.values()):
                worker.close()

    def test_rpc_exception_is_written_to_events_and_daemon_log(self):
        self.bridge_globals("serve_rpc_error")
        subprocess.run([sys.executable, str(BRIDGE_SCRIPT), "up"], env=self.env,
                       cwd=ROOT, check=True, timeout=10)
        deadline = time.time() + 10
        while not (self.home / "bridge.sock").exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue((self.home / "bridge.sock").exists())
        client = socket.socket(socket.AF_UNIX)
        try:
            client.settimeout(10)
            client.connect(str(self.home / "bridge.sock"))
            client.sendall(b'{"method":"thread/archive","params":{}}\n')
            response = client.makefile().readline()
            self.assertIn('"error"', response)
        finally:
            client.close()

        deadline = time.time() + 10
        while time.time() < deadline:
            if (self.home / "events.jsonl").exists():
                records = self.records()
                if any(r["event"] == "rpc_error" for r in records):
                    break
            time.sleep(0.05)
        rpc_errors = [r for r in self.records() if r["event"] == "rpc_error"]
        self.assertEqual(len(rpc_errors), 1)
        self.assertEqual(rpc_errors[0]["method"], "thread/archive")
        daemon_log = (self.home / "daemon.log").read_text()
        self.assertIn("rpc error", daemon_log)
        daemon_up = next(r for r in self.records() if r["event"] == "daemon_up")
        os.kill(daemon_up["pid"], signal.SIGTERM)

    def test_partial_write_epipe_is_written_and_not_retried(self):
        for method, params, count_name in (
                ("thread/start", {"_timeout": 2}, "thread_start"),
                ("turn/start", {"threadId": "synthetic-thread", "_timeout": 2}, "turn_start")):
            with self.subTest(method=method):
                self.bridge_globals()
                daemon = bridge.Daemon()
                try:
                    real_write = bridge.os.write
                    failed = False
                    partial_fd = None

                    def partial_write(fd, data):
                        nonlocal failed, partial_fd
                        if partial_fd is None and f'"method": "{method}"'.encode() in data:
                            partial_fd = fd
                            return real_write(fd, data[:1])
                        if partial_fd == fd and not failed:
                            failed = True
                            raise BrokenPipeError("synthetic EPIPE after partial write")
                        return real_write(fd, data)

                    with patch.object(bridge.os, "write", side_effect=partial_write):
                        raised = None
                        try:
                            daemon.rpc(method, params)
                        except bridge.AppServerSendError as exc:
                            raised = exc
                    self.assertTrue(failed)
                    self.assertIsNotNone(raised)
                    self.assertTrue(raised.written)
                    values = self.state_values()
                    self.assertEqual(values.get("servers"), 2)
                    self.assertEqual(values.get(count_name, 0), 0)
                finally:
                    daemon.main.close()
                    for worker in list(daemon.workers.values()):
                        worker.close()

    def test_delayed_first_pump_does_not_consume_second_stdout(self):
        self.bridge_globals("delayed_pump_retry")
        original_pump = bridge.AppServer._pump
        first_reader_started = threading.Event()
        second_process_started = threading.Event()
        reader_numbers = []
        reader_lock = threading.Lock()
        pump_assertions = []
        popen_count = 0
        real_popen = bridge.subprocess.Popen

        class DelayedFirstStdout:
            def __init__(self, stream):
                self.stream = stream
                self.released = False

            def __iter__(self):
                for line in self.stream:
                    yield line
                    if not self.released:
                        self.released = True
                        first_reader_started.set()
                        if not second_process_started.wait(3):
                            pump_assertions.append("el segundo proceso no arrancó")

            def close(self):
                self.stream.close()

        def tracked_popen(*args, **kwargs):
            nonlocal popen_count
            process = real_popen(*args, **kwargs)
            popen_count += 1
            if popen_count == 1:
                process.stdout = DelayedFirstStdout(process.stdout)
            if popen_count == 2:
                second_process_started.set()
            return process

        def delayed_pump(server, proc, waiting, pump_done):
            with reader_lock:
                reader_numbers.append(len(reader_numbers) + 1)
                number = reader_numbers[-1]
            return original_pump(server, proc, waiting, pump_done)

        with patch.object(bridge.subprocess, "Popen", side_effect=tracked_popen), \
             patch.object(bridge.AppServer, "_pump", delayed_pump):
            server = bridge.AppServer()
        try:
            self.assertTrue(first_reader_started.is_set())
            self.assertEqual(popen_count, 2)
            self.assertEqual(len(reader_numbers), 2)
            self.assertEqual(pump_assertions, [])
        finally:
            server.close()

    def test_response_buffered_before_eof_is_delivered_and_bookkept(self):
        class DelayedStdout:
            def __init__(self, stream, gate, release, block_at):
                self.stream = stream
                self.gate = gate
                self.release = release
                self.block_at = block_at
                self.count = 0
                self.blocked = False

            def __iter__(self):
                for line in self.stream:
                    self.count += 1
                    if self.count == self.block_at and not self.blocked:
                        self.blocked = True
                        self.gate.set()
                        self.release.wait(3)
                    yield line

            def close(self):
                self.stream.close()

        for method, mode, block_at in (
                ("thread/start", "delayed_thread_start", 2),
                ("turn/start", "delayed_turn_start", 3)):
            with self.subTest(method=method):
                self.bridge_globals(mode)
                gate = threading.Event()
                release = threading.Event()
                popen_count = 0
                real_popen = bridge.subprocess.Popen

                def delayed_popen(*args, **kwargs):
                    nonlocal popen_count
                    process = real_popen(*args, **kwargs)
                    popen_count += 1
                    if popen_count == 2:
                        process.stdout = DelayedStdout(process.stdout, gate, release, block_at)
                    return process

                daemon = None
                result = {}
                try:
                    with patch.object(bridge.subprocess, "Popen", side_effect=delayed_popen):
                        daemon = bridge.Daemon()
                        params = {"_timeout": 3}
                        if method == "turn/start":
                            params["threadId"] = "synthetic-thread"

                        def invoke():
                            result["value"] = daemon.rpc(method, params)

                        caller = threading.Thread(target=invoke)
                        caller.start()
                        self.assertTrue(gate.wait(3))
                        self.assertTrue(caller.is_alive())
                        release.set()
                        caller.join(3)
                    self.assertFalse(caller.is_alive())
                    self.assertNotIn("error", result["value"])
                    if method == "thread/start":
                        self.assertIn("synthetic-thread", daemon.workers)
                    else:
                        self.assertEqual(daemon.running["synthetic-thread"], "synthetic-turn")
                finally:
                    release.set()
                    if daemon:
                        daemon.main.close()
                        for worker in list(daemon.workers.values()):
                            worker.close()

    def test_close_wakes_pending_call_without_thread_traceback(self):
        self.bridge_globals("hold_call")
        server = bridge.AppServer()
        result = {}
        thread_errors = []
        old_excepthook = threading.excepthook

        def capture_thread_error(args):
            thread_errors.append(args)

        def pending_call():
            try:
                result["value"] = server.call("thread/archive", {}, timeout=10)
            except BaseException as exc:
                result["exception"] = exc

        try:
            threading.excepthook = capture_thread_error
            call_thread = threading.Thread(target=pending_call)
            call_thread.start()
            deadline = time.time() + 3
            while self.state_values().get("thread_archive") != 1 and time.time() < deadline:
                time.sleep(0.01)
            server.close()
            call_thread.join(3)
            self.assertFalse(call_thread.is_alive())
            self.assertNotIn("exception", result)
            self.assertEqual(result["value"]["error"]["message"], "Codex app server exited")
            self.assertEqual(thread_errors, [])
        finally:
            threading.excepthook = old_excepthook

    def test_sensitive_stderr_is_not_persisted(self):
        self.bridge_globals("startup_stderr_sensitive")
        daemon_log = self.home / "daemon.log"
        with daemon_log.open("w") as output:
            process = subprocess.Popen([sys.executable, str(BRIDGE_SCRIPT), "serve"], env=self.env,
                                       cwd=self.root, stdout=output, stderr=subprocess.STDOUT)
            process.wait(timeout=10)
        events = (self.home / "events.jsonl").read_text()
        log = daemon_log.read_text()
        for secret in ("prompt-secreto", "synthetic-token", "Bearer abcdef", "clave-secreta",
                       "detalle adicional"):
            self.assertNotIn(secret, events)
            self.assertNotIn(secret, log)
        records = self.records()
        self.assertTrue(records)
        for record in records:
            if record["event"] == "app_server_start_error":
                self.assertIn("returncode", record)
                self.assertIn("stderr_lines", record)
                self.assertEqual(record.get("stderr_error"), "Error:")
                self.assertNotIn("stderr", record)

    def test_serve_survives_deleted_cwd_and_starts_thread(self):
        daemon_cwd = self.root / "daemon-cwd"
        daemon_cwd.mkdir()
        self.bridge_globals()
        process = subprocess.Popen([sys.executable, str(BRIDGE_SCRIPT), "serve"], env=self.env,
                                   cwd=daemon_cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True)
        client = None
        try:
            deadline = time.time() + 10
            while not (self.home / "bridge.sock").exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.home / "bridge.sock").exists())
            daemon_cwd.rmdir()
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(10)
            client.connect(str(self.home / "bridge.sock"))
            client.sendall(b'{"method":"thread/start","params":{}}\n')
            response = json.loads(client.makefile().readline())
            self.assertEqual(response["result"]["thread"]["id"], "synthetic-thread")
        finally:
            if client:
                client.close()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
