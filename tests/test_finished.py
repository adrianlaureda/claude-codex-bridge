"""Pruebas del dueño del hilo, las marcas de lectura y `finished` (hilos terminados sin leer)."""
import argparse
import hashlib
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
            patch.object(bridge, "ARCHIVED_SESSIONS", base / "archived"),
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
        self.assertEqual(filas[0]["state"], "done_unread")
        self.assertEqual(filas[0]["agent"], "noche-x")
        self.assertTrue(filas[0]["unread"])

        bridge.mark_consumed(TID)
        leído = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertFalse(leído["unread"])
        self.assertEqual(leído["state"], "done_read")

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
        self.assertEqual(fila["state"], "failed")
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

    def test_rollout_archivado_sigue_contando(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        archivo = Path(self.tmp.name) / "archived"
        archivo.mkdir()
        (archivo / f"rollout-2026-10-18T07-00-00-{TID}.jsonl").write_text(
            evento("task_complete", "2026-10-18T07:48:21Z", turn_id="01a14e2b-0000-7000-8000-000000000001") + "\n")
        fila = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(fila["state"], "done_unread")
        self.assertTrue(fila["unread"])

    def test_archivado_antiguo_se_ignora(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        archivo = Path(self.tmp.name) / "archived"
        archivo.mkdir()
        viejo = archivo / f"rollout-2026-10-10T07-00-00-{TID}.jsonl"
        viejo.write_text(evento("task_complete", "2026-10-10T07:48:21Z") + "\n")
        os.utime(viejo, (self.now - 3 * 86400, self.now - 3 * 86400))
        self.assertEqual(bridge.finished_threads(hours=24, now_ts=self.now), [])

    def test_task_aborted_es_pendiente(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="01a14e2b-0000-7000-8000-000000000001"),
                           evento("task_aborted", "2026-10-18T07:01:00Z")])
        fila = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(fila["state"], "failed")
        self.assertTrue(fila["unread"])

    def test_turno_que_acaba_tras_la_lectura_sigue_pendiente(self):
        """Carrera: se lee el turno 1, termina el 2 y luego se escribe la marca del 1."""
        t1, t2 = "01a14e2b-0000-7000-8000-000000000001", "01a14e2b-0000-7000-8000-000000000002"
        bridge.save_thread(TID, **{"from": "sesion-a"})
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id=t1),
                           evento("task_complete", "2026-10-18T07:01:00Z", turn_id=t1),
                           evento("task_started", "2026-10-18T07:02:00Z", turn_id=t2),
                           evento("task_complete", "2026-10-18T07:03:00Z", turn_id=t2)])
        bridge.mark_consumed(TID, t1, seen_at=self.now + 3600)  # hora de escritura muy posterior
        self.assertTrue(bridge.finished_threads(hours=24, now_ts=self.now)[0]["unread"])
        bridge.mark_consumed(TID, t2)
        self.assertFalse(bridge.finished_threads(hours=24, now_ts=self.now)[0]["unread"])

    def test_marca_solo_avanza(self):
        t1, t2 = "01a14e2b-0000-7000-8000-000000000001", "01a14e2b-0000-7000-8000-000000000002"
        bridge.save_thread(TID, **{"from": "sesion-a"})
        bridge.mark_consumed(TID, t2, seen_at=200.0)
        bridge.mark_consumed(TID, t1, seen_at=100.0)
        meta = bridge.load_threads()[TID]
        self.assertEqual((meta["consumed_turn"], meta["consumed_at"]), (t2, 200.0))

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

    def test_turno_actual_en_marcha_sustituye_al_final_anterior(self):
        bridge.save_thread(TID, **{"from": "sesion-a", "consumed_at": 0})
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="t1"),
                           evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t1"),
                           evento("task_started", "2026-10-18T07:02:00Z", turn_id="t2")])
        row = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual((row["state"], row["turn_id"]), ("running", "t2"))
        self.assertFalse(row["unread"])

    def test_turno_en_cola_no_se_muestra_como_terminado(self):
        bridge.save_thread(TID, **{"from": "sesion-a", "queued_entries": [
            {"id": "q1", "digest": "queued-prompt-hash"}]})
        self.rollout(TID, [evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t1")])
        row = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(row["state"], "running")
        self.assertFalse(row["unread"])

    def test_registro_antiguo_con_contador_de_cola_sigue_running(self):
        bridge.save_thread(TID, **{"from": "sesion-a", "queued_at": self.now - 5, "queued_count": 1})
        self.rollout(TID, [evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t1")])
        row = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(row["state"], "running")
        self.assertFalse(row["unread"])

    def test_interrupted_es_failed_en_status_y_finished(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        self.rollout(TID, [evento("task_aborted", "2026-10-18T07:01:00Z", turn_id="t1")])
        thread = {"turns": [{"id": "t1", "status": "interrupted",
                             "completedAt": "2026-10-18T07:01:00Z"}]}
        responses = [{"result": {"thread": thread}}, {"result": {"data": []}},
                     {"result": {"thread": thread}}, {"result": {"data": []}}]

        class ClientMock:
            def call(self, method, params):
                return responses.pop(0)

        output = io.StringIO()
        with patch.object(bridge, "writer_name", return_value="nobody"), redirect_stdout(output):
            bridge.cmd_status(ClientMock(), argparse.Namespace(thread=TID, json=True))
        self.assertEqual(json.loads(output.getvalue())["state"], "failed")
        self.assertEqual(bridge.finished_threads(hours=24, now_ts=self.now)[0]["state"], "failed")

    def test_cola_por_id_no_confunde_inicios_historicos_con_envio_pendiente(self):
        q1, manual, q2 = "q1", "manual", "q2"
        queued_at = datetime.fromtimestamp(self.now - 300).isoformat() + "Z"
        bridge.save_thread(TID, **{"from": "sesion-a", "queued_entries": [
            {"id": q2}]})
        self.rollout(TID, [evento("task_started", queued_at, turn_id=q1),
                           evento("item_completed", queued_at, turn_id=q1,
                                  item={"type": "UserMessage", "content": [{"text": "q1 prompt"}]}),
                           evento("task_complete", queued_at, turn_id=q1),
                           evento("task_started", queued_at, turn_id=manual),
                           evento("item_completed", queued_at, turn_id=manual,
                                  item={"type": "UserMessage", "content": [{"text": "manual prompt"}]}),
                           evento("task_complete", queued_at, turn_id=manual)])
        row = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(row["state"], "running")
        self.assertFalse(row["unread"])

    def test_turno_que_consume_envio_retira_su_id_de_cola(self):
        bridge.save_thread(TID, **{"from": "sesion-a", "queued_entries": [
            {"id": "q1", "client_user_message_id": "client-q1"}]})
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="t1"),
                           evento("item_completed", "2026-10-18T07:00:01Z", turn_id="t1",
                                  item={"type": "UserMessage", "clientId": "client-q1",
                                        "content": [{"text": "q1 prompt"}]}),
                           evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t1")])
        row = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(row["state"], "done_unread")
        self.assertEqual(bridge.load_threads()[TID]["queued_entries"], [])

    def test_mensaje_igual_anterior_no_consume_envio_nuevo(self):
        digest = hashlib.sha256(b"repetido").hexdigest()
        bridge.save_thread(TID, **{"from": "sesion-a", "queued_entries": [
            {"id": "q-nuevo", "digest": digest, "queued_at": self.now + 30}]})
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="t-viejo"),
                           evento("item_completed", "2026-10-18T07:00:01Z", turn_id="t-viejo",
                                  item={"type": "UserMessage", "content": [{"text": "repetido"}]}),
                           evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t-viejo")])
        bridge.finished_threads(hours=24, now_ts=self.now)
        self.assertEqual([q["id"] for q in bridge.load_threads()[TID]["queued_entries"]], ["q-nuevo"])

    def test_un_consumo_no_elimina_dos_envios_iguales_tras_dos_sondeos(self):
        digest = hashlib.sha256(b"igual").hexdigest()
        bridge.save_thread(TID, **{"from": "sesion-a", "queued_entries": [
            {"id": "q1", "digest": digest, "client_user_message_id": "client-q1",
             "queued_at": bridge._epoch("2026-10-18T06:59:30Z")},
            {"id": "q2", "digest": digest, "client_user_message_id": "client-q2",
             "queued_at": bridge._epoch("2026-10-18T06:59:40Z")}]})
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="t1"),
                           evento("item_completed", "2026-10-18T07:00:01Z", turn_id="t1",
                                  item={"type": "UserMessage", "clientId": "client-q1",
                                        "content": [{"text": "igual"}]}),
                           evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t1")])
        bridge.finished_threads(hours=24, now_ts=self.now)
        bridge.finished_threads(hours=24, now_ts=self.now)
        self.assertEqual([q["id"] for q in bridge.load_threads()[TID]["queued_entries"]], ["q2"])

    def test_wait_for_reply_se_conserva_si_el_tail_recorta_el_turno(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        wait = {"type": "DynamicToolCall", "tool": "send_to_claude",
                "arguments": {"message": "¿Respuesta?", "wait_for_reply": True}}
        path = self.rollout(TID, [evento("task_started", "2026-10-25T06:00:00Z", turn_id="t1"),
                                  evento("item_completed", "2026-10-25T06:00:01Z", turn_id="t1", item=wait),
                                  evento("item_completed", "2026-10-25T06:00:02Z", turn_id="t1",
                                         item={"type": "Other", "data": "x" * 2_100_000}),
                                  evento("task_complete", "2026-10-25T06:01:00Z", turn_id="t1")])
        row = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(row["state"], "running")  # incertidumbre conservadora al perder la señal
        self.assertFalse(row["unread"])

    def test_waiting_input_leido_no_aparece_como_unread(self):
        bridge.save_thread(TID, **{"from": "sesion-a", "consumed_turn": "t1"})
        wait = {"type": "DynamicToolCall", "tool": "send_to_claude",
                "arguments": {"message": "¿Respuesta?", "wait_for_reply": True}}
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="t1"),
                           evento("item_completed", "2026-10-18T07:00:01Z", turn_id="t1", item=wait),
                           evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t1")])
        row = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(row["state"], "waiting_input")
        self.assertFalse(row["unread"])

    def test_last_changed_no_avanza_por_mtime_sin_transicion(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        path = self.rollout(TID, [evento("task_complete", "2026-10-18T07:00:00Z", turn_id="t1")])
        before = bridge.finished_threads(hours=24, now_ts=self.now)[0]["last_changed_at"]
        os.utime(path, (self.now + 30, self.now + 30))
        after = bridge.finished_threads(hours=24, now_ts=self.now + 31)[0]["last_changed_at"]
        self.assertEqual(after, before)

    def test_status_json_revalida_turno_despues_de_consultar_cola(self):
        responses = [
            {"result": {"thread": {"turns": [{"id": "t1", "status": "completed",
                                                "updatedAt": "2026-10-18T07:00:00Z"}]}}},
            {"result": {"data": []}},
            {"result": {"thread": {"turns": [{"id": "t2", "status": "inProgress",
                                                "updatedAt": "2026-10-18T07:01:00Z"}]}}},
            {"result": {"data": []}},
        ]

        class ClientMock:
            def call(self, method, params):
                return responses.pop(0)

        output = io.StringIO()
        with patch.object(bridge, "writer_name", return_value="nobody"), redirect_stdout(output):
            bridge.cmd_status(ClientMock(), argparse.Namespace(thread=TID, json=True))
        row = json.loads(output.getvalue())
        self.assertEqual((row["state"], row["turn_id"]), ("running", "t2"))

    def test_status_json_cola_vacia_reconcilia_envio_consumido(self):
        bridge.save_thread(TID, **{"queued_entries": [{"id": "q1", "client_user_message_id": "client-q1"}]})
        thread = {"turns": [{"id": "t2", "status": "completed",
                             "completedAt": datetime.fromtimestamp(self.now).isoformat() + "Z",
                             "items": [{"type": "UserMessage", "clientId": "client-q1",
                                        "content": [{"text": "mensaje"}]}]}]}
        responses = [
            {"result": {"thread": thread}}, {"result": {"data": []}},
            {"result": {"thread": thread}}, {"result": {"data": []}},
        ]

        class ClientMock:
            def call(self, method, params):
                return responses.pop(0)
        output = io.StringIO()
        with patch.object(bridge, "writer_name", return_value="nobody"), redirect_stdout(output):
            bridge.cmd_status(ClientMock(), argparse.Namespace(thread=TID, json=True))
        self.assertNotEqual(json.loads(output.getvalue())["state"], "running")

    def test_rollout_sin_frontera_en_trozo_final_no_se_convierte_en_done(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        path = self.rollout(TID, [evento("task_started", "2026-10-25T06:00:00Z", turn_id="t1"),
                                  evento("item_completed", "2026-10-25T06:00:01Z", turn_id="t1",
                                         item={"type": "Other", "data": "x" * 2_100_000})])
        row = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertIn(row["state"], ("running", "unknown"))

    def test_final_sin_hora_confiable_no_se_declara_done(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        self.rollout(TID, [evento("task_complete", "timestamp-inválido", turn_id="t1")])
        self.assertEqual(bridge.finished_threads(hours=24, now_ts=self.now)[0]["state"], "running")

    def test_heuristica_de_palabras_no_convierte_cierre_en_waiting_input(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        tool = {"type": "DynamicToolCall", "tool": "send_to_claude",
                "arguments": {"message": 'La incidencia "blocked" ya está resuelta. Trabajo terminado.'}}
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="t1"),
                           evento("item_completed", "2026-10-18T07:00:30Z", turn_id="t1", item=tool),
                           evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t1")])
        self.assertNotEqual(bridge.finished_threads(hours=24, now_ts=self.now)[0]["state"], "waiting_input")

    def test_rollout_que_desaparece_durante_lectura_no_aborta_finished(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        self.rollout(TID, [evento("task_complete", "2026-10-18T07:00:00Z", turn_id="t1")])
        with patch.object(bridge, "tail_lines", side_effect=FileNotFoundError("rollout movido")):
            row = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertIn(row["state"], ("running", "unknown"))

    def test_send_a_cola_no_readquiere_threads_lock(self):
        class ClientMock:
            def call(self, method, params):
                return {"result": {"queuedSubmission": {"id": "queue-1"}}}

        args = argparse.Namespace(thread=TID, prompt="synthetic", timeout=1, no_watch=True,
                                  model=None, effort=None)
        self.home.mkdir(parents=True, exist_ok=True)
        with patch.object(bridge, "ensure_unarchived", return_value=123), \
                patch.object(bridge, "_release_thread_lock"), \
                patch.object(bridge, "rollout_path", return_value=None), \
                patch.object(bridge, "held_by_app", return_value=True), \
                patch.object(bridge, "app_port_up", return_value=False), \
                patch.object(bridge, "append"), \
                patch.object(bridge, "_thread_lock", side_effect=AssertionError("lock readquirido")):
            self.assertEqual(bridge.cmd_send(ClientMock(), args), 0)

    def test_queue_delete_retira_el_id_persistido(self):
        bridge.save_thread(TID, **{"queued_entries": [{"id": "q1", "digest": "hash"},
                                                       {"id": "q2", "digest": "hash2"}]})
        c = type("ClientMock", (), {"call": lambda self, method, params: {"result": {}}})()
        args = argparse.Namespace(thread=TID, delete="q1")
        self.assertEqual(bridge.cmd_queue(c, args), 0)
        self.assertEqual([q["id"] for q in bridge.load_threads()[TID]["queued_entries"]], ["q2"])

    def test_send_to_claude_que_pide_respuesta_queda_waiting_input(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        tool = {"type": "DynamicToolCall", "tool": "send_to_claude",
                "arguments": {"message": "¿Puedes confirmar qué opción prefieres?", "wait_for_reply": True}}
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="t1"),
                           evento("item_completed", "2026-10-18T07:00:30Z", turn_id="t1", item=tool),
                           evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t1")])
        row = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(row["state"], "waiting_input")
        self.assertTrue(row["unread"])

    def test_cierre_estructurado_limpia_una_peticion_anterior(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        ask = {"type": "DynamicToolCall", "tool": "send_to_claude",
               "arguments": {"message": "¿Confirmas?", "wait_for_reply": True}}
        close = {"type": "DynamicToolCall", "tool": "send_to_claude",
                 "arguments": {"message": "Confirmado y terminado.", "wait_for_reply": False}}
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="t1"),
                           evento("item_completed", "2026-10-18T07:00:10Z", turn_id="t1", item=ask),
                           evento("item_completed", "2026-10-18T07:00:20Z", turn_id="t1", item=close),
                           evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t1")])
        self.assertNotEqual(bridge.finished_threads(hours=24, now_ts=self.now)[0]["state"], "waiting_input")

    def test_hilo_antiguo_sin_wait_for_reply_acepta_el_final(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        tool = {"type": "DynamicToolCall", "tool": "send_to_claude",
                "arguments": {"message": "¿Puedes revisar esta decisión?"}}
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="t1"),
                           evento("item_completed", "2026-10-18T07:00:10Z", turn_id="t1", item=tool),
                           evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t1")])
        self.assertEqual(bridge.finished_threads(hours=24, now_ts=self.now)[0]["state"], "done_unread")

    def test_finished_watch_termina_al_cambiar_un_hilo(self):
        before = [{"thread": TID, "state": "running", "last_changed_at": "a", "turn_id": "t1"}]
        after = [{"thread": TID, "state": "done_unread", "last_changed_at": "b", "turn_id": "t1"}]
        output = io.StringIO()
        args = argparse.Namespace(hours=24, owner=None, unread=False, json=True,
                                  watch=True, interval=0)
        with patch.object(bridge, "finished_threads", side_effect=[before, after]), \
                patch.object(bridge.time, "sleep") as pause, redirect_stdout(output):
            bridge.cmd_finished(args)
        self.assertEqual(json.loads(output.getvalue())[0]["state"], "done_unread")
        pause.assert_called_once_with(0)

    def test_finished_watch_ignora_lectura_incierta_durante_rename(self):
        before = [{"thread": TID, "state": "done_unread", "last_changed_at": "a", "turn_id": "t1"}]
        uncertain = [{"thread": TID, "state": "running", "last_changed_at": None, "turn_id": None,
                      "uncertain": True}]
        after = [{"thread": TID, "state": "running", "last_changed_at": "b", "turn_id": "t2"}]
        output = io.StringIO()
        args = argparse.Namespace(hours=24, owner=None, unread=False, json=True,
                                  watch=True, interval=0)
        with patch.object(bridge, "finished_threads", side_effect=[before, uncertain, after]), \
                patch.object(bridge.time, "sleep") as pause, redirect_stdout(output):
            bridge.cmd_finished(args)
        self.assertEqual(json.loads(output.getvalue())[0]["turn_id"], "t2")
        self.assertEqual(pause.call_count, 2)

    def test_status_json_devuelve_estado_unico_y_hora(self):
        thread = {"turns": [{"id": "t1", "status": "inProgress",
                             "startedAt": "2026-10-18T07:00:00Z",
                             "updatedAt": "2026-10-18T07:00:00Z"}]}
        responses = [{"result": {"thread": thread}}, {"result": {"data": []}},
                     {"result": {"thread": thread}}, {"result": {"data": []}}]

        class ClientMock:
            def call(self, method, params):
                return responses.pop(0)
        output = io.StringIO()
        with patch.object(bridge, "writer_name", return_value="nobody"), redirect_stdout(output):
            bridge.cmd_status(ClientMock(), argparse.Namespace(thread=TID, json=True))
        row = json.loads(output.getvalue())
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["last_changed_at"], "2026-10-18T07:00:00Z")

    def test_status_waiting_input_leido_no_es_unread(self):
        bridge.save_thread(TID, consumed_turn="t1")
        wait = {"type": "DynamicToolCall", "tool": "send_to_claude",
                "arguments": {"message": "¿Respuesta?", "wait_for_reply": True}}
        thread = {"turns": [{"id": "t1", "status": "completed",
                             "completedAt": "2026-10-18T07:01:00Z",
                             "items": [wait]}]}
        responses = [{"result": {"thread": thread}}, {"result": {"data": []}},
                     {"result": {"thread": thread}}, {"result": {"data": []}}]

        class ClientMock:
            def call(self, method, params):
                return responses.pop(0)

        output = io.StringIO()
        with patch.object(bridge, "writer_name", return_value="nobody"), redirect_stdout(output):
            bridge.cmd_status(ClientMock(), argparse.Namespace(thread=TID, json=True))
        row = json.loads(output.getvalue())
        self.assertEqual(row["state"], "waiting_input")
        self.assertFalse(row["unread"])

    def test_status_no_descarta_envio_creado_durante_lectura_de_cola(self):
        thread = {"turns": [{"id": "t1", "status": "completed",
                             "completedAt": "2026-10-18T07:01:00Z"}]}
        responses = [{"result": {"thread": thread}}, {"result": {"data": []}},
                     {"result": {"thread": thread}}, {"result": {"data": []}}]

        class ClientMock:
            def call(self, method, params):
                response = responses.pop(0)
                if method == "thread/queue/list" and not bridge.load_threads().get(TID):
                    bridge.save_thread(TID,
                                       queued_entries=[{"id": "q-new",
                                                        "queued_at": self_outer.now}])
                return response

        self_outer = self
        output = io.StringIO()
        with patch.object(bridge, "writer_name", return_value="nobody"), redirect_stdout(output):
            bridge.cmd_status(ClientMock(), argparse.Namespace(thread=TID, json=True))
        row = json.loads(output.getvalue())
        self.assertEqual(row["state"], "running")
        self.assertEqual([q["id"] for q in bridge.load_threads()[TID]["queued_entries"]], ["q-new"])

    def test_turno_manual_con_mismo_texto_no_consume_envio_sin_client_id(self):
        digest = hashlib.sha256(b"mismo texto").hexdigest()
        bridge.save_thread(TID, **{"from": "sesion-a", "queued_entries": [
            {"id": "q1", "digest": digest, "queued_at": bridge._epoch("2026-10-18T06:59:00Z"),
             "client_user_message_id": "client-q1"}]})
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="manual"),
                           evento("item_completed", "2026-10-18T07:00:01Z", turn_id="manual",
                                  item={"type": "UserMessage", "clientId": "manual-id",
                                        "content": [{"text": "mismo texto"}]}),
                           evento("task_complete", "2026-10-18T07:01:00Z", turn_id="manual")])
        bridge.finished_threads(hours=24, now_ts=self.now)
        self.assertEqual([q["id"] for q in bridge.load_threads()[TID]["queued_entries"]], ["q1"])

    def test_sesenta_y_cinco_turnos_no_se_reutilizan_para_el_envio_siguiente(self):
        digest = hashlib.sha256(b"repetido").hexdigest()
        entries = [{"id": f"q{i}", "digest": digest, "client_user_message_id": f"client-{i}"}
                   for i in range(66)]
        bridge.save_thread(TID, **{"from": "sesion-a", "queued_entries": entries})
        lines = []
        for i in range(65):
            turn = f"t{i:02d}"
            stamp = f"2026-10-18T07:{i // 60:02d}:{i % 60:02d}Z"
            lines.extend([evento("task_started", stamp, turn_id=turn),
                          evento("item_completed", stamp, turn_id=turn,
                                 item={"type": "UserMessage", "clientId": f"client-{i}",
                                       "content": [{"text": "repetido"}]}),
                          evento("task_complete", stamp, turn_id=turn)])
        self.rollout(TID, lines)
        bridge.finished_threads(hours=24, now_ts=self.now)
        bridge.finished_threads(hours=24, now_ts=self.now)
        self.assertEqual([q["id"] for q in bridge.load_threads()[TID]["queued_entries"]], ["q65"])

    def test_status_y_finished_usan_inicio_del_turno_activo_como_fecha_de_transicion(self):
        thread = {"turns": [{"id": "t1", "status": "inProgress",
                             "startedAt": "2026-10-18T07:00:00Z",
                             "updatedAt": "2026-10-18T07:15:00Z"}]}
        bridge.save_thread(TID, **{"from": "sesion-a"})
        self.rollout(TID, [evento("task_started", "2026-10-18T07:00:00Z", turn_id="t1")])
        responses = [{"result": {"thread": thread}}, {"result": {"data": []}},
                     {"result": {"thread": thread}}, {"result": {"data": []}}]

        class ClientMock:
            def call(self, method, params):
                return responses.pop(0)

        output = io.StringIO()
        with patch.object(bridge, "writer_name", return_value="nobody"), redirect_stdout(output):
            bridge.cmd_status(ClientMock(), argparse.Namespace(thread=TID, json=True))
        status = json.loads(output.getvalue())
        finished = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(status["last_changed_at"], "2026-10-18T07:00:00Z")
        self.assertEqual(status["last_changed_at"], finished["last_changed_at"])

    def test_send_persiste_intencion_y_client_id_antes_de_queue_add(self):
        self_outer = self
        class ClientMock:
            def call(self, method, params):
                if method == "thread/queue/add":
                    entry = bridge.load_threads()[TID]["queued_entries"][-1]
                    if entry.get("client_user_message_id") != params["clientUserMessageId"] or entry.get("status") != "pending":
                        raise AssertionError("el envío aún no estaba persistido antes del RPC")
                    self_outer.rollout(TID, [evento("task_started", "2026-10-25T07:00:00Z", turn_id="queued-turn"),
                                             evento("item_completed", "2026-10-25T07:00:01Z", turn_id="queued-turn",
                                                    item={"type": "UserMessage", "clientId": params["clientUserMessageId"],
                                                          "content": [{"text": "synthetic"}]}),
                                             evento("task_complete", "2026-10-25T07:00:02Z", turn_id="queued-turn")])
                    return {"result": {"queuedSubmission": {"id": "queue-server-id",
                                                                  "clientUserMessageId": params["clientUserMessageId"]}}}
                return {"result": {}}

        args = argparse.Namespace(thread=TID, prompt="synthetic", timeout=1, no_watch=True,
                                  model=None, effort=None)
        self.home.mkdir(parents=True)
        with patch.object(bridge, "ensure_unarchived", return_value=123), \
                patch.object(bridge, "_release_thread_lock"), \
                patch.object(bridge, "rollout_path", return_value=None), \
                patch.object(bridge, "held_by_app", return_value=True), \
                patch.object(bridge, "app_port_up", return_value=False), \
                patch.object(bridge, "append"):
            self.assertEqual(bridge.cmd_send(ClientMock(), args), 0)
        entry = bridge.load_threads()[TID]["queued_entries"][0]
        self.assertEqual((entry["id"], entry["status"]), ("queue-server-id", "accepted"))
        row = bridge.finished_threads(hours=24, now_ts=self.now)[0]
        self.assertEqual(row["state"], "done_unread")
        self.assertEqual(bridge.load_threads()[TID]["queued_entries"], [])

    def test_rechazo_confirmado_de_queue_add_retira_intencion(self):
        bridge.save_thread(TID, **{"from": "sesion-a"})
        self.rollout(TID, [evento("task_complete", "2026-10-18T07:01:00Z", turn_id="t1")])

        class ClientMock:
            def call(self, method, params):
                if method == "thread/queue/add":
                    return {"error": {"code": -32602, "message": "invalid params"}}
                return {"result": {}}

        args = argparse.Namespace(thread=TID, prompt="synthetic", timeout=1, no_watch=True,
                                  model=None, effort=None)
        self.home.mkdir(parents=True, exist_ok=True)
        with patch.object(bridge, "ensure_unarchived", return_value=123), \
                patch.object(bridge, "_release_thread_lock"), \
                patch.object(bridge, "rollout_path", return_value=None), \
                patch.object(bridge, "held_by_app", return_value=True), \
                patch.object(bridge, "app_port_up", return_value=False), \
                patch.object(bridge, "append"):
            with self.assertRaises(SystemExit) as raised:
                bridge.cmd_send(ClientMock(), args)
        self.assertEqual(raised.exception.code, 5)
        self.assertEqual(bridge.load_threads()[TID].get("queued_entries", []), [])
        self.assertEqual(bridge.finished_threads(hours=24, now_ts=self.now)[0]["state"], "done_unread")


class OwnerTests(unittest.TestCase):
    def test_host_session_tiene_preferencia(self):
        env = dict(os.environ, CLAUDE_CODE_HOST_SESSION_ID="anfitrion", CLAUDE_CODE_SESSION_ID="interna")
        argv = ["bridge.py", "start", "hola", "--role", "fast-worker", "--cwd", "/tmp"]
        captured = {}

        def fake_start(c, a):
            captured["sender"] = a.sender
            return 0

        with patch.dict(os.environ, env, clear=True), patch("sys.argv", argv), \
                patch.object(bridge, "Client", lambda: object()), patch.object(bridge, "cmd_start", fake_start):
            bridge.main()
        self.assertEqual(captured, {"sender": "anfitrion"})

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
