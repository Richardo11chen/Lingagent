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

/**
 * 会话 id → 人能读的时间：`20261024-145431-588700` → `10-24 14:54`。
 *
 * 会话 id 本身是「日期-时间-随机」，界面上直接印 id 等于让人自己拆字符串；
 * 完整 id 留在 title 里，需要精确定位时还能看到。
 */
export function fmtSessionTime(id: string): string {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(id)
  if (!m) return id
  const [, , mm, dd, hh, mi] = m
  return `${mm}-${dd} ${hh}:${mi}`
}

/** 工具状态 → 一个词。ok 不显示（没消息就是好消息），其余才占版面。 */
const TOOL_STATUS_ZH: Record<string, string> = {
  error: '失败',
  denied: '驳回',
  blocked: '拦截',
  running: '执行中',
}

export function toolStatusZh(status: string): string {
  return TOOL_STATUS_ZH[status] ?? status
}

/** 上下文用量：不足 1000 给原数，超过用 8.4k / 70k——顶栏只有一行的预算。 */
export function fmtK(n: number): string {
  if (n < 1000) return String(Math.round(n))
  const k = n / 1000
  return `${k >= 10 ? Math.round(k) : k.toFixed(1).replace(/\.0$/, '')}k`
}

const pad = (n: number) => String(n).padStart(2, '0')

/** 会话 id 的 `YYYYMMDD` 前缀 → 今天 / 昨天 / `MM-DD`。 */
function dayLabel(id: string, today: Date): string {
  const m = /^(\d{4})(\d{2})(\d{2})/.exec(id)
  if (!m) return '更早'
  const key = `${m[1]}${m[2]}${m[3]}`
  const of = (d: Date) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`
  if (key === of(today)) return '今天'
  const y = new Date(today)
  y.setDate(y.getDate() - 1)
  if (key === of(y)) return '昨天'
  return `${m[2]}-${m[3]}`
}

export type SessionGroup = { label: string; items: { id: string; preview: string }[] }

/**
 * 会话列表分组 + 过滤。
 *
 * 后端按**修改时间**倒序发，不是按 id 里的日期——所以同一天的会话可能在列表里被别天隔开
 * （改过一句旧会话，它就跳到最前面）。因此分组用 Map 合并同名标签，而不是"只看上一组的标签"，
 * 否则同一天会出现好几个重复的日期标题。
 */
export function groupSessions<T extends { id: string; preview: string }>(
  list: T[],
  query: string,
): SessionGroup[] {
  const q = query.trim().toLowerCase()
  const today = new Date()
  const groups: SessionGroup[] = []
  const byLabel = new Map<string, SessionGroup>()
  for (const s of list) {
    if (q && !(s.preview || '').toLowerCase().includes(q)) continue
    const label = dayLabel(s.id, today)
    let group = byLabel.get(label)
    if (!group) {
      group = { label, items: [] }
      byLabel.set(label, group)
      groups.push(group)
    }
    group.items.push({ id: s.id, preview: s.preview })
  }
  return groups
}
