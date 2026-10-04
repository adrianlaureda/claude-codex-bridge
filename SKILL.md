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

## Desde Claude Code

**No ejecutes `start`, `send` ni `watch` en un Bash en primer plano.** Usa Monitor o `--no-watch` y después `watch` bajo Monitor. Un comando en primer plano bloquea el chat hasta que termina.

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
| Buscar y leer | `list [--search T] [--archived]`, `read <thread> [--turns N]`, `status <thread>` (escritor, último turno, cola) |
| Gestionar | `rename`, `fork`, `archive`, `unarchive`, `sections`, `move <thread> <section>`, `settings <thread> --model M --effort E`, `goal <thread> ["objective"] [--clear]`, `queue <thread> [--delete ID]` |
| Herramientas MCP de Codex | `mcp [server]` lista las herramientas; `mcp-call <server> <tool> '{args}' [--thread T]` llama una sin turno de agente |
| Cualquier otro método | `rpc <method> '{params}'`: todos los métodos del app-server |
| Comprobar una actualización | `/usr/bin/python3 $B doctor [--verbose]`: read-only; informa `{ok, missing, codex_version, daemon}` y no inicia daemon ni turno |

Desde `start`, las respuestas vuelven a esta sesión (`--from` toma por defecto su identificador). Los nuevos hilos aparecen en la barra lateral de ChatGPT en su refresco de un minuto. Usa el enlace `codex://threads/<id>` de la línea `THREAD`.

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
| Iniciar una sesión de Claude | `/usr/bin/python3 $B claude-start "prompt" --cwd <dir>`; continuar con `claude --bg --resume <id> "msg"` y detener con `claude stop <id>` |
| Responder a quien despachó la tarea | Usa la herramienta `send_to_claude` si está disponible; si no, `to-claude` |

## Límites

- Escribir en una sesión de Claude pasa por el relay. El bridge nunca escribe en su socket privado; un agente de Codex no puede hacerse pasar por una sesión de Claude y cada mensaje identifica su hilo de origen.
- Las sesiones de Claude en segundo plano (`claude-start`) aparecen en `claude agents`, no en la barra lateral de la aplicación.
- Los hilos abiertos por la aplicación aceptan mensajes en cola sin la ruta de aplicación, pero no `steer` ni `stop`. Si la aplicación pierde el puerto tras una actualización o relanzamiento, vuelve a ejecutar `app-connect`.
- Cada sesión de Claude iniciada desde Codex usa el plan de Claude correspondiente.
- `codex exec` desde un script puede quedarse bloqueado sin `stdin=subprocess.DEVNULL`; pásalo con timeout.

## Mantenimiento

Ejecuta `/usr/bin/python3 $B doctor` después de cada actualización de ChatGPT: una nueva versión del Codex incluido puede renombrar o eliminar un método del bridge; `missing` enumera cada cambio. El doctor es read-only y nunca inicia el daemon ni un turno. Pruebas: `/usr/bin/python3 -m unittest discover -s tests`.
