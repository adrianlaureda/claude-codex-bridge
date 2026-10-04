// Talk to the ChatGPT app's own Codex server through its page, over the local Chromium debugging
// port (127.0.0.1 only). ChatGPT must run with --remote-debugging-port=$CODEX_CDP_PORT (9334).
//   node app.mjs eval '<js>'                  -> value (promises awaited)
//   node app.mjs call <method> '<json params>' -> the app server's JSON-RPC reply
// `call` uses the page's preload API: electronBridge.sendMessageFromView({type:'mcp-request',
// hostId:'local', request}) goes to the app server the app runs, which holds the threads it has
// open; the reply comes back to the page as a window message {type:'mcp-response', message}.
// So a turn on an app-held thread starts at once instead of waiting for the app's queue timer.
const port = process.env.CODEX_CDP_PORT || 9334
const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const page = pages.find(p => p.type === 'page' && p.url.startsWith('app://'))
if (!page) { console.error('no ChatGPT page on port ' + port); process.exit(2) }
const ws = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((ok, no) => { ws.onopen = ok; ws.onerror = no })
let n = 0
const pending = new Map()
ws.onmessage = m => { const r = JSON.parse(m.data); const p = pending.get(r.id); if (p) { pending.delete(r.id); r.error ? p[1](new Error(JSON.stringify(r.error))) : p[0](r.result) } }
const send = (method, params = {}) => new Promise((ok, no) => { pending.set(++n, [ok, no]); ws.send(JSON.stringify({ id: n, method, params })) })
async function evalJs(expression) {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text)
  return r.result.value
}
// One listener, installed once, keeps replies to our ids in window.__cbReplies; node polls, so a
// throttled background window never stalls a call.
async function call(method, params, waitMs = 30000) {
  const id = 'cb-' + Date.now() + '-' + Math.floor(Math.random() * 1e6)
  await evalJs(`(async () => {
    if (!window.electronBridge?.sendMessageFromView) throw new Error('no electronBridge.sendMessageFromView')
    if (!window.__cbReplies) { window.__cbReplies = {}; window.addEventListener('message', ev => { const d = ev.data; const id = d?.message?.id; if (d?.type === 'mcp-response' && typeof id === 'string' && id.startsWith('cb-')) window.__cbReplies[id] = JSON.parse(JSON.stringify(d.message)) }) }
    await window.electronBridge.sendMessageFromView({ type: 'mcp-request', hostId: 'local', request: { id: ${JSON.stringify(id)}, method: ${JSON.stringify(method)}, params: ${JSON.stringify(params ?? {})} } })
  })()`)
  for (const end = Date.now() + waitMs; Date.now() < end; await new Promise(r => setTimeout(r, 150))) {
    const got = await evalJs(`(() => { const r = window.__cbReplies[${JSON.stringify(id)}]; if (r) delete window.__cbReplies[${JSON.stringify(id)}]; return r ?? null })()`)
    if (got) return got
  }
  throw new Error(`no reply to ${method} in ${waitMs / 1000} s`)
}
const [cmd, a1, a2] = process.argv.slice(2)
try {
  const out = cmd === 'eval' ? await evalJs(a1) : cmd === 'call' ? await call(a1, a2 ? JSON.parse(a2) : {}) : undefined
  if (out === undefined && cmd !== 'eval') { console.error('usage: eval <js> | call <method> [json]'); process.exit(2) }
  process.stdout.write((typeof out === 'string' ? out : JSON.stringify(out)) + '\n', () => process.exit(0))
} catch (e) { console.error(String(e.message || e)); process.exit(1) }
