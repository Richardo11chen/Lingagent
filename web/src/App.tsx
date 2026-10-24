import { useEffect, useRef, useState } from 'react'
import BottomPanel from './components/BottomPanel'
import ChatArea from './components/ChatArea'
import Composer from './components/Composer'
import DetailsPanel from './components/DetailsPanel'
import Sidebar from './components/Sidebar'
import type { Ev, Msg, Session, TraceRec } from './lib/types'
import './styles/tokens.css'
import './styles/global.css'
import './styles/app.css'

// 端口跟后端配置走：web/.env 里写 VITE_WS_PORT=xxxx 即可（缺省 8000）
const WS_PORT = import.meta.env.VITE_WS_PORT ?? '8000'
const WS_URL = `ws://${location.hostname || '127.0.0.1'}:${WS_PORT}/ws`

export default function App() {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [approval, setApproval] = useState<{ command: string; reason: string } | null>(null)
  const [connected, setConnected] = useState(false)
  const [running, setRunning] = useState(false) // 这一轮在跑：决定底部显示「发送」还是「停止」
  const [pending, setPending] = useState(false) // 已发出、还没等到第一个字
  const [sessions, setSessions] = useState<Session[]>([])
  const [modelName, setModelName] = useState('')
  const [modelPicker, setModelPicker] = useState<{ list: string[]; current: string } | null>(null)
  const [trace, setTrace] = useState<{ list: TraceRec[]; total: number } | null>(null)
  const [collapsed, setCollapsed] = useState(false) // 侧栏
  const [detailsOpen, setDetailsOpen] = useState(true)
  const [selectedTool, setSelectedTool] = useState<number | null>(null)

  const wsRef = useRef<WebSocket | null>(null)
  const sidRef = useRef('') // 当前会话 id：重连时用它 resume，否则后端会悄悄开一个空会话

  useEffect(() => {
    let sock: WebSocket | null = null
    let retry: number | undefined
    let unmounted = false

    const onMessage = (e: MessageEvent) => {
      const ev: Ev = JSON.parse(e.data)
      switch (ev.type) {
        case 'session':
          setSessionId(ev.id)
          sidRef.current = ev.id
          if (ev.model) setModelName(ev.model)
          break
        case 'history':
          setMsgs(
            ev.messages.map((m) =>
              m.kind === 'tool'
                ? ({ kind: 'tool', name: m.name || '' } as const)
                : ({ kind: m.kind, text: m.text } as const),
            ),
          )
          setSelectedTool(null)
          break
        case 'sessions':
          setSessions(ev.list)
          break
        case 'trace':
          setTrace({ list: ev.list, total: ev.total })
          break
        case 'models':
          setModelPicker({ list: ev.list, current: ev.current })
          break
        case 'model_switched':
          if (ev.model) {
            setModelName(ev.model)
            setMsgs((ms) => [...ms, { kind: 'tool', name: `已切换到 ${ev.model}` }])
          } else {
            setMsgs((ms) => [...ms, { kind: 'error', text: ev.error || '切换失败' }])
          }
          break
        case 'assistant_chunk':
          setPending(false)
          setMsgs((ms) => {
            const last = ms[ms.length - 1]
            if (last && last.kind === 'assistant' && last.streaming) {
              return [...ms.slice(0, -1), { ...last, text: last.text + ev.text }]
            }
            return [...ms, { kind: 'assistant', text: ev.text, streaming: true }]
          })
          break
        case 'assistant_done':
          setRunning(false)
          setPending(false)
          setMsgs((ms) => {
            const last = ms[ms.length - 1]
            if (last && last.kind === 'assistant' && last.streaming) {
              return [...ms.slice(0, -1), { kind: 'assistant', text: ev.text }]
            }
            return [...ms, { kind: 'assistant', text: ev.text }]
          })
          // 一轮结束后刷新列表：新会话这时才有第一句可作预览
          sock?.send(JSON.stringify({ type: 'sessions_request' }))
          break
        case 'reasoning':
          setPending(false)
          setMsgs((ms) => {
            const last = ms[ms.length - 1]
            if (last && last.kind === 'reasoning') {
              return [...ms.slice(0, -1), { kind: 'reasoning', text: last.text + ev.text }]
            }
            return [...ms, { kind: 'reasoning', text: ev.text }]
          })
          break
        case 'tool':
          setPending(false)
          setMsgs((ms) => [...ms, { kind: 'tool', name: ev.name, detail: ev.detail }])
          break
        case 'hook':
          setPending(false)
          setMsgs((ms) => [...ms, { kind: 'hook', text: `⚡ 策略拦截：${ev.by} · ${ev.reason}` }])
          break
        case 'notice':
          setPending(false)
          setApproval(null)
          setMsgs((ms) => [...ms, { kind: 'notice', text: ev.text }])
          break
        case 'approval_request':
          setApproval({ command: ev.command, reason: ev.reason ?? '' })
          break
        case 'error':
          setRunning(false)
          setPending(false)
          setMsgs((ms) => [...ms, { kind: 'error', text: ev.text }])
          break
      }
    }

    const connect = () => {
      const ws = new WebSocket(WS_URL)
      sock = ws
      wsRef.current = ws
      ws.onopen = () => {
        setConnected(true)
        // 会话列表每次都问——只在「没有 sid」时问是个坑：那次请求一旦错过
        // （先离线后连上、或重连走 resume），侧栏就永远是空的，只能刷新页面
        ws.send(JSON.stringify({ type: 'sessions_request' }))
        const sid = sidRef.current
        if (sid) ws.send(JSON.stringify({ type: 'resume', id: sid }))
      }
      // 后端还没起来（TUI 是异步拉起的）时连不上——1.5 秒后重试，直到连上
      ws.onclose = () => {
        setConnected(false)
        if (wsRef.current === ws) wsRef.current = null // 别把新 socket 清掉
        // 断线 = 这一轮没了：清掉残留的审批卡（点它已经没人接）和「在跑」状态
        setApproval(null)
        setRunning(false)
        setPending(false)
        if (!unmounted) retry = window.setTimeout(connect, 1500)
      }
      ws.onmessage = onMessage
    }

    connect()
    return () => {
      unmounted = true
      window.clearTimeout(retry)
      sock?.close()
    }
  }, [])

  const send = () => {
    const text = input.trim()
    if (!text || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return
    if (running) return // 上一轮还没完——服务端也会忽略，这里先在按钮上挡住
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
    setRunning(true)
    setPending(true)
  }

  // 停止本轮：不再发起新动作（正在跑的那一步会跑完），与 TUI 的 Ctrl+C 同义
  const interrupt = () => wsRef.current?.send(JSON.stringify({ type: 'interrupt' }))

  const answer = (approved: boolean, remember = false) => {
    wsRef.current?.send(JSON.stringify({ type: 'approval_response', approved, remember }))
    setApproval(null)
  }

  const picked = selectedTool === null ? null : (msgs[selectedTool] ?? null)

  return (
    <div className="shell">
      <Sidebar
        sessions={sessions}
        current={sessionId}
        collapsed={collapsed}
        onToggle={() => setCollapsed((v) => !v)}
        onPick={(id) => wsRef.current?.send(JSON.stringify({ type: 'resume', id }))}
        onReload={() => wsRef.current?.send(JSON.stringify({ type: 'sessions_request' }))}
        // 新会话要后端支持一条 new_session 消息；在那之前重连一次最省事
        onNew={() => location.reload()}
      />

      <div className="main">
        <div className="topbar">
          <span className="pill plain" title="会话 id">
            {sessionId || '连接中…'}
          </span>
          <span className="grow" />

          <div className="popover-wrap">
            <button className="pill" onClick={() => wsRef.current?.send(JSON.stringify({ type: 'models_request' }))}>
              {modelName || '模型'}
            </button>
            {modelPicker && (
              <div className="popover">
                {modelPicker.list.map((n) => (
                  <button
                    key={n}
                    className={n === modelPicker.current ? 'active' : ''}
                    onClick={() => {
                      wsRef.current?.send(JSON.stringify({ type: 'switch_model', name: n }))
                      setModelPicker(null)
                    }}
                  >
                    {n}
                    {n === modelPicker.current ? ' ●' : ''}
                  </button>
                ))}
                <button onClick={() => setModelPicker(null)}>取消</button>
              </div>
            )}
          </div>

          <span className="pill plain" title={connected ? '已连接' : '未连接'}>
            <span className={connected ? 'dot ok' : 'dot'} />
            {connected ? '在线' : '离线'}
          </span>

          <button className="btn sm" onClick={() => setDetailsOpen((v) => !v)}>
            {detailsOpen ? '收起详情' : '详情'}
          </button>
        </div>

        <ChatArea msgs={msgs} pending={pending} selected={selectedTool} onSelect={setSelectedTool} />

        {approval && (
          <div className="approval">
            {/* 先给「为什么问」，再给命令本身——只给命令等于让人盲批 */}
            <div className="approval-reason">⚠ {approval.reason || '需要确认'}</div>
            <code className="approval-cmd">{approval.command}</code>
            <div className="approval-actions">
              <button className="btn sm primary" onClick={() => answer(true)}>
                批准
              </button>
              <button className="btn sm danger" onClick={() => answer(false)}>
                驳回
              </button>
              <button className="btn sm" onClick={() => answer(true, true)}>
                本会话不再问
              </button>
            </div>
          </div>
        )}

        <Composer
          value={input}
          onChange={setInput}
          onSend={send}
          onStop={interrupt}
          running={running}
          connected={connected}
        />

        {trace && (
          <BottomPanel
            trace={trace}
            onRefresh={() => wsRef.current?.send(JSON.stringify({ type: 'trace_request' }))}
            onClose={() => setTrace(null)}
          />
        )}
      </div>

      {detailsOpen && <DetailsPanel msg={picked} onClose={() => setDetailsOpen(false)} />}
    </div>
  )
}
