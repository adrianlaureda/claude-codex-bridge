# b32 · Auditoría aplicada del doctor

Estado de entrega: **pendiente de revisión de Sol y aceptación de Claude**.
Trabajo local preparado; sin commit, push, PR, integración ni ejecución en producción.

## Base y alcance

- Rama: `dia/astra-b32-20261029`.
- SHA base: `ac2605ae7e3756b2ce41709d6af1adc12ed4e8bf`.
- El 29-oct-2026, `git rev-parse HEAD`, `git rev-parse fork/main` y
  `git ls-remote fork refs/heads/main` coincidieron. `git diff --stat fork/main...HEAD`
  no mostró delta inicial; el árbol estaba limpio.
- `gh pr list --repo adrianlaureda/claude-codex-bridge --state all --limit 15
  --json number,title,state,headRefName,baseRefName,mergeCommit` mostró siete PR,
  todas fusionadas. La base incluye [#7](https://github.com/adrianlaureda/claude-codex-bridge/pull/7),
  [#6](https://github.com/adrianlaureda/claude-codex-bridge/pull/6) y
  [#5](https://github.com/adrianlaureda/claude-codex-bridge/pull/5).
  No se usó `origin/main` ni el estado antiguo de una issue para decidir trabajo.
- Únicos archivos modificados/creados: `scripts/doctor.py`,
  `tests/test_astra_b32_doctor.py` y este informe.
- Se leyó código de `bridge.py` y pruebas vecinas para comprobar el contrato.
  No se modificaron bridge, daemon, leases, status, configuración ni dependencias.

## Defectos reproducidos y corrección

| Caso | Antes | Ahora |
| --- | --- | --- |
| Fuente vacía, solo comentarios o sin callbacks reconocibles | Podía devolver `ok=true` por añadir `initialize` artificialmente | Diagnóstico incompleto, código 2 |
| AST inválido o archivo de fuente ausente | Excepción fuera del informe JSON | JSON con `ok=false`, `missing=null`, `error` y código 2 |
| Rama de schema vacía junto a otra válida | Se ignoraba la rama; podía dar verde | Se valida cada rama, código 2 si no se puede interpretar |
| Schema sin `oneOf`, enum vacío, tipo incorrecto o categoría ausente | Se confundía falta de evidencia con ausencia de métodos; `client_notifications` podía asumirse presente | Código 2; no hay categoría implícita |
| Llamada con `method=`, nombre sin `/`, tabla con parámetros variables y wrapper sencillo | Dependencias omitidas | Se incluyen y comparan con el schema |
| Callback asíncrono o pertenencia a colección literal | Dependencias omitidas | Se extraen métodos de ambos tipos de callback |
| Método de callback sin `/`, por ejemplo `error` | No se detectaba | Se identifica por comparación con el campo `method` |
| Comparación de otro campo con texto que contiene `/` | Se trataba como dependencia RPC | Solo se analizan comparaciones vinculadas a `method` |
| `bridge/*` en `WRITES` | Aviso falso de método externo desaparecido | Se conserva el enrutado en el inventario y se excluye del aviso externo |
| `--version` no cero con texto en stdout | El texto de error figuraba como versión | `codex_version=null` |

La primera corrección amplió demasiado el reconocimiento de tuplas y trataba
`("event/one", "event/two")` como petición saliente. Se reprodujo antes del cierre
y se restringió a pares con diccionario de parámetros o valores de una tabla.
La prueba exige que las notificaciones no aparezcan también como peticiones.

## Invariantes conservadas

1. La fuente se analiza mediante AST y **no se importa ni ejecuta**. Los wrappers
   sencillos se siguen por reenvío del argumento de método, sin evaluar Python.
2. Código 0: las dependencias reconocidas existen. Código 1: faltan nombres en un
   schema interpretable. Código 2: generación, lectura o análisis incompletos.
3. Todo fallo controlado de `run` produce JSON con `ok=false`, `missing=null` y
   `error`; nunca se aprovecha un schema escrito parcialmente por un proceso fallido.
4. La generación solo usa `app-server generate-json-schema --experimental --out`;
   se conservan los límites originales de tiempo. `--version` es informativo.
5. Se mantienen las claves del informe, `--bridge`, `--codex`, `--verbose`, los
   prefijos de `missing`, `initialize`/`initialized` obligatorios y los avisos de
   rutas externas huérfanas. Estas rutas siguen siendo avisos, no errores.
6. El ping conserva su carácter informativo: un daemon detenido no convierte la
   compatibilidad del protocolo en fallo. No se inicia daemon, app-server conversacional
   ni turno alguno para comprobarla.

## Evidencia de pruebas

Todas las órdenes se ejecutaron desde este worktree con Python 3.14 ya instalado,
`PYTHONDONTWRITEBYTECODE=1` y `TMPDIR=/private/tmp`. No se instalaron paquetes.

### Baseline separado

```sh
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/private/tmp /opt/homebrew/bin/python3.14 -m pytest -q -p no:cacheprovider tests/test_doctor.py -k 'not live_schema'
```

Antes de cambiar implementación: **4 passed, 1 deselected in 0.04s** (salida 0).
La prueba `live_schema` se excluyó expresamente: usa el binario instalado y el ping real.

### Rojo y corrección

```sh
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/private/tmp /opt/homebrew/bin/python3.14 -m pytest -q -p no:cacheprovider tests/test_astra_b32_doctor.py
```

Primera reproducción contra implementación original: **28 failed, 12 passed in 8.16s**
(salida 1). Los casos que ya pasaban incluyen métodos desaparecidos en el formato
original, generación fallida, JSON ilegible y timeout simulado.

```sh
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/private/tmp /opt/homebrew/bin/python3.14 -m pytest -q -p no:cacheprovider tests/test_astra_b32_doctor.py -k 'membership_does_not or server_method_without or wrappers_chain or version_failure'
```

Revisión de frontera antes de su corrección: **3 failed, 1 passed, 40 deselected in 0.03s**
(salida 1). Incluye la regresión de tuplas introducida y corregida en esta tarea.

Última frontera reproducida:

```sh
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/private/tmp /opt/homebrew/bin/python3.14 -m pytest -q -p no:cacheprovider tests/test_astra_b32_doctor.py -k unrecognized_notification_substring
```

**3 failed, 49 deselected in 0.03s** (salida 1). Una comparación parcial en
`note` se mezclaba con peticiones del servidor. Ahora se rechaza con código 2;
se conserva el patrón positivo de aprobación de `handle_tool_call`.

### Verde final

```sh
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/private/tmp /opt/homebrew/bin/python3.14 -m pytest -q -p no:cacheprovider tests/test_astra_b32_doctor.py
```

**52 passed in 8.17s** (salida 0).

```sh
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/private/tmp /opt/homebrew/bin/python3.14 -m pytest -q -p no:cacheprovider tests/test_doctor.py -k 'not live_schema'
```

**4 passed, 1 deselected in 0.07s** (salida 0).

```sh
git diff --check
```

Salida 0, sin diagnósticos.

La matriz cubre AST válido/inválido, fuente vacía/ausente/recortada, llamadas
posicionales y por nombre, wrappers encadenados, tablas, rutas internas,
notificaciones y peticiones del servidor, pérdida de handshake, schemas cambiados,
truncados o parcialmente reconocidos, binario ausente, salida no JSON y códigos 0/1/2.

El CLI se copia a un árbol temporal con un módulo `bridge` que únicamente ofrece
`ping() -> None` y un ejecutable sintético. El proceso hijo bloquea red, lecturas
fuera del árbol/biblioteca Python y subprocesos ajenos al doble. El doble valida
argumentos y destino de escritura. No se hereda el entorno personal del usuario.
El timeout se prueba tanto con `TimeoutExpired` simulado como con un ejecutable
real que escribe archivos y se duerme: solo el lanzador de prueba reduce el plazo
a 0,1 segundos. Los datos completos escritos antes del timeout tampoco dan verde.

## Límites y siguiente paso

- Es un análisis estático **de nombres**, no una prueba de parámetros, resultados,
  semántica del protocolo o conectividad del daemon. No acredita runtime ni producción.
- El lector admite el formato existente `oneOf[].properties.method.enum`. Formatos
  alternativos como `$ref`, `anyOf` o `const` quedan en código 2 y requieren adaptar
  el lector con evidencia; no se interpretan parcialmente como éxito.
- Se exige evidencia de peticiones externas y ambos canales de callbacks reconocidos.
  Esto detecta fuentes vacías o recortes que eliminan esas categorías, pero no prueba
  integridad de cualquier fichero: un recorte sintácticamente válido que conserve
  las categorías puede omitir otros comandos sin que exista un manifiesto con que
  contrastarlo.
- Los nombres construidos dinámicamente (`rpc` libre, alias arbitrarios, tablas
  importadas o funciones renombradas), los scopes complejos y el flujo de datos
  general no están resueltos. Las tablas literales conservan la aproximación del
  extractor original; no es un analizador completo de Python.
- Las comparaciones por substring en notificaciones, negativas o vacías no se
  interpretan como nombres exactos: producen código 2.
- Se mantuvo el patrón de aprobación por substring en `handle_tool_call`; no es una
  garantía de que cada futura variante de aprobación tenga la semántica esperada.
- No se ejecutó `live_schema` ni suites que prueban daemon/turnos: no hubo cambios en
  esas áreas. La regresión ampliada pertinente es la suite previa del doctor.
- No hay dependencias nuevas ni cambios que desplegar desde esta entrega. Sol debe
  revisar el diff por el circuito de Claude (máximo dos rondas); Claude decide
  aceptación/commit/PR y Adri la fusión. Cualquier comprobación con el binario real
  queda fuera de esta evidencia y de la autorización de esta tanda.
