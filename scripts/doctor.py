#!/usr/bin/env python3
"""Read-only health check for bridge.py against the installed Codex app server.

A ChatGPT update ships a new bundled codex binary, and app-server methods get renamed or
dropped without notice. This finds every method and notification bridge.py depends on by
reading its source (AST, so new commands are picked up without a list to maintain), asks the
installed binary for its current JSON schema, and names each one that moved.

It never starts the daemon, never starts a turn and never writes a thread: the daemon check
is the same socket ping `bridge.py ping` uses.

  python3 doctor.py [--bridge PATH] [--codex PATH] [--json]
Exit 0 when every relied-on method exists, 1 when any is missing, 2 when the schema could
not be generated.
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


def relied_on(source):
    """{'requests': set, 'notifications': set, 'server_requests': set, 'routing': set, 'substrings': set}"""
    tree = ast.parse(source)
    out = {k: set() for k in ("requests", "notifications", "server_requests", "routing", "substrings")}

    for node in ast.walk(tree):
        # x.call("method", ...) and c.call(*m) where m comes from a dict of ("method", params) tuples
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "call":
            if node.args and isinstance(node.args[0], ast.Constant) and _is_method(node.args[0].value):
                out["requests"].add(node.args[0].value)
        if isinstance(node, ast.Tuple) and node.elts and isinstance(node.elts[0], ast.Constant) \
                and _is_method(node.elts[0].value) and len(node.elts) == 2 and isinstance(node.elts[1], ast.Dict):
            out["requests"].add(node.elts[0].value)
        # WRITES = {...}: methods the daemon routes to a per-thread worker
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "WRITES" for t in node.targets):
            for e in getattr(node.value, "elts", []):
                if isinstance(e, ast.Constant):
                    out["routing"].add(e.value)

    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        kind = "notifications" if fn.name in NOTE_FUNCS else "server_requests" if fn.name in REQUEST_FUNCS else None
        if not kind:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Compare):
                for c in [node.left, *node.comparators]:
                    if isinstance(c, ast.Constant) and _is_method(c.value):
                        out[kind].add(c.value)
                    # `"pproval" in m`: the bridge declines every approval request by substring
                    if isinstance(c, ast.Constant) and isinstance(c.value, str) and isinstance(node.ops[0], ast.In) \
                            and c is node.left and "/" not in c.value:
                        out["substrings"].add(c.value)
    out["requests"] = {m for m in out["requests"] if not m.startswith(INTERNAL)}
    out["requests"].add("initialize")   # sent in AppServer.__init__ through self.call
    return out


def schema_methods(codex, outdir):
    r = subprocess.run(["perl", "-e", "alarm 60; exec @ARGV", codex, "app-server", "generate-json-schema",
                        "--experimental", "--out", outdir], capture_output=True, text=True, timeout=75)
    if r.returncode:
        raise RuntimeError(f"generate-json-schema exited {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}")

    def enum(name):
        d = json.loads((Path(outdir) / name).read_text())
        return {e for v in d.get("oneOf", []) for e in (v.get("properties", {}).get("method", {}).get("enum") or [])}

    return {"requests": enum("ClientRequest.json"), "notifications": enum("ServerNotification.json"),
            "server_requests": enum("ServerRequest.json"), "client_notifications": enum("ClientNotification.json")}


def codex_version(codex):
    try:
        r = subprocess.run(["perl", "-e", "alarm 20; exec @ARGV", codex, "--version"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or None
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
    missing = sorted(
        [m for m in need["requests"] if m not in schema["requests"]]
        + [f"notification:{m}" for m in need["notifications"] if m not in schema["notifications"]]
        + [f"server-request:{m}" for m in need["server_requests"] if m not in schema["server_requests"]]
        + ([] if "initialized" in schema.get("client_notifications", {"initialized"}) else ["notification:initialized"])
        + [f"server-request~{s}" for s in need["substrings"] if not any(s in m for m in schema["server_requests"])])
    warnings = [f"WRITES routes {m}, which the app server no longer has (harmless unless something calls it)"
                for m in sorted(need["routing"] - schema["requests"])]
    return {"ok": not missing, "missing": missing, "warnings": warnings,
            "relied_on": {k: sorted(v) for k, v in need.items() if v},
            "schema_counts": {k: len(v) for k, v in schema.items()}}


def run(bridge=BRIDGE, codex=None):
    codex = codex or os.environ.get("CODEX_BIN", DEFAULT_CODEX)
    rep = {"codex_bin": codex, "codex_version": codex_version(codex)}
    with tempfile.TemporaryDirectory(prefix="bridge-doctor-") as d:
        try:
            schema = schema_methods(codex, d)
        except Exception as e:
            rep.update(ok=False, missing=None, error=str(e), daemon=daemon_state(codex))
            return rep, 2
    rep.update(check(Path(bridge).read_text(), schema))
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
