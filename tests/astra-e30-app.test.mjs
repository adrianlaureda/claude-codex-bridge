import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'

const appUrl = new URL('../scripts/live/app.mjs', import.meta.url).href

function isolated(source) {
  return spawnSync(process.execPath, ['--input-type=module', '-e', source], {
    encoding: 'utf8', timeout: 1500,
    env: { PATH: process.env.PATH },
  })
}

test('importar no accede a red, WebSocket ni salida CLI', () => {
  const child = isolated(`
    globalThis.fetch = () => { throw new Error('red al importar') }
    globalThis.WebSocket = class { constructor() { throw new Error('WS al importar') } }
    await import(${JSON.stringify(appUrl)})
    console.log('importado')
  `)
  assert.equal(child.status, 0, child.stderr)
  assert.equal(child.stdout, 'importado\n')
})

for (const scenario of ['no abre', 'cierra con pendiente']) {
  test(`CLI acotada cuando WS ${scenario}`, () => {
    const child = isolated(`
      const hold = setInterval(() => {}, 100)
      const fetch = globalThis.fetch = async () => ({ ok: true, json: async () => [
        { type: 'page', url: 'app://fixture', webSocketDebuggerUrl: 'ws://127.0.0.1:9334/page' }
      ] })
      const WebSocket = globalThis.WebSocket = class {
        readyState = 0
        constructor() {
          if (${JSON.stringify(scenario)} !== 'no abre') setTimeout(() => {
            this.readyState = 1; this.onopen?.()
          }, 0)
        }
        send() { setTimeout(() => { this.readyState = 3; this.onclose?.() }, 0) }
        close() { this.readyState = 3; this.onclose?.() }
      }
      process.argv = ['node', 'synthetic', 'eval', '1']
      const { main } = await import(${JSON.stringify(appUrl)})
      const code = await main(['eval', '1'], { fetch, WebSocket, timeoutMs: 25,
        stdout: { write() {} }, stderr: { write() {} } })
      clearInterval(hold)
      console.log(code)
    `)
    assert.equal(child.error, undefined, child.error?.message)
    assert.equal(child.status, 0, child.stderr)
    assert.equal(child.stdout, '1\n')
  })
}

const { main } = await import(appUrl)
const { createContext, runInContext } = await import('node:vm')

function fakeClock() {
  let now = 0, serial = 0
  const timers = new Map()
  return {
    timers,
    now: () => now,
    setTimeout(fn, ms) { const id = ++serial; timers.set(id, { at: now + ms, fn }); return id },
    clearTimeout(id) { timers.delete(id) },
    advance(ms) {
      const end = now + ms
      while (true) {
        const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0]
        if (!next || next[1].at > end) break
        now = next[1].at; timers.delete(next[0]); next[1].fn()
      }
      now = end
    },
  }
}

async function flush() { for (let i = 0; i < 40; i++) await Promise.resolve() }
const never = () => new Promise(() => {})
const page = { type: 'page', url: 'app://fixture', webSocketDebuggerUrl: 'ws://127.0.0.1:9334/page' }

function fixture(options = {}) {
  const clock = fakeClock(), pageClock = fakeClock(), sockets = [], requests = [], listeners = new Set()
  let stdout = '', stderr = '', signal, fetchCount = 0
  const window = {
    addEventListener(type, listener) { assert.equal(type, 'message'); listeners.add(listener) },
    removeEventListener(type, listener) { listeners.delete(listener) },
    electronBridge: {
      sendMessageFromView(message) {
        requests.push(message)
        return options.rpc?.(message, emit)
      },
    },
  }
  function emit(message) { for (const listener of [...listeners]) listener({ data: message }) }
  const context = createContext({ window,
    setTimeout: pageClock.setTimeout.bind(pageClock), clearTimeout: pageClock.clearTimeout.bind(pageClock),
  })
  class Socket {
    readyState = 0
    closeCount = 0
    sent = []
    constructor() {
      if (options.constructError) throw new Error('Constructor sintético fallido')
      sockets.push(this)
      if (!options.noOpen) queueMicrotask(() => {
        this.readyState = 1
        this.onopen?.()
        options.afterOpen?.(this)
      })
    }
    message(data) { this.onmessage?.({ data: typeof data === 'string' ? data : JSON.stringify(data) }) }
    reply(request, value) { this.message({ id: request.id, result: { result: { value } } }) }
    send(data) {
      const request = JSON.parse(data)
      this.sent.push(request)
      if (options.sendError) throw new Error('Envío sintético fallido')
      if (options.onSend) return options.onSend(this, request)
      // Ejecuta la expresión real en una página sintética, sin navegador ni red.
      let value
      try { value = runInContext(request.params.expression, context) }
      catch (error) { this.exception(request, error); return }
      Promise.resolve(value).then(result => this.reply(request, result), error => this.exception(request, error))
    }
    exception(request, error) {
      this.message({ id: request.id, result: { exceptionDetails: { text: error.message } } })
    }
    disconnect() { this.readyState = 3; this.onclose?.() }
    close() { this.closeCount++; this.readyState = 3; this.onclose?.() }
  }
  const deps = {
    clock, WebSocket: Socket, timeoutMs: 100,
    async fetch(url, init) {
      fetchCount++; signal = init.signal
      assert.equal(url, 'http://127.0.0.1:9334/json/list')
      assert.equal(init.redirect, 'error')
      if (options.fetch) return options.fetch(init)
      return { ok: options.ok ?? true, status: 503, json: options.json ?? (async () => options.pages ?? [page]) }
    },
    stdout: { write(text) { stdout += text } }, stderr: { write(text) { stderr += text } },
  }
  return {
    deps, clock, pageClock, sockets, requests, listeners, window, emit,
    run: (args = ['eval', '1']) => main(args, deps),
    get stdout() { return stdout }, get stderr() { return stderr },
    get signal() { return signal }, get fetchCount() { return fetchCount },
    clean() {
      assert.equal(clock.timers.size, 0, 'sin temporizadores Node pendientes')
      if (signal) assert.equal(signal.aborted, true)
      for (const ws of sockets) {
        assert.equal(ws.readyState, 3)
        for (const handler of ['onopen', 'onmessage', 'onerror', 'onclose']) assert.equal(ws[handler], null)
      }
    },
    pageClean() {
      assert.equal(listeners.size, 0)
      assert.equal(pageClock.timers.size, 0)
      assert.equal(Object.keys(window.__cbLiveCalls ?? {}).length, 0)
    },
  }
}

for (const [name, options, code, error] of [
  ['lista vacía', { pages: [] }, 2, /No hay página/],
  ['lista objeto', { pages: {} }, 1, /malformada/],
  ['entradas incompletas', { pages: [null, {}, { type: 'page', url: 5 }] }, 2, /No hay página/],
  ['URL WebSocket ausente', { pages: [{ type: 'page', url: 'app://fixture' }] }, 1, /inválida/],
  ['URL WebSocket remota', { pages: [{ ...page, webSocketDebuggerUrl: 'ws://example.invalid:9334/' }] }, 1, /local/],
  ['otro puerto', { pages: [{ ...page, webSocketDebuggerUrl: 'ws://127.0.0.1:9999/' }] }, 1, /local/],
  ['fetch falla', { fetch: async () => { throw new Error('Fetch sintético fallido') } }, 1, /Fetch sintético/],
  ['HTTP falla', { ok: false }, 1, /HTTP 503/],
  ['JSON de páginas roto', { json: async () => { throw new Error('JSON sintético roto') } }, 1, /JSON sintético/],
  ['constructor WS falla', { constructError: true }, 1, /Constructor sintético/],
]) {
  test(name, async () => {
    const f = fixture(options)
    assert.equal(await f.run(), code)
    assert.match(f.stderr, error); assert.equal(f.stdout, ''); f.clean()
  })
}

for (const args of [[], ['otro'], ['eval'], ['eval', '1', 'extra'], ['call', 'method', '{']]) {
  test(`argumentos inválidos sin red: ${JSON.stringify(args)}`, async () => {
    const f = fixture()
    assert.equal(await f.run(args), 2)
    assert.equal(f.fetchCount, 0); f.clean()
  })
}

for (const stage of ['fetch', 'json', 'open', 'evaluate']) {
  test(`plazo global y limpieza durante ${stage}`, async () => {
    let late
    const f = fixture({
      ...(stage === 'fetch' ? { fetch: () => new Promise(resolve => { late = resolve }) } : {}),
      ...(stage === 'json' ? { json: never } : {}),
      ...(stage === 'open' ? { noOpen: true } : {}),
      ...(stage === 'evaluate' ? { onSend() {} } : {}),
    })
    let done = false
    const result = f.run().then(code => { done = true; return code })
    await flush(); f.clock.advance(99); await flush(); assert.equal(done, false)
    f.clock.advance(1); await flush()
    assert.equal(await result, 1); assert.match(f.stderr, /Plazo global/); f.clean()
    late?.({ ok: true, json() { assert.fail('no se lee una respuesta fetch tardía') } })
    await flush(); assert.equal(f.sockets.length, stage === 'open' || stage === 'evaluate' ? 1 : 0)
  })
}

test('el presupuesto consumido en fetch no se reinicia al abrir WS', async () => {
  let release
  const f = fixture({ fetch: () => new Promise(resolve => { release = resolve }), noOpen: true })
  const result = f.run()
  f.clock.advance(70)
  release({ ok: true, json: async () => [page] })
  await flush(); assert.equal(f.sockets.length, 1)
  f.clock.advance(30); await flush()
  assert.equal(await result, 1); f.clean()
})

for (const [name, onSend, error] of [
  ['JSON CDP roto', ws => ws.message('{'), /JSON CDP inválido/],
  ['CDP nulo', ws => ws.message('null'), /malformada/],
  ['CDP error', (ws, r) => ws.message({ id: r.id, error: { message: 'fallo sintético' } }), /Error CDP/],
  ['CDP sin resultado', (ws, r) => ws.message({ id: r.id }), /sin resultado/],
  ['evaluate malformado', (ws, r) => ws.message({ id: r.id, result: {} }), /Runtime.evaluate malformada/],
  ['exceptionDetails', (ws, r) => ws.message({ id: r.id, result: { exceptionDetails: { exception: { description: 'excepción sintética' } } } }), /excepción sintética/],
  ['cierre pendiente', ws => ws.disconnect(), /desconectado/],
  ['error pendiente', ws => ws.onerror(), /Error de WebSocket/],
]) {
  test(name, async () => {
    const f = fixture({ onSend })
    assert.equal(await f.run(), 1); assert.match(f.stderr, error); f.clean()
    assert.equal(f.sockets[0].sent.length, 1)
  })
}

test('send que lanza limpia la petición y cierra WS', async () => {
  const f = fixture({ sendError: true })
  assert.equal(await f.run(), 1); assert.match(f.stderr, /no se reintentará/)
  assert.equal(f.sockets[0].closeCount, 1); f.clean()
})

test('id CDP ajeno y eventos no resuelven la petición pendiente', async () => {
  let request
  const f = fixture({ onSend(ws, r) { request = r; ws.message({ id: r.id + 7, result: { result: { value: 'ajeno' } } }); ws.message({ method: 'Runtime.event' }) } })
  let done = false
  const result = f.run().then(code => { done = true; return code })
  await flush(); assert.equal(done, false)
  f.sockets[0].reply(request, 'propio')
  assert.equal(await result, 0); assert.equal(f.stdout, 'propio\n'); f.clean()
})

for (const [expression, output] of [
  ["'texto'", 'texto\n'], ['({ a: 1 })', '{"a":1}\n'], ['undefined', 'undefined\n'],
  ['null', 'null\n'], ['Promise.resolve(false)', 'false\n'],
]) {
  test(`stdout eval conserva ${expression}`, async () => {
    const f = fixture()
    assert.equal(await f.run(['eval', expression]), 0)
    assert.equal(f.stdout, output); assert.equal(f.stderr, ''); f.clean()
  })
}

for (const payload of [{ result: { accepted: true } }, { error: { code: -1, message: 'rechazo sintético' } }]) {
  test(`call devuelve respuesta RPC completa: ${Object.keys(payload)[0]}`, async () => {
    const f = fixture({ rpc(message, emit) {
      emit({ type: 'mcp-response', message: { id: 'cb-ajeno', result: 'ajeno' } })
      emit({ type: 'otro', message: { id: message.request.id, result: 'ajeno' } })
      emit({ type: 'mcp-response', message: { id: message.request.id, ...payload } })
    } })
    const params = { input: 'texto " sintético\n', value: 2 }
    assert.equal(await f.run(['call', 'turn/start', JSON.stringify(params)]), 0)
    const response = JSON.parse(f.stdout)
    assert.deepEqual(response, { id: f.requests[0].request.id, ...payload })
    assert.equal(f.requests.length, 1)
    assert.deepEqual(JSON.parse(JSON.stringify(f.requests[0])), {
      type: 'mcp-request', hostId: 'local', request: { id: response.id, method: 'turn/start', params },
    })
    f.clean(); f.pageClean()
  })
}

test('RPC tardía: plazo global, id conservado, una sola acción y listener eliminado', async () => {
  const f = fixture()
  const result = f.run(['call', 'turn/start', '{}'])
  await flush(); assert.equal(f.requests.length, 1); assert.equal(f.listeners.size, 1)
  f.clock.advance(100); await flush()
  assert.equal(await result, 1)
  assert.match(f.stderr, /Resultado incierto \(cb-/); assert.match(f.stderr, /no reenviar/)
  assert.ok(f.stderr.includes(f.requests[0].request.id))
  f.emit({ type: 'mcp-response', message: { id: f.requests[0].request.id, result: 'tardío' } })
  await flush()
  assert.equal(f.stdout, ''); assert.equal(f.requests.length, 1); f.clean(); f.pageClean()
})

test('desconexión tras envío aceptado no repite; la página expira por sí sola', async () => {
  const f = fixture()
  const result = f.run(['call', 'turn/start'])
  await flush(); f.sockets[0].disconnect(); await flush()
  assert.equal(await result, 1); assert.match(f.stderr, /Resultado incierto/)
  assert.equal(f.requests.length, 1); f.clean()
  // Sin CDP no se puede confirmar limpieza remota: se verifica el respaldo local de página.
  f.pageClock.advance(100); await flush(); f.pageClean()
  f.emit({ type: 'mcp-response', message: { id: f.requests[0].request.id, result: 'tardío' } })
  assert.equal(f.stdout, '')
})

test('RPC con otro id en la respuesta evaluate se rechaza', async () => {
  const f = fixture({ onSend(ws, r) { ws.reply(r, { id: 'cb-ajeno', result: {} }) } })
  assert.equal(await f.run(['call', 'turn/start']), 1)
  assert.match(f.stderr, /otro id/); assert.equal(f.stdout, ''); f.clean()
})

test('rechazo del preload se observa y elimina listener y temporizador', async () => {
  const f = fixture({ rpc: async () => { throw new Error('preload sintético fallido') } })
  assert.equal(await f.run(['call', 'turn/start']), 1)
  assert.match(f.stderr, /preload sintético/); assert.equal(f.requests.length, 1); f.clean(); f.pageClean()
})

for (const event of ['onclose', 'onerror']) {
  test(`${event} antes de abrir rechaza sin esperar el plazo`, async () => {
    const f = fixture({ noOpen: true })
    const result = f.run()
    await flush()
    f.sockets[0][event]()
    assert.equal(await result, 1)
    assert.equal(f.clock.now(), 0); f.clean()
  })
}

test('una respuesta CDP posterior al timeout no revive el pendiente', async () => {
  let request, handler
  const f = fixture({ onSend(ws, r) { request = r; handler = ws.onmessage } })
  const result = f.run()
  await flush(); f.clock.advance(100); await flush()
  assert.equal(await result, 1)
  handler({ data: JSON.stringify({ id: request.id, result: { result: { value: 'tardío' } } }) })
  await flush()
  assert.equal(f.stdout, ''); assert.equal(f.sockets[0].sent.length, 1); f.clean()
})

for (const [name, args, noOpen, code, output] of [
  ['éxito', ['eval', '7'], false, 0, '7\n'],
  ['plazo', ['eval', '7'], true, 1, ''],
  ['uso', ['inválido'], false, 2, ''],
]) {
  test(`entrada CLI real con transporte sintético: ${name}`, () => {
    const preload = `
      import assert from 'node:assert/strict'
      let socket
      const originalTimeout = globalThis.setTimeout
      globalThis.setTimeout = (fn, ms) => originalTimeout(fn, Math.min(ms, 25))
      globalThis.fetch = async () => ({ ok: true, json: async () => [${JSON.stringify(page)}] })
      globalThis.WebSocket = class {
        readyState = 0
        closed = false
        constructor() {
          socket = this
          if (!${noOpen}) queueMicrotask(() => { this.readyState = 1; this.onopen?.() })
        }
        send(data) {
          const { id } = JSON.parse(data)
          queueMicrotask(() => this.onmessage?.({ data: JSON.stringify({ id, result: { result: { value: 7 } } }) }))
        }
        close() { this.closed = true; this.readyState = 2 }
      }
      // Simula un peer que no confirma close: la CLI debe terminar tras drenar salida.
      setInterval(() => {}, 100)
      process.on('exit', () => { if (socket) assert.equal(socket.closed, true) })
    `
    const child = spawnSync(process.execPath, ['--import', `data:text/javascript,${encodeURIComponent(preload)}`,
      new URL('../scripts/live/app.mjs', import.meta.url).pathname, ...args], {
      encoding: 'utf8', timeout: 1500, env: { PATH: process.env.PATH },
    })
    assert.equal(child.error, undefined, child.error?.message)
    assert.equal(child.status, code, child.stderr)
    assert.equal(child.stdout, output)
    if (name === 'plazo') assert.match(child.stderr, /Plazo global/)
  })
}

test('el mapa pending queda vacío al vencer el plazo con CDP en vuelo', async () => {
  const f = fixture({ onSend() {} })
  const NativeMap = globalThis.Map
  let pending, result
  // Observa el mapa creado síncronamente por runApp sin añadir API de diagnóstico al helper.
  try {
    globalThis.Map = class extends NativeMap {
      constructor(...args) { super(...args); pending = this }
    }
    result = f.run()
  } finally { globalThis.Map = NativeMap }
  await flush(); assert.equal(pending.size, 1)
  f.clock.advance(100); await flush()
  assert.equal(await result, 1); assert.equal(pending.size, 0); f.clean()
})

test('dos llamadas simultáneas solo consumen su propia respuesta RPC', async () => {
  const f = fixture()
  const first = f.run(['call', 'method/first'])
  const second = f.run(['call', 'method/second'])
  await flush()
  assert.equal(f.requests.length, 2)
  const [a, b] = f.requests.map(r => r.request.id)
  assert.notEqual(a, b)
  f.emit({ type: 'mcp-response', message: { id: b, result: 'segunda' } })
  await flush(); assert.equal(f.listeners.size, 1)
  f.emit({ type: 'mcp-response', message: { id: a, result: 'primera' } })
  assert.deepEqual(await Promise.all([first, second]), [0, 0])
  assert.deepEqual(f.stdout.trim().split('\n').map(JSON.parse), [
    { id: b, result: 'segunda' }, { id: a, result: 'primera' },
  ])
  f.clean(); f.pageClean()
})
