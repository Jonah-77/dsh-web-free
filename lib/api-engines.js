/**
 * 免费「API 引擎」：给 free provider 的搜索链加两层高质量后端。
 *
 * ## 为什么加
 *
 * 原来的链路只有 `websearch.py` 抓搜索引擎 HTML：能出标题，但 snippet 普遍是空的
 * （DDG 在国内直连不通、必应/神马结构常变），模型拿到结果还得再 fetch 一遍才知道讲什么。
 * 这两个引擎返回的是**带正文片段的真结果**，直接补掉这个短板。
 *
 * ## 两个引擎的定位（实测 2026-10-30）
 *
 * - **Exa 匿名 MCP**（mcp.exa.ai）：本机**直连可用**（约 0.7s），无需 key、无需代理。
 *   中文招聘类查询返回完整正文片段 + 发布日期。→ 首选。
 * - **Tavily keyless**（x-tavily-access-mode: keyless）：本机直连不通，**必须走本机代理**
 *   （127.0.0.1:7890 实测可用）。唯一能真正做时间过滤（time_range）的引擎。→ 次选。
 *
 * 两个都失败才落回抓 HTML 的脚本，所以多了这两层只会「更不容易失败」，不会更容易。
 *
 * ## 为什么 tavily 用 spawn curl 而不是 fetch
 *
 * Node 内置 fetch 不支持指定代理，走代理要引 undici 或自己写 CONNECT 隧道；
 * 而系统自带的 curl 天然支持 `-x`。保持插件零 import + 零依赖的做法。
 *
 * ## 时间过滤
 *
 * 官方 `web_search` 的 schema 里没有 timeRange 参数，也不能改（那是 DSH 核心的工具）。
 * 所以改由 provider 自己从查询词里认出「最近一周 / 本月 / 最近3天 / last 7 days」这类说法，
 * 换算成 tavily 的 time_range（真正生效）和 exa 的 objective 提示（软过滤）。
 * 模型什么都不用改，照常说「帮我搜最近一周的新闻」就能生效。
 *
 * @module dsh-web-free/api-engines
 */

import { spawn } from 'node:child_process'

const EXA_URL = 'https://mcp.exa.ai/mcp'
const TAVILY_URL = 'https://api.tavily.com/search'

/** exa 的 objective 是必填字段，缺省时给一句中性描述。 */
const DEFAULT_OBJECTIVE = 'Find pages that directly answer the query. '
  + 'Prefer authoritative, recently updated pages.'

/** 时间窗口 → tavily 的 time_range（它只认 day/week/month/year 四档）。 */
function daysToTimeRange(days) {
  if (days <= 2) return 'day'
  if (days <= 14) return 'week'
  if (days <= 90) return 'month'
  return 'year'
}

const CN_DIGITS = {
  一: 1, 两: 2, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10, 半: 0.5,
}

function toNumber(raw) {
  const text = String(raw ?? '').trim()
  if (/^\d+(\.\d+)?$/u.test(text)) return Number(text)
  if (Object.hasOwn(CN_DIGITS, text)) return CN_DIGITS[text]
  // 十一 / 二十 / 二十四 这类
  const m = /^([一二两三四五六七八九])?十([一二三四五六七八九])?$/u.exec(text)
  if (m !== null) {
    const tens = m[1] === undefined ? 1 : CN_DIGITS[m[1]]
    const ones = m[2] === undefined ? 0 : CN_DIGITS[m[2]]
    return tens * 10 + ones
  }
  return null
}

/**
 * 从一句自然语言查询里认出时间窗口。
 *
 * 认得：今天 / 昨天 / 前天 / 最近N天|周|个月|年 / 近N天 / 本周 / 本月 / 今年 /
 *       绝对年月（2026-07、2026年7月）/ last N days / past week / this month。
 * 认不出返回 null —— 调用方按「没有时间限制」处理。
 *
 * @param {string} query 用户/模型的原始查询词
 * @returns {{days: number|null, timeRange: string|null, anchor: string|null, label: string}|null}
 *   days=窗口天数（绝对日期时为 null）；anchor=绝对起始日期 YYYY-MM-DD
 */
export function parseTimeHint(query) {
  const q = String(query ?? '')
  if (q.length === 0) return null

  // 绝对日期：2026-07 / 2026年7月 / 2026-07-15（含）之后
  const abs = /(\d{4})\s*[年/-]\s*(\d{1,2})(?:\s*[月/-]\s*(\d{1,2}))?/u.exec(q)
  if (abs !== null) {
    const y = Number(abs[1])
    const mo = Number(abs[2])
    // 「2026 年」这种只有年份的不当作时间过滤（多半是标题里的年份，比如「2027 届校招」）
    if (abs[3] !== undefined && y >= 2000 && y <= 2100 && mo >= 1 && mo <= 12) {
      const d = Number(abs[3])
      const anchor = `${y}-${String(mo).padStart(2, '0')}-${String(d).padStart(2, '0')}`
      return { days: null, timeRange: null, anchor, label: `自 ${anchor}` }
    }
    if (abs[3] === undefined && y >= 2000 && y <= 2100 && mo >= 1 && mo <= 12 && /月|月?份?以[后来]|since/iu.test(q)) {
      const anchor = `${y}-${String(mo).padStart(2, '0')}-01`
      return { days: null, timeRange: null, anchor, label: `自 ${anchor}` }
    }
  }

  // 相对：最近 / 近 / 过去 / 这 N 天|周|月|年
  const rel = /(?:最近|近|过去|这|前)\s*([0-9]+(?:\.[0-9]+)?|[一二两三四五六七八九十半]+)\s*(个)?\s*(小时|天|日|周|星期|礼拜|月|年)/u.exec(q)
  if (rel !== null) {
    const n = toNumber(rel[1])
    const unit = rel[3]
    if (n !== null && n > 0) {
      const days = unit === '小时' ? n / 24
        : (unit === '天' || unit === '日') ? n
          : (unit === '周' || unit === '星期' || unit === '礼拜') ? n * 7
            : unit === '月' ? n * 30 : n * 365
      return { days, timeRange: daysToTimeRange(days), anchor: null, label: `最近 ${rel[1]}${unit}` }
    }
  }

  // 固定说法
  const fixed = [
    [/今天|今日|当天/u, 1, '今天'],
    [/昨天|昨日/u, 2, '昨天'],
    [/前天/u, 3, '前天'],
    [/本周|这周|这一周|这个星期/u, 7, '本周'],
    [/本月|这个月|当月/u, 30, '本月'],
    [/今年|本年度|当年/u, 365, '今年'],
    [/\b(?:last|past)\s+(\d+)\s+(day|days|week|weeks|month|months|year|years)\b/iu, null, 'last N'],
    [/\b(?:this|last|past)\s+(day|week|month|year)\b/iu, null, 'this/last/past'],
  ]
  for (const [re, days, label] of fixed) {
    const m = re.exec(q)
    if (m === null) continue
    if (days !== null) return { days, timeRange: daysToTimeRange(days), anchor: null, label }
    // 英文带数字的形式
    if (m[2] !== undefined) {
      const n = Number(m[1])
      const unit = String(m[2]).toLowerCase()
      const d = unit.startsWith('day') ? n : unit.startsWith('week') ? n * 7
        : unit.startsWith('month') ? n * 30 : n * 365
      if (Number.isFinite(d) && d > 0) {
        return { days: d, timeRange: daysToTimeRange(d), anchor: null, label: m[0] }
      }
    }
    // this/last week|month|year
    const unit = String(m[1] ?? '').toLowerCase()
    const d = unit.startsWith('day') ? 1 : unit.startsWith('week') ? 7
      : unit.startsWith('month') ? 30 : 365
    return { days: d, timeRange: daysToTimeRange(d), anchor: null, label: m[0] }
  }
  return null
}

/** 把 SSE（`event: message` + `data: {...}`）或裸 JSON 统一解析成对象。 */
function parseMcpPayload(text) {
  const trimmed = text.trimStart()
  if (trimmed.startsWith('{')) return JSON.parse(trimmed)
  const chunks = []
  for (const line of text.split('\n')) {
    const m = /^data:\s*(.*)$/u.exec(line)
    if (m !== null && m[1].length > 0) chunks.push(m[1])
  }
  if (chunks.length === 0) throw new Error(`返回不是 SSE 也不是 JSON：${text.slice(0, 120)}`)
  return JSON.parse(chunks.join(''))
}

/** 一行错误信息（塞进 problems 时不要把整段堆栈搬过去）。 */
function brief(error) {
  return String(error?.message ?? error).split('\n')[0].slice(0, 200)
}

/**
 * Exa 匿名 MCP 搜索。直连即可，不需要 key。
 *
 * @param {object} opts 选项
 * @param {string} opts.query 查询词
 * @param {string} [opts.objective] 排序目标（可写时间偏好做软过滤）
 * @param {number} opts.limit 条数
 * @param {number} opts.timeoutMs 超时
 * @param {AbortSignal} [opts.signal] 取消信号
 * @returns {Promise<Array<{url: string, title?: string, snippet?: string}>>}
 */
export async function exaSearch({ query, objective, limit, timeoutMs, signal }) {
  const body = {
    jsonrpc: '2.0',
    id: 1,
    method: 'tools/call',
    params: {
      name: 'web_search_exa',
      arguments: {
        query,
        objective: typeof objective === 'string' && objective.length > 0 ? objective : DEFAULT_OBJECTIVE,
        numResults: limit,
      },
    },
  }
  const signals = [AbortSignal.timeout(timeoutMs)]
  if (signal !== undefined) signals.push(signal)
  let response
  try {
    response = await fetch(EXA_URL, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'accept': 'application/json, text/event-stream',
      },
      body: JSON.stringify(body),
      signal: AbortSignal.any(signals),
    })
  } catch (error) {
    if (signal?.aborted === true) throw error
    throw new Error(`exa 网络不通：${brief(error)}`)
  }
  if (!response.ok) throw new Error(`exa HTTP ${response.status}`)
  const payload = parseMcpPayload(await response.text())
  if (payload?.error !== undefined) throw new Error(`exa 报错：${brief(payload.error)}`)
  if (payload?.result?.isError === true) {
    throw new Error(`exa 返回 isError：${brief(payload.result.content?.[0]?.text)}`)
  }
  const text = payload?.result?.content?.find?.((c) => typeof c?.text === 'string')?.text ?? ''
  return parseExaBlocks(text).slice(0, limit)
}

/**
 * Exa 的返回是一整段拼接文本，每个结果以 `Title: / URL: / Published: / Highlights:` 开头，
 * 块之间用 `---` 分隔。按块拆成结构化结果。
 * @param {string} text 拼接文本
 * @returns {Array<{url: string, title?: string, snippet?: string}>} 结果
 */
function parseExaBlocks(text) {
  const sources = []
  for (const block of String(text).split(/\n\s*---\s*\n/u)) {
    const url = /^\s*URL:\s*(\S+)\s*$/mu.exec(block)?.[1]
    if (url === undefined || url.length === 0) continue
    const title = /^\s*Title:\s*(.+)$/mu.exec(block)?.[1]?.trim()
    const published = /^\s*Published:\s*(.+)$/mu.exec(block)?.[1]?.trim()
    const idx = block.indexOf('Highlights:')
    let snippet = idx >= 0 ? block.slice(idx + 'Highlights:'.length) : ''
    snippet = snippet.replace(/\s+/gu, ' ').trim()
    // 有真实发布日期就前置一段，方便判断时效（Published 常为 N/A）
    const dateLabel = published !== undefined && /^\d{4}-\d{2}-\d{2}/u.test(published)
      ? `[${published.slice(0, 10)}] ` : ''
    sources.push({
      url,
      ...(title !== undefined && title.length > 0 ? { title } : {}),
      ...(snippet.length > 0 ? { snippet: `${dateLabel}${snippet}` } : {}),
    })
  }
  return sources
}

/**
 * Tavily keyless 搜索。本机直连不通，必须经本机代理（用系统 curl 发请求）。
 *
 * @param {object} opts 选项
 * @param {string} opts.query 查询词
 * @param {number} opts.limit 条数
 * @param {string|null} opts.timeRange day|week|month|year 或不限
 * @param {string|null} opts.proxy 代理地址，null = 直连
 * @param {string} opts.curl curl 可执行文件路径
 * @param {number} opts.timeoutMs 超时
 * @param {AbortSignal} [opts.signal] 取消信号
 * @returns {Promise<Array<{url: string, title?: string, snippet?: string}>>}
 */
export async function tavilySearch({ query, limit, timeRange, proxy, curl, timeoutMs, signal }) {
  const payload = {
    query,
    max_results: Math.min(20, Math.max(1, limit)),
    search_depth: 'basic',
    ...(timeRange !== null && timeRange !== undefined ? { time_range: timeRange } : {}),
  }
  const args = [
    '-s', '-X', 'POST', TAVILY_URL,
    '-H', 'content-type: application/json',
    '-H', 'x-tavily-access-mode: keyless',
    '--max-time', String(Math.max(3, Math.round(timeoutMs / 1000))),
    '--data-binary', '@-',
  ]
  if (typeof proxy === 'string' && proxy.length > 0) args.push('-x', proxy)

  const text = await runCurl(curl, args, JSON.stringify(payload), timeoutMs, signal)
  let data
  try {
    data = JSON.parse(text)
  } catch {
    throw new Error(`tavily 返回无法解析：${text.slice(0, 120)}`)
  }
  if (data?.error !== undefined) throw new Error(`tavily 报错：${brief(data.error)}`)
  const rows = Array.isArray(data?.results) ? data.results : []
  return rows.slice(0, limit).map((row) => ({
    url: String(row?.url ?? ''),
    ...(typeof row?.title === 'string' && row.title.length > 0 ? { title: row.title } : {}),
    ...(typeof row?.content === 'string' && row.content.length > 0 ? { snippet: row.content } : {}),
  })).filter((row) => row.url.length > 0)
}

/**
 * 用系统 curl 发一次 POST，body 从 stdin 进（免得 shell 引号地狱）。
 * @param {string} curl curl 路径
 * @param {string[]} args curl 参数
 * @param {string} body 请求体
 * @param {number} timeoutMs 墙钟超时（curl 自己也有 --max-time）
 * @param {AbortSignal} [signal] 取消信号
 * @returns {Promise<string>} stdout
 */
function runCurl(curl, args, body, timeoutMs, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted === true) {
      reject(new Error('搜索已取消'))
      return
    }
    let child
    try {
      child = spawn(curl, args, { stdio: ['pipe', 'pipe', 'pipe'] })
    } catch (error) {
      reject(new Error(`无法启动 curl（${curl}）：${String(error)}`))
      return
    }
    let out = ''
    let err = ''
    let settled = false
    const finish = (fn, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal?.removeEventListener?.('abort', onAbort)
      fn(value)
    }
    const onAbort = () => {
      try { child.kill('SIGKILL') } catch { /* 已退出 */ }
      finish(reject, new Error('搜索已取消'))
    }
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch { /* 已退出 */ }
      finish(reject, new Error(`curl 超过 ${timeoutMs}ms 未返回`))
    }, timeoutMs + 2000)
    signal?.addEventListener?.('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => { out += chunk })
    child.stderr.on('data', (chunk) => { err += chunk })
    child.on('error', (error) => finish(reject, new Error(`curl 运行失败：${String(error)}`)))
    child.stdin.on('error', () => { /* EPIPE：curl 提前退出，交给 close 处理 */ })
    child.on('close', (code) => {
      const stderr = err.trim()
      // curl 的网络类失败退出码非 0；HTTP 错误码不会让 curl 失败，所以只认退出码
      if ((code ?? 0) !== 0 && out.trim().length === 0) {
        finish(reject, new Error(`curl 退出码 ${code}${stderr.length > 0 ? `：${stderr.slice(0, 160)}` : ''}`))
        return
      }
      finish(resolve, out)
    })
    try {
      child.stdin.end(body)
    } catch { /* 上面 on('error') 会兜 */ }
  })
}
