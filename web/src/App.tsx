import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './App.css'

type Ev =
  | { type: 'session'; id: string; history_n: number; resumed?: boolean }
  | { type: 'sessions'; list: { id: string; preview: string }[] }
  | { type: 'history'; messages: { kind: 'user' | 'assistant' | 'tool'; text: string; name?: string }[] }
  | { type: 'assistant_chunk'; text: string }
  | { type: 'assistant_done'; text: string }
  | { type: 'tool'; name: string; detail?: string }
  | { type: 'reasoning'; text: string }
  | { type: 'approval_request'; command: string }

type Msg =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string; streaming?: boolean }
  | { kind: 'tool'; name: string; detail?: string }
  | { kind: 'reasoning'; text: string }

const WS_URL = 'ws://127.0.0.1:8000/ws'

function App() {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [approval, setApproval] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  const [sessions, setSessions] = useState<{ id: string; preview: string }[] | null>(null)
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
      } else if (ev.type === 'history') {
        setMsgs(
          ev.messages.map((m) =>
            m.kind === 'tool' ? { kind: 'tool' as const, name: m.name || '' } : { kind: m.kind, text: m.text },
          ),
        )
      } else if (ev.type === 'sessions') {
        if (ev.list.length > 0) setSessions(ev.list)
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
