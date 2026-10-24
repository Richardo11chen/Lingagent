import DiffBlock from './DiffBlock'
import { fmtMs, toolStatusZh } from '../lib/format'
import type { Msg } from '../lib/types'

type Props = {
  msg: Msg | null
  onClose: () => void
}

export default function DetailsPanel({ msg, onClose }: Props) {
  const tool = msg && msg.kind === 'tool' ? msg : null
  const changed = tool ? (tool.added ?? 0) + (tool.removed ?? 0) > 0 : false

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
              <div className="details-label">
                工具
                {tool.status && tool.status !== 'ok' && (
                  <span className="tool-badge">{toolStatusZh(tool.status)}</span>
                )}
                {tool.ms != null && <span className="details-ms">{fmtMs(tool.ms)}</span>}
              </div>
              <div className="details-pre">{tool.name}</div>
            </div>

            {tool.detail && (
              <div className="details-section">
                <div className="details-label">参数</div>
                <div className="details-pre">{tool.detail}</div>
              </div>
            )}

            {changed && (
              <div className="details-section">
                <div className="details-label">
                  改动
                  <span className="delta-add">+{tool.added}</span>
                  <span className="delta-del">−{tool.removed}</span>
                </div>
                <DiffBlock diff={tool.diff ?? ''} />
              </div>
            )}

            <div className="details-section">
              <div className="details-label">输出</div>
              {tool.body ? (
                <div className="details-pre">{tool.body}</div>
              ) : (
                <div className="details-empty">还没有结果——这一条可能是历史记录，或正在执行。</div>
              )}
            </div>
          </>
        )}
      </div>
    </aside>
  )
}
