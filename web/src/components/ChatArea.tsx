import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
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
          if (m.kind === 'tool') {
            const open = expanded.has(i)
            return (
              <div key={i} className={selected === i ? 'tool-row selected' : 'tool-row'}>
                <button className="tool-head" onClick={() => toggle(i)}>
                  <span className="tool-caret">{open ? '▾' : '▸'}</span>
                  <span className="tool-name">{m.name}</span>
                  {m.detail && <span className="tool-detail">{m.detail}</span>}
                </button>
                {open && m.body && <div className="tool-body">{m.body}</div>}
              </div>
            )
          }
          if (m.kind === 'reasoning') {
            return (
              <div key={i} className="msg-reasoning">
                {m.text}
              </div>
            )
          }
          if (m.kind === 'hook') {
            return (
              <div key={i} className="line-warn">
                {m.text}
              </div>
            )
          }
          if (m.kind === 'notice') {
            return (
              <div key={i} className="line-note">
                {m.text}
              </div>
            )
          }
          if (m.kind === 'error') {
            return (
              <div key={i} className="line-error">
                {m.text}
              </div>
            )
          }
          if (m.kind === 'user') {
            return (
              <div key={i} className="msg-user">
                {m.text}
              </div>
            )
          }
          return (
            <div key={i} className="msg-assistant">
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
