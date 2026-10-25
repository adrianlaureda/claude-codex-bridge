# Claude Codex Bridge

We have this command line interface called Bridge. Claude Code can start the bridge and directly communicate to the Codex app server and start a new thread for our Astra. It can also pick different model. It can use Astra, it can use Luna or any type of model. Then Claude Code gonna be watching the result and the progress on the task, and once Codex is done, it has this command send to Claude. It notifies Claude Code about the progress, and can message it if something goes wrong while Codex is working on our task.

You can also manage it the other way. Codex can also orchestrate your Claude Code sessions. There is a way for Codex to list your active Claude Code sessions. You can message directly specific session, or can also start a new sessions with the Claude Code. So it works back and forth.

Start using both orchestrators together.

El comando `tidy` archiva solo hilos registrados, inactivos y no protegidos. Su umbral por defecto es de 8 horas; `tidy --hours N` lo sobrescribe y `CLAUDE_CODEX_BRIDGE_TIDY_HOURS` configura el valor usado por `tidy`, `start` y el barrido automático. El daemon (`serve`) barre al arrancar y cada hora, en segundo plano y bajo `tidy.lock`; no archiva hilos con `keep`, turnos en curso, colas pendientes ni hilos abiertos en la aplicación. `CLAUDE_CODEX_BRIDGE_AUTOTIDY=0` desactiva el barrido automático.

La decisión de archivar se serializa con todos los RPC del mismo hilo, incluidos turnos y colas enviados por `rpc`; las peticiones de otros hilos siguen atendidas. `keep` avisa si el archivado ya se completó. Los errores del barrido quedan registrados y no cancelan la siguiente iteración. `start` solo lanza un barrido separado como respaldo si el daemon no responde a `ping`. La nueva operación de archivado requiere cargar el daemon actualizado mediante un reinicio seguro autorizado; con un daemon antiguo, `tidy` devuelve error sin recurrir a un archivado sin lock.

El daemon posee la validación del registro y la escritura de `archived_at` durante todo el archivado (`threads.lock` → lock por hilo). El cliente de `tidy` no mantiene ese lock durante la llamada; el deadline de la petición impide iniciar un archivado si el plazo vence antes del envío. Las reaperturas confirmadas limpian `archived_at`, de modo que `keep` vuelve a funcionar sobre el hilo reabierto.

## Señal de cierre para orquestadores

`finished [--unread] [--owner <sessionId>] [--json]` informa el estado del último turno real de cada hilo registrado. El estado es `running`, `done_unread`, `done_read`, `failed` o `waiting_input`, e incluye `last_changed_at` en UTC, que solo cambia ante una transición de estado o turno. Los turnos activos y los envíos en cola no se anuncian como terminados. Cada envío en cola se registra antes del RPC con `clientUserMessageId` y solo se retira al encontrar ese mismo `clientId` en el `UserMessage` del turno; el texto y la fecha no se usan para inferir consumo. Los registros antiguos sin ese identificador permanecen como `running` hasta que se eliminen explícitamente. Para marcar que se necesita respuesta, `send_to_claude` debe incluir `wait_for_reply: true`; para avances y resultado final debe incluir `false`. En hilos antiguos, un turno completado sin esa bandera sigue contando como final; si el rollout está truncado y no permite comprobar el turno completo, el resultado se conserva como `running`.

Para sustituir el sondeo repetido desde un orquestador, ejecuta una sola espera para todos sus hilos:

```bash
python3 $B finished --watch --owner <sessionId> --json
```

El proceso termina cuando detecta el primer cambio y emite la fila del hilo afectado. Omite `--owner` para vigilar todos los hilos registrados. `status <thread> --json` da la misma señal para un hilo individual.

Un timeout de respuesta después de enviar `thread/archive` no cancela el archivado: queda `archive_uncertain` en el registro. `keep` y `tidy` concilian leyendo el estado real, sin reenviar el archive. Mientras la respuesta original siga pendiente o la lectura no confirme el estado, `keep` falla sin aplicar la protección. Si está archivado, registra `archived_at` y avisa; si no, limpia la incertidumbre y permite `keep`.

La conciliación consulta también `archived_sessions`: un rollout allí impide aceptar un `archived=False` obsoleto del app-server. Cualquier evidencia de archivado registra `archived_at` y bloquea `keep`; una discrepancia conserva `archive_uncertain`. Solo se permite `keep` cuando lectura y disco coinciden en no archivado; si no puede consultarse el disco, la marca se conserva.

Cuando Codex inicia Claude, `claude-start` usa `--bg --name` y Remote Control por defecto. Usa nombres neutros, no guarda ni muestra el prompt en el registro, resuelve el identificador de sesión, intenta obtener la URL de `claude.ai`, registra el arranque en `~/.claude-codex-bridge/claude-started.jsonl` y deja en el hilo una línea `CLAUDE-STARTED` reutilizable. `claude-started --last N` muestra esos arranques cruzados con `claude agents --json --all`. Usa `--no-remote-control` para omitir Remote Control y `--notify` para una notificación opcional de macOS.

```bash
git clone https://github.com/ArtemXTech/claude-codex-bridge ~/.claude/skills/claude-codex-bridge
```

MIT
