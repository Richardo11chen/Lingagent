import type { TraceRec } from './types'

export const fmtMs = (v?: number) =>
  v == null ? '' : v < 1000 ? `${Math.round(v)}ms` : `${(v / 1000).toFixed(1)}s`

const STATUS_ZH: Record<string, string> = { ok: '成功', denied: '驳回', blocked: '拦截', error: '错误' }
const DECISION_ZH: Record<string, string> = { approved: '已批准', denied: '已驳回' }

/** 轨迹行文案：口径与 TUI 的 /trace 一致（同一个事件在两端说同一句话） */
export function traceRow(r: TraceRec): { label: string; detail: string } {
  if (r.kind === 'model')
    return {
      label: '● 模型',
      detail: r.error ? `失败 · ${r.error}` : `step ${r.step} · 工具调用 ×${r.n_actions ?? '?'}`,
    }
  if (r.kind === 'tool')
    return {
      label: `⚙ ${r.tool ?? ''}`,
      detail: `${STATUS_ZH[r.status ?? ''] ?? r.status ?? ''} · ${r.args?.command ?? r.args?.path ?? ''}${
        r.error ? ' ' + r.error : ''
      }`,
    }
  if (r.kind === 'approval')
    return {
      label: '⚠ 审批',
      detail: `${DECISION_ZH[r.decision ?? ''] ?? r.decision ?? ''} · ${(r.command ?? '').slice(0, 60)}`,
    }
  if (r.kind === 'auto') return { label: '⚡ 自动批准', detail: r.reason ?? '' }
  if (r.kind === 'compact')
    return {
      label: '⟳ 压缩',
      detail: r.before != null && r.after != null ? `${r.before} → ${r.after} 字符` : (r.text ?? ''),
    }
  if (r.kind === 'hook')
    return {
      label: '⚡ 策略',
      detail: `${r.verdict === 'deny' ? '拦下' : '放行'}（${r.by ?? ''}）${r.reason ? ' · ' + r.reason : ''}`,
    }
  return { label: r.kind, detail: '' }
}

export const fmtVal = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v))
