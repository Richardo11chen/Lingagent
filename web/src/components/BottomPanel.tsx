import { useState } from 'react'
import { fmtMs, fmtVal, traceRow } from '../lib/format'
import type { TaskSnap, TraceRec } from '../lib/types'

export type DrawerTab = 'trace' | 'tasks'

type Props = {
  tab: DrawerTab | null
  trace: { list: TraceRec[]; total: number } | null
  tasks: TaskSnap[]
  onTab: (t: DrawerTab) => void
  onRefresh: () => void
  onStopTask: (id: string) => void
}

/**
 * 底部抽屉：轨迹 / 后台任务两个 tab。
 *
 * 收起时只留一条 30px 的 tab 条——这两块都是「平时不占地、要看时整屏」的东西。
 * 后台任务尤其如此：dev server 的输出原来只有模型自己读得到，人看不见。
 */
export default function BottomPanel({ tab, trace, tasks, onTab, onRefresh, onStopTask }: Props) {
  const [sel, setSel] = useState<number | null>(null)
  const rec = trace && sel !== null ? (trace.list[sel] ?? null) : null
  const runningCount = tasks.filter((t) => t.alive).length

  return (
    <section className={tab ? 'bottom' : 'bottom closed'}>
      {tab === 'trace' && (
        <div className="bottom-body">
          {!trace ? (
            <div className="details-empty">还没有拉取轨迹</div>
          ) : !rec ? (
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
              <button className="btn sm back" onClick={() => setSel(null)}>
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
      )}

      {tab === 'tasks' && (
        <div className="bottom-body">
          {tasks.length === 0 ? (
            <div className="details-empty">
              没有后台任务。让 agent 用 <code>run_shell(background=true)</code> 起一个才会出现在这里。
            </div>
          ) : (
            tasks.map((t) => (
              <div key={t.id} className="task">
                <div className="task-head">
                  <span className={t.alive ? 'dot ok' : 'dot'} />
                  <span className="task-id">{t.id}</span>
                  <span className="task-cmd">{t.command}</span>
                  <span className="task-status">
                    {t.alive ? `运行中 ${Math.round(t.elapsed)}s` : `退出码 ${t.exit_code}`}
                  </span>
                  {t.alive && (
                    <button className="btn sm danger" onClick={() => onStopTask(t.id)}>
                      停止
                    </button>
                  )}
                </div>
                <pre className="task-tail">{t.tail.trim() || '（还没有输出）'}</pre>
              </div>
            ))
          )}
        </div>
      )}

      <div className="bottom-tabs">
        <button className={tab === 'trace' ? 'tab active' : 'tab'} onClick={() => onTab('trace')}>
          轨迹
        </button>
        <button className={tab === 'tasks' ? 'tab active' : 'tab'} onClick={() => onTab('tasks')}>
          后台任务
          {/* 运行中的数量常驻在 tab 上：抽屉收着也知道有东西在跑 */}
          {runningCount > 0 && <span className="tab-badge">{runningCount}</span>}
        </button>
        <span className="grow" />
        {tab === 'trace' && trace && (
          <span className="trace-time">
            共 {trace.total} 条{trace.total > trace.list.length ? `（显示最近 ${trace.list.length}）` : ''}
          </span>
        )}
        {tab && (
          <button className="btn sm" onClick={onRefresh}>
            刷新
          </button>
        )}
        {tab && (
          <button className="btn sm" onClick={() => onTab(tab)} title="收起">
            收起
          </button>
        )}
      </div>
    </section>
  )
}
