#!/usr/bin/env python3
"""Read-only health check for bridge.py against the installed Codex app server.

A ChatGPT update ships a new bundled codex binary, and app-server methods get renamed or
dropped without notice. This extracts statically recognizable methods and notifications from bridge.py
(AST, without executing its source), asks the installed binary for its current JSON schema,
and names each missing dependency. Dynamic RPC names are outside this check.

It never starts the daemon, never starts a turn and never writes a thread: the daemon check
is the same socket ping `bridge.py ping` uses.

  python3 doctor.py [--bridge PATH] [--codex PATH] [--verbose]
Exit 0 when every relied-on method exists, 1 when any is missing, 2 when the schema could
not be generated or the source/schema could not be fully inspected.
"""
import argparse, ast, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE / "bridge.py"
DEFAULT_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
INTERNAL = ("bridge/",)          # methods the daemon answers itself, never sent to Codex
NOTE_FUNCS = {"note"}            # functions that dispatch on server notifications
REQUEST_FUNCS = {"handle_tool_call"}  # functions that answer server -> client requests


def _is_method(s):
    return isinstance(s, str) and "/" in s and " " not in s and not s.startswith(("/", ".", "~"))


def _method_name(value):
    return isinstance(value, str) and bool(value) and not any(c.isspace() for c in value)


def _call_method(node, wrappers):
    """Localiza el argumento de método sin ejecutar la fuente."""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Attribute) and node.func.attr == "call":
        index, name = 0, "method"
    elif isinstance(node.func, ast.Name) and node.func.id in wrappers:
        index, name = wrappers[node.func.id]
    else:
        return None
    if len(node.args) > index and not any(isinstance(a, ast.Starred) for a in node.args[:index + 1]):
        return node.args[index]
    return next((k.value for k in node.keywords if k.arg == name), None)


def _reads_method(node):
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "method"
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "method")


def _method_variables(fn):
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                pairs = [(target, node.value)]
                if isinstance(target, (ast.Tuple, ast.List)) and isinstance(node.value, (ast.Tuple, ast.List)):
                    pairs = zip(target.elts, node.value.elts)
                for name, value in pairs:
                    if isinstance(name, ast.Name) and _reads_method(value):
                        names.add(name.id)
    return names


def relied_on(source):
    """Extrae dependencias estáticas; no evalúa Python ni resuelve llamadas dinámicas."""
    tree = ast.parse(source)
    out = {k: set() for k in ("requests", "notifications", "server_requests", "routing", "substrings")}
    table_values = {value for n in ast.walk(tree) if isinstance(n, ast.Dict) for value in n.values}
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    # Sigue wrappers sencillos como call_held y _tidy_call, incluso encadenados.
    wrappers = {}
    changed = True
    while changed:
        changed = False
        for fn in functions:
            if fn.name in wrappers:
                continue
            args = [a.arg for a in fn.args.posonlyargs + fn.args.args]
            for node in ast.walk(fn):
                method = _call_method(node, wrappers)
                if isinstance(method, ast.Name) and method.id in args:
                    wrappers[fn.name] = (args.index(method.id), method.id)
                    changed = True
                    break

    for node in ast.walk(tree):
        method = _call_method(node, wrappers)
        if isinstance(method, ast.Constant) and _method_name(method.value):
            out["requests"].add(method.value)
        # Tablas de pares (método, parámetros), incluidos parámetros calculados.
        if isinstance(node, ast.Tuple) and len(node.elts) == 2 and isinstance(node.elts[0], ast.Constant) \
                and _is_method(node.elts[0].value) \
                and (isinstance(node.elts[1], ast.Dict) or node in table_values):
            out["requests"].add(node.elts[0].value)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "WRITES" for t in targets):
                for e in getattr(node.value, "elts", []):
                    if isinstance(e, ast.Constant) and _method_name(e.value):
                        out["routing"].add(e.value)

    for fn in functions:
        kind = "notifications" if fn.name in NOTE_FUNCS else "server_requests" if fn.name in REQUEST_FUNCS else None
        if not kind:
            continue
        method_vars = _method_variables(fn)

        def is_method(value):
            return _reads_method(value) or isinstance(value, ast.Name) and value.id in method_vars

        for node in ast.walk(fn):
            if not isinstance(node, ast.Compare):
                continue
            for left, op, right in zip([node.left, *node.comparators], node.ops, node.comparators):
                if not (is_method(left) or is_method(right)):
                    continue
                # Coincidencias parciales no equivalen a métodos literales.
                if isinstance(op, (ast.In, ast.NotIn)) and isinstance(left, ast.Constant) and isinstance(left.value, str):
                    if kind != "server_requests" or not left.value or isinstance(op, ast.NotIn):
                        raise ValueError("Comparación parcial de método no reconocida en callback del servidor")
                    out["substrings"].add(left.value)
                    continue
                for value in (left, right):
                    values = value.elts if isinstance(value, (ast.Tuple, ast.List, ast.Set)) else [value]
                    for literal in values:
                        if isinstance(literal, ast.Constant) and _method_name(literal.value):
                            out[kind].add(literal.value)
    out["requests"] = {m for m in out["requests"] if not m.startswith(INTERNAL)}
    out["requests"].add("initialize")
    return out


def _validate_source(need):
    # Una fuente vacía o recortada no demuestra compatibilidad. Estos son los
    # puntos de entrada del bridge que este extractor sabe inspeccionar.
    if not (need["requests"] - {"initialize"}) or not need["notifications"] \
            or not (need["server_requests"] or need["substrings"]):
        raise ValueError("Fuente incompleta o no reconocida: faltan peticiones o callbacks del servidor")


def _validate_schema(schema):
    if not isinstance(schema, dict):
        raise ValueError("Schema inválido: se esperaba un mapa de categorías")
    for kind in ("requests", "notifications", "server_requests", "client_notifications"):
        values = schema.get(kind)
        if not isinstance(values, (set, frozenset)) or not all(_method_name(m) for m in values):
            raise ValueError(f"Schema incompleto o inválido: {kind}")


def schema_methods(codex, outdir):
    r = subprocess.run(["perl", "-e", "alarm 60; exec @ARGV", codex, "app-server", "generate-json-schema",
                        "--experimental", "--out", outdir], capture_output=True, text=True, timeout=75)
    if r.returncode:
        raise RuntimeError(f"generate-json-schema terminó con código {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}")

    def enum(name):
        d = json.loads((Path(outdir) / name).read_text())
        variants = d.get("oneOf") if isinstance(d, dict) else None
        if not isinstance(variants, list) or not variants:
            raise ValueError(f"Schema incompleto o no reconocido: {name}: oneOf")
        methods = set()
        for i, variant in enumerate(variants):
            props = variant.get("properties") if isinstance(variant, dict) else None
            method = props.get("method") if isinstance(props, dict) else None
            values = method.get("enum") if isinstance(method, dict) else None
            if not isinstance(values, list) or not values or not all(_method_name(m) for m in values):
                raise ValueError(f"Schema incompleto o no reconocido: {name}: oneOf[{i}].method.enum")
            methods.update(values)
        return methods

    return {"requests": enum("ClientRequest.json"), "notifications": enum("ServerNotification.json"),
            "server_requests": enum("ServerRequest.json"), "client_notifications": enum("ClientNotification.json")}


def codex_version(codex):
    try:
        r = subprocess.run(["perl", "-e", "alarm 20; exec @ARGV", codex, "--version"],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except Exception:
        return None


def daemon_state(codex):
    """Ping the running daemon over its socket (never starts one). A daemon older than the codex
    binary still runs the app server from before the update until it is restarted."""
    sys.path.insert(0, str(HERE))
    try:
        import bridge
        p = bridge.ping()
    except Exception as e:
        return {"up": False, "error": str(e)}
    if not p:
        return {"up": False, "note": "not running; it starts on the next bridge command"}
    st = {"up": True, "pid": p.get("pid"), "running": p.get("running"), "workers": p.get("workers")}
    try:
        etime = subprocess.run(["ps", "-o", "etime=", "-p", str(p["pid"])], capture_output=True, text=True,
                               timeout=5).stdout.strip()
        st["uptime"] = etime
        secs = _etime_seconds(etime)
        import time
        if secs is not None and os.path.getmtime(codex) > time.time() - secs:
            st["stale"] = ("codex binary changed after the daemon started; its app server is the old "
                           "version until `bridge.py down` runs while no turn is in flight")
    except Exception:
        pass
    return st


def _etime_seconds(s):
    try:
        days, _, rest = s.partition("-") if "-" in s else ("0", "", s)
        parts = [int(x) for x in rest.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, sec = parts
        return int(days) * 86400 + h * 3600 + m * 60 + sec
    except ValueError:
        return None


def check(bridge_src, schema):
    need = relied_on(bridge_src)
    _validate_source(need)
    _validate_schema(schema)
    missing = sorted(
        [m for m in need["requests"] if m not in schema["requests"]]
        + [f"notification:{m}" for m in need["notifications"] if m not in schema["notifications"]]
        + [f"server-request:{m}" for m in need["server_requests"] if m not in schema["server_requests"]]
        + ([] if "initialized" in schema["client_notifications"] else ["notification:initialized"])
        + [f"server-request~{s}" for s in need["substrings"] if not any(s in m for m in schema["server_requests"])])
    warnings = [f"WRITES routes {m}, which the app server no longer has (harmless unless something calls it)"
                for m in sorted(need["routing"] - schema["requests"]) if not m.startswith(INTERNAL)]
    return {"ok": not missing, "missing": missing, "warnings": warnings,
            "relied_on": {k: sorted(v) for k, v in need.items() if v},
            "schema_counts": {k: len(v) for k, v in schema.items()}}


def run(bridge=BRIDGE, codex=None):
    codex = codex or os.environ.get("CODEX_BIN", DEFAULT_CODEX)
    rep = {"codex_bin": codex, "codex_version": codex_version(codex)}
    try:
        source = Path(bridge).read_text()
        # Valida la fuente antes de ejecutar el generador; nunca la importa.
        _validate_source(relied_on(source))
        with tempfile.TemporaryDirectory(prefix="bridge-doctor-") as d:
            schema = schema_methods(codex, d)
        rep.update(check(source, schema))
    except Exception as e:
        rep.update(ok=False, missing=None, error=str(e), daemon=daemon_state(codex))
        return rep, 2
    rep["daemon"] = daemon_state(codex)
    return rep, 0 if rep["ok"] else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bridge", default=str(BRIDGE))
    ap.add_argument("--codex")
    ap.add_argument("--verbose", action="store_true", help="include the full relied-on list")
    a = ap.parse_args(argv)
    rep, code = run(a.bridge, a.codex)
    if not a.verbose:
        rep.pop("relied_on", None)
    print(json.dumps(rep, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
