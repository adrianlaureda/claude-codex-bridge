#!/usr/bin/env python3
"""Claude Code <-> Codex (ChatGPT app) bridge.

Claude side drives every Codex thread operation through Codex's own app server.
Codex side messages and manages Claude Code sessions.

A small daemon keeps one app-server connection open (no per-call startup),
stays the writer for threads it starts (so turns can be steered and stopped),
and answers the `send_to_claude` tool the moment an agent calls it. Commands
start it on demand. It listens on a unix socket readable only by this user.

Codex allows one writer per thread. Threads the ChatGPT app has open are held
by the app; `send` detects that and puts the message in the thread's queue,
which the app runs itself. `watch` reads the thread's rollout file, which Codex
writes for every thread, so it streams app-held threads too.

CLAUDE -> CODEX
  bridge.py start "prompt" --role ROLE --cwd D [--title T] [--from local_<id>] [--no-watch]
                     [--sandbox {workspace-write,read-only} | --full-access] [--no-network]
  bridge.py send <thread> "prompt" [--model M] [--effort E] [--no-watch]   unheld: new turn; held: queued
  bridge.py watch <thread> [--timeout S] [--from-start]   stream until the current turn ends
  bridge.py steer <thread> "text"            add input to the running turn (bridge-held threads)
  bridge.py stop <thread>                    interrupt the running turn (bridge-held threads)
  bridge.py list [--limit N] [--search TEXT] [--archived]
  bridge.py read <thread> [--turns N]
  bridge.py status <thread>                  held by / running / queue
  bridge.py rename <thread> "name" | archive <thread> | unarchive <thread> | fork <thread>
  bridge.py sections | move <thread> <section name or id>
  bridge.py settings <thread> [--model M] [--effort E]
  bridge.py queue <thread> [--delete ID]
  bridge.py goal <thread> ["objective"] [--clear]
  bridge.py models | mcp [server] | mcp-call <server> <tool> ['{json args}'] [--thread T]
  bridge.py rpc <method> ['{json params}']   any of the ~167 app-server methods

CODEX -> CLAUDE
  bridge.py claude-list                      live Claude Code sessions (name, status, ids)
  bridge.py to-claude --to <name|local_id> "message" [--thread T]
  bridge.py relay [--minutes 55]             run by a Claude session under Monitor; prints each
                                             inbox message as one line to deliver with SendMessage
  bridge.py claude-start "prompt" [--cwd D] [--name N] [--no-remote-control]
                                             start a visible background Claude Code session and record its URL
  bridge.py claude-started [--last N]          list recorded sessions with their current status
  bridge.py claude-read <local_id|name|sessionId> [--last N]   recent messages from its transcript

DAEMON
  bridge.py up | down | ping
"""
import argparse, fcntl, glob, json, os, queue, re, socket, socketserver, subprocess, sys, threading, time, uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

CODEX = os.environ.get("CODEX_BIN",
                       "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
HOME = Path(os.environ.get("CLAUDE_CODEX_BRIDGE_HOME", Path.home() / ".claude-codex-bridge"))
SOCK = HOME / "bridge.sock"
INBOX = HOME / "inbox.jsonl"
THREADS = HOME / "threads.json"
LOG = HOME / "events.jsonl"
SESSIONS = Path.home() / ".codex" / "sessions"
LOCKED = "already has an active writer"

TOOL = {
    "type": "function", "name": "send_to_claude",
    "description": ("Send a message to the Claude Code session that dispatched this task. Use it for "
                    "meaningful progress, for a question you cannot resolve yourself, and once at the end "
                    "with the result. Optional `to` names a different Claude session (name or local_ id)."),
    "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}, "to": {"type": "string"}},
                    "required": ["message"]},
}
# An agent fixing a capture glitch can raise its browser window and take over the user's screen.
# Background-only is the default; foreground needs an explicit marker. One browser per task, pages
# as tabs, a fresh context per clean visit, so a task never spawns a browser per page.
INSTRUCTIONS = ("This task was dispatched by a Claude Code session, not typed by the user. Report back with "
                "the send_to_claude tool: progress when a step finishes, a question when blocked, and the "
                "final result. Do not message people, purchase, or change account or security settings "
                "unless the task text says so explicitly. Never take over the user's screen: do not raise, "
                "focus or activate windows, move the pointer, or open a visible browser or app window. Browse "
                "and capture in a headless browser you start yourself (its own user-data-dir, screenshots over "
                "CDP, a hard 60 second limit per page). Run ONE browser for the whole task and open pages as tabs in it "
                "(at most 4 at once); for a clean first visit, give a tab its own browser context "
                "(Target.createBrowserContext) instead of launching another browser. Close it at the end. Foreground computer use is allowed only when the task "
                "text contains FOREGROUND AUTHORIZED; if a step seems to need it, stop and ask with "
                "send_to_claude instead. Every time you tell the user about a thread or session, give its "
                "clickable link: a Claude Code session as claude://claude.ai/epitaxy/<local_id>, a Codex "
                "thread as codex://threads/<id>. Stop and ask with send_to_claude before accepting new "
                "terms, paying, or granting account access (OAuth). Without explicit authorization in the "
                "task text, do not commit, push, merge, deploy, delete, install, change credentials, or "
                "message families or students, and do not write to Abalar, XADE, or Moodle. Keep student "
                "data inside the approved circuit and never put it in logs, commits, or published artifacts. "
                "No datos de alumnado fuera del circuito aprobado ni en logs, commits o artefactos publicados.")


def now():
    return datetime.now().strftime("%H:%M:%S")


def emit(line):
    print(line.replace("\n", " ⏎ "), flush=True)


def append(path, obj):
    """One os.write per record in append mode, so lines from concurrent processes never interleave."""
    HOME.mkdir(parents=True, exist_ok=True)
    data = (json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **obj}) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def tail_lines(path, limit=4_000_000):
    """Complete lines from the last `limit` bytes of a file, as bytes. Logs here reach 1.4 GB."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        base = max(0, size - limit)
        f.seek(base)
        if base:
            f.readline()
        return f.read().split(b"\n")


def load_threads():
    try:
        return json.loads(THREADS.read_text())
    except Exception:
        return {}


def save_thread(tid, **kw):
    d = load_threads()
    d.setdefault(tid, {}).update(kw)
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = THREADS.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(THREADS)


def normalize_cwd(value):
    """Devuelve un directorio existente, absoluto y normalizado."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("--cwd debe ser un directorio existente no vacío")
    try:
        cwd = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError(f"--cwd no es un directorio existente: {value!r}") from exc
    if not cwd.is_dir():
        raise ValueError(f"--cwd no es un directorio existente: {value!r}")
    return cwd


def route_model(role):
    """Resuelve provider, modelo y esfuerzo mediante el router canónico, sin fallback."""
    command = ["uv", "run", "python3", str(Path.home() / ".dotfiles/ai/scripts/model-routing.py"),
               "--role", role, "--format", "json"]
    try:
        result = subprocess.run(command, cwd=Path.home() / ".dotfiles/ai", stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"fallo del router canónico: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "sin detalle").strip()
        raise ValueError(f"fallo del router canónico: {detail[:300]}")
    try:
        routed = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("el router canónico devolvió JSON inválido") from exc
    if not isinstance(routed, dict):
        raise ValueError("el router canónico no devolvió un objeto JSON")
    if routed.get("provider") != "openai":
        raise ValueError(f"proveedor del router no permitido: {routed.get('provider')!r}")
    model, effort = routed.get("model"), routed.get("effort")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("el router canónico no devolvió un modelo válido")
    if not isinstance(effort, str) or not effort.strip():
        raise ValueError("el router canónico no devolvió un esfuerzo válido")
    return {"provider": "openai", "model": model.strip(), "effort": effort.strip()}


def execution_policy(cwd, sandbox=None, full_access=False, no_network=False):
    """Construye las formas de sandbox aceptadas por thread/start y turn/start."""
    if full_access and no_network:
        raise ValueError("--no-network no es compatible con --full-access")
    if full_access:
        mode = "danger-full-access"
    else:
        mode = sandbox or "workspace-write"
    if mode == "workspace-write":
        network_access = not no_network
        return mode, network_access, {"sandbox_workspace_write.network_access": network_access}, {
            "type": "workspaceWrite", "networkAccess": network_access, "writableRoots": [str(cwd)]
        }
    if mode == "read-only":
        return mode, False, {}, {"type": "readOnly", "networkAccess": False}
    if mode == "danger-full-access":
        return mode, True, {}, {"type": "dangerFullAccess"}
    raise ValueError(f"sandbox no permitido: {mode!r}")


# ---------------------------------------------------------------- app server

def compact_error(value, limit=500):
    """Reduce un error a una línea y evita volcar datos de la petición en los logs."""
    text = str(value).strip().replace("\n", " ⏎ ")
    return (text or type(value).__name__)[:limit]


def redact_sensitive(value):
    """Redacta tokens y valores con aspecto de secreto antes de registrar texto."""
    text = str(value).replace("\n", " ⏎ ")
    text = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+\b", "[REDACTED]", text)
    text = re.sub(r"\b(key|token|password)\s*=\s*\S+", r"\1=[REDACTED]", text, flags=re.IGNORECASE)
    return re.sub(r"\b[A-Za-z0-9+/=_-]{24,}\b", "[REDACTED]", text)


class AppServerSendError(OSError):
    """Fallo del envío, con indicación de si llegó algún byte al proceso."""

    def __init__(self, method, cause, written=False):
        self.method = method
        self.cause = cause
        self.written = written
        super().__init__(f"{method}: app server send failed: {compact_error(cause)}")


class AppServer:
    """One JSON-RPC connection to `codex app-server` over stdio."""

    def __init__(self, on_request=None, on_note=None):
        if not os.access(CODEX, os.X_OK):
            sys.exit(f"Codex binary not found at {CODEX}. Set CODEX_BIN.")
        self.on_request, self.on_note = on_request, on_note
        self.n, self.waiting, self.wlock = 0, {}, threading.Lock()
        self.p, self._stderr_thread = None, None
        self._pump_done = threading.Event()
        self._stderr_state = {"lines": 0, "error": None}
        for attempt in range(2):
            proc = None
            waiting = {}
            pump_done = threading.Event()
            stderr_state = {"lines": 0, "error": None}
            try:
                proc = subprocess.Popen([CODEX, "app-server"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        cwd=Path.home(), text=True, bufsize=1)
                self.p = proc
                self.waiting = waiting
                self._pump_done = pump_done
                self._stderr_state = stderr_state
                self._stderr_thread = threading.Thread(target=self._read_stderr,
                                                        args=(proc.stderr, stderr_state), daemon=True)
                self._stderr_thread.start()
                # A reader thread, not select(): select() misses lines Python already buffered.
                threading.Thread(target=self._pump, args=(proc, waiting, pump_done), daemon=True).start()
                result = self.call("initialize", {
                    "clientInfo": {"name": "claude-codex-bridge", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                }, proc=proc, waiting=waiting)
                if "error" in result:
                    raise RuntimeError((result.get("error") or {}).get("message", "initialize failed"))
                self._send({"jsonrpc": "2.0", "method": "initialized"}, proc=proc)
                return
            except Exception as exc:
                self._log_start_error(exc, proc, stderr_state, self._stderr_thread, attempt + 1)
                self._wake_waiting(waiting)
                self._terminate(proc)
                if attempt:
                    raise

    def _read_stderr(self, stream, state):
        try:
            for line in stream:
                line = line.strip()
                state["lines"] += 1
                if state["error"] is None and line.startswith("Error:"):
                    state["error"] = redact_sensitive(line)
        except (OSError, ValueError):
            pass

    def _stderr_summary(self, state, thread, proc):
        if thread and proc and proc.poll() is not None:
            thread.join(timeout=0.5)
        error_line = state.get("error")
        return "Error:" if error_line else None

    def _log_start_error(self, exc, proc, stderr_state, stderr_thread, attempt):
        record = {"event": "app_server_start_error", "pid": proc.pid if proc else None,
                  "method": "initialize", "attempt": attempt,
                  "returncode": proc.poll() if proc else None,
                  "stderr_lines": stderr_state["lines"]}
        stderr_error = self._stderr_summary(stderr_state, stderr_thread, proc)
        if stderr_error:
            record["stderr_error"] = stderr_error
        append(LOG, record)
        detail = f" stderr_error={stderr_error}" if stderr_error else ""
        emit(f"app-server start error pid={record['pid']} method=initialize "
             f"returncode={record['returncode']} stderr_lines={record['stderr_lines']}{detail}")

    def _send(self, o, proc=None):
        proc = proc or self.p
        with self.wlock:
            payload = (json.dumps(o) + "\n").encode()
            sent = 0
            try:
                while sent < len(payload):
                    count = os.write(proc.stdin.fileno(), payload[sent:])
                    if not count:
                        raise BrokenPipeError("app server stdin accepted no bytes")
                    sent += count
            except (BrokenPipeError, OSError) as exc:
                raise AppServerSendError(o.get("method", "response"), exc, written=sent > 0) from exc

    def _pump(self, proc, waiting, pump_done):
        try:
            for line in proc.stdout:
                if proc is not self.p:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if "id" in o and "method" not in o:
                    q = waiting.pop(o["id"], None)
                    if q:
                        q.put(o)
                elif "id" in o:
                    threading.Thread(target=self._request, args=(proc, o), daemon=True).start()
                elif self.on_note:
                    self.on_note(o)
        except (ValueError, OSError):
            pass
        finally:
            self._wake_waiting(waiting)
            pump_done.set()

    def _request(self, proc, o):
        result = self.on_request(o) if self.on_request else None
        try:
            if result is None:
                self._send({"jsonrpc": "2.0", "id": o["id"],
                            "error": {"code": -32601, "message": f"{o['method']} is not handled by the bridge"}},
                           proc=proc)
            else:
                self._send({"jsonrpc": "2.0", "id": o["id"], "result": result}, proc=proc)
        except (AppServerSendError, BrokenPipeError, OSError):
            pass

    @staticmethod
    def _wake_waiting(waiting):
        for q in list(waiting.values()):
            q.put({"error": {"message": "Codex app server exited"}})
        waiting.clear()

    @staticmethod
    def _terminate(proc):
        if not proc:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass
        finally:
            AppServer._close_streams(proc)

    @staticmethod
    def _close_streams(proc):
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except (OSError, ValueError):
                pass

    def call(self, method, params, timeout=60, proc=None, waiting=None):
        proc = proc or self.p
        waiting = waiting if waiting is not None else self.waiting
        with self.wlock:
            self.n += 1
            i = self.n
        q = queue.Queue()
        waiting[i] = q
        if proc.poll() is not None:
            waiting.pop(i, None)
            raise AppServerSendError(method, "process already terminated")
        try:
            self._send({"jsonrpc": "2.0", "id": i, "method": method, "params": params}, proc=proc)
        except AppServerSendError:
            waiting.pop(i, None)
            raise
        except (BrokenPipeError, OSError) as exc:
            waiting.pop(i, None)
            raise AppServerSendError(method, exc) from exc

        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            waiting.pop(i, None)
            return {"error": {"message": f"{method}: no response in {timeout}s"}}

    def close(self):
        proc = self.p
        if not proc:
            return
        self._wake_waiting(self.waiting)
        self._terminate(proc)


# ---------------------------------------------------------------- daemon

def handle_tool_call(o):
    m, p = o["method"], o.get("params") or {}
    if m == "item/tool/call" and p.get("tool") == "send_to_claude":
        args = p.get("arguments") or {}
        tid = p.get("threadId")
        # Every message goes to the inbox, so it reaches Claude through the relay even when no
        # watch is running. Without `to`, it goes to the session that started the thread.
        to = args.get("to") or load_threads().get(tid, {}).get("from")
        if not to:
            return {"success": False, "contentItems": [{"type": "inputText", "text":
                    "No Claude session is attached to this thread. Name one with `to` (see bridge.py claude-list)."}]}
        append(INBOX, {"id": uuid.uuid4().hex[:8], "from_thread": tid, "to": to, "via": "tool",
                       "message": args.get("message", "")})
        return {"success": True, "contentItems": [{"type": "inputText", "text": f"Queued for Claude session {to}."}]}
    if "pproval" in m:
        append(LOG, {"event": "approval_declined", "method": m, "thread": p.get("threadId")})
        return {"decision": "decline"}
    return None


# Methods that make the connection a thread's writer. Codex holds the writer lock for as long
# as the process keeps the thread loaded, and there is no unload call. So each written thread
# gets its own short-lived worker process, closed a few seconds after its turn ends; that
# releases the lock and the ChatGPT app can open the thread again.
WRITES = {"thread/start", "thread/fork", "thread/resume", "turn/start", "turn/steer", "turn/interrupt",
          "turn/settings/update", "thread/compact/start", "thread/shellCommand", "review/start",
          "mcpServer/tool/call", "thread/inject_items", "thread/rollback", "thread/revert"}
IDLE_CLOSE = 5


class Daemon:
    def __init__(self):
        self.mlock = threading.Lock()
        self._main = AppServer(on_request=handle_tool_call)  # reads and management, holds no locks
        self.workers = {}                      # threadId -> AppServer
        self.running = {}                      # threadId -> active turn id
        self.done = set()                      # turn ids already completed
        self.touched = {}                      # threadId -> last use
        self.inflight = defaultdict(int)       # threadId -> calls in progress on its worker
        self.tlocks = defaultdict(threading.Lock)  # one worker start per thread at a time
        self.lock = threading.Lock()
        threading.Thread(target=self._reap, daemon=True).start()

    @property
    def main(self):
        """Restart the read connection if its app server exited, instead of failing every read."""
        with self.mlock:
            if self._main is None or self._main.p.poll() is not None:
                append(LOG, {"event": "main_restarted"})
                self._main = AppServer(on_request=handle_tool_call)
            return self._main

    def _main_call(self, method, params, timeout):
        for attempt in range(2):
            worker = self.main
            try:
                return worker.call(method, params, timeout)
            except AppServerSendError as exc:
                with self.mlock:
                    if self._main is worker:
                        self._main = None
                worker.close()
                if exc.written:
                    raise
                if attempt:
                    raise

    def note(self, o):
        m, p = o.get("method"), o.get("params") or {}
        tid = p.get("threadId")
        if m == "turn/started":
            t = (p.get("turn") or {}).get("id")
            if t not in self.done:
                self.running[tid] = t
        elif m == "turn/completed":
            self.done.add((p.get("turn") or {}).get("id"))
            self.running.pop(tid, None)
            self.touched[tid] = time.time()

    def _reap(self):
        while True:
            time.sleep(1)
            with self.lock:
                for tid in list(self.workers):
                    w = self.workers[tid]
                    if w.p.poll() is not None and w._pump_done.is_set():  # EOF confirms the pump drained stdout
                        self.workers.pop(tid)
                        self.running.pop(tid, None)
                    elif (w.p.poll() is None and tid not in self.running and not self.inflight[tid]
                          and time.time() - self.touched.get(tid, 0) > IDLE_CLOSE):
                        self.workers.pop(tid).close()

    def _worker(self, tid, method):
        with self.tlocks[tid]:
            with self.lock:
                w = self.workers.get(tid)
                if w and w.p.poll() is None:
                    return w, None
            w = AppServer(on_request=handle_tool_call, on_note=self.note)
            if method != "thread/resume":
                try:
                    r = w.call("thread/resume", {"threadId": tid})
                except AppServerSendError:
                    w.close()
                    raise
                if "error" in r:
                    w.close()
                    return None, r
            with self.lock:
                self.workers[tid] = w
                self.touched[tid] = time.time()
            return w, None

    def rpc(self, method, params):
        timeout = params.pop("_timeout", 60)
        if method == "bridge/ping":
            return {"result": {"pid": os.getpid(), "running": self.running, "workers": list(self.workers)}}
        if method == "bridge/running":
            return {"result": {"turnId": self.running.get(params.get("threadId"))}}
        if method not in WRITES:
            # A thread a worker has loaded is only visible to that worker until its rollout exists.
            w = self.workers.get(params.get("threadId")) if isinstance(params, dict) else None
            if w:
                tid = params.get("threadId")
                self.inflight[tid] += 1
                try:
                    for attempt in range(2):
                        try:
                            return w.call(method, params, timeout)
                        except AppServerSendError as exc:
                            with self.lock:
                                if self.workers.get(tid) is w:
                                    self.workers.pop(tid)
                            w.close()
                            if exc.written:
                                raise
                            if attempt:
                                raise
                            w, err = self._worker(tid, method)
                            if err:
                                return err
                finally:
                    self.inflight[tid] -= 1
                    self.touched[tid] = time.time()
            return self._main_call(method, params, timeout)
        if method in ("thread/start", "thread/fork"):
            for attempt in range(2):
                w = AppServer(on_request=handle_tool_call, on_note=self.note)
                try:
                    r = w.call(method, params, timeout)
                except AppServerSendError as exc:
                    w.close()
                    if exc.written:
                        raise
                    if attempt:
                        raise
                    continue
                tid = ((r.get("result") or {}).get("thread") or {}).get("id")
                if not tid:
                    w.close()
                    return r
                with self.lock:
                    self.workers[tid] = w
                    self.touched[tid] = time.time()
                return r
        tid = params.get("threadId")
        self.inflight[tid] += 1  # the reaper never closes a worker mid-call
        try:
            for attempt in range(2):
                w = None
                try:
                    w, err = self._worker(tid, method)
                    if err:
                        return err
                    r = w.call(method, params, timeout)
                    if method == "thread/resume" and "error" in r:
                        with self.lock:
                            if self.workers.get(tid) is w:
                                self.workers.pop(tid)
                        w.close()
                    if method == "turn/start" and "result" in r:
                        t = r["result"]["turn"]["id"]
                        if t not in self.done:  # a turn that ended before this reply must not stay "running"
                            self.running[tid] = t
                    return r
                except AppServerSendError as exc:
                    if w:
                        with self.lock:
                            if self.workers.get(tid) is w:
                                self.workers.pop(tid)
                        w.close()
                    if exc.written:
                        raise
                    if attempt:
                        raise
        finally:
            self.inflight[tid] -= 1
            self.touched[tid] = time.time()


def serve():
    HOME.mkdir(parents=True, exist_ok=True)
    # The daemon holds this lock for its whole life. Only the holder may remove the socket, so a
    # slow daemon is never replaced by a second one that orphans its workers.
    lk = open(HOME / "daemon.lock", "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("bridge daemon already running")
    SOCK.unlink(missing_ok=True)
    d = Daemon()

    class H(socketserver.StreamRequestHandler):
        def handle(self):
            req = None
            for line in self.rfile:
                try:
                    req = json.loads(line)
                    res = d.rpc(req["method"], req.get("params") or {})
                except Exception as e:  # keep the daemon up on any bad request
                    method = req.get("method") if isinstance(req, dict) else None
                    record = {"event": "rpc_error", "pid": os.getpid(), "method": method,
                              "error": compact_error(e)}
                    append(LOG, record)
                    emit(f"rpc error pid={record['pid']} method={method} error={record['error']}")
                    res = {"error": {"message": f"bridge: {e}"}}
                self.wfile.write((json.dumps(res) + "\n").encode())
                self.wfile.flush()

    class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    old = os.umask(0o077)
    s = S(str(SOCK), H)
    os.umask(old)
    append(LOG, {"event": "daemon_up", "pid": os.getpid()})
    try:
        s.serve_forever()
    finally:
        SOCK.unlink(missing_ok=True)


def ping():
    try:
        c = socket.socket(socket.AF_UNIX)
        c.settimeout(3)
        c.connect(str(SOCK))
        c.sendall(b'{"method":"bridge/ping"}\n')
        return json.loads(c.makefile().readline())["result"]
    except Exception:
        return None


def up():
    if ping():
        return
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / "up.lock", "w") as lk:  # two commands at once start one daemon
        fcntl.flock(lk, fcntl.LOCK_EX)
        if ping():
            return
        with open(HOME / "daemon.log", "a") as daemon_log:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"], cwd=Path.home(),
                             start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=daemon_log, stderr=subprocess.STDOUT)
        for _ in range(100):
            if ping():
                return
            time.sleep(0.1)
    sys.exit("bridge daemon did not start; see ~/.claude-codex-bridge/daemon.log")


class Client:
    def __init__(self):
        up()
        self.c = socket.socket(socket.AF_UNIX)
        self.c.settimeout(5)
        self.c.connect(str(SOCK))
        self.f = self.c.makefile()

    def call(self, method, params=None, timeout=60):
        params = dict(params or {})
        params["_timeout"] = timeout
        self.c.settimeout(timeout + 15)  # a stuck daemon never hangs the command
        try:
            self.c.sendall((json.dumps({"method": method, "params": params}) + "\n").encode())
            line = self.f.readline()
        except (socket.timeout, OSError) as e:
            return {"error": {"message": f"bridge daemon did not answer {method}: {e}"}}
        return json.loads(line) if line else {"error": {"message": "bridge daemon closed the connection"}}


def ok(r, what):
    if "error" in r:
        emit(f"FAILED: {what}: {r['error'].get('message')}")
        sys.exit(5)
    return r.get("result")


# ---------------------------------------------------------------- rollout watch

def rollout_path(tid, wait=5, glob_ok=True):
    """The file Codex is appending to now. Long threads are split into segment files
    (rollout-...-<tid>_<segment>.jsonl), so ask the app server; guess by glob only as a fallback."""
    try:
        c = Client()
        path = ((c.call("thread/read", {"threadId": tid}, timeout=10).get("result") or {}).get("thread") or {}).get("path")
        if path and os.path.exists(path):
            return path
    except (Exception, SystemExit):
        pass
    if not glob_ok:
        return None
    end = time.time() + wait
    while True:
        hits = glob.glob(str(SESSIONS / "**" / f"rollout-*{tid}*.jsonl"), recursive=True)
        if hits:
            return max(hits, key=os.path.getmtime)
        if time.time() > end:
            return None
        time.sleep(0.2)


def watch(tid, timeout=3300, path=None, offset=None, turn_id=None, match_text=None, follow=False):
    """Stream one turn from the thread's rollout until it ends.

    Which turn: `turn_id` (from turn/start), else the first turn whose user message contains
    `match_text` (a queued send), else the first turn that starts after `offset`. `follow` means
    the turn is already running before `offset`. Other people's turns are ignored.

    Reads bytes and parses complete lines only, so a line caught mid-write is never lost. Every
    10 s of quiet it asks Codex for the current file (long threads move to new segment files), and
    after 30 s of quiet it checks that some process still holds the thread."""
    path = path or rollout_path(tid)
    if not path:
        emit(f"FAILED: no rollout file for {tid}")
        return 5
    f = open(path, "rb")
    f.seek(offset or 0)
    buf, last, candidate = b"", None, None
    started, ours = follow, turn_id
    end = time.time() + timeout
    quiet, next_check, orphan = time.time(), time.time() + 10, 0

    while time.time() < end:
        chunk = f.read(1 << 20)
        if chunk:
            quiet = time.time()
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                p = o.get("payload") or {}
                t, tt = p.get("type"), p.get("turn_id")
                if t == "task_started":
                    if turn_id:
                        hit = tt == turn_id
                    elif match_text:
                        candidate, hit = tt, False
                    else:
                        hit = not started or ours is None
                    if hit:
                        started, ours = True, tt
                        emit(f"STARTED {now()} turn {tt}")
                    continue
                if not started and candidate and tt == candidate and t == "item_completed":
                    it = p.get("item") or {}
                    if it.get("type") == "UserMessage":
                        text = " ".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict))
                        if match_text.strip()[:200] in text:
                            started, ours = True, candidate
                            emit(f"STARTED {now()} turn {candidate}")
                        candidate = None
                    continue
                if not started or (ours and tt and tt != ours):
                    continue
                if t == "item_completed":
                    it = p.get("item") or {}
                    if it.get("type") == "AgentMessage":
                        text = " ".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict))
                        if text and text != last:
                            last = text
                            emit(f"AGENT: {text}")
                    elif it.get("type") == "DynamicToolCall" and it.get("tool") == "send_to_claude":
                        emit(f"CODEX: {(it.get('arguments') or {}).get('message', '')}")
                elif t == "task_complete":
                    emit(f"DONE {now()}: {p.get('last_agent_message') or ''}")
                    return 0
                elif t in ("turn_aborted", "task_aborted"):
                    emit(f"FAILED {now()}: turn {p.get('reason', 'aborted')}")
                    return 5
            continue
        if time.time() >= next_check:
            next_check = time.time() + 10
            cur = rollout_path(tid, wait=0, glob_ok=False)
            if cur and cur != path:
                f.close()
                f, path, buf = open(cur, "rb"), cur, b""
                continue
            if time.time() - quiet > 30:
                orphan = 0 if lock_holder(tid) else orphan + 1
                if orphan >= 2:
                    what = "the turn stopped without finishing" if started else "the message was not picked up"
                    emit(f"FAILED {now()}: no process holds {tid}; {what}. Check with: bridge.py status {tid}")
                    return 5
        time.sleep(0.2)
    emit(f"TIMEOUT: {tid} still running. Resume with: bridge.py watch {tid}")
    return 4


def file_size(path):
    return os.path.getsize(path) if path else 0


# ---------------------------------------------------------------- commands

def held_by_app(c, tid):
    """Resume through the daemon. Returns True when another process (the ChatGPT app) is the writer."""
    r = c.call("thread/resume", {"threadId": tid})
    if "error" in r:
        if LOCKED in r["error"].get("message", ""):
            return True
        ok(r, "thread/resume")
    return False


# The app route: the ChatGPT app's page reaches its own app server (the writer of every thread the
# app has open) through its preload API. With the app running on a local debugging port, a turn,
# steer or stop on an app-held thread goes through the app itself and starts at once, instead of
# waiting up to 10 s for the app's queue timer. `app-connect` relaunches the app with the port.
APP_PORT = int(os.environ.get("CODEX_CDP_PORT", "9334"))
APP_JS = Path(__file__).resolve().parent / "live" / "app.mjs"
NODE = next((p for p in (os.environ.get("NODE_BIN"), "/opt/homebrew/bin/node", "/usr/local/bin/node")
             if p and os.path.exists(p)), "node")


def app_port_up():
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/json/version", timeout=2):
            return True
    except Exception:
        return False


def app_call(method, params, timeout=40):
    """One app-server call through the ChatGPT app's page. None when the app has no debugging port."""
    if not app_port_up():
        return None
    p = subprocess.run(["perl", "-e", f"alarm {timeout}; exec @ARGV", NODE, str(APP_JS), "call", method,
                        json.dumps(params)], capture_output=True, text=True)
    if p.returncode:
        return {"error": {"message": (p.stderr.strip() or f"app route exited {p.returncode}")[:300]}}
    return json.loads(p.stdout)


def app_active_turn(tid):
    r = app_call("thread/read", {"threadId": tid, "includeTurns": True}) or {}
    turns = ((r.get("result") or {}).get("thread") or {}).get("turns") or []
    return turns[-1]["id"] if turns and turns[-1].get("status") == "inProgress" else None


def call_held(c, method, params):
    """Call through the daemon; when the ChatGPT app is the thread's writer, through the app instead."""
    r = c.call(method, params)
    if LOCKED in ((r.get("error") or {}).get("message") or ""):
        r = app_call(method, params) or r
    return r


def turn_params(tid, text, a, policy=None, allow_overrides=True):
    p = {"threadId": tid, "input": [{"type": "text", "text": text}]}
    stored = policy if policy is not None else load_threads().get(tid, {})
    if stored.get("sandbox") and stored.get("cwd"):
        _, _, _, sandbox_policy = execution_policy(
            Path(stored["cwd"]), sandbox=stored["sandbox"],
            no_network=not stored.get("network_access", True),
        )
        p["sandboxPolicy"] = sandbox_policy
    model = (getattr(a, "model", None) if allow_overrides else None) or stored.get("model")
    effort = (getattr(a, "effort", None) if allow_overrides else None) or stored.get("effort")
    if model:
        p["model"] = model
    if effort:
        p["effort"] = effort
    return p


def set_title(c, tid, title):
    """Name a new thread without failing the task. Right after turn/start the rollout file can still be
    empty, and Codex refuses the rename; the turn is already running, so a hard exit here ended the watch
    on a live task. Retry briefly, then warn and carry on."""
    for delay in (0, 2, 4, 8):
        time.sleep(delay)
        if "error" not in c.call("thread/name/set", {"threadId": tid, "name": title}):
            return True
    emit(f"WARN: title not set yet; the turn is running. Retry: rename {tid} \"{title}\"")
    return False


def cmd_start(c, a):
    try:
        cwd = normalize_cwd(a.cwd)
        mode, network_access, config, _ = execution_policy(
            cwd, sandbox=getattr(a, "sandbox", None), full_access=getattr(a, "full_access", False),
            no_network=getattr(a, "no_network", False),
        )
        route = route_model(a.role)
    except (AttributeError, ValueError) as exc:
        emit(f"FAILED: start no autorizado: {exc}")
        return 5
    thread_params = {
        "cwd": str(cwd), "dynamicTools": [TOOL], "developerInstructions": INSTRUCTIONS,
        "sandbox": mode, "approvalPolicy": "never", "model": route["model"],
    }
    if config:
        thread_params["config"] = config
    r = ok(c.call("thread/start", thread_params), "thread/start")
    tid = r["thread"]["id"]
    policy = {"from": a.sender, "title": a.title, "role": a.role, "cwd": str(cwd), "sandbox": mode,
              "network_access": network_access, "model": route["model"], "effort": route["effort"]}
    save_thread(tid, **policy)
    emit(f"THREAD {tid} codex://threads/{tid}")
    append(LOG, {"event": "start", "thread": tid, "from": a.sender, "title": a.title})
    turn = ok(c.call("turn/start", turn_params(tid, a.prompt, a, policy=policy, allow_overrides=False)),
              "turn/start")["turn"]["id"]
    if a.title:
        set_title(c, tid, a.title)
    if a.section:
        move(c, tid, a.section)
    if a.no_watch:
        return 0
    return watch(tid, a.timeout, offset=0, turn_id=turn)


def cmd_send(c, a):
    path = rollout_path(a.thread, wait=0)
    off = file_size(path)
    if held_by_app(c, a.thread):
        if app_port_up():
            if app_active_turn(a.thread):
                emit("FAILED: a turn is running in the ChatGPT app. Use steer to add to it, or wait.")
                return 5
            r = app_call("turn/start", turn_params(a.thread, a.prompt, a))
            if r and "result" in r:
                turn = r["result"]["turn"]["id"]
                emit(f"SENT {now()} to {a.thread} turn {turn} (through the ChatGPT app)")
                append(LOG, {"event": "sent_app", "thread": a.thread, "turn": turn})
                return 0 if a.no_watch else watch(a.thread, a.timeout, path=path, offset=off, turn_id=turn)
            emit(f"WARN: app route failed ({(r or {}).get('error', {}).get('message')}); queueing instead")
        r = ok(c.call("thread/queue/add", {"threadId": a.thread, "clientUserMessageId": str(uuid.uuid4()),
                                           "input": [{"type": "text", "text": a.prompt}]}), "thread/queue/add")
        emit(f"QUEUED {now()} {r['queuedSubmission'].get('id')}: the ChatGPT app holds this thread; "
             "it checks its queue about every 10 s and then starts the turn")
        append(LOG, {"event": "queued", "thread": a.thread})
        return 0 if a.no_watch else watch(a.thread, a.timeout, path=path, offset=off, match_text=a.prompt)
    running = c.call("bridge/running", {"threadId": a.thread}).get("result", {}).get("turnId")
    if running:
        emit(f"FAILED: a turn is running ({running}). Use steer to add to it, or wait.")
        return 5
    turn = ok(c.call("turn/start", turn_params(a.thread, a.prompt, a)), "turn/start")["turn"]["id"]
    emit(f"SENT {now()} to {a.thread} turn {turn}")
    append(LOG, {"event": "sent", "thread": a.thread, "turn": turn})
    if a.no_watch:
        return 0
    return watch(a.thread, a.timeout, path=path, offset=off, turn_id=turn)


def cmd_watch(c, a):
    """Follow the turn that is running, or the next one if a message is waiting; otherwise
    print the last answer. --from-start replays the latest turn."""
    path = rollout_path(a.thread, wait=0)
    if not path:
        emit(f"FAILED: no rollout file for {a.thread}")
        return 5
    size = file_size(path)
    lines = tail_lines(path)
    pos = size - sum(len(x) + 1 for x in lines) + 1  # byte offset of the first complete line
    start_at, ended, answer = None, False, ""
    for raw in lines:
        if b'"task_started"' in raw:
            start_at, ended = pos, False
        elif b'"task_complete"' in raw or b'"turn_aborted"' in raw:
            ended = True
            try:
                answer = (json.loads(raw).get("payload") or {}).get("last_agent_message") or ""
            except ValueError:
                pass
        pos += len(raw) + 1
    if a.from_start and start_at is not None:
        return watch(a.thread, a.timeout, path=path, offset=start_at)
    if start_at is not None and not ended:
        return watch(a.thread, a.timeout, path=path, offset=start_at)
    if start_at is None:  # the running turn began more than 4 MB ago
        return watch(a.thread, a.timeout, path=path, offset=size, follow=True)
    busy = c.call("bridge/running", {"threadId": a.thread}).get("result", {}).get("turnId")
    waiting = (c.call("thread/queue/list", {"threadId": a.thread}).get("result") or {}).get("data") or []
    if busy or waiting:
        emit(f"WAITING {now()}: " + (f"turn {busy} is starting" if busy else f"{len(waiting)} queued message(s)"))
        return watch(a.thread, a.timeout, path=path, offset=size, turn_id=busy)
    emit("IDLE: no turn running. Last answer: " + answer)
    return 0


def active_turn(c, tid):
    t = c.call("bridge/running", {"threadId": tid}).get("result", {}).get("turnId")
    if t:
        return t
    r = ok(c.call("thread/read", {"threadId": tid, "includeTurns": True}), "thread/read")
    turns = r["thread"].get("turns") or []
    return turns[-1]["id"] if turns and turns[-1].get("status") == "inProgress" else None


def app_turn_op(c, a, method, verb):
    """Steer or stop a turn the ChatGPT app is running, through the app. None when not app-held."""
    if not held_by_app(c, a.thread):
        return None
    t = app_active_turn(a.thread)
    if not t:
        emit("FAILED: no running turn in this thread (the ChatGPT app holds it)" +
             ("" if app_port_up() else "; run app-connect to steer or stop app-held turns"))
        return 5
    p = {"threadId": a.thread, "turnId": t} if method == "turn/interrupt" else \
        {"threadId": a.thread, "expectedTurnId": t, "input": [{"type": "text", "text": a.text}]}
    r = app_call(method, p)
    if not r or "error" in r:
        emit(f"FAILED: {method} through the app: {(r or {}).get('error', {}).get('message')}")
        return 5
    emit(f"{verb} {now()} turn {t} (through the ChatGPT app)")
    return 0


def cmd_steer(c, a):
    r = app_turn_op(c, a, "turn/steer", "STEERED")
    if r is not None:
        return r
    t = active_turn(c, a.thread)
    if not t:
        emit("FAILED: no running turn in this thread.")
        return 5
    ok(c.call("turn/steer", {"threadId": a.thread, "expectedTurnId": t, "input": [{"type": "text", "text": a.text}]}),
       "turn/steer")
    emit(f"STEERED {now()} turn {t}")
    return 0


def cmd_stop(c, a):
    r = app_turn_op(c, a, "turn/interrupt", "STOPPED")
    if r is not None:
        return r
    t = active_turn(c, a.thread)
    if not t:
        emit("FAILED: no running turn in a thread this bridge writes.")
        return 5
    ok(c.call("turn/interrupt", {"threadId": a.thread, "turnId": t}), "turn/interrupt")
    emit(f"STOPPED {now()} turn {t}")
    return 0


def cmd_list(c, a):
    if a.search:
        r = ok(c.call("thread/search", {"searchTerm": a.search, "limit": a.limit, "archived": a.archived}), "thread/search")
    else:
        r = ok(c.call("thread/list", {"limit": a.limit, "archived": a.archived}), "thread/list")
    for t in r.get("data", []):
        t = t.get("thread", t)
        when = datetime.fromtimestamp(t.get("recencyAt") or t.get("updatedAt") or 0).strftime("%m-%d %H:%M")
        sec = (t.get("section") or {}).get("name", "-")
        name = t.get("name") or (t.get("preview") or "").splitlines()[0][:60] if (t.get("name") or t.get("preview")) else ""
        print(f"{when}  {sec[:14]:<14} {name[:60]:<60} {t['id']}")
    return 0


def cmd_read(c, a):
    r = ok(c.call("thread/read", {"threadId": a.thread, "includeTurns": True}), "thread/read")
    t = r["thread"]
    turns = t.get("turns", [])
    print(f"# {t.get('name') or a.thread}  ({len(turns)} turns)  codex://threads/{a.thread}")
    for turn in turns[-a.turns:]:
        print(f"\n## turn {turn['status']}")
        for i in turn.get("items", []):
            if i.get("type") == "userMessage":
                print("user:", " ".join(x.get("text", "") for x in i.get("content", []) if isinstance(x, dict)))
            elif i.get("type") == "agentMessage":
                print("agent:", i.get("text", ""))
            elif i.get("type") == "dynamicToolCall":
                print("send_to_claude:", (i.get("arguments") or {}).get("message", ""))
    return 0


def lock_holder(tid):
    lock = Path.home() / ".codex" / "thread-writer-locks" / f"{tid}.lock"
    if not lock.exists():
        return None
    try:
        out = subprocess.run(["lsof", "-t", str(lock)], capture_output=True, text=True, timeout=5).stdout.split()
    except subprocess.TimeoutExpired:
        return "unknown"
    return out[0] if out else None


def writer_name(pid):
    if not pid:
        return "nobody"
    if pid == "unknown":
        return "unknown (lsof timed out)"
    ppid = subprocess.run(["ps", "-o", "ppid=", "-p", pid], capture_output=True, text=True, timeout=5).stdout.strip()
    parent = subprocess.run(["ps", "-o", "command=", "-p", ppid], capture_output=True, text=True, timeout=5).stdout
    if str((ping() or {}).get("pid")) == ppid:
        return "this bridge"
    if "/Contents/MacOS/ChatGPT" in parent:
        return "the ChatGPT app"
    return f"pid {pid} (parent: {parent.strip()[:60]})"


def cmd_status(c, a):
    who = writer_name(lock_holder(a.thread))
    r = ok(c.call("thread/read", {"threadId": a.thread, "includeTurns": True}), "thread/read")
    turns = r["thread"].get("turns") or []
    q = c.call("thread/queue/list", {"threadId": a.thread}).get("result", {}).get("data", [])
    print(f"writer: {who}\nlast turn: {turns[-1]['status'] if turns else 'none'}  turns: {len(turns)}\nqueued: {len(q)}")
    return 0


def sections(c):
    return ok(c.call("threadSection/list", {}), "threadSection/list").get("data", [])


def move(c, tid, sec):
    ss = sections(c)
    hit = [s for s in ss if s.get("id") == sec or s.get("name", "").lower() == sec.lower()]
    if not hit:
        emit(f"FAILED: no section {sec!r}. Have: {', '.join(s.get('name', '') for s in ss)}")
        return 5
    ok(c.call("thread/section/move", {"threadId": tid, "sectionId": hit[0]["id"]}), "thread/section/move")
    emit(f"MOVED {tid} to {hit[0].get('name')}")
    return 0


def cmd_simple(c, a):
    m = {"rename": ("thread/name/set", {"threadId": a.thread, "name": getattr(a, "name", None)}),
         "archive": ("thread/archive", {"threadId": a.thread}),
         "unarchive": ("thread/unarchive", {"threadId": a.thread}),
         "fork": ("thread/fork", {"threadId": a.thread})}[a.cmd]
    r = ok(call_held(c, *m), m[0])
    new = ((r or {}).get("thread") or {}).get("id")
    emit(f"OK {a.cmd} {a.thread}" + (f" -> {new}" if new else ""))
    return 0


def cmd_settings(c, a):
    p = {"threadId": a.thread}
    if a.model:
        p["model"] = a.model
    if a.effort:
        p["effort"] = a.effort
    ok(c.call("thread/settings/update", p), "thread/settings/update")
    emit(f"OK settings {a.thread} {a.model or ''} {a.effort or ''}")
    return 0


def cmd_queue(c, a):
    if a.delete:
        ok(c.call("thread/queue/delete", {"threadId": a.thread, "queuedSubmissionId": a.delete}), "thread/queue/delete")
        emit(f"OK deleted {a.delete}")
        return 0
    for q in ok(c.call("thread/queue/list", {"threadId": a.thread}), "thread/queue/list").get("data", []):
        print(json.dumps(q)[:300])
    return 0


def cmd_goal(c, a):
    if a.clear:
        r = c.call("thread/goal/clear", {"threadId": a.thread})
    elif a.objective:
        r = c.call("thread/goal/set", {"threadId": a.thread, "objective": a.objective})
    else:
        r = c.call("thread/goal/get", {"threadId": a.thread})
    print(json.dumps(ok(r, "goal"), indent=1))
    return 0


def cmd_models(c, a):
    for m in ok(c.call("model/list", {}), "model/list")["data"]:
        print(m.get("id"))
    return 0


def cmd_mcp(c, a):
    p = {"serverName": a.server} if a.server else {}
    for s in ok(c.call("mcpServerStatus/list", p), "mcpServerStatus/list").get("data", []):
        tools = list((s.get("tools") or {}).keys())
        print(f"{s['name']}  {len(tools)} tools")
        if a.server:
            for t in tools:
                print("  ", t)
    return 0


def cmd_mcp_call(c, a):
    tid = a.thread
    if not tid:  # tool calls need a loaded thread; use a scratch one
        tid = ok(c.call("thread/start", {"cwd": str(HOME), "ephemeral": True}), "thread/start")["thread"]["id"]
    else:
        held_by_app(c, tid)
    r = ok(c.call("mcpServer/tool/call", {"threadId": tid, "server": a.server, "tool": a.tool,
                                          "arguments": json.loads(a.args or "{}")}, timeout=120), "mcpServer/tool/call")
    print(json.dumps(r, indent=1)[:20000])
    return 0


def cmd_rpc(c, a):
    print(json.dumps(c.call(a.method, json.loads(a.params or "{}"), timeout=120), indent=1))
    return 0


# ---------------------------------------------------------------- codex -> claude

def claude_sessions():
    out = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=30).stdout
    rows = []
    for d in json.loads(out or "[]"):
        rec = {}
        try:
            rec = json.loads((Path.home() / ".claude" / "sessions" / f"{d.get('pid')}.json").read_text())
        except Exception:
            pass
        rows.append({"name": d.get("name"), "status": d.get("status"), "kind": d.get("kind"),
                     "local_id": rec.get("hostSessionId"), "sessionId": d.get("sessionId"), "cwd": d.get("cwd")})
    return rows


def claude_link(r):
    """Clickable address for a Claude session. Desktop Code-tab sessions open by local_ id; background
    `claude --bg` sessions have no app page, so their bare session id is the only handle."""
    return f"claude://claude.ai/epitaxy/{r['local_id']}" if r.get("local_id") else (r.get("sessionId") or "-")


def cmd_claude_list(a):
    for r in claude_sessions():
        print(f"{r['status'] or '-':<8} {r['kind'] or '-':<12} {(r['name'] or '-')[:40]:<40} {claude_link(r)}")
    return 0


def cmd_to_claude(a):
    live = claude_sessions()
    hit = [r for r in live if a.to in (r["name"], r["local_id"], r["sessionId"])]
    if not hit:
        emit(f"FAILED: no live Claude session {a.to!r}. Live: " + ", ".join(r["name"] or "" for r in live))
        return 5
    tid = a.thread or os.environ.get("CODEX_THREAD_ID")
    mid = uuid.uuid4().hex[:8]
    append(INBOX, {"id": mid, "from_thread": tid, "to": hit[0]["local_id"] or hit[0]["sessionId"],
                   "to_name": hit[0]["name"], "message": a.message})
    emit(f"QUEUED {mid} for {hit[0]['name']} {claude_link(hit[0])}; a Claude relay delivers it with SendMessage")
    return 0


def cmd_relay(a):
    """Tail the inbox. Each new message becomes one line for the Claude session running this under Monitor.

    The read position is saved after every line, so messages written while the relay was down
    (between expiry and re-arm) are delivered when it comes back. The first run starts at the end."""
    HOME.mkdir(parents=True, exist_ok=True)
    INBOX.touch()
    mark = HOME / f"relay-{a.name}.offset"
    size = INBOX.stat().st_size
    try:
        pos = int(mark.read_text())
        pos = pos if pos <= size else size
    except (OSError, ValueError):
        pos = size
    mark.write_text(str(pos))
    f = open(INBOX, "rb")
    f.seek(pos)
    buf, end = b"", time.time() + a.minutes * 60
    try:
        while time.time() < end:
            chunk = f.read(65536)
            if not chunk:
                time.sleep(0.2)
                continue
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                pos += len(raw) + 1
                try:
                    m = json.loads(raw)
                    emit(f"RELAY {m['id']} to={m.get('to')} name={m.get('to_name') or ''} "
                         f"via={m.get('via', 'to-claude')} from=codex://threads/{m.get('from_thread')} :: {m['message']}")
                except (ValueError, KeyError):
                    emit(f"RELAY-BAD-LINE at byte {pos}: {raw[:200]!r}")
                mark.write_text(str(pos))
    finally:
        emit("RELAY-EXPIRED: re-arm the relay")
    return 0


def cmd_claude_read(a):
    """Last messages of a Claude Code session, from the tail of its transcript."""
    rec = [r for r in claude_sessions() if a.session in (r["name"], r["local_id"], r["sessionId"])]
    sid = rec[0]["sessionId"] if rec else a.session
    hits = glob.glob(str(Path.home() / ".claude" / "projects" / "*" / f"{sid}.jsonl"))
    hits = hits or glob.glob(str(Path.home() / ".claude" / "projects" / "*" / f"{a.session}*.jsonl"))
    if not hits:
        emit(f"FAILED: no transcript for {a.session}")
        return 5
    out = []
    for line in tail_lines(max(hits, key=os.path.getmtime)):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        msg = o.get("message") or {}
        if o.get("type") in ("user", "assistant") and not o.get("isMeta"):
            c = msg.get("content")
            text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text")
            if text.strip():
                out.append(f"{o['type']}: {text.strip()[:1500]}")
    print("\n".join(out[-a.last:]))
    return 0


ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
BACKGROUND_ID = re.compile(r"backgrounded\s*[·•]\s*([0-9a-fA-F]+)\s*[·•]", re.IGNORECASE)
REMOTE_URL = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]+")
REMOTE_CONTROL_TIMEOUT = 20
REMOTE_CONTROL_POLL = 0.5


def strip_ansi(value):
    """Elimina secuencias ANSI antes de analizar la salida de la CLI."""
    return ANSI_ESCAPE.sub("", value or "")


def parse_backgrounded_id(output):
    """Devuelve el identificador corto que imprime ``claude --bg``."""
    match = BACKGROUND_ID.search(strip_ansi(output))
    return match.group(1) if match else None


def claude_agents_all():
    """Consulta todas las sesiones sin leer estado privado de Claude a mano."""
    try:
        result = subprocess.run(["claude", "agents", "--json", "--all"],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"claude agents no respondió: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "sin salida").strip()
        raise RuntimeError(f"claude agents falló ({result.returncode}): {detail}")
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise RuntimeError(f"claude agents devolvió JSON inválido: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("sessions") or data.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError("claude agents no devolvió una lista de sesiones")
    return data


def session_for_short_id(rows, short_id):
    """Encuentra la fila de ``claude agents`` correspondiente al id corto."""
    return next((row for row in rows if str(row.get("id", "")) == short_id), None)


def remote_control_url(short_id, timeout=REMOTE_CONTROL_TIMEOUT):
    """Sondea los logs durante un tiempo limitado hasta que Remote Control esté activo."""
    deadline = time.monotonic() + max(0, timeout)
    while True:
        remaining = deadline - time.monotonic()
        if remaining < 0:
            return None
        try:
            result = subprocess.run(["claude", "logs", short_id], capture_output=True, text=True,
                                    timeout=max(0.1, min(5, remaining)))
            match = REMOTE_URL.search(strip_ansi((result.stdout or "") + "\n" + (result.stderr or "")))
            if match:
                return match.group(0)
        except (subprocess.TimeoutExpired, OSError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(REMOTE_CONTROL_POLL, remaining))


def started_log_path():
    return HOME / "claude-started.jsonl"


def append_started(record):
    """Añade un registro completo en una sola escritura, con permisos de usuario."""
    HOME.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode()
    fd = os.open(started_log_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(f"escritura parcial de claude-started.jsonl: {written}/{len(data)} bytes")
    finally:
        os.close(fd)


def read_started():
    records = []
    try:
        lines = started_log_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return records
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def claude_state(row):
    """Normaliza los estados que imprime ``claude agents`` al contrato del bridge."""
    values = {str(row.get(key, "")).lower() for key in ("status", "state")}
    if values & {"running", "active", "connecting", "working", "in_progress"}:
        return "running"
    if values & {"completed", "complete", "done", "exited", "stopped", "failed", "error"}:
        return "completed"
    return "unknown"


def session_row(record, rows):
    row = session_for_short_id(rows, str(record.get("short_id", "")))
    if row is None and record.get("session_id"):
        row = next((item for item in rows if item.get("sessionId") == record["session_id"]), None)
    return row


def session_state(record, rows):
    row = session_row(record, rows)
    return claude_state(row) if row else "unknown"


def resume_command(session_id):
    return f"claude --resume {session_id}" if session_id else "pending"


def default_claude_name(thread):
    return f"codex {datetime.now().strftime('%H:%M')} {(thread or 'none')[:4]}"


def notify_started(name, remote_url):
    """Envía una notificación opcional sin convertirla en requisito de arranque."""
    url = remote_url or "-"
    text = f"{name} · {url}"

    def apple_string(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'

    script = f"display notification {apple_string(text)} with title {apple_string('Codex → Claude')}"
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    if result.returncode:
        detail = (result.stderr or result.stdout or "sin detalle").strip()
        print(f"WARNING: no se pudo mostrar la notificación: {detail}", file=sys.stderr)


def cmd_claude_start(a):
    name = a.name or default_claude_name(a.thread)
    command = ["claude", "--bg", f"--name={name}"]
    if a.remote_control:
        command += [f"--remote-control={name}"]
    command += ["--", a.prompt]
    result = subprocess.run(command, cwd=a.cwd, capture_output=True, text=True, timeout=60)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if output:
        print(output)
    if result.returncode:
        print(f"ERROR: claude --bg terminó con código {result.returncode}", file=sys.stderr)
        return result.returncode
    short_id = parse_backgrounded_id(output)
    if not short_id:
        print("ERROR: claude --bg no devolvió un id corto en una línea 'backgrounded · <id> · <name>'. "
              f"Salida: {output or '(vacía)'}", file=sys.stderr)
        return 5

    try:
        rows = claude_agents_all()
    except Exception as exc:
        rows = []
        print(f"WARNING: no se pudo resolver sessionId con claude agents: {exc}", file=sys.stderr)
    row = session_for_short_id(rows, short_id)
    session_id = row.get("sessionId") if row else None
    if not session_id:
        print(f"WARNING: claude agents no contiene todavía el id {short_id}; resume queda pendiente.", file=sys.stderr)

    url = remote_control_url(short_id) if a.remote_control else None
    if a.remote_control and not url:
        print(f"WARNING: Remote Control no publicó URL para {short_id} dentro de {REMOTE_CONTROL_TIMEOUT} s.",
              file=sys.stderr)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "short_id": short_id,
        "session_id": session_id,
        "name": name,
        "cwd": str(Path(a.cwd).resolve()),
        "from_thread": a.thread,
        "remote_url": url,
    }
    append_started(record)
    if a.notify:
        try:
            notify_started(name, url)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"WARNING: no se pudo mostrar la notificación: {exc}", file=sys.stderr)
    print(f"CLAUDE-STARTED {short_id} name={name} url={url or '-'} "
          f"resume=\"{resume_command(session_id)}\" from=codex://threads/{a.thread or '-'}")
    return 0


def cmd_claude_started(a):
    records = read_started()
    if a.last is not None:
        records = records[-a.last:] if a.last > 0 else []
    try:
        rows = claude_agents_all()
    except (OSError, RuntimeError) as exc:
        rows = []
        print(f"WARNING: no se pudo consultar el estado actual: {exc}", file=sys.stderr)
    for record in records:
        short_id = record.get("short_id", "-")
        url = record.get("remote_url") or "-"
        row = session_row(record, rows)
        session_id = record.get("session_id") or (row or {}).get("sessionId")
        print(f"{session_state(record, rows):<9} {record.get('name', '-')} "
              f"id={short_id} session_id={session_id or '-'} "
              f"url={url} resume=\"{resume_command(session_id)}\" "
              f"cwd={record.get('cwd', '-')}")
    return 0


def cmd_doctor(a):
    """Read-only: every app-server method and notification this file relies on, checked against the
    installed codex's schema, plus a daemon ping. Never starts the daemon or a turn. Run after every
    ChatGPT update; logic lives in doctor.py so this file stays small."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import doctor
    return doctor.main(["--codex", CODEX] + (["--verbose"] if a.verbose else []))


# ---------------------------------------------------------------- main

def cmd_app_connect(a):
    """Relaunch the ChatGPT app in the background with its debugging port on 127.0.0.1, so app-held
    threads take turns, steers and stops at once. Quitting ends any turn the app is running, so it
    refuses while a Codex log was written in the last minute (--force overrides)."""
    if app_port_up():
        print(json.dumps({"ok": True, "port": APP_PORT, "relaunched": False})); return 0
    busy = [p for p in glob.glob(str(SESSIONS / "**" / "*.jsonl"), recursive=True) if time.time() - os.path.getmtime(p) < 60]
    if busy and not a.force:
        print(f"REFUSED: {len(busy)} Codex thread(s) wrote in the last minute; relaunching the app would cut them. "
              "Wait, or pass --force."); return 3
    app = "ChatGPT.app/Contents/MacOS/ChatGPT"
    subprocess.run(["osascript", "-e", 'tell application "ChatGPT" to quit'], capture_output=True, timeout=20)
    for _ in range(40):
        if subprocess.run(["pgrep", "-f", app], capture_output=True).returncode:
            break
        # The app asks "Quit ChatGPT? Scheduled tasks won't run while ChatGPT is closed"; it relaunches
        # seconds later, so confirm it.
        subprocess.run(["osascript", "-e", 'tell application "System Events" to tell process "ChatGPT" to '
                        'repeat with w in windows\nif exists button "Quit" of w then click button "Quit" of w\nend repeat'],
                       capture_output=True, timeout=10)
        time.sleep(0.5)
    else:
        print("FAILED: the ChatGPT app did not quit in 20 s"); return 5
    subprocess.run(["open", "-g", "-a", "/Applications/ChatGPT.app", "--args", f"--remote-debugging-port={APP_PORT}",
                    "--remote-debugging-address=127.0.0.1"], timeout=20)
    for _ in range(60):
        if app_port_up():
            print(json.dumps({"ok": True, "port": APP_PORT, "relaunched": True})); return 0
        time.sleep(1)
    print("FAILED: the ChatGPT app did not open its debugging port in 60 s"); return 5


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    E = ["minimal", "low", "medium", "high", "xhigh"]

    p = sub.add_parser("start"); p.add_argument("prompt"); p.add_argument("--title"); p.add_argument("--section")
    p.add_argument("--role", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--from", dest="sender", default=os.environ.get("CLAUDE_CODE_HOST_SESSION_ID"))
    sandbox = p.add_mutually_exclusive_group()
    sandbox.add_argument("--sandbox", choices=("workspace-write", "read-only"))
    sandbox.add_argument("--full-access", action="store_true")
    p.add_argument("--no-network", action="store_true")
    p.add_argument("--timeout", type=int, default=3300)
    p.add_argument("--no-watch", action="store_true")
    p = sub.add_parser("send"); p.add_argument("thread"); p.add_argument("prompt"); p.add_argument("--model")
    p.add_argument("--effort", choices=E); p.add_argument("--timeout", type=int, default=3300)
    p.add_argument("--no-watch", action="store_true")
    p = sub.add_parser("watch"); p.add_argument("thread"); p.add_argument("--timeout", type=int, default=3300)
    p.add_argument("--from-start", action="store_true")
    p = sub.add_parser("steer"); p.add_argument("thread"); p.add_argument("text")
    p = sub.add_parser("stop"); p.add_argument("thread")
    p = sub.add_parser("list"); p.add_argument("--limit", type=int, default=25); p.add_argument("--search")
    p.add_argument("--archived", action="store_true")
    p = sub.add_parser("read"); p.add_argument("thread"); p.add_argument("--turns", type=int, default=2)
    p = sub.add_parser("status"); p.add_argument("thread")
    p = sub.add_parser("rename"); p.add_argument("thread"); p.add_argument("name")
    for n in ("archive", "unarchive", "fork"):
        sub.add_parser(n).add_argument("thread")
    sub.add_parser("sections")
    p = sub.add_parser("move"); p.add_argument("thread"); p.add_argument("section")
    p = sub.add_parser("settings"); p.add_argument("thread"); p.add_argument("--model"); p.add_argument("--effort", choices=E)
    p = sub.add_parser("queue"); p.add_argument("thread"); p.add_argument("--delete")
    p = sub.add_parser("goal"); p.add_argument("thread"); p.add_argument("objective", nargs="?"); p.add_argument("--clear", action="store_true")
    sub.add_parser("models")
    p = sub.add_parser("mcp"); p.add_argument("server", nargs="?")
    p = sub.add_parser("mcp-call"); p.add_argument("server"); p.add_argument("tool"); p.add_argument("args", nargs="?")
    p.add_argument("--thread")
    p = sub.add_parser("rpc"); p.add_argument("method"); p.add_argument("params", nargs="?")
    sub.add_parser("claude-list")
    p = sub.add_parser("to-claude"); p.add_argument("--to", required=True); p.add_argument("message"); p.add_argument("--thread")
    p = sub.add_parser("relay"); p.add_argument("--minutes", type=int, default=55); p.add_argument("--name", default="default")
    p = sub.add_parser("claude-start"); p.add_argument("prompt"); p.add_argument("--cwd", default=str(Path.home()))
    p.add_argument("--name", default=None)
    p.add_argument("--remote-control", dest="remote_control", action="store_true", default=True)
    p.add_argument("--no-remote-control", dest="remote_control", action="store_false")
    p.add_argument("--thread", default=os.environ.get("CODEX_THREAD_ID"))
    p.add_argument("--notify", action="store_true")
    p = sub.add_parser("claude-started"); p.add_argument("--last", type=int, default=None)
    p = sub.add_parser("claude-read"); p.add_argument("session"); p.add_argument("--last", type=int, default=4)
    for n in ("serve", "up", "ping"):
        sub.add_parser(n)
    p = sub.add_parser("down"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("app-connect"); p.add_argument("--force", action="store_true")
    sub.add_parser("doctor").add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if a.cmd == "doctor":
        return cmd_doctor(a)

    if a.cmd == "serve":
        return serve()
    if a.cmd == "up":
        up(); print(json.dumps(ping())); return 0
    if a.cmd == "ping":
        r = ping(); print(json.dumps(r) if r else "down"); return 0 if r else 1
    if a.cmd == "down":
        r = ping()
        # Stopping the daemon interrupts every turn it is carrying (a restart to load new
        # instructions kills a live turn). Refuse while anything runs unless forced.
        if r and (r.get("running") or r.get("workers")) and not a.force:
            print("REFUSED: turns are running: " + json.dumps({"running": r.get("running"), "workers": r.get("workers")})
                  + ". Wait for them to finish, or pass --force to interrupt them.")
            return 3
        if r:
            os.kill(r["pid"], 15)
        print("down"); return 0
    if a.cmd == "app-connect":
        return cmd_app_connect(a)
    local = {"claude-read": cmd_claude_read, "claude-list": cmd_claude_list, "to-claude": cmd_to_claude,
             "relay": cmd_relay, "claude-start": cmd_claude_start, "claude-started": cmd_claude_started}
    if a.cmd in local:
        return local[a.cmd](a)
    c = Client()
    if a.cmd == "sections":
        for s in sections(c):
            print(f"{s.get('name', ''):<24} {s.get('id')}")
        return 0
    if a.cmd == "move":
        return move(c, a.thread, a.section)
    return {"start": cmd_start, "send": cmd_send, "watch": cmd_watch, "steer": cmd_steer, "stop": cmd_stop,
            "list": cmd_list, "read": cmd_read, "status": cmd_status, "rename": cmd_simple, "archive": cmd_simple,
            "unarchive": cmd_simple, "fork": cmd_simple, "settings": cmd_settings, "queue": cmd_queue,
            "goal": cmd_goal, "models": cmd_models, "mcp": cmd_mcp, "mcp-call": cmd_mcp_call, "rpc": cmd_rpc}[a.cmd](c, a)


if __name__ == "__main__":
    sys.exit(main() or 0)
