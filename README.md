# dsh-web-free

不开 DeepSeek 官方 Search API 也能用 `web_search` / `web_fetch`：给 DeepSeek Harness 的 `ctx.web` 能力缝注册一套免费 provider（id = `free`），工具本身、schema、引用渲染全部不变，只是换了后端。

官方默认的搜索 provider 走 DeepSeek 付费接口（约 5 元/千次，且要有有效 API Key）；Key 失效或欠费时联网直接归零。本插件把联网交给三层免费链路，逐级回退：

| 顺序 | 引擎 | 说明 |
|---|---|---|
| ① | **Exa**（匿名 MCP） | 不要 key、不要代理，直连返回带正文片段和发布日期的结果 |
| ② | **Tavily**（keyless） | 免费额度，需能访问外网；唯一支持时间过滤 |
| ③ | `websearch.py` | 抓 Bing / DuckDuckGo / 搜狗 / 百度 等 HTML 自己解析，多引擎降级 |

查询词里带时间说法（「最近一周」「本月」「last 7 days」等）时，provider 会把 Tavily 提前做硬过滤——`web_search` 的 schema 没有 `timeRange` 参数，所以这一层完全在插件内部完成。

## 要求

- DeepSeek Harness（带 `ctx.web` provider seam 的版本；在 0.2.x 上实测）
- Node.js（跑插件）和 Python 3（跑兜底脚本，仅标准库，无需 pip install）
- ① ③ 层不需要任何 API Key；② Tavily keyless 也不需要 Key

## 安装

```bash
dsh plugin --profile web add github:Jonah-77/dsh-web-free
```

或按包名（发布到 npm 后）：`npm:dsh-web-free`。装完**重启 Harness**。

包的 `cordis.patch.yml` 会做两件事：插入本插件，并把 `web` 的 `searchProvider` / `fetchProvider` 钉成 `free`。如果你的 profile 已有别的 `web` 行配置，patch 是整块替换 config，请按需合并。

## 回滚

从 profile 的依赖/ bundles 里移除本包，并删掉/改回 `web` 行的 `searchProvider`、`fetchProvider`（出厂值是 `deepseek-official` / `http`），重启即可。插件只注册自己的 id，不动内置 provider。

## 配置（可选）

写在 profile 的 `cordis.patch.yml` 里本插件挂载行的 `config:` 下，全部有默认值，通常不用填：

| 字段 | 默认 | 说明 |
|---|---|---|
| `apiEngines` | `exa,tavily` | 免费 API 引擎顺序；`off` = 只用脚本抓 HTML |
| `proxy` | `auto` | `auto` = 先直连再代理；`off` = 只直连；也可写死如 `127.0.0.1:7890` |
| `fallbackProxy` | 空 | 直连失败后的兜底代理地址（需要代理的环境填） |
| `curl` | 自动探测 | Tavily 走代理时用的 curl 可执行文件 |
| `python` | 自动探测 | `python3` / `python`（PATH 查找） |
| `script` | 包内 `websearch.py` | 兜底脚本路径 |
| `snippetChars` | `300` | 每条结果摘要截断长度 |
| `searchCacheTtlSec` | `600` | 同查询缓存秒数，`0` 关闭 |

`websearch.py` 的代理默认取环境变量 `DSH_WEB_PROXY`，没设时用 `http://127.0.0.1:7890`；没有代理的环境它会在直连失败后跳过代理，不会卡死。

## 文件

| 文件 | 作用 |
|---|---|
| `lib/index.js` | 插件本体：两个 provider（search/fetch），零 npm 依赖 |
| `lib/api-engines.js` | Exa / Tavily keyless 引擎 + 时间词解析 |
| `websearch.py` | 兜底搜索/抓取脚本，纯 Python 标准库 |
| `selftest.py` | `websearch.py` 的自测工具 |
| `test.mjs` | 插件离线测试（假 ctx，不重启 Harness） |

## 自测

```bash
node test.mjs                       # 插件离线测试
python3 websearch.py probe          # 逐个探测脚本引擎通路
python3 selftest.py                 # 脚本自测
```

## 已知边界

- 抓搜索引擎 HTML 本质不稳：某家改版会让该引擎解析失效；脚本有多引擎降级 + 相关度门槛兜底。
- 没有官方 API「模型替你读过再总结」的 `content`，只有标题/链接/摘要；读全文走 `web_fetch`。
- 中文垂类（招聘、地方政策）的 snippet 质量偶不如官方 API。

## License

MIT
