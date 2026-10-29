# e30 — evidencia sintética

Estado: **pendiente de revisión y aceptación por Claude**. Sin commit ni integración.

- Base: `ac2605ae7e3756b2ce41709d6af1adc12ed4e8bf`, rama `dia/astra-e30-lote5`.
- `git ls-remote fork refs/heads/main` confirmó esa base el 29-oct-2026.
- PR abiertas del fork: solo #8 (`dia/astra-b32-20261029`, doctor), fuera del alcance.
- Archivos: `scripts/live/app.mjs`, `tests/astra-e30-app.test.mjs` y este documento.

## Reproducción y cambio

Antes del cambio, `node --test tests/astra-e30-app.test.mjs`: **0/3 pasan, 3 fallan**.
El import intentó fetch; WS sin abrir y WS cerrado con evaluate pendiente agotaron
el watchdog sintético de 1,5 s. No hubo conexiones reales.

`runApp(argv, deps)` devuelve el valor; `main(argv, deps)` escribe el mismo formato
stdout de eval/call y devuelve 0/1/2. El import no ejecuta la CLI. Fetch, WebSocket,
reloj y salidas son inyectables. Un presupuesto monotónico de 30 s cubre fetch,
JSON, apertura y respuesta. Los errores vacían pending, abortan fetch y cierran WS.

`call` envía una sola RPC. La respuesta se espera mediante Runtime.evaluate con
awaitPromise, en lugar del sondeo anterior; el plazo externo de Node evita depender
del temporizador de una página en segundo plano. El listener acepta solo el id
activo, se elimina al responder y descarta respuestas tardías. Una pérdida de
confirmación devuelve error con ese id y «no reenviar». Las respuestas RPC con
error conservan su JSON y código CLI 0, como antes; errores de transporte usan 1;
uso incorrecto o ausencia de página usan 2.

## Pruebas finales

- `node --test tests/astra-e30-app.test.mjs`: **52 pasan, 0 fallan, 0 canceladas, 0 omitidas**, exit 0.
- `node --check scripts/live/app.mjs`: exit 0.
- `git diff --check`: exit 0, sin errores.
- Regresión vecina inspeccionada: `test_start_policy.py`, **24 pasan, 0 fallan**, `OK`, exit 0.
  Se ejecutó así, sustituyendo HOME antes del import y bloqueando red/procesos reales:

```sh
TMPDIR=/private/tmp PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.14 - <<'PY'
import os, socket, subprocess, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
with tempfile.TemporaryDirectory(prefix='e30-regresion-') as synthetic_home:
    with patch.object(Path, 'home', return_value=Path(synthetic_home)), \
         patch.dict(os.environ, {'CLAUDE_CODEX_BRIDGE_HOME': synthetic_home, 'CODEX_BIN': '/no-codex-sintetico'}), \
         patch.object(socket, 'socket', side_effect=AssertionError('Red real prohibida')), \
         patch.object(subprocess, 'Popen', side_effect=AssertionError('Proceso real prohibido')):
        suite = unittest.defaultTestLoader.discover('tests', pattern='test_start_policy.py')
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        raise SystemExit(not result.wasSuccessful())
PY
```

La suite focal ejecuta expresiones reales del helper en `node:vm`, con página,
preload, reloj, fetch y WS sintéticos. También ejecuta la entrada CLI en procesos
con transporte sustituido antes del import, verificando exit 0/1/2 y cierre aunque
el peer no confirme close. Cubre lista vacía/malformada, HTTP/fetch/JSON fallidos,
plazo en cada fase, desconexión, send fallido, CDP roto/error/id ajeno,
exceptionDetails, RPC tardía, concurrencia, stdout y vaciado explícito de pending.

## Prueba de sensibilidad

Dos mutaciones temporales del helper, restauradas con `finally` y comparación del
contenido original, ejecutaron el mismo comando focal:

- Quitar `|| d.message?.id !== id`: **49 pasan, 3 fallan**, exit 1
  (respuestas RPC result/error y llamadas simultáneas).
- Quitar `pending.clear()`: **51 pasan, 1 falla**, exit 1
  (mapa pending vacío tras timeout).
- Tras restaurar: **52 pasan, 0 fallan**, exit 0.

## Límites y siguiente gate

No se probó ChatGPT, CDP real, el daemon ni datos reales. El cierre se solicita
sin esperar un handshake remoto fuera del plazo; la CLI drena salida y termina.
El API importable libera sus handlers y timers, pero no puede garantizar que un
WebSocket nativo termine su handshake dentro del plazo. La limpieza de página
por CDP es best-effort y no se confirma: si CDP desaparece, queda su temporizador
de respaldo, que una página suspendida puede retrasar. Estas limitaciones no se
presentan como servicio real verificado ni autorizan repetir una RPC incierta.

Pendiente: revisión Sol (máximo dos rondas) coordinada por Claude; aceptación,
commit y PR por Claude, fusión por Adri. Cualquier humo real requiere autorización
aparte y no forma parte de esta entrega.
