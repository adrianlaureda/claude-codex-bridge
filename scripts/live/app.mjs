// Acceso a la app por CDP local. Importar este módulo no abre conexiones.
//   node app.mjs eval '<js>'                  -> valor (espera promesas)
//   node app.mjs call <method> '<json params>' -> respuesta JSON-RPC completa
import { pathToFileURL } from 'node:url'
import { randomUUID } from 'node:crypto'

const systemClock = {
  now: () => performance.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: timer => clearTimeout(timer),
}

function usage(message = 'Uso: eval <js> | call <method> [json]') {
  return Object.assign(new Error(message), { exitCode: 2 })
}

// Cada llamada registra solo su id; las respuestas ajenas o tardías se descartan.
// El temporizador de página es un respaldo si CDP desaparece; el plazo real vive en Node.
function rpcExpression(id, method, params, waitMs) {
  return `new Promise((resolve, reject) => {
    if (!window.electronBridge?.sendMessageFromView) throw new Error('Falta electronBridge.sendMessageFromView')
    const id = ${JSON.stringify(id)}
    const calls = window.__cbLiveCalls ??= Object.create(null)
    const finish = (error, reply) => {
      clearTimeout(timer)
      window.removeEventListener('message', listener)
      delete calls[id]
      error ? reject(error) : resolve(reply)
    }
    const listener = ev => {
      const d = ev.data
      if (d?.type !== 'mcp-response' || d.message?.id !== id) return
      try { finish(null, JSON.parse(JSON.stringify(d.message))) }
      catch { finish(new Error('Respuesta RPC no serializable')) }
    }
    const timer = setTimeout(() => finish(new Error('Plazo RPC agotado')), ${waitMs})
    calls[id] = () => finish(new Error('Llamada finalizada sin respuesta confirmada'))
    window.addEventListener('message', listener)
    Promise.resolve().then(() => window.electronBridge.sendMessageFromView({
      type: 'mcp-request', hostId: 'local',
      request: { id, method: ${JSON.stringify(method)}, params: ${JSON.stringify(params)} }
    })).catch(error => finish(error))
  })`
}

export async function runApp(argv, {
  fetch = globalThis.fetch,
  WebSocket = globalThis.WebSocket,
  clock = systemClock,
  port = process.env.CODEX_CDP_PORT || 9334,
  timeoutMs = 30000,
} = {}) {
  const [cmd, arg, json] = argv
  if (!['eval', 'call'].includes(cmd) || typeof arg !== 'string' || !arg.trim()
      || argv.length > (cmd === 'eval' ? 2 : 3)) throw usage()
  let params = {}
  if (cmd === 'call' && json !== undefined) {
    try { params = JSON.parse(json) ?? {} }
    catch { throw usage('Parámetros JSON inválidos') }
  }
  if (!Number.isInteger(Number(port)) || Number(port) < 1 || Number(port) > 65535) {
    throw usage('Puerto CDP inválido')
  }
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 2147483647) {
    throw usage('Plazo inválido')
  }
  const deadline = clock.now() + timeoutMs
  const controller = new AbortController()
  const pending = new Map()
  let ws, serial = 0, failure, rejectFailure, rpcId, rpcSent = false
  const failed = new Promise((_, reject) => { rejectFailure = reject })
  // También puede llegar un cierre entre dos awaits; siempre queda observado.
  failed.catch(() => {})
  function stop(error) {
    if (failure) return
    failure = error
    rejectFailure(error)
    for (const entry of pending.values()) entry.reject(error)
    pending.clear()
    controller.abort()
  }
  function check() {
    if (!failure && clock.now() >= deadline) stop(new Error(`Plazo global agotado (${timeoutMs} ms)`))
    if (failure) throw failure
  }
  async function bounded(operation) {
    const result = await Promise.race([operation, failed])
    check()
    return result
  }
  const timer = clock.setTimeout(() => stop(new Error(`Plazo global agotado (${timeoutMs} ms)`)), timeoutMs)
  function send(method, params) {
    check()
    if (ws.readyState !== 1) throw new Error('WebSocket no está abierto')
    return new Promise((resolve, reject) => {
      const id = ++serial
      pending.set(id, { resolve, reject })
      try { ws.send(JSON.stringify({ id, method, params })) }
      catch { stop(new Error('Falló el envío CDP; no se reintentará')) }
    })
  }
  async function evalJs(expression) {
    const r = await bounded(send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }))
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text || 'Excepción en Runtime.evaluate')
    }
    if (!r.result || typeof r.result !== 'object') throw new Error('Respuesta Runtime.evaluate malformada')
    return r.result.value
  }
  try {
    const response = await bounded(fetch(`http://127.0.0.1:${Number(port)}/json/list`, {
      signal: controller.signal, redirect: 'error',
    }))
    if (!response.ok) throw new Error(`Falló el descubrimiento CDP (HTTP ${response.status})`)
    const pages = await bounded(response.json())
    if (!Array.isArray(pages)) throw new Error('Lista de páginas CDP malformada')
    const page = pages.find(p => p?.type === 'page' && typeof p.url === 'string' && p.url.startsWith('app://'))
    if (!page) throw usage('No hay página ChatGPT en el puerto ' + port)
    let address
    try { address = new URL(page.webSocketDebuggerUrl) }
    catch { throw new Error('Dirección WebSocket CDP inválida') }
    if (address.protocol !== 'ws:' || address.hostname !== '127.0.0.1'
        || (address.port || '80') !== String(Number(port)) || address.username || address.password) {
      throw new Error('WebSocket CDP debe usar el mismo puerto local')
    }
    check()
    ws = new WebSocket(address.href)
    await bounded(new Promise(resolve => {
      ws.onopen = resolve
      ws.onerror = () => stop(new Error('Error de WebSocket CDP'))
      ws.onclose = () => stop(new Error('WebSocket CDP desconectado'))
      ws.onmessage = event => {
        let reply
        try { reply = JSON.parse(event.data) }
        catch { stop(new Error('JSON CDP inválido')); return }
        if (!reply || typeof reply !== 'object' || Array.isArray(reply)) {
          stop(new Error('Respuesta CDP malformada')); return
        }
        const entry = pending.get(reply.id)
        if (!entry) return
        pending.delete(reply.id)
        if (reply.error) entry.reject(new Error('Error CDP: ' + JSON.stringify(reply.error)))
        else if (!reply.result || typeof reply.result !== 'object') entry.reject(new Error('Respuesta CDP sin resultado'))
        else entry.resolve(reply.result)
      }
    }))
    if (cmd === 'eval') return await evalJs(arg)
    rpcId = 'cb-' + randomUUID()
    const expression = rpcExpression(rpcId, arg, params, Math.max(1, deadline - clock.now()))
    // Una pérdida de confirmación desde este punto puede ocultar un envío aceptado.
    rpcSent = true
    const reply = await evalJs(expression)
    if (!reply || reply.id !== rpcId || (!Object.hasOwn(reply, 'result') && !Object.hasOwn(reply, 'error'))) {
      throw new Error('Respuesta RPC malformada o de otro id')
    }
    return reply
  } catch (error) {
    if (rpcSent) {
      throw new Error(`Resultado incierto (${rpcId}); no reenviar: ${error.message || error}`)
    }
    throw error
  } finally {
    clock.clearTimeout(timer)
    stop(new Error('Conexión finalizada'))
    if (ws) {
      // Limpieza de página sin otra llamada RPC ni espera fuera del plazo global.
      // Si no llega, su temporizador local elimina el listener y la entrada.
      if (rpcId && ws.readyState === 1) {
        try {
          ws.send(JSON.stringify({ id: ++serial, method: 'Runtime.evaluate', params: {
            expression: `window.__cbLiveCalls?.[${JSON.stringify(rpcId)}]?.()`,
          } }))
        } catch { /* El cierre y el temporizador de página siguen siendo necesarios. */ }
      }
      ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null
      if (ws.readyState < 2) ws.close()
    }
  }
}

export async function main(argv = process.argv.slice(2), deps = {}) {
  const stdout = deps.stdout ?? process.stdout
  const stderr = deps.stderr ?? process.stderr
  try {
    const out = await runApp(argv, deps)
    stdout.write((typeof out === 'string' ? out : JSON.stringify(out)) + '\n')
    return 0
  } catch (error) {
    stderr.write(String(error.message || error) + '\n')
    return error.exitCode || 1
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const code = await main()
  // Drena stdout/stderr antes de terminar, incluso si el peer nunca confirma close.
  process.stdout.write('', () => process.stderr.write('', () => process.exit(code)))
}
