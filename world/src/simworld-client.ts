// Minimal streamable-HTTP MCP client used by world-side prefetch — daily
// prompt injection wants index quotes + held-fund NAV from simworld-data
// without forcing every bot to discover_tools + tool_call every morning.
//
// Each callTool() opens a fresh session (initialize → notifications/initialized
// → tools/call → DELETE) so callers don't have to manage state. Three calls
// per bot per day at ~50ms each is well under the per-day budget, and a
// stateless client is much harder to get wrong than a pooled one.
//
// Mirrors the handshake in simworld-proxy/server.ts:probeUpstreamTools — same
// transport, same SSE-or-JSON content-type tolerance.

interface JsonRpcMessage {
  jsonrpc?: string
  id?: number | string | null
  method?: string
  params?: Record<string, unknown>
  result?: unknown
  error?: { code?: number; message?: string } | unknown
}

function parseJsonOrSse(text: string): unknown {
  if (text.startsWith('event:') || text.includes('\ndata:')) {
    for (const ev of text.split(/\n\n/)) {
      const dataLine = ev.split('\n').find(l => l.startsWith('data:'))
      if (!dataLine) continue
      try { return JSON.parse(dataLine.slice(5).trim()) } catch { /* try next event */ }
    }
    return null
  }
  try { return JSON.parse(text) } catch { return null }
}

/** Open a session, call one tool, tear down. The tool result's first text
 *  content block is returned as already-parsed JSON (every simworld-data tool
 *  we use returns JSON in a text block). Returns null on transport error or
 *  if the upstream wraps an `error` envelope. */
export async function callSimworldTool(
  upstreamUrl: string,
  toolName: string,
  args: Record<string, unknown>,
  opts: { timeoutMs?: number } = {},
): Promise<unknown> {
  const baseHeaders = { 'content-type': 'application/json', 'accept': 'application/json, text/event-stream' }
  const abort = AbortSignal.timeout(opts.timeoutMs ?? 15000)

  const initResp = await fetch(upstreamUrl, {
    method: 'POST',
    headers: baseHeaders,
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'world-daily-context', version: '0.1' } } }),
    signal: abort,
  })
  if (!initResp.ok) throw new Error(`simworld init HTTP ${initResp.status}`)
  const sid = initResp.headers.get('mcp-session-id')
  await initResp.text()
  if (!sid) throw new Error('simworld init: no mcp-session-id')
  const sessionHeaders = { ...baseHeaders, 'mcp-session-id': sid }

  try {
    await fetch(upstreamUrl, {
      method: 'POST', headers: sessionHeaders, signal: abort,
      body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized', params: {} }),
    }).then(r => r.text())

    const callResp = await fetch(upstreamUrl, {
      method: 'POST', headers: sessionHeaders, signal: abort,
      body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: toolName, arguments: args } }),
    })
    if (!callResp.ok) throw new Error(`simworld ${toolName} HTTP ${callResp.status}`)
    const text = await callResp.text()
    const parsed = parseJsonOrSse(text) as JsonRpcMessage | null
    const result = parsed && typeof parsed === 'object' ? (parsed as JsonRpcMessage).result : null
    if (!result || typeof result !== 'object') return null
    const content = (result as { content?: Array<{ type?: string; text?: string }> }).content ?? []
    const textBlock = content.find(c => c?.type === 'text' && typeof c.text === 'string')
    if (!textBlock?.text) return null
    try { return JSON.parse(textBlock.text) } catch { return textBlock.text }
  } finally {
    // Best-effort session teardown so upstream doesn't accumulate dangling sessions.
    fetch(upstreamUrl, { method: 'DELETE', headers: sessionHeaders }).catch(() => { /* ignore */ })
  }
}
