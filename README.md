# Claude Codex Bridge

We have this command line interface called Bridge. Claude Code can start the bridge and directly communicate to the Codex app server and start a new thread for our Astra. It can also pick different model. It can use Astra, it can use Luna or any type of model. Then Claude Code gonna be watching the result and the progress on the task, and once Codex is done, it has this command send to Claude. It notifies Claude Code about the progress, and can message it if something goes wrong while Codex is working on our task.

You can also manage it the other way. Codex can also orchestrate your Claude Code sessions. There is a way for Codex to list your active Claude Code sessions. You can message directly specific session, or can also start a new sessions with the Claude Code. So it works back and forth.

Start using both orchestrators together.

Cuando Codex inicia Claude, `claude-start` usa `--bg --name` y Remote Control por defecto. Usa nombres neutros, no guarda ni muestra el prompt en el registro, resuelve el identificador de sesión, intenta obtener la URL de `claude.ai`, registra el arranque en `~/.claude-codex-bridge/claude-started.jsonl` y deja en el hilo una línea `CLAUDE-STARTED` reutilizable. `claude-started --last N` muestra esos arranques cruzados con `claude agents --json --all`. Usa `--no-remote-control` para omitir Remote Control y `--notify` para una notificación opcional de macOS.

```bash
git clone https://github.com/ArtemXTech/claude-codex-bridge ~/.claude/skills/claude-codex-bridge
```

MIT
