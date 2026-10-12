import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './App.css'

type Ev =
  | { type: 'session'; id: string; history_n: number; resumed?: boolean; model?: string }
  | { type: 'sessions'; list: { id: string; preview: string }[] }
  | { type: 'history'; messages: { kind: 'user' | 'assistant' | 'tool'; text: string; name?: string }[] }
  | { type: 'assistant_chunk'; text: string }
  | { type: 'assistant_done'; text: string }
  | { type: 'tool'; name: string; detail?: string }
  | { type: 'reasoning'; text: string }
  | { type: 'models'; list: string[]; current: string }
  | { type: 'model_switched'; model: string; error?: string }
  | { type: 'trace'; list: TraceRec[]; total: number }
  | { type: 'approval_request'; command: string }

type Msg =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string; streaming?: boolean }
  | { kind: 'tool'; name: string; detail?: string }
  | { kind: 'reasoning'; text: string }

type TraceRec = {
  ts: string
  kind: string
  step?: number
  n_actions?: number
  tool?: string
  args?: { command?: string; path?: string }
  status?: string
  result?: string
  error?: string
  command?: string
  decision?: string
  verdict?: string
  by?: string
  reason?: string
  duration_ms?: number
}

const WS_URL = 'ws://127.0.0.1:8000/ws'

const fmtMs = (v?: number) => (v == null ? '' : v < 1000 ? `${Math.round(v)}ms` : `${(v / 1000).toFixed(1)}s`)
const STATUS_ZH: Record<string, string> = { ok: '成功', denied: '驳回', blocked: '拦截', error: '错误' }
const DECISION_ZH: Record<string, string> = { approved: '已批准', denied: '已驳回' }

// 口径与 TUI /trace 对齐（工程化措辞）
function traceRow(r: TraceRec): { label: string; detail: string } {
  if (r.kind === 'model')
    return {
      label: '● 模型',
      detail: r.error ? `失败 · ${r.error}` : `step ${r.step} · 工具调用 ×${r.n_actions ?? '?'}`,
    }
  if (r.kind === 'tool')
    return {
      label: `⚙ ${r.tool ?? ''}`,
      detail: `${STATUS_ZH[r.status ?? ''] ?? r.status ?? ''} · ${r.args?.command ?? r.args?.path ?? ''}${r.error ? ' ' + r.error : ''}`,
    }
  if (r.kind === 'approval')
    return {
      label: '⚠ 审批',
      detail: `${DECISION_ZH[r.decision ?? ''] ?? r.decision ?? ''} · ${(r.command ?? '').slice(0, 60)}`,
    }
  if (r.kind === 'hook')
    return {
      label: '⚡ 策略',
      detail: `${r.verdict === 'deny' ? '拦下' : '放行'}（${r.by ?? ''}）${r.reason ? ' · ' + r.reason : ''}`,
    }
  return { label: r.kind, detail: '' }
}

const fmtVal = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v))

function App() {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [approval, setApproval] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  const [sessions, setSessions] = useState<{ id: string; preview: string }[] | null>(null)
  const [modelName, setModelName] = useState('')
  const [modelPicker, setModelPicker] = useState<{ list: string[]; current: string } | null>(null)
  const [trace, setTrace] = useState<{ list: TraceRec[]; total: number } | null>(null)
  const [traceSel, setTraceSel] = useState<number | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws
    ws.onopen = () => {
      setConnected(true)
      ws.send(JSON.stringify({ type: 'sessions_request' }))
    }
    ws.onclose = () => setConnected(false)
    ws.onmessage = (e) => {
      const ev: Ev = JSON.parse(e.data)
      if (ev.type === 'session') {
        setSessionId(ev.id)
        if (ev.model) setModelName(ev.model)  // 会话级模型：恢复时把实际模型带给表头
      } else if (ev.type === 'history') {
        setMsgs(
          ev.messages.map((m) =>
            m.kind === 'tool' ? { kind: 'tool' as const, name: m.name || '' } : { kind: m.kind, text: m.text },
          ),
        )
      } else if (ev.type === 'sessions') {
        if (ev.list.length > 0) setSessions(ev.list)
      } else if (ev.type === 'trace') {
        setTrace({ list: ev.list, total: ev.total })
        setTraceSel(null)
      } else if (ev.type === 'models') {
        setModelPicker({ list: ev.list, current: ev.current })
      } else if (ev.type === 'model_switched') {
        if (ev.model) {
          setModelName(ev.model)
          setMsgs((ms) => [...ms, { kind: 'tool', name: `已切换到 ${ev.model}` }])
        } else {
          setMsgs((ms) => [...ms, { kind: 'tool', name: ev.error || '切换失败' }])
        }
      } else if (ev.type === 'assistant_chunk') {
        setMsgs((ms) => {
          const last = ms[ms.length - 1]
          if (last && last.kind === 'assistant' && last.streaming) {
            return [...ms.slice(0, -1), { ...last, text: last.text + ev.text }]
          }
          return [...ms, { kind: 'assistant', text: ev.text, streaming: true }]
        })
      } else if (ev.type === 'assistant_done') {
        setMsgs((ms) => {
          const last = ms[ms.length - 1]
          if (last && last.kind === 'assistant' && last.streaming) {
            return [...ms.slice(0, -1), { kind: 'assistant', text: ev.text }]
          }
          return [...ms, { kind: 'assistant', text: ev.text }]
        })
      } else if (ev.type === 'reasoning') {
        setMsgs((ms) => {
          const last = ms[ms.length - 1]
          if (last && last.kind === 'reasoning') {
            return [...ms.slice(0, -1), { kind: 'reasoning', text: last.text + ev.text }]
          }
          return [...ms, { kind: 'reasoning', text: ev.text }]
        })
      } else if (ev.type === 'tool') {
        setMsgs((ms) => [...ms, { kind: 'tool', name: ev.name, detail: ev.detail }])
      } else if (ev.type === 'approval_request') {
        setApproval(ev.command)
      }
    }
    return () => ws.close()
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [msgs])

  const send = () => {
    const text = input.trim()
    if (!text || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    if (text === '/model') {
      wsRef.current.send(JSON.stringify({ type: 'models_request' }))
      setInput('')
      return
    }
    if (text === '/trace') {
      wsRef.current.send(JSON.stringify({ type: 'trace_request' }))
      setInput('')
      return
    }
    wsRef.current.send(JSON.stringify({ type: 'user', text }))
    setMsgs((ms) => [...ms, { kind: 'user', text }])
    setInput('')
  }

  const answer = (approved: boolean) => {
    wsRef.current?.send(JSON.stringify({ type: 'approval_response', approved }))
    setApproval(null)
  }

  return (
    <div className="app">
      <header>
        <strong>Agent Harness</strong>
        {sessionId && <span className="sid">{sessionId}</span>}
        {modelName && <span className="sid">{modelName}</span>}
        <span className={connected ? 'dot ok' : 'dot'}>●</span>
      </header>
      <main>
        {msgs.map((m, i) =>
          m.kind === 'tool' ? (
            <div key={i} className="tool">
              ⚙ {m.name}
              {m.detail ? `: ${m.detail}` : ''}
            </div>
          ) : m.kind === 'reasoning' ? (
            <div key={i} className="reasoning">
              💭 {m.text}
            </div>
          ) : (
            <div key={i} className={`bubble ${m.kind}`}>
              {m.kind === 'assistant' ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.text}</ReactMarkdown>
              ) : (
                m.text
              )}
            </div>
          ),
        )}
        <div ref={bottomRef} />
      </main>
      {sessions !== null && (
        <div className="session-picker">
          <h3>继续哪个会话？</h3>
          {sessions.map((s) => (
            <button
              key={s.id}
              onClick={() => {
                wsRef.current?.send(JSON.stringify({ type: 'resume', id: s.id }))
                setSessions(null)
              }}
            >
              {s.preview || '(空会话)'}
              <span className="sid">{s.id}</span>
            </button>
          ))}
          <button className="new" onClick={() => setSessions(null)}>
            开新会话
          </button>
        </div>
      )}
      {modelPicker !== null && (
        <div className="session-picker">
          <h3>选择模型（输入 /model 呼出）</h3>
          {modelPicker.list.map((n) => (
            <button
              key={n}
              onClick={() => {
                wsRef.current?.send(JSON.stringify({ type: 'switch_model', name: n }))
                setModelPicker(null)
              }}
            >
              {n}
              {n === modelPicker.current ? ' ●' : ''}
            </button>
          ))}
          <button className="new" onClick={() => setModelPicker(null)}>
            取消
          </button>
        </div>
      )}
      {trace !== null && (
        <div className="session-picker trace-panel">
          {traceSel === null ? (
            <>
              <h3>
                轨迹时间线（共 {trace.total} 条
                {trace.total > trace.list.length ? `，显示最近 ${trace.list.length} 条` : ''}）
              </h3>
              {trace.list.map((r, i) => {
                const row = traceRow(r)
                return (
                  <button key={i} onClick={() => setTraceSel(i)}>
                    <span className="trace-main">
                      #{i + 1} {row.label} {row.detail}
                    </span>
                    <span className="sid">
                      {r.ts.slice(11, 23)} {fmtMs(r.duration_ms)}
                    </span>
                  </button>
                )
              })}
              <button className="new" onClick={() => setTrace(null)}>
                关闭
              </button>
            </>
          ) : (
            <>
              <h3>
                #{traceSel + 1} · {trace.list[traceSel].kind}
              </h3>
              {Object.entries(trace.list[traceSel]).map(([k, v]) => (
                <div key={k} className="trace-field">
                  <span className="sid">{k}</span>
                  <pre>{fmtVal(v)}</pre>
                </div>
              ))}
              <button className="new" onClick={() => setTraceSel(null)}>
                返回列表
              </button>
            </>
          )}
        </div>
      )}
      {approval !== null && (
        <div className="approval">
          ⚠️ 允许执行「{approval}」吗？
          <button onClick={() => answer(true)}>批准</button>
          <button onClick={() => answer(false)}>驳回</button>
        </div>
      )}
      <footer>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder="❯ 说点什么…"
          autoFocus
        />
        <button onClick={send}>发送</button>
      </footer>
    </div>
  )
}

export default App
