"""Pruebas unitarias del contrato seguro de ``bridge.py start`` y ``send``."""
import argparse
import importlib.util
import json
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
    def __init__(self, thread_id="thread-1"):
        self.thread_id = thread_id
        self.calls = []

    def call(self, method, params, timeout=60):
        self.calls.append((method, params, timeout))
        if method == "thread/start":
            return {"result": {"thread": {"id": self.thread_id}}}
        if method == "turn/start":
            return {"result": {"turn": {"id": "turn-1"}}}
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

    def tearDown(self):
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
        self.assertEqual([method for method, _, _ in client.calls], ["thread/start", "turn/start"])

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
