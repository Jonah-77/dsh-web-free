/**
 * dsh-web-free —— 用本机免费链路顶替 DeepSeek 官方联网（Search API）的插件。
 *
 * ## 解决什么问题
 *
 * DSH 自带的 `web_search` / `web_fetch` 走 `ctx.web` 这个能力缝：
 * 具体干活的是「provider」。出厂默认的搜索 provider 是 `deepseek-official`
 * （DeepSeek 官方 Search API，5 元/千次，要有效 API key），没开就等于联网归零。
 *
 * 本插件注册自己的 provider（id = `free`），把活交给本机的
 * `tools/web/websearch.py`（抓 Bing / DuckDuckGo / 搜狗 / 百度 的 HTML 自己解析），
 * 于是 **`web_search` / `web_fetch` 这两个工具本身、schema、渲染都不用改** ——
 * 对模型来说完全透明，就是换了个后端。
 *
 * ## 两个 provider
 *
 * - search（`free`）：调脚本 `search -n N --json`，映射成 {url,title,snippet}。
 * - fetch（`free`） ：先本进程直连（快、保留 HTML 让 tool-web 转 markdown），
 *   直连不通（TLS 重置 / 超时，比如 wikipedia 这类被墙站点）再交给脚本走代理兜底。
 *
 * ## 为什么 fetch 也要自己写
 *
 * 出厂 `web-fetch-http` 是匿名直连，本机实测 example.com / github 通、
 * en.wikipedia.org 直接 `fetch failed`。而搜索结果的链接里被墙站点很常见，
 * 所以要在直连失败时自动走本机代理，否则搜到也读不到。
 *
 * ## 刻意保持的东西
 *
 * - **零 import**（只用 node 内置模块）：这样插件放在工作区任意目录都能被
 *   `file://` 直接加载，不依赖 `$DSH_HOME/node_modules` 的解析。
 * - **无 Config schema**：配置项直接读 yml 里的 config，改完重启即生效，
 *   少一层 schemastery 依赖，也少一处启动失败的可能。
 * - **不碰内置 provider**：只注册自己的 id，回滚 = 把 web 行的
 *   searchProvider/fetchProvider 改回 deepseek-official / http（或删掉挂载）。
 *
 * @module dsh-web-free
 */

import { spawn, execFileSync } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { exaSearch, tavilySearch, parseTimeHint } from './api-engines.js'

/** 插件目录（lib/ 的上一级）：默认脚本就放在仓库根目录，跟平台无关。 */
const PLUGIN_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))

/** 在 PATH 里找可执行文件；找不到返回 null。用于 python3 / curl 的跨平台默认值。 */
function which(bin) {
  try {
    const out = execFileSync(process.platform === 'win32' ? 'where' : 'which', [bin], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 3000,
    })
    const first = String(out).split(/\r?\n/u)[0]?.trim()
    return first !== undefined && first.length > 0 && existsSync(first) ? first : null
  } catch {
    return null
  }
}

/** 解释器：优先 PATH 里的 python3，再 python；都没有就留 'python3' 让 spawn 报清楚。 */
const DEFAULT_PYTHON = which('python3') ?? which('python') ?? 'python3'

/** 兜底链路脚本：仓库根目录下随包携带，不再写死任何机器的私有路径。 */
const DEFAULT_SCRIPT = join(PLUGIN_ROOT, 'websearch.py')

/** 直连失败后默认走的本机代理（本机有代理时改 config 指向它；默认 off = 只直连）。 */
const DEFAULT_FALLBACK_PROXY = ''

/** 系统 curl：跨平台探测；tavily 需要它支持 -x。 */
const DEFAULT_CURL = which('curl') ?? (process.platform === 'win32' ? 'curl.exe' : 'curl')

const UA = 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36'

const DEFAULTS = {
  python: DEFAULT_PYTHON,
  script: DEFAULT_SCRIPT,
  /** auto = 先直连再代理；off = 只直连；也可以写死 127.0.0.1:7890。 */
  proxy: 'auto',
  /** 直连不通时 fetch 用的代理。 */
  fallbackProxy: DEFAULT_FALLBACK_PROXY,
  /** auto 时按 ddg→bing→sogou→baidu→google 的顺序试。 */
  engine: 'auto',
  /** 一个子进程的总超时。 */
  timeoutMs: 55000,
  /** 传给脚本的单次 HTTP 超时（秒）。 */
  requestTimeoutSec: 12,
  /** fetch 直连的超时。 */
  directTimeoutMs: 9000,
  /** fetch 返回正文的上限（字符）。 */
  maxChars: 200000,
  /** 搜索结果的进程内缓存时长（秒），0 = 关闭。 */
  searchCacheTtlSec: 600,
  /**
   * API 引擎链（按顺序试，都失败才落回脚本抓 HTML）：
   * exa = 匿名 MCP 直连；tavily = keyless、必须走本机代理。off = 只用脚本。
   */
  apiEngines: 'exa,tavily',
  /** exa 匿名 MCP 的单次超时。 */
  exaTimeoutMs: 12000,
  /** tavily keyless 的单次超时（要经代理，比 exa 慢）。 */
  tavilyTimeoutMs: 15000,
  /** 每条结果的摘要上限（字符）。太长会把上下文塞满，官方 API 一般也是这个量级。 */
  snippetChars: 300,
  /** 走代理时用的系统 curl（Node 的 fetch 不支持指定代理）；跨平台自动探测。 */
  curl: DEFAULT_CURL,
}

const SEARCH_CACHE = new Map()

// ─────────────────────────── 工具函数 ───────────────────────────

function resolveConfig(raw) {
  const cfg = { ...DEFAULTS, ...(raw ?? {}) }
  cfg.proxy = String(cfg.proxy ?? 'auto')
  cfg.fallbackProxy = String(cfg.fallbackProxy ?? DEFAULT_FALLBACK_PROXY)
  return cfg
}

  /** 子进程环境：去掉写沙箱的 LD_PRELOAD，补上 Python UTF-8 与可写 TMPDIR。 */
function childEnv(cfg) {
  const env = { ...process.env }
  delete env.LD_PRELOAD
  delete env.BOO_SANDBOX_LIB
  env.PYTHONIOENCODING = 'utf-8'
  env.PYTHONUTF8 = '1'
  env.DSH_WEB_TIMEOUT = String(cfg.requestTimeoutSec)
  const tmp = join(dirOf(cfg.script), '.cache', 'tmp')
  try {
    mkdirSync(tmp, { recursive: true })
    env.TMPDIR = tmp
  } catch { /* 用继承的 TMPDIR */ }
  return env
}

function dirOf(path) {
  const i = path.lastIndexOf('/')
  return i > 0 ? path.slice(0, i) : '.'
}

/**
 * 跑一次脚本，返回 { code, stdout, stderr }。中止信号会杀掉子进程。
 * @param {object} cfg 已合并的配置
 * @param {string[]} args 传给脚本的参数（不含解释器与脚本路径）
 * @param {AbortSignal} [signal] 调用方的取消信号
 */
function runScript(cfg, args, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted === true) {
      reject(new Error('搜索已取消'))
      return
    }
    let child
    try {
      child = spawn(cfg.python, [cfg.script, ...args], {
        cwd: dirOf(cfg.script),
        env: childEnv(cfg),
        stdio: ['ignore', 'pipe', 'pipe'],
      })
    } catch (error) {
      reject(new Error(`无法启动 python3（${cfg.python}）：${String(error)}`))
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
      finish(reject, new Error(`websearch.py 超过 ${cfg.timeoutMs}ms 未返回，已中止`))
    }, cfg.timeoutMs)
    signal?.addEventListener?.('abort', onAbort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => { out += chunk })
    child.stderr.on('data', (chunk) => { err += chunk })
    child.on('error', (error) => {
      finish(reject, new Error(`python3 运行失败：${String(error)}`))
    })
    child.on('close', (code) => {
      finish(resolve, { code: code ?? -1, stdout: out, stderr: err })
    })
  })
}

/** 跑脚本并解析它输出的 JSON；解析失败时把 stderr 尾巴带进错误里。 */
async function runJson(cfg, args, signal) {
  const { code, stdout, stderr } = await runScript(cfg, args, signal)
  try {
    return JSON.parse(stdout)
  } catch {
    const tail = `${stderr || stdout}`.trim().slice(-400)
    throw new Error(`websearch.py 输出无法解析（exit ${code}）：${tail || '（无输出）'}`)
  }
}

/** 错误压成一行，塞进逐条失败原因时不带整段堆栈。 */
function firstLine(error) {
  return String(error?.message ?? error).split('\n')[0].slice(0, 200)
}

function clamp(value, lo, hi) {
  const n = Number(value)
  if (!Number.isFinite(n)) return lo
  return Math.min(hi, Math.max(lo, Math.trunc(n)))
}

/** 按 Content-Type → <meta charset> 的顺序猜编码，猜不到用 utf-8。 */
function decodeBody(bytes, contentType) {
  const labels = []
  const ct = String(contentType ?? '')
  const fromHeader = /charset=["']?([\w-]+)/iu.exec(ct)
  if (fromHeader) labels.push(fromHeader[1])
  const head = new TextDecoder('utf-8', { fatal: false })
    .decode(bytes.subarray(0, Math.min(bytes.length, 4096)))
  const fromMeta = /charset=["']?([\w-]+)/iu.exec(head)
  if (fromMeta) labels.push(fromMeta[1])
  labels.push('utf-8')
  for (const label of labels) {
    try {
      return new TextDecoder(label, { fatal: false }).decode(bytes)
    } catch { /* 不认识的编码名，下一个 */ }
  }
  return new TextDecoder('utf-8', { fatal: false }).decode(bytes)
}

/** 读响应体但设上限，避免把几百 MB 的视频拉进内存。 */
async function readCapped(response, maxBytes) {
  const body = response.body
  if (body === null || body === undefined) return { bytes: new Uint8Array(0), truncated: false }
  const reader = body.getReader()
  const chunks = []
  let total = 0
  let truncated = false
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done === true) break
      if (value === undefined) continue
      chunks.push(value)
      total += value.byteLength
      if (total >= maxBytes) {
        truncated = true
        try { await reader.cancel() } catch { /* 忽略 */ }
        break
      }
    }
  } finally {
    try { reader.releaseLock() } catch { /* 忽略 */ }
  }
  const bytes = new Uint8Array(Math.min(total, maxBytes))
  let offset = 0
  for (const chunk of chunks) {
    if (offset >= bytes.length) break
    const slice = chunk.subarray(0, bytes.length - offset)
    bytes.set(slice, offset)
    offset += slice.byteLength
  }
  return { bytes, truncated }
}

function looksLikeHtml(text, contentType) {
  const ct = String(contentType ?? '').toLowerCase()
  if (ct.includes('html') || ct.includes('xml')) return true
  return /^\s*<(!doctype|html|head|body)/iu.test(text.slice(0, 200))
}

function kindOf(text, contentType) {
  return looksLikeHtml(text, contentType) ? 'html' : 'text'
}

// ─────────────────────────── 搜索 provider ───────────────────────────

/**
 * 免费搜索：把查询交给 websearch.py，映射成 seam 的 sources[]。
 * 中文查询用 DDG 最准（脚本里排的顺序就是这么来的），失败自动换下一个引擎。
 */
class FreeSearchProvider {
  id = 'free'

  /** tavily 失败后的冷却截止时间（毫秒时间戳）。代理没开时它必挂，免得每次查询白等一轮。 */
  tavilyDownUntil = 0

  constructor(getConfig) {
    this.getConfig = getConfig
  }

  available() {
    const cfg = this.getConfig()
    if (this.apiChain(cfg).length > 0 && existsSync(cfg.curl === undefined ? '' : cfg.curl)) return true
    return existsSync(cfg.python) && existsSync(cfg.script)
  }

  async search(request, signal) {
    const cfg = this.getConfig()
    const limit = clamp(request.maxResults ?? 10, 1, 20)
    const key = `${limit}\u0000${request.query}`
    const ttl = Number(cfg.searchCacheTtlSec) || 0
    if (ttl > 0) {
      const hit = SEARCH_CACHE.get(key)
      if (hit !== undefined && Date.now() - hit.at < ttl * 1000) {
        return { sources: hit.sources, truncated: false }
      }
    }

    // 查询词里的时间说法（「最近一周」「本月」「last 7 days」）在这里被认出来：
    // 官方 web_search 的 schema 没有 timeRange 参数、也不能改，所以由 provider 自己换算。
    const hint = parseTimeHint(request.query)
    const problems = []

    // ① 免费 API 引擎：结果自带正文片段，质量高于抓 HTML，所以排在最前面。
    //    两个引擎都失败（限流/网络）才往下走，不会比原来更容易失败。
    // 只有 tavily 能真正按时间过滤，所以查询里带明确时间窗时把它提到最前面；
    // 它要经代理，刚失败过（代理没开）就在冷却期内跳过，免得每次查询都白等一轮。
    const ordered = this.apiChain(cfg)
    const chain = (hint !== null && ordered.includes('tavily')
      ? ['tavily', ...ordered.filter((item) => item !== 'tavily')]
      : ordered).filter((item) => item !== 'tavily' || Date.now() >= this.tavilyDownUntil)
    for (const engine of chain) {
      if (signal?.aborted === true) throw new Error('搜索已取消')
      const started = Date.now()
      try {
        const rows = engine === 'exa'
          ? await exaSearch({
            query: request.query,
            // exa 没有时间参数，只能用 objective 做软过滤
            objective: hint === null
              ? undefined
              : (hint.anchor !== null
                ? `Prefer pages published on or after ${hint.anchor}.`
                : `Prefer pages published within the last ${Math.max(1, Math.round(hint.days))} days.`),
            limit,
            timeoutMs: clamp(cfg.exaTimeoutMs, 3000, 60000),
            signal,
          })
          : await tavilySearch({
            query: request.query,
            limit,
            timeRange: hint === null ? null : hint.timeRange,
            proxy: this.proxyFor(cfg),
            curl: cfg.curl,
            timeoutMs: clamp(cfg.tavilyTimeoutMs, 3000, 60000),
            signal,
          })
        const sources = this.toSources(rows, limit, cfg)
        if (sources.length > 0) {
          this.lastEngine = engine
          this.cacheSet(key, ttl, sources)
          return { sources, truncated: false }
        }
        problems.push(`${engine}: 0 条结果（${Date.now() - started}ms）`)
      } catch (error) {
        if (signal?.aborted === true) throw error
        if (engine === 'tavily') this.tavilyDownUntil = Date.now() + 300000
        problems.push(`${engine}: ${firstLine(error)}`)
      }
    }

    // ② 兜底：脚本抓搜索引擎 HTML。通路（直连/代理）由 python 侧按引擎决定：
    //    DDG 必须走代理，百度/神马走代理会被判机器，必应直连才给真结果。
    //    这里只把配置原样传下去，不再自己钉一条通路。
    const args = ['search', request.query, '-n', String(limit), '--json', '--proxy', cfg.proxy]
    if (cfg.engine !== undefined && cfg.engine !== null && cfg.engine !== 'auto') {
      args.push('--engine', String(cfg.engine))
    }
    let result
    try {
      result = await runJson(cfg, args, signal)
    } catch (error) {
      throw new Error(this.failMessage(request.query, problems, [firstLine(error)]))
    }

    const rows = Array.isArray(result?.results) ? result.results : []
    if (rows.length === 0) {
      const scriptProblems = Array.isArray(result?.problems) ? result.problems.slice(0, 6) : []
      throw new Error(this.failMessage(request.query, problems, scriptProblems))
    }
    const sources = this.toSources(rows, limit, cfg)
    this.lastEngine = 'script'
    this.cacheSet(key, ttl, sources)
    return { sources, truncated: false }
  }

  /** 按配置排出的 API 引擎顺序；off 或空 = 只用脚本。 */
  apiChain(cfg) {
    const raw = String(cfg.apiEngines ?? '')
    if (raw === '' || raw.toLowerCase() === 'off') return []
    return raw.split(',')
      .map((name) => name.trim().toLowerCase())
      .filter((name) => name === 'exa' || name === 'tavily')
  }

  /** tavily 必须经代理：auto → 兜底代理地址；off → 直连（本机大概率失败，快速落下一层）。 */
  proxyFor(cfg) {
    if (cfg.proxy === 'off') return null
    return cfg.proxy === 'auto' ? cfg.fallbackProxy : cfg.proxy
  }

  /** rows → seam 的 sources[]：丢空 url、压平空白、按 snippetChars 截断。 */
  toSources(rows, limit, cfg) {
    const maxChars = clamp(cfg.snippetChars, 80, 2000)
    return (Array.isArray(rows) ? rows : []).slice(0, limit).map((row) => ({
      url: String(row?.url ?? ''),
      ...(typeof row?.title === 'string' && row.title.length > 0 ? { title: row.title } : {}),
      ...(typeof row?.snippet === 'string' && row.snippet.replace(/\s+/gu, ' ').trim().length > 0
        ? { snippet: row.snippet.replace(/\s+/gu, ' ').trim().slice(0, maxChars) } : {}),
    })).filter((source) => source.url.length > 0)
  }

  /** 写缓存（超过 50 条整体清空，免得常驻会话无限长）。 */
  cacheSet(key, ttl, sources) {
    if (!(ttl > 0)) return
    if (SEARCH_CACHE.size > 50) SEARCH_CACHE.clear()
    SEARCH_CACHE.set(key, { at: Date.now(), sources })
  }

  /** 全线失败时的报错：逐层原因都带上，好定位是 exa 限流、代理挂了还是脚本被反爬。 */
  failMessage(query, apiProblems, scriptProblems) {
    const lines = [...apiProblems, ...scriptProblems.map((item) => `script: ${item}`)]
    return `免费搜索没有拿到结果（query=${JSON.stringify(query)}）`
      + (lines.length > 0 ? `\n逐条失败原因：\n- ${lines.join('\n- ')}` : '')
      + '\n下一步：`websearch.py probe` 看直连/代理哪条通；必要时把插件的 proxy 配置写死成代理地址。'
  }
}

// ─────────────────────────── 抓取 provider ───────────────────────────

/**
 * 抓取：先直连（快、保留 HTML 结构），失败再走脚本的代理兜底。
 * 非 2xx 是「结果」不是「错误」，按 seam 语义原样返回状态码。
 */
class FreeFetchProvider {
  id = 'free'

  constructor(getConfig) {
    this.getConfig = getConfig
  }

  available() {
    const cfg = this.getConfig()
    return existsSync(cfg.python) && existsSync(cfg.script)
  }

  async fetch(request, signal) {
    const cfg = this.getConfig()
    const direct = await this.tryDirect(request.url, cfg, signal)
    if (direct !== null) return direct
    if (signal?.aborted === true) throw new Error('抓取已取消')
    return await this.viaScript(request.url, cfg, signal)
  }

  /** 本进程直连一次；网络层失败返回 null（交给脚本兜底），HTTP 错误码照常返回。 */
  async tryDirect(url, cfg, signal) {
    const signals = [AbortSignal.timeout(Number(cfg.directTimeoutMs) || 9000)]
    if (signal !== undefined) signals.push(signal)
    let response
    try {
      response = await fetch(url, {
        redirect: 'follow',
        headers: {
          'user-agent': UA,
          'accept': 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8',
          'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
        },
        signal: AbortSignal.any(signals),
      })
    } catch (error) {
      if (signal?.aborted === true) throw error
      // 留给测试/排查看的：直连到底怎么失败的
      this.lastDirectError = `fetch: ${String(error)}${error?.cause ? ` / ${String(error.cause)}` : ''}`
      return null
    }
    let read
    try {
      read = await readCapped(response, clamp(cfg.maxChars, 1000, 500000) * 4)
    } catch (error) {
      this.lastDirectError = `read: ${String(error)}`
      return null
    }
    const text = decodeBody(read.bytes, response.headers.get('content-type'))
    const content = text.slice(0, clamp(cfg.maxChars, 1000, 500000))
    return {
      url: response.url.length > 0 ? response.url : url,
      statusCode: response.status,
      body: { kind: kindOf(content, response.headers.get('content-type')), content },
      truncated: read.truncated || content.length < text.length,
    }
  }

  /** 直连不通时的兜底：脚本自己会试直连→代理，这里直接把代理放前面省时间。 */
  async viaScript(url, cfg, signal) {
    const proxy = cfg.proxy === 'off'
      ? 'off'
      : (cfg.proxy === 'auto' ? cfg.fallbackProxy : cfg.proxy)
    const maxChars = clamp(cfg.maxChars, 1000, 500000)
    const result = await runJson(
      cfg,
      ['fetch', url, '--raw', '--json', '--max', String(maxChars), '--proxy', proxy],
      signal,
    )
    const status = Number(result?.status ?? 0)
    const text = typeof result?.text === 'string' ? result.text : ''
    if (status !== 200 || text.length === 0) {
      const problems = Array.isArray(result?.problems) ? result.problems.slice(0, 6) : []
      throw new Error(
        `抓取失败 ${url}（直连与代理都没成）`
        + (problems.length > 0 ? `\n逐条失败原因：\n- ${problems.join('\n- ')}` : ''),
      )
    }
    return {
      url: typeof result.final === 'string' && result.final.length > 0 ? result.final : url,
      statusCode: status,
      body: { kind: kindOf(text, ''), content: text.slice(0, maxChars) },
      // 脚本按 --max 自己截断了，所以「达到上限」就等于被截断
      truncated: text.length >= maxChars,
    }
  }
}

// ─────────────────────────── 插件入口 ───────────────────────────

/** Cordis 诊断用的插件名。 */
export const name = 'web-free'

/** 依赖 web 能力缝（ctx.web）。 */
export const inject = ['web']

/**
 * 注册两个自由 provider。只注册自己的 id，出厂 provider 原封不动，
 * 回滚只需把 `web` 行的 searchProvider/fetchProvider 改回原值。
 * @param {object} ctx 插件上下文
 * @param {object} [config] yml 里的 config
 */
export function apply(ctx, config) {
  const cfg = resolveConfig(config)
  const getConfig = () => resolveConfig(config)
  ctx.web.registerSearchProvider(new FreeSearchProvider(getConfig))
  ctx.web.registerFetchProvider(new FreeFetchProvider(getConfig))
  ctx.logger?.info?.(
    `web-free 已挂载：search/fetch provider = free（脚本 ${cfg.script}，proxy=${cfg.proxy}）`,
  )
}

export { FreeSearchProvider, FreeFetchProvider, DEFAULTS }
