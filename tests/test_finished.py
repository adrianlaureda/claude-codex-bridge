"""Pruebas del dueño del hilo, las marcas de lectura y `finished` (hilos terminados sin leer)."""
import argparse
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bridge_finished", ROOT / "scripts" / "bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)

TID = "01a14de5-0efa-7ed3-a4d6-301b48fb6409"
OTRO = "01a14dfc-7508-7c00-a0ea-4a7ea4fa7b08"


def evento(kind, stamp, **payload):
    return json.dumps({"timestamp": stamp, "type": "event_msg", "payload": {"type": kind, **payload}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "bridge"
        self.sessions = base / "sessions"
        self.patches = [
            patch.object(bridge, "HOME", self.home),
            patch.object(bridge, "THREADS", self.home / "threads.json"),
            patch.object(bridge, "SESSIONS", self.sessions),
        ]
        for p in self.patches:
            p.start()
        self.now = time.time()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def rollout(self, tid, lines, segment=""):
        day = datetime.fromtimestamp(self.now)
        folder = self.sessions / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-{day:%Y-%m-%dT%H-%M-%S}-{tid}{segment}.jsonl"
        path.write_text("\n".join(lines) + "\n")
        return path


class FinishedTests(Base):
    def test_terminado_sin_leer_y_leido(self):
        bridge.save_thread(TID, **{"from": "sesion-a", "agent": "noche-x", "title": "Sol r1"})
        self.rollout(TID, [evento("task_started", "2026-10-18T07:47:36Z"),
                           evento("task_complete", "2026-10-18T07:48:21Z", last_agent_message="APROBADO")])
        filas = bridge.finished_threads(hours=24, now_ts=self.now)
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["state"], "done")
        self.assertEqual(filas[0]["agent"], "noche-x")
        self.assertTrue(filas[0]["unread"])

        bridge.mark_consumed(TID)
        self.assertFalse(bridge.finished_threads(hours=24, now_ts=self.now)[0]["unread"])

    def test_turno_en_marcha_no_es_pendiente(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        self.rollout(TID, [evento("task_complete", "2026-10-18T07:00:00Z"),
                           evento("task_started", "2026-10-18T07:10:00Z")])
        fila = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(fila["state"], "running")
        self.assertFalse(fila["unread"])

    def test_turno_nuevo_tras_leer_vuelve_a_pendiente(self):
        bridge.save_thread(TID, **{"from": "sesion-a", "consumed_at": 1.0})
        self.rollout(TID, [evento("turn_aborted", "2026-10-18T07:00:00Z")])
        fila = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(fila["state"], "aborted")
        self.assertTrue(fila["unread"])

    def test_filtra_por_dueno_y_ignora_hilos_no_registrados(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        bridge.save_thread(OTRO, **{"from": "sesion-b"})
        self.rollout(TID, [evento("task_complete", "2026-10-18T07:00:00Z")])
        self.rollout(OTRO, [evento("task_complete", "2026-10-18T07:00:00Z")])
        self.rollout("01a14c0b-1225-7ca3-85a9-7ef86600e40b", [evento("task_complete", "2026-10-18T07:00:00Z")])
        filas = bridge.finished_threads(hours=24, owner="sesion-a", now_ts=self.now)
        self.assertEqual([f["thread"] for f in filas], [TID])

    def test_segmentos_usan_el_rollout_mas_reciente(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        viejo = self.rollout(TID, [evento("task_complete", "2026-10-18T06:00:00Z")])
        os.utime(viejo, (self.now - 60, self.now - 60))
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z")], segment="_2")
        self.assertEqual(bridge.finished_threads(hours=24, now_ts=self.now)[0]["state"], "running")

    def test_mark_consumed_no_registra_hilos_ajenos(self):
        bridge.mark_consumed(TID)
        self.assertEqual(bridge.load_threads(), {})

    def test_cmd_finished_json_unread(self):
        bridge.save_thread(TID, **{"from": "sesion-a", "agent": "noche-x"})
        bridge.save_thread(OTRO, **{"from": "sesion-a", "consumed_at": self.now + 10})
        self.rollout(TID, [evento("task_complete", "2026-10-18T07:00:00Z")])
        self.rollout(OTRO, [evento("task_complete", "2026-10-18T07:00:00Z")])
        out = io.StringIO()
        with redirect_stdout(out):
            bridge.cmd_finished(argparse.Namespace(hours=24, owner=None, unread=True, json=True))
        filas = json.loads(out.getvalue())
        self.assertEqual([f["thread"] for f in filas], [TID])


class OwnerTests(unittest.TestCase):
    def test_from_por_defecto_usa_la_sesion_de_claude(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_HOST_SESSION_ID",)}
        env["CLAUDE_CODE_SESSION_ID"] = "sesion-dueña"
        env["CLAUDE_CODEX_BRIDGE_AGENT"] = "noche-y"
        argv = ["bridge.py", "start", "hola", "--role", "fast-worker", "--cwd", "/tmp"]
        captured = {}

        def fake_start(c, a):
            captured.update(sender=a.sender, agent=a.agent)
            return 0

        with patch.dict(os.environ, env, clear=True), patch("sys.argv", argv), \
                patch.object(bridge, "Client", lambda: object()), patch.object(bridge, "cmd_start", fake_start):
            bridge.main()
        self.assertEqual(captured, {"sender": "sesion-dueña", "agent": "noche-y"})


if __name__ == "__main__":
    unittest.main()
