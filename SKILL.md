---
name: claude-codex-bridge
description: Two-way bridge between Claude Code and Codex (the ChatGPT desktop app's agents). From Claude - start Codex agent tasks (computer use included), send to any Codex thread including ones the ChatGPT app has open, stream progress back live, steer, stop, rename, fork, archive, file into sections, switch model and effort, set goals, call Codex's MCP tools, and reach every app-server method. From Codex - list live Claude Code sessions, message any of them, start background Claude sessions and read their transcripts. USE WHEN "send this to ChatGPT", "give this to Codex", "Codex agent", "Codex computer use", "ChatGPT computer use", "message the Codex thread", "check the Codex task", "codex bridge", "claude codex bridge", or, inside Codex, "tell Claude", "message Claude", "ask the Claude session", "start a Claude session".
---

# Claude ⇄ Codex bridge

One script, `scripts/bridge.py`, used from both sides. It talks to Codex through the app server bundled with the ChatGPT app, and to Claude Code through the `claude` CLI plus a relay inbox.

```bash
B=~/.claude/skills/claude-codex-bridge/scripts/bridge.py
```

A small daemon starts on first use (`python3 $B up`, `ping`, `down`). `down` refuses while a turn runs (`--force` overrides). The socket is `~/.claude-codex-bridge/bridge.sock`, mode 0600.

## From Claude Code

**Never run `start`, `send` or `watch` in foreground Bash.** Use Monitor (or `--no-watch`, then `watch` under Monitor). A foreground command blocks the chat until it exits.

**Codex never takes over the screen.** Every `start` task carries a developer instruction (`INSTRUCTIONS` in `scripts/bridge.py`): no raising or focusing windows, no pointer moves, a headless browser only, one browser per task with pages as tabs. Foreground computer use needs `FOREGROUND AUTHORIZED` in the prompt with the exact app and action.

**App route.** When the ChatGPT app has a thread open, no outside process can write it. `python3 $B app-connect` relaunches the app in the background with a debugging port on 127.0.0.1, after which `send`, `steer`, `stop`, `rename`, `archive`, `unarchive` and `fork` go through the app itself (`scripts/live/app.mjs`). Without it, `send` falls back to the app's queue. `app-connect` refuses while a Codex turn ran in the last minute (`--force` overrides), because quitting the app ends its turns.

| Do | Command |
|---|---|
| New agent task | `python3 $B start "prompt" --title "Name" --section Personal --cwd <dir> [--model gpt-6.1-sol] [--effort low]`. Replies go to this session (`--from` defaults to it) |
| Message any thread | `python3 $B send <thread> "prompt"`: a new turn if the thread is free, queued if the ChatGPT app has it open (the app runs it itself). The watch follows only that turn, matched by turn id or by the message text, never someone else's |
| Follow a running or finished turn | `python3 $B watch <thread>`: follows the running turn, prints `WAITING` and follows the next one if a message is queued or starting, else `IDLE` with the last answer. `--from-start` replays the latest turn |
| Add to a running turn / stop it | `steer <thread> "text"` / `stop <thread>`: threads this bridge writes, and app-held threads through the app route |
| Instant app-held delivery | `python3 $B app-connect`: once per ChatGPT launch; afterwards every app-held verb goes through the app (above) |
| Find and read | `list [--search T] [--archived]`, `read <thread> [--turns N]`, `status <thread>` (writer, last turn, queue) |
| Manage | `rename`, `fork`, `archive`, `unarchive`, `sections`, `move <thread> <section>`, `settings <thread> --model M --effort E`, `goal <thread> ["objective"] [--clear]`, `queue <thread> [--delete ID]` |
| Codex's MCP tools | `mcp [server]` lists them (codex_apps: 286 tools across Linear, HubSpot, Teams, Stripe, Clay, Fireflies; cua_repl computer use; node_repl; computer-history). `mcp-call <server> <tool> '{args}' [--thread T]` calls one with no agent turn |
| Anything else | `rpc <method> '{params}'`: all ~167 app-server methods. List them with `codex app-server generate-json-schema --experimental --out <dir>` |
| Check after a ChatGPT update | `python3 $B doctor [--verbose]`: read-only. Every app-server method and notification this script relies on (read from its source), checked against the installed codex's schema; prints `{ok, missing, codex_version, daemon}` and exits 1 when one moved. Pings the daemon, never starts it or a turn. Tests: `python3 -m unittest discover -s tests` |

Stream lines: `THREAD`, `SENT`, `QUEUED`, `WAITING`, `STARTED`, `AGENT:` (agent text), `CODEX:` (agent called `send_to_claude`), `DONE <time>: <final answer>`, `FAILED`, `TIMEOUT` (still running; `watch` again). Exit codes: 0 done, 4 timeout, 5 failed. A watch fails on its own when no process has held the thread for 30 s, so a crashed worker or closed app never leaves you waiting. Monitor notifications cut long lines; for a long answer, run `read <thread> --turns 1` after `DONE`.

Every `send_to_claude` call and every `to-claude` message goes to the inbox. One Claude session runs the relay under Monitor and forwards each `RELAY` line to the addressed session with SendMessage. The relay saves its position, so messages written while it was down arrive when it is re-armed.

```bash
python3 $B relay --minutes 30
```

## From Codex

A Codex agent uses the same script through its shell.

| Do | Command |
|---|---|
| See Claude sessions | `python3 $B claude-list` (status, name, `local_` id) |
| Message one | `python3 $B to-claude --to "<name or local_id>" "message" --thread <your thread id>` |
| Read one | `python3 $B claude-read <name or local_id> [--last N]` |
| Start a Claude session | `python3 $B claude-start "prompt" --cwd <dir>`. It starts a background Claude Code session (`claude --bg`); continue it with `claude --bg --resume <id> "msg"`, stop it with `claude stop <id>` |
| Reply to the Claude that dispatched you | call the `send_to_claude` tool when you have it; otherwise `to-claude` |

A message to Claude is delivered when the relay session forwards it. `to-claude` fails with the list of live sessions when the name matches none.

## Limits

- Writing to a Claude desktop session goes through the relay. The bridge never writes to a session's private socket, so a Codex agent never poses as a Claude session, and every message arrives labeled with its source thread.
- Background Claude sessions (`claude-start`) show in `claude agents`, not in the Claude app's sidebar.
- New threads show in the ChatGPT app's sidebar on its one-minute refresh. Use the `codex://threads/<id>` link from the `THREAD` line.
- App-held threads without the app route take queued messages but not steer or stop. An app update or relaunch drops the port; run `app-connect` again.
- Every Claude session started from Codex uses your Claude plan.
- `codex exec` from a script hangs without `stdin=subprocess.DEVNULL`; pass it with a timeout.

## Maintenance

Run `python3 $B doctor` after every ChatGPT update: a new bundled codex can rename or drop a method the bridge calls, and `missing` names each one. Tests: `python3 -m unittest discover -s tests`.
