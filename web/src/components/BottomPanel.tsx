import { useState } from 'react'
import { fmtMs, fmtVal, traceRow } from '../lib/format'
import type { TraceRec } from '../lib/types'

type Props = {
  trace: { list: TraceRec[]; total: number }
  onRefresh: () => void
  onClose: () => void
}

export default function BottomPanel({ trace, onRefresh, onClose }: Props) {
  const [sel, setSel] = useState<number | null>(null)
  const rec = sel === null ? null : trace.list[sel]

  return (
    <section className="bottom">
      <div className="bottom-head">
        <button className="tab active">轨迹</button>
        <span style={{ flex: 1 }} />
        <span className="trace-time">
          共 {trace.total} 条{trace.total > trace.list.length ? `（显示最近 ${trace.list.length}）` : ''}
        </span>
        <button className="btn sm" onClick={onRefresh}>
          刷新
        </button>
        <button className="btn sm" onClick={onClose}>
          收起
        </button>
      </div>

      <div className="bottom-body">
        {!rec ? (
          trace.list.map((r, i) => {
            const row = traceRow(r)
            return (
              <button key={i} className="trace-row" onClick={() => setSel(i)}>
                <span className="trace-idx">#{i + 1}</span>
                <span className="trace-main">
                  {row.label} {row.detail}
                </span>
                <span className="trace-time">
                  {r.ts.slice(11, 23)} {fmtMs(r.duration_ms)}
                </span>
              </button>
            )
          })
        ) : (
          <>
            <button className="btn sm" onClick={() => setSel(null)} style={{ margin: '2px 8px 8px' }}>
              ← 返回列表
            </button>
            {Object.entries(rec).map(([k, v]) => (
              <div key={k} className="trace-field">
                <div className="k">{k}</div>
                <pre>{fmtVal(v)}</pre>
              </div>
            ))}
          </>
        )}
      </div>
    </section>
  )
}
