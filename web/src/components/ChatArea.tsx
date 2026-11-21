import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import DiffBlock from './DiffBlock'
import { toolStatusZh } from '../lib/format'
import type { Msg } from '../lib/types'

type Props = {
  msgs: Msg[]
  pending: boolean
  waitingApproval: boolean // 有命令在等用户点头——那一行不是"运行中"，是"等你"
  selected: number | null
  onSelect: (i: number) => void
}

export default function ChatArea({ msgs, pending, waitingApproval, selected, onSelect }: Props) {
  const endRef = useRef<HTMLDivElement>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState<Set<number>>(new Set())

  // 最后一条还没结果的行：审批卡片是给它弹的，只有它有资格显示"等你点头"
  const lastRunning = msgs.reduce(
    (acc, m, i) => (m.kind === 'tool' && m.body === undefined ? i : acc),
    -1,
  )

  // 有工具还在跑就每半秒重渲染一次，让"运行中 · 12s"自己数——服务端不发心跳，
  // 前端用收到「开始」那一刻的本地时间算，省一堆协议字段。
  const hasRunning = msgs.some((m) => m.kind === 'tool' && m.body === undefined && m.at)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!hasRunning) return
    const timer = setInterval(() => setNow(Date.now()), 500)
    return () => clearInterval(timer)
  }, [hasRunning])

  // 自动跟随：只在贴近底部时滚——你往上翻历史时不该被拽回来
  useEffect(() => {
    const box = boxRef.current
    if (!box) return
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 160
    if (nearBottom || pending) endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [msgs, pending])

  const toggle = (i: number) => {
    onSelect(i)
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })
  }

  // 思考块的展开只动本地状态，不 onSelect——右侧详情面板只认工具行，
  // 拿一个思考块的下标过去，它只会开出一个空面板。
  const toggleReasoning = (i: number) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })
  }

  return (
    <div className="timeline" ref={boxRef}>
      <div className="thread">
        {msgs.map((m, i) => {
          // 最后一条淡入：新消息落到底部时给一点"接上了"的感觉。
          // 用显式类而不是 `:last-child`，因为 .thread 末尾还有个滚动锚点 div。
          const enter = i === msgs.length - 1 ? ' enter' : ''
          if (m.kind === 'tool') {
            const open = expanded.has(i)
            const changed = (m.added ?? 0) + (m.removed ?? 0) > 0
            return (
              <div
                key={i}
                className={(selected === i ? 'tool-row selected' : 'tool-row') + enter}
              >
                <button className="tool-head" onClick={() => toggle(i)}>
                  <span className="tool-caret">{open ? '▾' : '▸'}</span>
                  <span className="tool-name">{m.name}</span>
                  {m.detail && <span className="tool-detail">{m.detail}</span>}
                  <span className="tool-tail">
                    {/* 还没回结果 = 正在跑。带秒数，是因为"卡住"和"在算"看起来一样。
                        但如果这张卡正等着你批，说"运行中"就是撒谎——它压根还没开始跑。 */}
                    {m.body === undefined && m.at ? (
                      i === lastRunning && waitingApproval ? (
                        <span className="tool-waiting">等待批准</span>
                      ) : (
                        <span className="tool-running">
                          <span className="spin" aria-hidden="true" />
                          运行中 · {Math.max(0, Math.round((now - m.at) / 1000))}s
                        </span>
                      )
                    ) : null}
                    {/* 折叠态就带上「改了多少」——不点开也知道这一下动了什么 */}
                    {changed && (
                      <>
                        <span className="delta-add">+{m.added}</span>
                        <span className="delta-del">−{m.removed}</span>
                      </>
                    )}
                    {m.status && m.status !== 'ok' && (
                      <span className="tool-badge">{toolStatusZh(m.status)}</span>
                    )}
                  </span>
                </button>
                {open &&
                  (m.diff ? (
                    <DiffBlock diff={m.diff} />
                  ) : m.body ? (
                    <pre className="tool-body">{m.body}</pre>
                  ) : null)}
              </div>
            )
          }
          if (m.kind === 'reasoning') {
            // 默认收起：思考过程经常比回答长三倍，铺开会把正文冲出屏幕。
            // 标题上带字数，收着也知道它想了多少。
            const open = expanded.has(i)
            return (
              <div key={i} className={"msg-reasoning" + enter}>
                <button className="reasoning-head" onClick={() => toggleReasoning(i)}>
                  <span className="tool-caret">{open ? '▾' : '▸'}</span>
                  <span>思考过程</span>
                  <span className="reasoning-size">{m.text.length} 字</span>
                </button>
                {open && <div className="reasoning-body">{m.text}</div>}
              </div>
            )
          }
          if (m.kind === 'hook') {
            return (
              <div key={i} className={"line-warn" + enter}>
                {m.text}
              </div>
            )
          }
          if (m.kind === 'notice') {
            return (
              <div key={i} className={"line-note" + enter}>
                {m.text}
              </div>
            )
          }
          if (m.kind === 'error') {
            return (
              <div key={i} className={"line-error" + enter}>
                {m.text}
              </div>
            )
          }
          if (m.kind === 'user') {
            return (
              <div key={i} className={"msg-user" + enter}>
                {m.text}
              </div>
            )
          }
          return (
            <div key={i} className={"msg-assistant" + enter}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.text}</ReactMarkdown>
            </div>
          )
        })}

        {pending && (
          <div className="line-note">⋯ 等待模型响应</div>
        )}
        <div ref={endRef} />
      </div>
    </div>
  )
}
