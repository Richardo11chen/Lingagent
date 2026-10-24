import { useEffect, useRef } from 'react'

type Props = {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  onStop: () => void
  running: boolean
  connected: boolean
}

export default function Composer({ value, onChange, onSend, onStop, running, connected }: Props) {
  const ref = useRef<HTMLTextAreaElement>(null)

  // 随内容长高，到 200px 封顶（超出就内部滚）
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }, [value])

  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea
          ref={ref}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              onSend()
            }
          }}
          placeholder={connected ? '说点什么…' : '正在连接后端…'}
          rows={1}
          autoFocus
        />
        <div className="composer-row">
          <span className="composer-hint">{running ? '运行中 · 停止只在本步结束后生效' : 'Enter 发送 · Shift+Enter 换行'}</span>
          {running ? (
            <button className="btn sm danger" onClick={onStop}>
              停止
            </button>
          ) : (
            <button className="btn sm primary" onClick={onSend} disabled={!connected}>
              发送
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
