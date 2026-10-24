// 前端用到的全部数据形状：WS 事件、时间线条目、轨迹记录。

export type Session = { id: string; preview: string }

export type TraceRec = {
  ts: string
  kind: string
  step?: number
  n_actions?: number
  tool?: string
  args?: { command?: string; path?: string }
  status?: string
  result?: string
  error?: string
  command?: string
  decision?: string
  verdict?: string
  text?: string
  before?: number
  after?: number
  by?: string
  reason?: string
  duration_ms?: number
}

/** 后端推来的事件（server.py 的 send(...) 是唯一来源） */
export type Ev =
  | { type: 'session'; id: string; history_n: number; resumed?: boolean; model?: string }
  | { type: 'sessions'; list: Session[] }
  | { type: 'history'; messages: { kind: 'user' | 'assistant' | 'tool'; text: string; name?: string }[] }
  | { type: 'assistant_chunk'; text: string }
  | { type: 'assistant_done'; text: string }
  | { type: 'tool'; name: string; detail?: string }
  | { type: 'reasoning'; text: string }
  | { type: 'models'; list: string[]; current: string }
  | { type: 'model_switched'; model: string; error?: string }
  | { type: 'trace'; list: TraceRec[]; total: number }
  | { type: 'hook'; by: string; reason: string }
  | { type: 'notice'; text: string }
  | { type: 'approval_request'; command: string; reason?: string }
  | { type: 'error'; text: string }

/** 时间线上的一条。tool 行带 index，用来和右侧详情对上 */
export type Msg =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string; streaming?: boolean }
  | { kind: 'tool'; name: string; detail?: string; body?: string }
  | { kind: 'reasoning'; text: string }
  | { kind: 'hook'; text: string }
  | { kind: 'notice'; text: string }
  | { kind: 'error'; text: string }
