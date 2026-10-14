"""Pruebas unitarias del contrato seguro de ``bridge.py start`` y ``send``."""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bridge", ROOT / "scripts" / "bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
sys.modules["bridge"] = bridge
SPEC.loader.exec_module(bridge)


class FakeClient:
    def __init__(self, thread_id="thread-1", sections=None, fail_turn_archived_once=False):
        self.thread_id = thread_id
        self.sections = sections or []
        self.archived = False
        self.fail_turn_archived_once = fail_turn_archived_once
        self.calls = []

    def call(self, method, params, timeout=60):
        self.calls.append((method, params, timeout))
        if method == "thread/start":
            return {"result": {"thread": {"id": self.thread_id}}}
        if method == "turn/start":
            if self.fail_turn_archived_once:
                self.fail_turn_archived_once = False
                self.archived = True
                return {"error": {"message": "thread is archived"}}
            return {"result": {"turn": {"id": "turn-1"}}}
        if method == "threadSection/list":
            return {"result": {"data": self.sections}}
        if method == "thread/section/move":
            return {"result": {}}
        if method == "thread/read":
            return {"result": {"thread": {"id": self.thread_id, "archived": self.archived}}}
        if method == "thread/unarchive":
            self.archived = False
            return {"result": {}}
        if method == "bridge/running":
            return {"result": {"turnId": None}}
        if method == "thread/queue/list":
            return {"result": {"data": []}}
        return {"result": {}}


def start_args(cwd, **overrides):
    values = {
        "prompt": "haz la tarea",
        "title": None,
        "section": None,
        "cwd": str(cwd),
        "sender": "local_test",
        "role": "codex-code-worker",
        "sandbox": "workspace-write",
        "full_access": False,
        "no_network": False,
        "timeout": 1,
        "no_watch": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class StartPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name) / "workspace"
        self.cwd.mkdir()
        self.home = Path(self.temp.name) / "bridge-home"
        self.home.mkdir()
        self.paths = patch.multiple(
            bridge, HOME=self.home, THREADS=self.home / "threads.json", LOG=self.home / "events.jsonl"
        )
        self.paths.start()
        self.autotidy = patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_AUTOTIDY": "0"})
        self.autotidy.start()

    def tearDown(self):
        self.autotidy.stop()
        self.paths.stop()
        self.temp.cleanup()

    def router_result(self, **values):
        result = {"provider": "openai", "model": "gpt-test", "effort": "high"}
        result.update(values)
        return subprocess.CompletedProcess([], 0, json.dumps(result), "")

    def test_start_routes_before_thread_rpc(self):
        client = FakeClient()
        runner = Mock(return_value=self.router_result())
        args = start_args(self.cwd)
        with patch.object(bridge.subprocess, "run", runner), \
             patch.object(bridge, "save_thread"), \
             patch.object(bridge, "append"), \
             patch.object(bridge, "set_title"), \
             patch.object(bridge, "watch"):
            bridge.cmd_start(client, args)

        runner.assert_called_once_with(
            ["uv", "run", "python3", str(Path.home() / ".dotfiles/ai/scripts/model-routing.py"),
             "--role", args.role, "--format", "json"],
            cwd=Path.home() / ".dotfiles/ai",
            stdin=bridge.subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual([method for method, _, _ in client.calls],
                         ["thread/start", "turn/start", "threadSection/list"])

    def test_router_failure_invalid_json_and_provider_fail_before_rpc(self):
        cases = [
            subprocess.CompletedProcess([], 1, "", "router caído"),
            subprocess.CompletedProcess([], 0, "{", ""),
            subprocess.CompletedProcess([], 0, json.dumps({"provider": "otro", "model": "m", "effort": "e"}), ""),
        ]
        for result in cases:
            with self.subTest(result=result):
                client = FakeClient()
                with patch.object(bridge.subprocess, "run", return_value=result):
                    self.assertEqual(bridge.cmd_start(client, start_args(self.cwd)), 5)
                self.assertEqual(client.calls, [])

    def test_router_rejects_empty_model_and_effort(self):
        for field in ("model", "effort"):
            with self.subTest(field=field):
                values = {field: ""}
                with patch.object(bridge.subprocess, "run", return_value=self.router_result(**values)):
                    with self.assertRaises(ValueError):
                        bridge.route_model("codex-code-worker")

    def test_start_rejects_missing_or_non_directory_cwd_before_rpc(self):
        client = FakeClient()
        args = start_args("")
        with patch.object(bridge, "route_model") as route:
            self.assertEqual(bridge.cmd_start(client, args), 5)
        route.assert_not_called()
        self.assertEqual(client.calls, [])

        file_path = Path(self.temp.name) / "file"
        file_path.write_text("x")
        args = start_args(str(file_path))
        with patch.object(bridge, "route_model") as route:
            self.assertEqual(bridge.cmd_start(client, args), 5)
        route.assert_not_called()
        self.assertEqual(client.calls, [])

    def test_start_thread_and_turn_parameters_default_workspace(self):
        client = FakeClient()
        args = start_args(self.cwd)
        args.model = "override-no-permitido"
        args.effort = "minimal"
        with patch.object(bridge, "route_model", return_value={"model": "gpt-test", "effort": "high"}), \
             patch.object(bridge, "save_thread"), \
             patch.object(bridge, "append"), \
             patch.object(bridge, "set_title"), \
             patch.object(bridge, "watch"):
            bridge.cmd_start(client, args)

        thread = client.calls[0][1]
        turn = client.calls[1][1]
        self.assertEqual(thread["sandbox"], "workspace-write")
        self.assertEqual(thread["approvalPolicy"], "never")
        self.assertEqual(thread["config"], {"sandbox_workspace_write.network_access": True})
        self.assertEqual(thread["model"], "gpt-test")
        self.assertNotIn("sandboxPolicy", thread)
        self.assertEqual(turn["model"], "gpt-test")
        self.assertEqual(turn["effort"], "high")
        self.assertEqual(turn["sandboxPolicy"], {
            "type": "workspaceWrite", "networkAccess": True, "writableRoots": [str(self.cwd.resolve())]
        })

    def test_start_read_only_and_no_network(self):
        client = FakeClient()
        args = start_args(self.cwd, sandbox="read-only")
        with patch.object(bridge, "route_model", return_value={"model": "gpt-test", "effort": "low"}), \
             patch.object(bridge, "save_thread"), patch.object(bridge, "append"), \
             patch.object(bridge, "set_title"), patch.object(bridge, "watch"):
            bridge.cmd_start(client, args)

        thread = client.calls[0][1]
        turn = client.calls[1][1]
        self.assertEqual(thread["sandbox"], "read-only")
        self.assertNotIn("config", thread)
        self.assertEqual(turn["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})

        client = FakeClient()
        args = start_args(self.cwd, no_network=True)
        with patch.object(bridge, "route_model", return_value={"model": "gpt-test", "effort": "low"}), \
             patch.object(bridge, "save_thread"), patch.object(bridge, "append"), \
             patch.object(bridge, "set_title"), patch.object(bridge, "watch"):
            bridge.cmd_start(client, args)
        self.assertEqual(client.calls[0][1]["config"], {"sandbox_workspace_write.network_access": False})
        self.assertEqual(client.calls[1][1]["sandboxPolicy"]["networkAccess"], False)

    def test_start_full_access_only_explicit(self):
        client = FakeClient()
        args = start_args(self.cwd, sandbox=None, full_access=True)
        with patch.object(bridge, "route_model", return_value={"model": "gpt-test", "effort": "medium"}), \
             patch.object(bridge, "save_thread"), patch.object(bridge, "append"), \
             patch.object(bridge, "set_title"), patch.object(bridge, "watch"):
            bridge.cmd_start(client, args)

        self.assertEqual(client.calls[0][1]["sandbox"], "danger-full-access")
        self.assertNotIn("config", client.calls[0][1])
        self.assertEqual(client.calls[1][1]["sandboxPolicy"], {"type": "dangerFullAccess"})

    def test_start_persists_routing_and_sandbox_policy(self):
        client = FakeClient()
        args = start_args(self.cwd, sandbox="read-only")
        with patch.object(bridge, "route_model", return_value={"model": "gpt-test", "effort": "low"}), \
             patch.object(bridge, "save_thread") as save, patch.object(bridge, "append"), \
             patch.object(bridge, "set_title"), patch.object(bridge, "watch"):
            bridge.cmd_start(client, args)

        saved = save.call_args.kwargs
        self.assertEqual(saved["role"], args.role)
        self.assertEqual(saved["model"], "gpt-test")
        self.assertEqual(saved["effort"], "low")
        self.assertEqual(saved["sandbox"], "read-only")
        self.assertEqual(saved["cwd"], str(self.cwd.resolve()))

    def test_start_moves_to_environment_default_section(self):
        client = FakeClient(sections=[{"id": "agentes-id", "name": "🤖 Agentes-encargos"}])
        args = start_args(self.cwd)
        with patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_SECTION": "🤖 Agentes-encargos"}), \
             patch.object(bridge, "route_model", return_value={"model": "gpt-test", "effort": "low"}), \
             patch.object(bridge, "save_thread"), patch.object(bridge, "append"), \
             patch.object(bridge, "set_title"), patch.object(bridge, "watch"), \
             patch.object(bridge, "cmd_tidy"):
            self.assertEqual(bridge.cmd_start(client, args), 0)

        self.assertEqual(client.calls[-1][0], "thread/section/move")
        self.assertEqual(client.calls[-1][1], {"threadId": "thread-1", "sectionId": "agentes-id"})

    def test_start_uses_builtin_default_section(self):
        client = FakeClient(sections=[{"id": "agentes-id", "name": "🤖 Agentes-encargos"}])
        args = start_args(self.cwd)
        with patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_SECTION": ""}), \
             patch.object(bridge, "route_model", return_value={"model": "gpt-test", "effort": "low"}), \
             patch.object(bridge, "save_thread"), patch.object(bridge, "append"), \
             patch.object(bridge, "set_title"), patch.object(bridge, "watch"), \
             patch.object(bridge, "cmd_tidy"):
            self.assertEqual(bridge.cmd_start(client, args), 0)
        self.assertEqual(client.calls[-1][1], {"threadId": "thread-1", "sectionId": "agentes-id"})

    def test_start_missing_default_section_does_not_fail(self):
        client = FakeClient()
        args = start_args(self.cwd)
        with patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_SECTION": ""}), \
             patch.object(bridge, "route_model", return_value={"model": "gpt-test", "effort": "low"}), \
             patch.object(bridge, "save_thread"), patch.object(bridge, "append"), \
             patch.object(bridge, "set_title"), patch.object(bridge, "watch"), \
             patch.object(bridge, "cmd_tidy"):
            self.assertEqual(bridge.cmd_start(client, args), 0)

    def test_autotidy_zero_does_not_run_from_start(self):
        client = FakeClient()
        args = start_args(self.cwd)
        with patch.object(bridge, "route_model", return_value={"model": "gpt-test", "effort": "low"}), \
             patch.object(bridge, "save_thread"), patch.object(bridge, "append"), \
             patch.object(bridge, "set_title"), patch.object(bridge, "watch"), \
             patch.object(bridge, "cmd_tidy") as tidy:
            self.assertEqual(bridge.cmd_start(client, args), 0)
        tidy.assert_not_called()

    def test_autotidy_is_detached_and_start_does_not_wait(self):
        client = FakeClient()
        args = start_args(self.cwd)
        with patch.dict(os.environ, {"CLAUDE_CODEX_BRIDGE_AUTOTIDY": "1"}), \
             patch.object(bridge, "route_model", return_value={"model": "gpt-test", "effort": "low"}), \
             patch.object(bridge, "save_thread"), patch.object(bridge, "append"), \
             patch.object(bridge, "set_title"), patch.object(bridge, "watch"), \
             patch.object(bridge.subprocess, "Popen") as popen, \
             patch.object(bridge, "cmd_tidy") as tidy:
            self.assertEqual(bridge.cmd_start(client, args), 0)

        popen.assert_called_once_with(
            [sys.executable, str(Path(bridge.__file__).resolve()), "tidy", "--hours", "24", "--quiet"],
            stdin=bridge.subprocess.DEVNULL, stdout=bridge.subprocess.DEVNULL,
            stderr=bridge.subprocess.DEVNULL, start_new_session=True,
        )
        popen.return_value.wait.assert_not_called()
        tidy.assert_not_called()

    def test_send_unarchives_destination_before_turn(self):
        client = FakeClient()
        client.archived = True
        args = argparse.Namespace(thread="thread-1", prompt="sigue", model=None, effort=None,
                                  no_watch=True, timeout=1)
        with patch.object(bridge, "rollout_path", return_value=None), \
             patch.object(bridge, "held_by_app", return_value=False), \
             patch.object(bridge, "append"):
            self.assertEqual(bridge.cmd_send(client, args), 0)

        methods = [method for method, _, _ in client.calls]
        self.assertLess(methods.index("thread/unarchive"), methods.index("turn/start"))

    def test_send_retries_turn_start_once_after_archived_error(self):
        client = FakeClient(fail_turn_archived_once=True)
        args = argparse.Namespace(thread="thread-1", prompt="sigue", model=None, effort=None,
                                  no_watch=True, timeout=1)
        with patch.object(bridge, "rollout_path", return_value=None), \
             patch.object(bridge, "held_by_app", return_value=False), \
             patch.object(bridge, "append"):
            self.assertEqual(bridge.cmd_send(client, args), 0)

        methods = [method for method, _, _ in client.calls]
        self.assertEqual(methods.count("turn/start"), 2)
        self.assertEqual(methods.count("thread/unarchive"), 1)

    def test_send_preserves_registered_policy(self):
        client = FakeClient()
        args = argparse.Namespace(thread="thread-1", prompt="sigue", model=None, effort=None,
                                  no_watch=True, timeout=1)
        record = {
            "cwd": str(self.cwd.resolve()), "sandbox": "workspace-write", "network_access": False,
            "model": "gpt-stored", "effort": "low", "role": "codex-code-worker",
        }
        with patch.object(bridge, "load_threads", return_value={"thread-1": record}), \
             patch.object(bridge, "held_by_app", return_value=False), \
             patch.object(bridge, "rollout_path", return_value=None), \
             patch.object(bridge, "append"):
            bridge.cmd_send(client, args)

        turn = client.calls[-1][1]
        self.assertEqual(turn["model"], "gpt-stored")
        self.assertEqual(turn["effort"], "low")
        self.assertEqual(turn["sandboxPolicy"], {
            "type": "workspaceWrite", "networkAccess": False, "writableRoots": [str(self.cwd.resolve())]
        })

    def test_send_explicit_model_and_effort_override_registered_values(self):
        client = FakeClient()
        args = argparse.Namespace(thread="thread-1", prompt="sigue", model="gpt-explicit", effort="high",
                                  no_watch=True, timeout=1)
        record = {
            "cwd": str(self.cwd.resolve()), "sandbox": "read-only", "network_access": False,
            "model": "gpt-stored", "effort": "low", "role": "codex-code-worker",
        }
        with patch.object(bridge, "load_threads", return_value={"thread-1": record}), \
             patch.object(bridge, "held_by_app", return_value=False), \
             patch.object(bridge, "rollout_path", return_value=None), \
             patch.object(bridge, "append"):
            bridge.cmd_send(client, args)

        turn = client.calls[-1][1]
        self.assertEqual(turn["model"], "gpt-explicit")
        self.assertEqual(turn["effort"], "high")
        self.assertEqual(turn["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})

    def test_full_access_cannot_claim_network_is_disabled(self):
        client = FakeClient()
        args = start_args(self.cwd, sandbox=None, full_access=True, no_network=True)
        with patch.object(bridge, "route_model") as route:
            self.assertEqual(bridge.cmd_start(client, args), 5)
        route.assert_not_called()
        self.assertEqual(client.calls, [])

    def test_instructions_include_adri_prohibitions(self):
        for text in ("commit", "push", "merge", "deploy", "Abalar", "XADE", "Moodle", "datos de alumnado"):
            self.assertIn(text, bridge.INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
