import type { Session } from '../lib/types'

type Props = {
  sessions: Session[]
  current: string
  collapsed: boolean
  onToggle: () => void
  onPick: (id: string) => void
  onNew: () => void
  onReload: () => void
}

export default function Sidebar({ sessions, current, collapsed, onToggle, onPick, onNew, onReload }: Props) {
  return (
    <aside className={collapsed ? 'sidebar collapsed' : 'sidebar'}>
      <div className="sidebar-head">
        <span className="grow">会话</span>
        <button
          className="collapse-btn"
          onClick={onToggle}
          title={collapsed ? '展开侧栏' : '收起侧栏'}
          aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
        >
          {collapsed ? '»' : '«'}
        </button>
      </div>

      <div className="session-list">
        {sessions.map((s) => (
          <button
            key={s.id}
            className={s.id === current ? 'session active' : 'session'}
            onClick={() => onPick(s.id)}
            title={s.preview || '(空会话)'}
          >
            <span className="preview">{s.preview || '(空会话)'}</span>
          </button>
        ))}
        {sessions.length === 0 && (
          <div className="details-empty">
            还没有历史会话
            <button className="btn sm" onClick={onReload}>
              刷新列表
            </button>
          </div>
        )}
      </div>

      <div className="sidebar-foot">
        <button className="new-session" onClick={onNew}>
          ＋ 新会话
        </button>
      </div>
    </aside>
  )
}
