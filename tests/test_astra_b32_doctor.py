"""Regresión del doctor con fuentes, ping y ejecutable completamente sintéticos."""
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("b32_doctor", ROOT / "scripts/doctor.py")
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)

SOURCE = '''
WRITES = {"turn/start", "thread/obsolete", "bridge/ping"}
def note(o):
    m = o.get("method")
    if m == "turn/completed":
        pass
def handle_tool_call(o):
    m = o["method"]
    if m == "item/tool/call":
        pass
    if "pproval" in m:
        pass
def command(c):
    c.call("initialize", {})
    c.call("turn/start", {})
    c.call("bridge/ping", {})
    table = {"archive": ("thread/archive", {})}
'''
SCHEMA = {
    "requests": {"initialize", "turn/start", "thread/archive"},
    "notifications": {"turn/completed"},
    "server_requests": {"item/tool/call", "item/requestApproval"},
    "client_notifications": {"initialized"},
}
FILES = {"requests": "ClientRequest.json", "notifications": "ServerNotification.json",
         "server_requests": "ServerRequest.json", "client_notifications": "ClientNotification.json"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("La prueba intentó acceder a la red o al daemon real")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(doctor, "daemon_state", lambda codex: {"up": False, "note": "ping sintético"})


@pytest.fixture
def sandbox(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts/doctor.py", scripts / "doctor.py")
    (scripts / "bridge.py").write_text('def ping():\n    return None\n')
    source = tmp_path / "source.py"
    source.write_text(SOURCE)
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({FILES[k]: {"oneOf": [
        {"properties": {"method": {"enum": [m]}}} for m in sorted(v)]}
        for k, v in SCHEMA.items()}))
    binary = tmp_path / "codex-falso"
    binary.write_text(f'''#!{sys.executable}
import json, pathlib, sys
root = pathlib.Path(__file__).resolve().parent
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("codex sintético")
    sys.exit(0)
assert sys.argv[1:5] == ["app-server", "generate-json-schema", "--experimental", "--out"]
assert len(sys.argv) == 6
out = pathlib.Path(sys.argv[5]).resolve()
assert out.is_relative_to(root)
for name, value in json.loads((root / "bundle.json").read_text()).items():
    (out / name).write_text(value if isinstance(value, str) else json.dumps(value))
mode = (root / "mode").read_text() if (root / "mode").exists() else "ok"
if mode == "timeout":
    import time
    time.sleep(10)
if mode == "fail":
    print("fallo sintético", file=sys.stderr)
    sys.exit(7)
''')
    binary.chmod(0o700)
    # El CLI solo puede leer su árbol y la biblioteca de Python. No hay socket real.
    (tmp_path / "launch.py").write_text('''import pathlib, runpy, subprocess, sys
root = pathlib.Path(__file__).resolve().parent
allowed = (root, pathlib.Path(sys.base_prefix).resolve(), pathlib.Path(sys.prefix).resolve())
real_run = subprocess.run
def bounded_run(command, **kwargs):
    if "generate-json-schema" in command and (root / "short-timeout").exists():
        kwargs["timeout"] = 0.1
    return real_run(command, **kwargs)
subprocess.run = bounded_run
def audit(event, args):
    if event == "subprocess.Popen":
        command = args[1]
        if (args[0] != "perl" or len(command) < 5 or command[1] != "-e"
                or command[3] != str(root / "codex-falso")
                or command[4:] != ["--version"] and command[4:8] != [
                    "app-server", "generate-json-schema", "--experimental", "--out"]):
            raise RuntimeError("subproceso fuera del doble sintético")
    if event in {"socket.connect", "socket.bind"}:
        raise RuntimeError("red prohibida en prueba")
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = pathlib.Path(args[0]).resolve()
        if not any(path.is_relative_to(base) for base in allowed):
            raise RuntimeError("lectura fuera del árbol sintético")
sys.addaudithook(audit)
sys.path.insert(0, str(root / "scripts"))
runpy.run_path(str(root / "scripts/doctor.py"), run_name="__main__")
''')
    return tmp_path, source, binary, bundle


def cli(sandbox, *extra):
    root, source, binary, _ = sandbox
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "TMPDIR": str(root),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    result = subprocess.run([sys.executable, str(root / "launch.py"), "--bridge", str(source),
                             "--codex", str(binary), *extra], env=env, cwd=root,
                            capture_output=True, text=True, timeout=10)
    assert result.stderr == "", result.stderr
    return result.returncode, json.loads(result.stdout)


def test_cli_compatible_and_verbose(sandbox):
    code, report = cli(sandbox, "--verbose")
    assert code == 0 and report["ok"] is True
    assert report["daemon"]["up"] is False
    assert report["codex_version"] == "codex sintético"
    assert report["missing"] == []
    assert len(report["warnings"]) == 1  # Las rutas internas no pertenecen al schema.
    assert report["relied_on"]["requests"] == sorted(SCHEMA["requests"])
    calls = [json.loads(line) for line in (sandbox[0] / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 2 and calls[0] == ["--version"]
    assert calls[1][:3] == ["app-server", "generate-json-schema", "--experimental"]


@pytest.mark.parametrize("kind,method,label", [
    ("requests", "turn/start", "turn/start"),
    ("notifications", "turn/completed", "notification:turn/completed"),
    ("server_requests", "item/tool/call", "server-request:item/tool/call"),
    ("server_requests", "item/requestApproval", "server-request~pproval"),
    ("client_notifications", "initialized", "notification:initialized"),
])
def test_cli_removed_method(sandbox, kind, method, label):
    bundle = sandbox[3]
    data = json.loads(bundle.read_text())
    data[FILES[kind]]["oneOf"] = [
        {"properties": {"method": {"enum": ["replacement/method" if m == method else m]}}}
        for m in sorted(SCHEMA[kind])]
    bundle.write_text(json.dumps(data))
    code, report = cli(sandbox)
    assert code == 1 and report["ok"] is False
    assert report["missing"] == [label]
    assert "relied_on" not in report


@pytest.mark.parametrize("value", [
    {}, {"oneOf": []}, {"oneOf": [{"$ref": "#/definitions/Event"}]},
    {"oneOf": [{"properties": {"method": {"enum": []}}}]},
    {"oneOf": [{"properties": {"method": {"enum": "initialized"}}}]},
    {"oneOf": [{"properties": {"method": {"enum": [42]}}}]},
    {"oneOf": [{"properties": {"method": {"enum": ["initialized", None]}}}]},
    {"oneOf": [{"properties": {"method": {"enum": ["initialized"]}}}, {}]},
    {"anyOf": [{"properties": {"method": {"enum": ["initialized"]}}}]},
    [], '{"oneOf":', "salida no JSON",
])
def test_cli_invalid_or_partial_schema(sandbox, value):
    data = json.loads(sandbox[3].read_text())
    data["ClientNotification.json"] = value
    sandbox[3].write_text(json.dumps(data))
    code, report = cli(sandbox)
    assert code == 2 and report["ok"] is False
    assert report["missing"] is None and report["error"]


@pytest.mark.parametrize("mode", ["missing_file", "generation_failure", "missing_binary"])
def test_cli_generation_failure(sandbox, mode):
    if mode == "missing_file":
        data = json.loads(sandbox[3].read_text())
        del data["ServerRequest.json"]
        sandbox[3].write_text(json.dumps(data))
    elif mode == "generation_failure":
        (sandbox[0] / "mode").write_text("fail")
    else:
        sandbox[2].rename(sandbox[0] / "not-codex")
    code, report = cli(sandbox)
    assert code == 2 and report["missing"] is None and report["error"]


@pytest.mark.parametrize("source", ["", "# fuente truncada\n", "def broken(:", "x = 1",
    'c.call("initialize", {})', SOURCE.split("def handle_tool_call")[0]])
def test_cli_incomplete_or_invalid_source(sandbox, source):
    sandbox[1].write_text(source)
    code, report = cli(sandbox)
    assert code == 2 and report["ok"] is False and report["error"]
    assert report["missing"] is None


def test_cli_missing_source(sandbox):
    sandbox[1].rename(sandbox[0] / "not-source")
    code, report = cli(sandbox)
    assert code == 2 and report["missing"] is None


@pytest.mark.parametrize("statement", [
    'c.call("newMethod", {})',
    'c.call(method="thread/new", params={})',
    'table = {"new": ("thread/new", params)}',
    'forward(c, "thread/new", {})\ndef forward(c, method, params):\n    return c.call(method, params)',
])
def test_request_extraction(statement):
    need = doctor.relied_on(SOURCE + "\n" + statement)
    assert ("newMethod" if "newMethod" in statement else "thread/new") in need["requests"]


@pytest.mark.parametrize("handler,kind", [("note", "notifications"), ("handle_tool_call", "server_requests")])
@pytest.mark.parametrize("expression", ['m in ("event/one", "event/two")',
                                         'm in {"event/one", "event/two"}',
                                         'm == "event/one"'])
def test_async_and_membership_handlers(handler, kind, expression):
    source = SOURCE + f'\nasync def {handler}(o):\n    m = o["method"]\n    if {expression}:\n        pass\n'
    assert "event/one" in doctor.relied_on(source)[kind]


def test_initialized_category_required():
    schema = dict(SCHEMA)
    del schema["client_notifications"]
    with pytest.raises(ValueError):
        doctor.check(SOURCE, schema)


def test_timeout_is_unknown(sandbox, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("codex-falso", kwargs["timeout"])
    monkeypatch.setattr(doctor.subprocess, "run", timeout)
    report, code = doctor.run(sandbox[1], str(sandbox[2]))
    assert code == 2 and report["missing"] is None and report["error"]
    assert report["codex_version"] is None


def test_membership_does_not_invent_outgoing_requests():
    source = SOURCE + '\ndef note(o):\n    m = o["method"]\n    if m in ("event/one", "event/two"):\n        pass\n'
    schema = {k: set(v) for k, v in SCHEMA.items()}
    schema["notifications"].update({"event/one", "event/two"})
    report = doctor.check(source, schema)
    assert report["ok"] and report["missing"] == []
    assert set(report["relied_on"]["requests"]) == SCHEMA["requests"]


def test_server_method_without_slash_and_non_method_fields():
    source = SOURCE + '''
def note(o):
    event = o["method"]
    if event == "error":
        pass
    if o.get("message") == "not/a/method":
        pass
'''
    report = doctor.check(source, SCHEMA)
    assert report["missing"] == ["notification:error"]


def test_wrappers_chain_and_keyword_argument():
    source = SOURCE + '''
def outer(c, operation, params):
    return inner(c, method=operation, params=params)
def inner(c, method, params):
    return c.call(method=method, params=params)
outer(c, operation="thread/chained", params={})
'''
    assert "thread/chained" in doctor.relied_on(source)["requests"]


def test_version_failure_is_not_a_version(monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout="fallo sintético", stderr=""))
    assert doctor.codex_version("codex-falso") is None


def test_cli_timeout_after_partial_output(sandbox):
    (sandbox[0] / "mode").write_text("timeout")
    (sandbox[0] / "short-timeout").touch()
    code, report = cli(sandbox)
    assert code == 2 and report["missing"] is None and report["error"]
    assert "timed out" in report["error"]


@pytest.mark.parametrize("schema", [None, [], {**SCHEMA, "requests": {42}},
                                     {**SCHEMA, "notifications": "turn/completed"}])
def test_invalid_schema_map(schema):
    with pytest.raises(ValueError):
        doctor.check(SOURCE, schema)


@pytest.mark.parametrize("expression", ['"" in m', '"error" in m', '"error" not in m'])
def test_unrecognized_notification_substring_is_not_a_server_request(expression):
    source = SOURCE + f'\ndef note(o):\n    m = o["method"]\n    if {expression}:\n        pass\n'
    with pytest.raises(ValueError):
        doctor.check(source, SCHEMA)
