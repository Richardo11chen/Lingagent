import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import DiffBlock from './DiffBlock'
import { toolStatusZh } from '../lib/format'
import type { Msg } from '../lib/types'

type Props = {
  msgs: Msg[]
  pending: boolean
  selected: number | null
  onSelect: (i: number) => void
}

export default function ChatArea({ msgs, pending, selected, onSelect }: Props) {
  const endRef = useRef<HTMLDivElement>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState<Set<number>>(new Set())

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
            return (
              <div key={i} className={"msg-reasoning" + enter}>
                {m.text}
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
