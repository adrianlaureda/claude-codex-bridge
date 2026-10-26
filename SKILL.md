---
name: claude-codex-bridge
description: Puente bidireccional entre Claude Code y Codex (los agentes de la aplicación de escritorio ChatGPT). Desde Claude permite iniciar tareas de Codex, enviar mensajes a hilos existentes, seguir su progreso, detenerlos, renombrarlos, archivarlos, cambiar modelo y esfuerzo, fijar objetivos, llamar herramientas MCP y usar métodos del app-server. Desde Codex permite listar sesiones de Claude Code, enviarles mensajes, iniciar sesiones en segundo plano y leer sus transcripciones. Úsalo cuando se pida enviar algo a ChatGPT o Codex, trabajar con un agente de Codex, usar Codex computer use, escribir en un hilo de Codex, consultar una tarea de Codex o usar el puente Claude-Codex.
---

# Puente Claude ⇄ Codex

Un único script, `scripts/bridge.py`, se usa desde ambos lados. Habla con Codex mediante el app-server incluido en la aplicación ChatGPT y con Claude Code mediante la CLI `claude` más una bandeja de entrada de relay.

La fuente canónica de esta skill es `~/.dotfiles/ai/skills/claude-codex-bridge/SKILL.md`.

En macOS se usa `/usr/bin/python3` por ruta absoluta para evitar el shim de `python3` del entorno de Claude. El bridge utiliza la biblioteca estándar; el daemon hereda ese intérprete mediante `sys.executable`.

```bash
B=$HOME/Proyectos/Claude/apps/claude-codex-bridge/scripts/bridge.py
```

Un daemon pequeño se inicia al primer uso (`/usr/bin/python3 $B up`, `ping`, `down`). `down` se niega mientras haya un turno en curso (`--force` lo sobrescribe). El socket es `~/.claude-codex-bridge/bridge.sock`, con permisos 0600.

Mientras está vivo, el daemon ejecuta `tidy` en segundo plano al arrancar y después cada hora, usando el mismo `tidy.lock` que el comando manual. El umbral predeterminado es de 8 horas de inactividad. Se puede cambiar con `CLAUDE_CODEX_BRIDGE_TIDY_HOURS` o con `tidy --hours N`. Solo se consideran hilos registrados en `threads.json`; `keep`, turnos activos, colas pendientes y hilos abiertos en la aplicación quedan protegidos. `CLAUDE_CODEX_BRIDGE_AUTOTIDY=0` desactiva el barrido automático. Con un daemon que responde a `ping`, `start` no lanza otra limpieza; el proceso separado queda como respaldo si no responde.

La operación interna `bridge/tidy-archive` comprueba actividad y archiva bajo el mismo lock por hilo que todos los RPC del daemon, incluidos `turn/start` y `thread/queue/add` enviados mediante `rpc`. Los RPC de ese hilo esperan al terminar la decisión; `ping` y los otros hilos siguen atendidos. El worker mantiene su writer durante el ciclo para protegerlo del reaper y de la aplicación. `keep`/`unkeep` comparten `threads.lock` con el ciclo de registro y avisan con error si el hilo ya quedó archivado. Los fallos se registran en `tidy_failed` y `tidy_daemon_failed`; la siguiente iteración sigue programada. Tras actualizar, el daemon necesita un reinicio seguro autorizado para cargar esta operación; un daemon antiguo la rechaza y `tidy` falla sin archivar por una vía alternativa.

El daemon valida registro y `keep`, archiva y anota `archived_at` bajo el orden `threads.lock` → lock por hilo; el cliente de `tidy` no retiene el lock del registro durante el RPC. Las peticiones llevan un deadline monotónico, que limita la espera de locks y se comprueba antes del archivado. Tras una reapertura confirmada con `send`, `watch`, `unarchive` o `rpc thread/unarchive`, se elimina `archived_at` bajo lock; una reapertura fallida conserva la marca.

Si `thread/archive` ya se envió y vence la espera de respuesta, se registra `archive_uncertain` bajo lock, sin reenviar la operación. `keep`/`unkeep` y el siguiente barrido concilian mediante `thread/read`: si está archivado, anotan `archived_at` y `keep` devuelve el aviso de «ya archivado»; si no, eliminan la marca y `keep` puede aplicarse. Mientras la respuesta original siga pendiente, o si la lectura falla o no confirma el estado, se conserva la marca y `keep` devuelve error, nunca OK. El worker se conserva hasta esta conciliación para no perder la respuesta tardía. El barrido que concilia no vuelve a archivar ese hilo; `--dry-run` no cambia el registro.

La conciliación contrasta `thread/read` con los rollouts de `archived_sessions`: el disco puede confirmar un archivado que el app-server todavía no refleja. Si cualquiera indica archivado, se anota `archived_at` y `keep` falla. Si discrepan, se conserva además `archive_uncertain`; solo se elimina al coincidir ambas fuentes. `keep` solo puede aplicarse si ambas indican no archivado. Un error al consultar el disco conserva la marca y bloquea la protección, sin reenviar `archive`.

## Desde Claude Code

**En la sesión principal, no ejecutes `start`, `send` ni `watch` en un Bash en primer plano.** Usa Monitor o `--no-watch` y después `watch` bajo Monitor. Un comando en primer plano bloquea el chat hasta que termina.

**Un subagente (Agent tool) hace lo contrario: `watch` en primer plano, nunca Monitor.** El aviso de un Monitor o de un comando en segundo plano no despierta a un subagente idle: el 18-oct uno terminó a las 00:44 y no se leyó hasta las 03:21. Usa `start ... --no-watch` y después `watch <thread> --timeout 540` en primer plano, repitiéndolo mientras salga `TIMEOUT` (el Bash en primer plano corta a los 10 min). Pasa siempre `--agent <tu nombre>`: con él `finished` y el vigilante nocturno saben a quién despertar.

**Codex no toma el control de la pantalla.** Cada tarea `start` lleva instrucciones de desarrollador (`INSTRUCTIONS` en `scripts/bridge.py`): no eleva ni enfoca ventanas, no mueve el puntero, usa solo navegador headless, un navegador por tarea y páginas como pestañas. El uso de computadora en primer plano exige `FOREGROUND AUTHORIZED` en el prompt con la aplicación y acción exactas.

`start` exige `--role` y `--cwd`; el directorio debe existir. El modelo y el esfuerzo los resuelve el router canónico. Por defecto el sandbox es `workspace-write` con red habilitada; `--no-network` desactiva su red. `read-only` limita la escritura y `--full-access` solo se permite cuando se solicita de forma explícita y es incompatible con `--no-network`. No hay fallback de modelo. Las prohibiciones de autorizaciones de Adri se aplican también a las tareas de Codex: sin autorización explícita en el texto no hay commit, push, merge, deploy, borrados, instalaciones, cambios de credenciales, mensajes a familias o alumnado ni escrituras en Abalar, XADE o Moodle; los datos de alumnado permanecen dentro del circuito aprobado y fuera de logs y artefactos publicados.

**Ruta de la aplicación.** Cuando la aplicación ChatGPT tiene abierto un hilo, ningún proceso externo puede escribirlo directamente. `/usr/bin/python3 $B app-connect` relanza la aplicación en segundo plano con un puerto de depuración en 127.0.0.1; desde entonces `send`, `steer`, `stop`, `rename`, `archive`, `unarchive` y `fork` pasan por la propia aplicación (`scripts/live/app.mjs`). Sin esta conexión, `send` usa la cola de la aplicación. `app-connect` se niega si hubo un turno de Codex en el último minuto (`--force` lo sobrescribe), porque cerrar la aplicación termina sus turnos.

| Acción | Comando |
|---|---|
| Nueva tarea de agente | `/usr/bin/python3 $B start "prompt" --role codex-code-worker --cwd <dir> --title "Name" --section Personal [--sandbox read-only] [--no-network]` |
| Enviar a cualquier hilo | `/usr/bin/python3 $B send <thread> "prompt"`: turno nuevo si está libre; en cola si lo tiene abierta la aplicación |
| Seguir un turno | `/usr/bin/python3 $B watch <thread>`: sigue el turno actual, imprime `WAITING` y continúa con el siguiente si hay un mensaje en cola; si no, `IDLE` con la última respuesta. `--from-start` repite el último turno |
| Añadir texto a un turno / detenerlo | `steer <thread> "text"` / `stop <thread>`: para hilos gestionados por este bridge y, mediante la aplicación, para hilos abiertos allí |
| Entrega inmediata a hilos de la aplicación | `/usr/bin/python3 $B app-connect`: una vez por lanzamiento de ChatGPT; después los verbos para hilos abiertos pasan por la aplicación |
| Buscar y leer | `list [--search T] [--archived]`, `read <thread> [--turns N]`, `status <thread> [--json]` (escritor, turno, cola y estado) |
| Estado de hilos | `/usr/bin/python3 $B finished [--unread] [--owner <sessionId>] [--json]`: estado del último turno real de cada hilo registrado. No usa el daemon |
| Gestionar | `rename`, `fork`, `archive`, `unarchive`, `sections`, `move <thread> <section>`, `settings <thread> --model M --effort E`, `goal <thread> ["objective"] [--clear]`, `queue <thread> [--delete ID]` |
| Herramientas MCP de Codex | `mcp [server]` lista las herramientas; `mcp-call <server> <tool> '{args}' [--thread T]` llama una sin turno de agente |
| Cualquier otro método | `rpc <method> '{params}'`: todos los métodos del app-server |
| Comprobar una actualización | `/usr/bin/python3 $B doctor [--verbose]`: read-only; informa `{ok, missing, codex_version, daemon}` y no inicia daemon ni turno |

### Lease y relevo de supervisión

`version` consulta al daemon activo sin abrir un turno de modelo y devuelve su versión, hash de código, PID e inicio. `ping` incluye el mismo recibo.

Los comandos `send`, `steer` y `stop` aceptan `--owner <sessionId> --generation <n>`; por defecto toman la sesión de `CLAUDE_CODE_HOST_SESSION_ID` o `CLAUDE_CODE_SESSION_ID` y generación `1`. Para consultar la lease: `owner <thread>`. El propietario actual ofrece un relevo con `handoff-offer <thread> <nuevo-owner> --owner <actual> --generation <n>`. El entrante acepta el identificador devuelto con `handoff-accept <thread> <handoffId> --owner <nuevo-owner> --generation <n>`. La aceptación incrementa la generación; desde entonces el propietario anterior no puede enviar, dirigir ni detener turnos. Comprueba `owner` después de un timeout antes de repetir el relevo.

Las mutaciones aceptan `--request-id <id>` para deduplicar reintentos. Si hay timeout, consulta `request <id>` antes de repetir: un recibo `complete` devuelve el resultado mínimo guardado y `started` o `uncertain` exige reconciliar el turno o la cola. No se guardan prompts ni respuestas en el journal.

Desde `start`, las respuestas vuelven a esta sesión (`--from` toma por defecto su identificador: `CLAUDE_CODE_HOST_SESSION_ID` o, si falta, `CLAUDE_CODE_SESSION_ID`; `--agent` o `CLAUDE_CODEX_BRIDGE_AGENT` guarda el subagente que espera). `watch` y `read` anotan en `threads.json` cuándo se leyó el final del turno. Los nuevos hilos aparecen en la barra lateral de ChatGPT en su refresco de un minuto. Usa el enlace `codex://threads/<id>` de la línea `THREAD`.

El estado es único por hilo: `running`, `done_unread`, `done_read`, `failed` o `waiting_input`. Incluye `last_changed_at` (UTC, ISO 8601), que solo cambia al cambiar el estado o el turno, y el id del último turno. Un turno activo o un envío registrado en cola mantiene el hilo como `running`; así un final anterior no despierta al vigilante. El bridge registra `clientUserMessageId` antes de `thread/queue/add` y retira el envío solo al encontrar el mismo `clientId` en el `UserMessage` del turno. Nunca infieras consumo por texto o fecha. Los registros antiguos de cola sin ese ID se conservan en `running` hasta que se eliminen explícitamente. En `send_to_claude`, usa `wait_for_reply: true` solo si el turno debe esperar respuesta; usa `false` en avances y resultados finales. Por compatibilidad, un turno completado en un hilo antiguo sin esa bandera cuenta como final. Si falta evidencia fiable porque el rollout está truncado, el estado queda `running`.

Para esperar a que cambie cualquiera de tus hilos, deja un único proceso esperando:

```bash
/usr/bin/python3 $B finished --watch --owner <sessionId> --json
```

El comando compara los estados iniciales, espera internamente y termina al detectar el primer hilo nuevo o cambiado, emitiendo su fila JSON. Usa `finished --watch --json` para vigilar todos los hilos registrados. Esto sustituye el bucle de sondeo externo. `status <thread> --json` devuelve el mismo estado y hora para un hilo concreto.

Líneas del stream: `THREAD`, `SENT`, `QUEUED`, `WAITING`, `STARTED`, `AGENT:` (texto del agente), `CODEX:` (el agente llamó a `send_to_claude`), `DONE <time>: <final answer>`, `FAILED`, `TIMEOUT` (sigue ejecutándose; vuelve a usar `watch`). Un `watch` termina por sí solo si ningún proceso ha tenido el hilo durante 30 s. Para respuestas largas, ejecuta `read <thread> --turns 1` después de `DONE`.

Cada llamada a `send_to_claude` y cada mensaje `to-claude` entra en la bandeja de entrada. Una sesión de Claude ejecuta el relay bajo Monitor y reenvía cada línea `RELAY` a la sesión indicada mediante SendMessage. El relay guarda su posición, por lo que los mensajes escritos mientras estaba detenido llegan al rearmarlo.

```bash
/usr/bin/python3 $B relay --minutes 120 --name claude-codex
```

Para trabajo habitual se recomiendan 120 minutos bajo Monitor. Al expirar, rearma el relay con el mismo `--name` para conservar su posición y recibir los mensajes pendientes. Si ya hay un relay escuchando, espera a que termine antes de iniciar otro con ese nombre.

## Desde Codex

Un agente de Codex usa el mismo script desde su shell.

| Acción | Comando |
|---|---|
| Ver sesiones de Claude | `/usr/bin/python3 $B claude-list` (estado, nombre, ids `local_`) |
| Enviar a una sesión | `/usr/bin/python3 $B to-claude --to "<name or local_id>" "message" --thread <your thread id>` |
| Leer una sesión | `/usr/bin/python3 $B claude-read <name or local_id> [--last N]` |
| Iniciar una sesión visible de Claude | `/usr/bin/python3 $B claude-start "prompt" --cwd <dir> [--name N] [--no-remote-control] [--notify]`; queda registrada en `claude-started.jsonl` |
| Ver sesiones iniciadas desde Codex | `/usr/bin/python3 $B claude-started [--last N]` (estado actual, URL y reanudación) |
| Responder a quien despachó la tarea | Usa la herramienta `send_to_claude` si está disponible; si no, `to-claude` |

## Límites

- Escribir en una sesión de Claude pasa por el relay. El bridge nunca escribe en su socket privado; un agente de Codex no puede hacerse pasar por una sesión de Claude y cada mensaje identifica su hilo de origen.
- `claude-start` usa un nombre neutro `codex HH:MM <4 primeros del thread>` si no se indica `--name`; nunca deriva el nombre ni el registro del prompt. Activa Remote Control por defecto y deja una línea `CLAUDE-STARTED` con la URL de `claude.ai` cuando está disponible. `--no-remote-control` lo desactiva.
- Las sesiones iniciadas con `claude-start` aparecen en `claude agents` y quedan visibles para Adri mediante la URL registrada; `claude-started` cruza el registro local con su estado actual.
- Los hilos abiertos por la aplicación aceptan mensajes en cola sin la ruta de aplicación, pero no `steer` ni `stop`. Si la aplicación pierde el puerto tras una actualización o relanzamiento, vuelve a ejecutar `app-connect`.
- Cada sesión de Claude iniciada desde Codex usa el plan de Claude correspondiente.
- `codex exec` desde un script puede quedarse bloqueado sin `stdin=subprocess.DEVNULL`; pásalo con timeout.

## Mantenimiento

Ejecuta `/usr/bin/python3 $B doctor` después de cada actualización de ChatGPT: una nueva versión del Codex incluido puede renombrar o eliminar un método del bridge; `missing` enumera cada cambio. El doctor es read-only y nunca inicia el daemon ni un turno. Pruebas: `/usr/bin/python3 -m unittest discover -s tests`.
