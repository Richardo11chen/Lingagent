import type { Msg } from '../lib/types'

type Props = {
  msg: Msg | null
  onClose: () => void
}

export default function DetailsPanel({ msg, onClose }: Props) {
  const tool = msg && msg.kind === 'tool' ? msg : null

  return (
    <aside className="details">
      <div className="details-head">
        <span className="grow">详情</span>
        <button className="collapse-btn" onClick={onClose} title="收起详情" aria-label="收起详情">
          »
        </button>
      </div>

      <div className="details-body">
        {!tool ? (
          <div className="details-empty">
            点左边任意一条 <code>▸ 工具</code>，这里显示它的命令、输出和改动。
          </div>
        ) : (
          <>
            <div className="details-section">
              <div className="details-label">工具</div>
              <div className="details-pre">{tool.name}</div>
            </div>
            {tool.detail && (
              <div className="details-section">
                <div className="details-label">参数</div>
                <div className="details-pre">{tool.detail}</div>
              </div>
            )}
            {tool.body && (
              <div className="details-section">
                <div className="details-label">输出</div>
                <div className="details-pre">{tool.body}</div>
              </div>
            )}
            {!tool.body && (
              <div className="details-empty">
                输出与 diff 还没接到前端——后端目前只推「工具开始」，没有「工具结果」。
              </div>
            )}
          </>
        )}
      </div>
    </aside>
  )
}
