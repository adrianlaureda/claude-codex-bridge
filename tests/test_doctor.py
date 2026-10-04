"""python3 -m unittest discover -s tests"""
import os, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import doctor

SRC = '''
WRITES = {"turn/start", "thread/gone"}
def handle_tool_call(o):
    m = o["method"]
    if m == "item/tool/call":
        pass
    if "pproval" in m:
        pass
class D:
    def note(self, o):
        m = o.get("method")
        if m == "turn/started":
            pass
def cmd(c, a):
    c.call("turn/start", {})
    c.call("bridge/ping", {})
    m = {"x": ("thread/archive", {"threadId": 1})}["x"]
'''

SCHEMA = {"requests": {"initialize", "turn/start", "thread/archive"},
          "notifications": {"turn/started"},
          "server_requests": {"item/tool/call", "item/commandExecution/requestApproval"},
          "client_notifications": {"initialized"}}


class Doctor(unittest.TestCase):
    def test_extracts_methods_from_source(self):
        r = doctor.relied_on(SRC)
        self.assertEqual(r["requests"], {"initialize", "turn/start", "thread/archive"})  # bridge/ping is internal
        self.assertEqual(r["notifications"], {"turn/started"})
        self.assertEqual(r["server_requests"], {"item/tool/call"})
        self.assertEqual(r["substrings"], {"pproval"})
        self.assertEqual(r["routing"], {"turn/start", "thread/gone"})

    def test_all_present(self):
        rep = doctor.check(SRC, SCHEMA)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["missing"], [])
        self.assertEqual(len(rep["warnings"]), 1)  # thread/gone routed but absent: warning, not failure

    def test_names_each_moved_method(self):
        s = {k: set(v) for k, v in SCHEMA.items()}
        s["requests"].discard("thread/archive")
        s["notifications"] = {"turn/begun"}
        s["server_requests"] = {"item/tool/call"}  # no approval request left to decline
        rep = doctor.check(SRC, s)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["missing"], ["notification:turn/started", "server-request~pproval", "thread/archive"])

    def test_real_bridge_parses(self):
        r = doctor.relied_on((Path(doctor.BRIDGE)).read_text())
        for m in ("turn/start", "thread/resume", "thread/queue/add"):
            self.assertIn(m, r["requests"])
        self.assertIn("turn/completed", r["notifications"])

    @unittest.skipUnless(os.access(doctor.DEFAULT_CODEX, os.X_OK), "ChatGPT app not installed")
    def test_live_schema(self):
        rep, code = doctor.run()
        self.assertIn(code, (0, 1))
        self.assertGreater(rep["schema_counts"]["requests"], 50)


if __name__ == "__main__":
    unittest.main()
