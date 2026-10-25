"""Pruebas del arranque visible de sesiones Claude desde Codex."""
import argparse
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bridge_claude_start", ROOT / "scripts" / "bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
sys.modules["bridge_claude_start"] = bridge
SPEC.loader.exec_module(bridge)


class ClaudeStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name) / "workspace"
        self.cwd.mkdir()
        self.home = Path(self.temp.name) / "bridge-home"
        self.home.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def args(self, **overrides):
        values = {
            "prompt": "haz la tarea",
            "cwd": str(self.cwd),
            "name": None,
            "remote_control": True,
            "thread": "thread-123",
            "notify": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    @staticmethod
    def launch_result(name="prueba-visibilidad-bridge"):
        return subprocess.CompletedProcess(
            ["claude", "--bg"], 0, f"Starting background service…\nbackgrounded · 0faca2ea · {name}\n", ""
        )

    def test_parsea_linea_backgrounded_con_ansi(self):
        output = "\x1b[32mbackgrounded · 0faca2ea · prueba\x1b[0m"
        self.assertEqual(bridge.parse_backgrounded_id(output), "0faca2ea")

    def test_falla_si_no_hay_id_corto(self):
        result = subprocess.CompletedProcess(["claude"], 0, "background service iniciado", "")
        with patch.object(bridge.subprocess, "run", return_value=result) as runner, \
             patch.object(bridge, "append_started") as append:
            code = bridge.cmd_claude_start(self.args())
        self.assertNotEqual(code, 0)
        runner.assert_called_once()
        append.assert_not_called()

    def test_remote_control_devuelve_url(self):
        result = subprocess.CompletedProcess(["claude", "logs"], 0,
                                             "connecting\n/remote-control is active · "
                                             "https://claude.ai/code/session_ABC123\n", "")
        with patch.object(bridge.subprocess, "run", return_value=result):
            self.assertEqual(
                bridge.remote_control_url("0faca2ea", timeout=1),
                "https://claude.ai/code/session_ABC123",
            )

    def test_remote_control_termina_sin_url_en_timeout_acotado(self):
        result = subprocess.CompletedProcess(["claude", "logs"], 0, "connecting", "")
        with patch.object(bridge.subprocess, "run", return_value=result), \
             patch.object(bridge.time, "monotonic", side_effect=[0, 0, 1]), \
             patch.object(bridge.time, "sleep") as sleep:
            self.assertIsNone(bridge.remote_control_url("0faca2ea", timeout=0.01))
        sleep.assert_not_called()

    def test_registra_jsonl_con_from_thread_y_url(self):
        agents = [{"id": "0faca2ea", "sessionId": "session-uuid", "name": "nombre",
                   "status": "running", "state": "running", "cwd": str(self.cwd)}]

        def run(command, **kwargs):
            if command[1] == "--bg":
                self.assertEqual(command, ["claude", "--bg", "--name=nombre", "--remote-control=nombre",
                                           "--", "haz la tarea"])
                return self.launch_result()
            self.assertEqual(command, ["claude", "agents", "--json", "--all"])
            return subprocess.CompletedProcess(command, 0, json.dumps(agents), "")

        with patch.object(bridge, "HOME", self.home), \
             patch.object(bridge.subprocess, "run", side_effect=run), \
             patch.object(bridge, "remote_control_url", return_value="https://claude.ai/code/session_X"):
            code = bridge.cmd_claude_start(self.args(name="nombre"))

        self.assertEqual(code, 0)
        record = json.loads((self.home / "claude-started.jsonl").read_text().strip())
        self.assertEqual(record["from_thread"], "thread-123")
        self.assertEqual(record["session_id"], "session-uuid")
        self.assertEqual(record["remote_url"], "https://claude.ai/code/session_X")
        self.assertNotIn("prompt_preview", record)

    def test_no_remote_control_no_pasa_la_opcion(self):
        agents = [{"id": "0faca2ea", "sessionId": "session-uuid"}]

        def run(command, **kwargs):
            if command[1] == "--bg":
                return self.launch_result()
            self.assertEqual(command, ["claude", "agents", "--json", "--all"])
            return subprocess.CompletedProcess(command, 0, json.dumps(agents), "")

        with patch.object(bridge, "HOME", self.home), \
             patch.object(bridge.subprocess, "run", side_effect=run) as runner, \
             patch.object(bridge, "remote_control_url") as remote:
            code = bridge.cmd_claude_start(self.args(remote_control=False))

        self.assertEqual(code, 0)
        command = runner.call_args_list[0].args[0]
        self.assertEqual(command[:2], ["claude", "--bg"])
        self.assertRegex(command[2], r"^--name=codex \d{2}:\d{2} thre$")
        self.assertEqual(command[3:], ["--", "haz la tarea"])
        remote.assert_not_called()

    def test_claude_started_cruza_running_completed_unknown(self):
        records = [
            {"ts": "2026-10-09T10:00:00", "short_id": "run", "session_id": "sid-run",
             "name": "en curso", "cwd": str(self.cwd), "remote_url": "https://claude.ai/code/session_RUN"},
            {"ts": "2026-10-09T10:01:00", "short_id": "done", "session_id": "sid-done",
             "name": "terminada", "cwd": str(self.cwd), "remote_url": None},
            {"ts": "2026-10-09T10:02:00", "short_id": "gone", "session_id": "sid-gone",
             "name": "desconocida", "cwd": str(self.cwd), "remote_url": None},
        ]
        with patch.object(bridge, "HOME", self.home):
            for record in records:
                bridge.append_started(record)
        rows = [
            {"id": "run", "sessionId": "sid-run", "status": "running"},
            {"id": "done", "sessionId": "sid-done", "state": "completed"},
        ]
        output = io.StringIO()
        with patch.object(bridge, "HOME", self.home), \
             patch.object(bridge, "claude_agents_all", return_value=rows), \
             contextlib.redirect_stdout(output):
            code = bridge.cmd_claude_started(argparse.Namespace(last=3))
        text = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("running   en curso", text)
        self.assertIn("completed terminada", text)
        self.assertIn("unknown   desconocida", text)
        self.assertIn("claude --resume sid-run", text)
        self.assertIn("https://claude.ai/code/session_RUN", text)

    def test_notify_tolera_fallo_y_conserva_el_arranque(self):
        agents = [{"id": "0faca2ea", "sessionId": "session-uuid"}]

        def run(command, **kwargs):
            if command[1] == "--bg":
                return self.launch_result()
            return subprocess.CompletedProcess(command, 0, json.dumps(agents), "")

        with patch.object(bridge, "HOME", self.home), \
             patch.object(bridge.subprocess, "run", side_effect=run), \
             patch.object(bridge, "remote_control_url", return_value=None), \
             patch.object(bridge, "notify_started", side_effect=OSError("osascript no disponible")):
            code = bridge.cmd_claude_start(self.args(notify=True))
        self.assertEqual(code, 0)
        self.assertTrue((self.home / "claude-started.jsonl").exists())

    def test_timeout_de_agents_registra_pending_y_emite_linea_estable(self):
        prompt = "--alumnado dato sensible que no debe persistirse"

        def run(command, **kwargs):
            if command[1] == "--bg":
                return self.launch_result()
            raise subprocess.TimeoutExpired(command, 30)

        output = io.StringIO()
        with patch.object(bridge, "HOME", self.home), \
             patch.object(bridge.subprocess, "run", side_effect=run), \
             patch.object(bridge, "remote_control_url", return_value=None), \
             contextlib.redirect_stdout(output):
            code = bridge.cmd_claude_start(self.args(prompt=prompt, remote_control=True))

        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("CLAUDE-STARTED 0faca2ea", text)
        self.assertIn('resume="pending"', text)
        record = json.loads((self.home / "claude-started.jsonl").read_text().strip())
        self.assertIsNone(record["session_id"])
        self.assertNotIn("dato sensible", json.dumps(record, ensure_ascii=False))
        self.assertNotIn("dato sensible", text)

    def test_argumentos_seguros_para_prompt_que_empieza_por_guion(self):
        agents = [{"id": "0faca2ea", "sessionId": "session-uuid"}]
        prompt = "--alumnado dato"

        def run(command, **kwargs):
            if command[1] == "--bg":
                return self.launch_result()
            return subprocess.CompletedProcess(command, 0, json.dumps(agents), "")

        with patch.object(bridge, "HOME", self.home), \
             patch.object(bridge.subprocess, "run", side_effect=run) as runner, \
             patch.object(bridge, "remote_control_url", return_value=None):
            bridge.cmd_claude_start(self.args(prompt=prompt, name="nombre seguro"))
        self.assertEqual(runner.call_args_list[0].args[0], [
            "claude", "--bg", "--name=nombre seguro", "--remote-control=nombre seguro", "--", prompt,
        ])

    def test_claude_started_recupera_session_id_tardio(self):
        record = {"ts": "2026-10-09T10:00:00", "short_id": "late", "session_id": None,
                  "name": "codex 10:00 late", "cwd": str(self.cwd), "remote_url": None}
        with patch.object(bridge, "HOME", self.home):
            bridge.append_started(record)
        output = io.StringIO()
        with patch.object(bridge, "HOME", self.home), \
             patch.object(bridge, "claude_agents_all", return_value=[
                 {"id": "late", "sessionId": "late-session", "status": "running"}
             ]), \
             contextlib.redirect_stdout(output):
            code = bridge.cmd_claude_started(argparse.Namespace(last=None))
        self.assertEqual(code, 0)
        self.assertIn("session_id=late-session", output.getvalue())
        self.assertIn('resume="claude --resume late-session"', output.getvalue())

    def test_escritura_parcial_del_jsonl_falla_explicitamente(self):
        with patch.object(bridge, "HOME", self.home), \
             patch.object(bridge.os, "write", return_value=1) as write:
            with self.assertRaisesRegex(OSError, "escritura parcial"):
                bridge.append_started({"ts": "now"})
        write.assert_called_once()

    def test_notify_escapa_argv_real_de_osascript(self):
        result = subprocess.CompletedProcess(["osascript"], 0, "", "")
        with patch.object(bridge.subprocess, "run", return_value=result) as runner:
            bridge.notify_started('codex "nombre" \\ salto\n', "https://claude.ai/code/session_X")
        argv = runner.call_args.args[0]
        self.assertEqual(argv[:2], ["osascript", "-e"])
        script = argv[2]
        self.assertNotIn("\n", script)
        self.assertIn('display notification "codex \\"nombre\\" \\\\ salto ', script)
        self.assertIn('with title "Codex → Claude"', script)
        self.assertIn("https://claude.ai/code/session_X", script)


if __name__ == "__main__":
    unittest.main()
