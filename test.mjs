/**
 * 不重启 App 也能验的离线测试：假装成 cordis 上下文，把 provider 要的
 * registerSearchProvider / registerFetchProvider 接住，然后直接调用。
 *
 *   node test.mjs            # 搜索 1 条查询 + 抓 1 个被墙页面
 *   node test.mjs --search-only
 *
 * 退出码：0 全通 / 1 有失败。
 */
import { apply } from './lib/index.js'

const providers = {}
const logs = []
const ctx = {
  web: {
    registerSearchProvider: (p) => { providers.search = p; return () => {} },
    registerFetchProvider: (p) => { providers.fetch = p; return () => {} },
  },
  logger: { info: (m) => logs.push(String(m)), warn: (m) => logs.push(String(m)) },
}

apply(ctx, {})
console.log(logs.join('\n'))

let failed = 0
const t = () => Date.now()

console.log('\n--- available() ---')
console.log('search:', providers.search.available(), '| fetch:', providers.fetch.available())

console.log('\n--- search ---')
const t0 = t()
try {
  const res = await providers.search.search({ query: 'DeepSeek V4.1 Flash 发布', maxResults: 5 })
  console.log(`ok ${t() - t0}ms · ${res.sources.length} 条 · truncated=${res.truncated}`)
  for (const s of res.sources.slice(0, 3)) console.log(` - ${s.title}\n   ${s.url}\n   ${(s.snippet ?? '').slice(0, 80)}`)
} catch (error) {
  failed += 1
  console.log('FAIL:', String(error?.message ?? error))
}

console.log('\n--- search（空查询边界，应当报错而不是静默空） ---')
try {
  const res = await providers.search.search({ query: 'zzzqqqxxyy 不存在的词 9173', maxResults: 3 })
  console.log('ok（居然有结果）:', res.sources.length)
} catch (error) {
  console.log('按预期报错:', String(error?.message ?? error).split('\n')[0])
}

if (!process.argv.includes('--search-only')) {
  console.log('\n--- fetch 直连正常站点 ---')
  const t1 = t()
  try {
    const r = await providers.fetch.fetch({ url: 'https://example.com' })
    console.log(`ok ${t() - t1}ms · ${r.statusCode} · kind=${r.body.kind} · ${r.body.content.length} 字符 · truncated=${r.truncated}`)
  } catch (error) {
    failed += 1
    console.log('FAIL:', String(error?.message ?? error))
    console.log('直连失败原因:', providers.fetch.lastDirectError ?? '(未记录)')
  }

  console.log('\n--- fetch 被墙站点（走代理兜底） ---')
  const t2 = t()
  try {
    const r = await providers.fetch.fetch({ url: 'https://en.wikipedia.org/wiki/DeepSeek' })
    console.log(`ok ${t() - t2}ms · ${r.statusCode} · kind=${r.body.kind} · ${r.body.content.length} 字符`)
    console.log('前 80 字:', r.body.content.replace(/\s+/gu, ' ').slice(0, 80))
  } catch (error) {
    failed += 1
    console.log('FAIL:', String(error?.message ?? error))
  }
}

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 项失败`)
process.exit(failed === 0 ? 0 : 1)
