#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-web —— 网页检索 / 抓取的兜底链路（不依赖 DeepSeek Search API）。

为什么存在
----------
DSH 自带的 web_search 走 DeepSeek 官方接口，要有效 API key；key 失效或欠费时
（实测 401 / 402）联网能力直接归零。这个脚本是那条链路的**替代品**：
抓 Bing / DuckDuckGo / 搜狗 / 百度的 HTML，自己解析成 标题 + 链接 + 摘要。

它不追求官方 Search API 的结构化质量，只保证三件事：
  1. **永远有路走**：直连失败自动改走本机代理（Clash 7890），反之亦然；
  2. **不会乱码**：GBK / GB18030 / UTF-8 自动识别；
  3. **失败要出声**：每一种失败都给出具体原因，不静默返回空结果。

用法
----
  python3 websearch.py search "2027届 校园招聘 质量工程师" -n 10
  python3 websearch.py search "..." --engine ddg --json
  python3 websearch.py fetch https://example.com/article --max 8000
  python3 websearch.py batch urls.txt --out ./pages        # 批量、可断点续跑
  python3 websearch.py probe                              # 测通道（直连/代理）

全局开关
--------
  --proxy auto          默认。先直连，失败再走代理
  --proxy off           只直连
  --proxy 127.0.0.1:7890 强制走指定代理
  环境变量 DSH_WEB_PROXY 可以改默认代理地址。
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html as H
import json
import os
import random
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

# 有些站（搜狗、必应、百度）见到移动 UA 会甩一个 JS 壳页面，解析不出东西；
# 桌面 UA 反而给服务端渲染的完整结果页。实测：搜狗移动 UA 只回「QQ浏览器搜索」，
# 桌面 UA 直接给 400KB 真结果 —— 所以按引擎分别给 UA，别一刀切。
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

DEFAULT_PROXY = os.environ.get("DSH_WEB_PROXY", "http://127.0.0.1:7890")
TIMEOUT = float(os.environ.get("DSH_WEB_TIMEOUT", "20"))
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("DSH_WEB_CACHE", os.path.join(HERE, ".cache"))
CACHE_TTL = int(os.environ.get("DSH_WEB_CACHE_TTL", "3600"))

_SSL = ssl._create_unverified_context()

# 搜索结果里要丢掉的域名（搜索引擎自己的导航/广告位）
_JUNK_HOSTS = (
    "bing.com", "microsoft.com", "msn.com", "go.microsoft.com",
    "google.com", "gstatic.com", "googleusercontent.com",
    "baidu.com", "hao123.com", "sogou.com", "sina.com.cn",
    "duckduckgo.com", "w3.org",
)


# ─────────────────────────── 传输层 ───────────────────────────

class Route:
    """一条通路：直连，或者经某个 HTTP 代理。"""

    def __init__(self, proxy: str | None):
        self.proxy = proxy

    @property
    def label(self) -> str:
        return "direct" if not self.proxy else f"proxy:{self.proxy}"

    def opener(self):
        handlers = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        handlers.append(urllib.request.HTTPSHandler(context=_SSL))
        return urllib.request.build_opener(*handlers)


def _safe_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((
        p.scheme, p.netloc,
        urllib.parse.quote(p.path, safe="/%"),
        urllib.parse.quote_plus(p.query, safe="=&%+"),
        p.fragment,
    ))


def _curl_get(url: str, proxy: str | None, timeout: float = TIMEOUT) -> tuple[bytes, str, str]:
    """用系统 curl 再试一次。

    为什么需要它：本机 Python(OpenSSL) 对一部分站点会 TLS 握手失败
    （`SSLV3_ALERT_HANDSHAKE_FAILURE`，example.com 就是），而同一台机器上的
    curl 却能直接 200。所以「Python 失败 ⇒ 交给 curl」不是多余的一层，
    而是这类站点的唯一通路。
    """
    fd, path = tempfile.mkstemp(prefix="dshweb-", suffix=".bin")
    os.close(fd)
    try:
        args = ["curl", "-sS", "-L", "--compressed",
                "--max-time", str(int(timeout) + 5),
                "-A", UA,
                "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
                "-o", path,
                "-w", "%{http_code}\t%{content_type}\t%{url_effective}",
                url]
        if proxy:
            args += ["-x", proxy]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 20)
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(f"curl 不可用: {type(e).__name__}") from e
        meta = (proc.stdout or "").strip().split("\t")
        code = meta[0] if meta else "000"
        if code != "200":
            raise RuntimeError(f"curl HTTP {code} {proc.stderr.strip()[:80]}")
        ctype = meta[1] if len(meta) > 1 else ""
        final = meta[2] if len(meta) > 2 else url
        with open(path, "rb") as f:
            return f.read(), ctype, final
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def get(url: str, route: Route, timeout: float = TIMEOUT,
        headers: dict | None = None) -> tuple[bytes, str, str, str]:
    """GET 一个 URL，返回 (body, content_type, final_url, via)。失败抛异常。

    via 会带 `+curl` 后缀 —— 表示 urllib 失败、由系统 curl 完成的那一次。
    """
    hdrs = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(_safe_url(url), headers=hdrs)
    try:
        resp = route.opener().open(req, timeout=timeout)
    except urllib.error.HTTPError:
        raise                      # 4xx/5xx 是真实响应，交给上层判断，不要转 curl
    except Exception:              # noqa: BLE001 —— TLS/连接问题交给 curl 重试
        raw, ctype, final = _curl_get(url, route.proxy, timeout)
        return raw, ctype, final, route.label + "+curl"
    with resp as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                raw = zlib.decompress(raw)
        return raw, r.headers.get("Content-Type", ""), r.geturl(), route.label


def routes_for(mode: str) -> list[Route]:
    if mode == "off":
        return [Route(None)]
    if mode in ("on", "auto-proxy", "proxy"):
        return [Route(DEFAULT_PROXY)]
    if mode and mode not in ("auto", ""):
        addr = mode if mode.startswith("http") else f"http://{mode}"
        return [Route(addr)]
    return [Route(None), Route(DEFAULT_PROXY)]  # auto


def decode(raw: bytes, ctype: str = "") -> str:
    """按 Content-Type → <meta charset> → 试探 的顺序解码，杜绝 GBK 乱码。"""
    cand: list[str] = []
    m = re.search(r"charset=[\"']?([\w\-]+)", ctype or "", re.I)
    if m:
        cand.append(m.group(1))
    m2 = re.search(rb"charset=[\"']?([\w\-]+)", raw[:4096], re.I)
    if m2:
        cand.append(m2.group(1).decode("ascii", "ignore"))
    cand += ["utf-8", "gb18030", "big5"]
    seen = set()
    for c in cand:
        c = c.lower().replace("gbk", "gb18030").replace("gb2312", "gb18030").replace("gb-18030", "gb18030")
        if c in seen:
            continue
        seen.add(c)
        try:
            return raw.decode(c)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def html_to_text(doc: str) -> str:
    doc = re.sub(r"(?is)<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", " ", doc)
    doc = re.sub(r"(?is)<br\s*/?>", "\n", doc)
    doc = re.sub(r"(?is)</(p|div|tr|li|h[1-6]|table|section|article|pre|blockquote)>", "\n", doc)
    doc = re.sub(r"(?is)<td[^>]*>", " | ", doc)
    doc = re.sub(r"(?is)<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                 lambda m: (re.sub(r"<[^>]+>", "", m.group(2)).strip() or "") + " <" + m.group(1) + ">", doc)
    doc = re.sub(r"(?is)<[^>]+>", " ", doc)
    doc = H.unescape(doc)
    doc = re.sub(r"[ \t\u00a0\u3000]+", " ", doc)
    doc = re.sub(r"\n\s*\n+", "\n", doc)
    return doc.strip()


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"(?is)<[^>]+>", "", s or ""))).strip()


# ═══════════════════════ 引擎层 v2（2026-10-28 重写）═══════════════════════
# 这一层是拿实测数据重排的，不是照搬某个库。几条硬结论先写在前面，免得后人再踩：
#
# 1. **同一台机器上，不同引擎要走的通路不一样。**
#    · DDG：直连被墙（TLS reset），必须走代理；
#    · 百度 / 神马：走代理会被判定为机器（代理出口是共享 IP，早被打黑了），
#      直连（国内运营商出口）反而是通的；
#    · 必应：两条路都通，但走代理时返回的是「首页/品牌页」这类大路货
#      （查 `python asyncio timeout` 回 python.org 首页），直连才给真结果。
#    所以 ENGINES 里每个引擎都带 route，"先试哪条"由它决定。
#
# 2. **反爬是常态，不是异常。** DDG 会撞「选鸭子」验证页、搜狗跳 antispider、
#    神马跳 punish 页、百度要安全验证。对策：认出特征 → 冷却 30s → 换引擎，
#    并把「谁刚成功过」记到文件里，下次从上次灵的那条开始。
#
# 3. **宁缺毋滥。** 跑题的结果比没结果更糟（会把「欢迎来到 Python 官网」当成
#    「asyncio 超时」的答案）。所以每条结果至少要命中 2 个查询词才认，
#    全都不达标就如实报错，而不是塞一堆垃圾。
#
# 4. **免 key 的 JSON 接口优先用起来。** 维基百科 / Stack Exchange / GitHub
#    不会反爬，覆盖「是什么」「报错了怎么办」这类查询比抓页面靠谱得多。

import base64 as _b64

# ─────────────────────────── 反爬特征 / 引擎健康状态 ───────────────────────────
_BLOCK_MARKERS = (
    "unusual traffic", "captcha", "验证码", "安全验证", "人机验证",
    "访问异常", "请开启 JavaScript", "enable javascript",
    # DDG 的验证页（连续请求几次就会撞上）
    "bots use duckduckgo", "complete the following challenge",
    "select all squares", "error-lite",
    # 搜狗跳 antispider；神马跳 punish 页（rgv587_flag 是它的标记）
    "antispider", "/antispider/", "/_____tmd_____/punish", "rgv587_flag",
)

_EMPTY_MARKERS = (
    "没有与此相关的结果", "没有找到", "no results", "did not match any documents",
)

# 状态落盘而不是放内存：一次 web_search 最多并发 4 条查询 → 4 个进程，
# 谁被拦过、谁刚成功过必须共享，否则四个进程一起把同一个 IP 打得更黑。
_STATE_FILE = os.path.join(CACHE_DIR, "engine_state.json")
_POLITE_GAP = float(os.environ.get("DSH_WEB_POLITE_GAP", "1.0"))


def _looks_blocked(doc: str) -> str | None:
    low = doc[:8000].lower()
    for m in _BLOCK_MARKERS:
        if m in low:
            return m
    return None


def _load_state() -> dict:
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _STATE_FILE)      # 原子替换：多进程不会读到半个文件
    except OSError:
        pass


def _cooldown_left(engine: str) -> float:
    try:
        return max(0.0, float(_load_state().get(f"{engine}.blocked_until", 0)) - time.time())
    except (TypeError, ValueError):
        return 0.0


def _set_cooldown(engine: str, seconds: float) -> None:
    state = _load_state()
    state[f"{engine}.blocked_until"] = time.time() + seconds
    _save_state(state)


def _polite(engine: str) -> None:
    """同一个引擎两条请求之间至少隔 _POLITE_GAP 秒（跨进程）。
    限流基本都是「打太急」打出来的，这一行比任何重试都管用。"""
    if _POLITE_GAP <= 0:
        return
    key = f"{engine}.last_hit"
    state = _load_state()
    try:
        last = float(state.get(key, 0))
    except (TypeError, ValueError):
        last = 0.0
    wait = _POLITE_GAP - (time.time() - last)
    if 0 < wait <= _POLITE_GAP:
        time.sleep(wait)
    state[key] = time.time()
    _save_state(state)




# 直连一定被墙、必须走代理的站点（少了这一条，每次抓取都要先白等一次超时）
_PROXY_ONLY_HOSTS = (
    "wikipedia.org", "wikimedia.org", "google.com", "googleapis.com", "gstatic.com",
    "duckduckgo.com", "youtube.com", "twitter.com", "x.com", "reddit.com", "medium.com",
    "openai.com", "github.io", "telegram.org", "instagram.com", "facebook.com",
    "pypi.org", "npmjs.com", "docker.com", "stackoverflow.com",
)


def _ok_host(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return False
    if not host:
        return False
    return not any(host == j or host.endswith("." + j) for j in _JUNK_HOSTS)


_CJK_JUNK_TITLES = {
    "反馈", "登录", "首页", "更多", "下载", "设置", "帮助", "意见反馈", "隐私", "举报", "广告",
    "客户服务", "关于我们", "上一页", "下一页", "搜索", "顶部", "查看更多", "展开", "收起",
    "百度", "搜狗", "神马", "360", "夸克", "头条",
}


def _unescape_u(text: str) -> str:
    """把字面量 \\u53cd\\u9988 之类的转义还原成中文。"""
    if "\\u" not in text:
        return text
    try:
        return text.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except (UnicodeDecodeError, ValueError):
        return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)


def _clean_title(raw_inner: str) -> str:
    """标题里常混进 <cite> 的站点名和裸 URL（Bing 尤其明显），这里一并清掉。"""
    t = re.sub(r"(?is)<cite.*?</cite>", " ", raw_inner or "")
    t = _unescape_u(strip_tags(t))
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    m = re.match(r"^(\S+\.(?:com|cn|net|org|edu|gov|io|co)(?:\.cn)?)\s+(.{5,})$", t)
    if m:
        t = m.group(2).strip()
    return t


def _dedupe(rows: list[dict], limit: int) -> list[dict]:
    out, seen = [], set()
    for r in rows:
        u = (r.get("url") or "").split("#")[0]
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _unwrap_ddg(href: str) -> str:
    href = H.unescape(href or "")
    if "uddg=" in href:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("uddg")
        if q:
            href = q[0]
    if href.startswith("//"):
        href = "https:" + href
    return href


def _decode_bing_redirect(url: str) -> str:
    """必应结果是 https://www.bing.com/ck/a?...&u=a1<base64url> 的跳转壳。"""
    if "bing.com/ck/a" not in url:
        return url
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        u = (qs.get("u") or [""])[0]
        if not u.startswith("a1"):
            return url
        b64 = u[2:] + "=" * (-len(u[2:]) % 4)
        real = _b64.urlsafe_b64decode(b64).decode("utf-8", "replace")
        return real if real.startswith("http") else url
    except Exception:  # noqa: BLE001
        return url


# ─────────────────────────── HTML 引擎的解析器 ───────────────────────────

def _parse_ddg(doc: str, limit: int) -> list[dict]:
    rows = []
    snips = re.findall(r'(?is)<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', doc)
    for i, m in enumerate(re.finditer(r'(?is)<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', doc)):
        rows.append({"title": _clean_title(m.group(2)), "url": _unwrap_ddg(m.group(1)),
                     "snippet": strip_tags(snips[i])[:400] if i < len(snips) else ""})
    return rows


def _parse_ddg_lite(doc: str, limit: int) -> list[dict]:
    rows = []
    links = re.findall(r"(?is)<a[^>]*class=['\"]result-link['\"][^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", doc)
    snips = re.findall(r"(?is)<td[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</td>", doc)
    for i, (href, inner) in enumerate(links):
        rows.append({"title": _clean_title(inner), "url": _unwrap_ddg(href),
                     "snippet": strip_tags(snips[i])[:400] if i < len(snips) else ""})
    return rows


def _parse_sogou(doc: str, limit: int) -> list[dict]:
    """.vrwrap 卡片；链接是 /link?url=… 壳（真实地址由 _resolve_redirects 补）。"""
    rows = []
    for blk in re.findall(r'(?is)<div class="vrwrap".*?(?=<div class="vrwrap"|<div class="r-sech|</body>)', doc):
        mh = re.search(r'(?is)<h3 class="vr-title"[^>]*>(.*?)</h3>', blk)
        if not mh:
            continue
        ma = re.search(r'(?is)<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', mh.group(1))
        if not ma:
            continue
        url = H.unescape(ma.group(1))
        if url.startswith("/"):
            url = "https://www.sogou.com" + url
        title = _clean_title(ma.group(2)) or _clean_title(mh.group(1))
        if not title:
            continue
        sn = ""
        ms = re.search(r'(?is)id="cacheresult_summary_[^"]*"[^>]*>(.*?)</div>', blk)
        if not ms:
            ms = re.search(r'(?is)<div class="[^"]*(?:fz-mid|space-txt|clamp\d)[^"]*"[^>]*>(.*?)</div>', blk)
        if ms:
            sn = strip_tags(ms.group(1))[:400]
        row = {"title": title, "url": url, "snippet": sn}
        md = re.search(r'(?is)<span class="cite-date">([\d\-/\.]+)</span>', blk)
        if md:
            d = md.group(1).replace("/", "-").replace(".", "-").strip()
            if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", d):
                y, mo, da = d.split("-")
                row["publishedAt"] = f"{y}-{int(mo):02d}-{int(da):02d}"
        rows.append(row)
    return rows


def _parse_bing(doc: str, limit: int) -> list[dict]:
    rows = []
    for blk in re.findall(r'(?is)<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', doc):
        mh = re.search(r'(?is)<h2[^>]*>(.*?)</h2>', blk)
        if not mh:
            continue
        links = re.findall(r'(?i)<a[^>]+href="(https?://[^"]+)"', blk[:mh.start()])
        url = _decode_bing_redirect(H.unescape(links[-1])) if links else ""
        if not url or not _ok_host(url):
            continue
        title = _clean_title(mh.group(1))
        if not title:
            continue
        sn = ""
        for pat in (r'(?is)<p[^>]*>(.*?)</p>',
                    r'(?is)<div class="b_caption"[^>]*>(.*?)</div>',
                    r'(?is)<span class="b_algoSlug"[^>]*>(.*?)</span>'):
            ms = re.search(pat, blk)
            if ms and strip_tags(ms.group(1)):
                sn = strip_tags(ms.group(1))
                break
        rows.append({"title": title, "url": url, "snippet": sn})
    return rows


def _decode_jump(url: str) -> str:
    """360/夸克/神马把真实地址塞在跳转壳里：…/jump?u=<urlencoded>。"""
    if "jump?u=" not in url and "/jump/" not in url:
        return url
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        for key in ("u", "url", "to"):
            if qs.get(key):
                real = urllib.parse.unquote(qs[key][0])
                if real.startswith("http"):
                    return real
    except ValueError:
        pass
    return url


def _parse_so360(doc: str, limit: int) -> list[dict]:
    """360 移动版：每条结果是一个 <div class="g-card res-list" data-pcurl="真实地址"> 卡片。
    按 data-pcurl 切块（比按 <div class="g-card 切稳，卡片里还嵌着小卡片），
    真实地址直接就在属性里，不用再抓一次跳转壳。"""
    marks = [(m.start(), m.group(1)) for m in re.finditer(r'(?i)data-pcurl="([^"]+)"', doc)]
    rows = []
    for i, (pos, url) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else min(len(doc), pos + 8000)
        blk = doc[pos:end]
        mh = re.search(r'(?is)<h3[^>]*>(.*?)</h3>', blk)
        if not mh:
            continue
        title = _clean_title(mh.group(1))
        title = re.sub(r"(?is)<[^>]+>", "", title).strip()
        if len(title) < 4:
            continue
        real = H.unescape(url).strip()
        if not real.startswith("http"):
            continue
        sn = ""
        ms = (re.search(r'(?is)<div class="res-desc"[^>]*>(.*?)</div>', blk)
              or re.search(r'(?is)<p[^>]*>(.*?)</p>', blk)
              or re.search(r'(?is)class="res-comm-con"[^>]*>(.*?)</div>', blk))
        if ms:
            sn = strip_tags(ms.group(1))[:400]
        rows.append({"title": title, "url": _decode_jump(real), "snippet": sn})
        if len(rows) >= limit:
            break
    return rows


_SELF_HOSTS = ("so.com", "sm.cn", "quark.sm.cn", "baidu.com", "sogou.com", "bing.com",
               "toutiao.com", "so.html5.qq.com")


def _parse_generic(doc: str, limit: int) -> list[dict]:
    """结构不认识时的兜底：捞所有像样的链接，挑掉导航链接，剩下的交给相关度判定。"""
    rows = []
    for m in re.finditer(r'(?is)<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', doc):
        title = _clean_title(m.group(2))
        url = H.unescape(m.group(1))
        if len(title) < 8 or title in _CJK_JUNK_TITLES:
            continue
        if re.search(r"['\"]\s*\+|=>|function\s*\(|\{\s*\"|\$\{", title):
            continue                       # 页面里的 JS 片段，不是结果标题
        try:
            host = urllib.parse.urlsplit(url).netloc.lower()
        except ValueError:
            continue
        # 搜索站自己的导航/反馈/推广链接不是结果（除非是 /link? 跳转壳）
        if any(host == h or host.endswith("." + h) for h in _SELF_HOSTS) and "/link" not in url:
            continue
        rows.append({"title": title, "url": _decode_jump(url), "snippet": ""})
    return rows


# 跳转壳：链接里藏真实地址（搜狗/百度/必应）
_REDIRECT_HOSTS = ("sogou.com/link", "baidu.com/link", "bing.com/ck/a", "so.com/link")


def _is_redirect(url: str) -> bool:
    return any(h in url for h in _REDIRECT_HOSTS)


def _resolve_one(url: str, route: Route) -> str:
    """搜狗的壳页只有 200 多字节，内容是一句 window.location.replace("真实地址")。"""
    try:
        raw, ctype, final, via = get(url, route, timeout=10,
                                     headers={"User-Agent": UA_DESKTOP,
                                              "Referer": "https://www.sogou.com/"})
        doc = decode(raw, ctype)
        m = (re.search(r'location\.(?:replace|href)\s*=\s*["\']([^"\']+)', doc)
             or re.search(r'(?i)URL=([^"\'>\s]+)', doc))
        if m:
            real = H.unescape(m.group(1)).strip()
            if real.startswith("http"):
                return real
    except Exception:  # noqa: BLE001
        pass
    return url


def _resolve_redirects(rows: list[dict], route: Route, limit: int) -> list[dict]:
    targets = [(i, r["url"]) for i, r in enumerate(rows[:limit]) if _is_redirect(r["url"])]
    if not targets:
        return rows
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {i: pool.submit(_resolve_one, u, route) for i, u in targets}
        for i, fut in futures.items():
            rows[i]["url"] = fut.result()
    return rows


# ─────────────────────────── 无 key 的 JSON 接口引擎 ───────────────────────────

def _json_get(url: str, route: Route, timeout: float = 12) -> dict:
    raw, ctype, final, via = get(url, route, timeout=timeout,
                                 headers={"User-Agent": UA_DESKTOP, "Accept": "application/json"})
    return json.loads(decode(raw, ctype))


def _fetch_wikipedia(q: str, limit: int, route: Route) -> list[dict]:
    """只在「是什么/是谁/简介」这类实体查询里用 —— 它对时政、招聘一点用没有，
    而且标题往往只是「沾边」（查社保政策能给你「毛泽东」），所以门槛也高。"""
    cores = [w for w in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9.\-]{1,}", q)]
    core = " ".join(cores[:3]) if cores else q
    rows = []
    for lang in ("zh", "en"):
        try:
            url = (f"https://{lang}.wikipedia.org/w/api.php?action=query&list=search"
                   f"&srsearch={urllib.parse.quote(core)}&srlimit={limit}&format=json&srprop=snippet")
            data = _json_get(url, route)
        except Exception:  # noqa: BLE001
            continue
        for item in (data.get("query") or {}).get("search") or []:
            title = item.get("title") or ""
            if not title:
                continue
            rows.append({
                "title": title,
                "url": f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                "snippet": strip_tags(item.get("snippet") or "")[:400],
            })
        if len(rows) >= limit:
            break
    return rows


def _fetch_stackexchange(q: str, limit: int, route: Route) -> list[dict]:
    """编程问题一枪一个。中文词对 SO 没用，所以只把拉丁词当查询。"""
    latin = " ".join(re.findall(r"[A-Za-z][A-Za-z0-9.+_\-#]{1,}", q)[:8]) or q
    if not re.search(r"[A-Za-z]", latin):
        return []
    url = ("https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance"
           f"&q={urllib.parse.quote(latin)}&site=stackoverflow&pagesize={min(limit, 10)}")
    data = _json_get(url, route)
    rows = []
    for item in data.get("items") or []:
        if not item.get("link"):
            continue
        rows.append({
            "title": H.unescape(item.get("title") or "")[:200],
            "url": item["link"],
            "snippet": f"score {item.get('score')} · {item.get('answer_count')} 个回答"
                       f"{' · 已采纳' if item.get('is_answered') else ''}",
        })
    return rows


def _fetch_github(q: str, limit: int, route: Route) -> list[dict]:
    latin = " ".join(re.findall(r"[A-Za-z][A-Za-z0-9.+_\-#]{1,}", q)[:8]) or q
    if not re.search(r"[A-Za-z]", latin):
        return []
    url = (f"https://api.github.com/search/repositories?q={urllib.parse.quote(latin)}"
           f"&sort=stars&order=desc&per_page={min(limit, 10)}")
    data = _json_get(url, route)
    rows = []
    for item in data.get("items") or []:
        if not item.get("html_url"):
            continue
        rows.append({
            "title": f"{item.get('full_name')}（★{item.get('stargazers_count')}）",
            "url": item["html_url"],
            "snippet": f"{item.get('description') or ''} · {item.get('language') or ''}".strip(" ·")[:300],
        })
    return rows


# ─────────────────────────── 引擎注册表 ───────────────────────────

ENGINES = {
    # HTML 引擎：route = 先试哪条通路；min_rel = 这条引擎的结果要有多「对」才认
    "ddg": {"kind": "html", "url": "https://html.duckduckgo.com/html/?q={q}",
            "parse": _parse_ddg, "ua": "mobile", "route": "proxy", "resolve": False, "min_rel": 0.34},
    "ddg-lite": {"kind": "html", "url": "https://lite.duckduckgo.com/lite/?q={q}",
                 "parse": _parse_ddg_lite, "ua": "desktop", "route": "proxy", "resolve": False, "min_rel": 0.34},
    "sogou": {"kind": "html", "url": "https://www.sogou.com/web?query={q}",
              "parse": _parse_sogou, "ua": "desktop", "route": "any", "resolve": True, "min_rel": 0.34},
    "sm": {"kind": "html", "url": "https://m.sm.cn/s?q={q}",
           "parse": _parse_generic, "ua": "mobile", "route": "direct", "resolve": False, "min_rel": 0.4},
    "so360": {"kind": "html", "url": "https://m.so.com/s?q={q}",
              "parse": _parse_so360, "ua": "mobile", "route": "direct", "resolve": False, "min_rel": 0.4},
    "quark": {"kind": "html", "url": "https://quark.sm.cn/s?q={q}",
              "parse": _parse_generic, "ua": "mobile", "route": "direct", "resolve": False, "min_rel": 0.4},
    "bing": {"kind": "html", "url": "https://www.bing.com/search?q={q}&count={n}",
             "parse": _parse_bing, "ua": "desktop", "route": "direct", "resolve": False, "min_rel": 0.5},
    "baidu": {"kind": "html", "url": "https://m.baidu.com/s?word={q}",
              "parse": _parse_generic, "ua": "mobile", "route": "direct", "resolve": False, "min_rel": 0.5},
    # 接口引擎：不会被反爬，但覆盖面窄，只按查询类型插入
    "wikipedia": {"kind": "api", "fetch": _fetch_wikipedia, "route": "proxy", "min_rel": 0.75},
    "stackexchange": {"kind": "api", "fetch": _fetch_stackexchange, "route": "any", "min_rel": 0.34},
    "github": {"kind": "api", "fetch": _fetch_github, "route": "any", "min_rel": 0.34},
}

_TECH_HINT = re.compile(
    r"(?i)\b(python|js|javascript|ts|typescript|react|vue|svelte|node|npm|pnpm|deno|async|asyncio|"
    r"await|thread|concurrency|api|sdk|error|exception|traceback|docker|kubernetes|git|sql|regex|"
    r"css|html|http|json|yaml|rust|golang|java|kotlin|swift|linux|bash|shell|pandas|numpy|"
    r"pytorch|torch|llm|gpt|transformer|embedding|vector|redis|mysql|sqlite|nginx|android|ios)\b")
_ENTITY_HINT = re.compile(r"(是什么|是谁|简介|百科|定义|什么意思|介绍一下|what is|who is)")

# 中文查询：先中文引擎（搜狗质量最好、神马最不容易被拦），DDG 兜底
_AUTO_CN = ["sogou", "sm", "so360", "quark", "ddg", "ddg-lite", "baidu", "bing"]
# 拉丁查询：DDG 为主
_AUTO_EN = ["ddg", "ddg-lite", "sogou", "sm", "so360", "bing"]


def _auto_order(query: str) -> list[str]:
    order = list(_AUTO_CN if re.search(r"[\u4e00-\u9fff]", query) else _AUTO_EN)
    if _ENTITY_HINT.search(query):
        order = ["wikipedia"] + order
    if _TECH_HINT.search(query):
        order = ["stackexchange", "github"] + order
    return order


def _routes_for_engine(spec: dict, proxy: str) -> list[Route]:
    if proxy not in ("auto", ""):
        return routes_for(proxy)          # 用户显式指定了就听用户的
    pref = spec.get("route", "any")
    if pref == "proxy":
        return [Route(DEFAULT_PROXY), Route(None)]
    if pref == "direct":
        return [Route(None), Route(DEFAULT_PROXY)]
    return routes_for("auto")


# ─────────────────────────── 相关度 & 健康状态 ───────────────────────────

def _query_terms(query: str) -> set[str]:
    terms = {w.lower() for w in re.findall(r"[A-Za-z0-9]{2,}", query)}
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        for i in range(len(run) - 1):
            terms.add(run[i:i + 2])
        if len(run) <= 4:
            terms.add(run)      # 短词整词更重要（「泉州」「校招」）
    return terms


def _relevance(rows: list[dict], query: str) -> float:
    """一条结果至少命中 2 个查询词才算「像在回答」。
    只命中 1 个就放行的话，必应那类「首页/品牌页」垃圾会一路通关。"""
    terms = _query_terms(query)
    if not terms or not rows:
        return 1.0
    need = 2 if len(terms) >= 3 else 1
    hit = 0
    for r in rows:
        blob = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
        if sum(1 for t in terms if t in blob) >= need:
            hit += 1
    return hit / len(rows)


_RESULT_CACHE_TTL = int(os.environ.get("DSH_WEB_RESULT_TTL", "21600"))  # 6h


def _result_cache_path(query: str, limit: int) -> str:
    key = hashlib.sha1(f"{limit}\x00{query}".encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, "q_" + key + ".json")


def _result_cache_get(query: str, limit: int) -> dict | None:
    if _RESULT_CACHE_TTL <= 0:
        return None
    path = _result_cache_path(query, limit)
    try:
        if time.time() - os.path.getmtime(path) > _RESULT_CACHE_TTL:
            return None
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("results") else None
    except (OSError, ValueError):
        return None


def _result_cache_put(query: str, limit: int, payload: dict) -> None:
    if _RESULT_CACHE_TTL <= 0 or not payload.get("results"):
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _result_cache_path(query, limit)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _rank_rows(rows: list[dict], query: str) -> list[dict]:
    """按命中查询词的数量降序（稳定排序，同分保持原序）。
    为什么需要：兜底解析捞到的顺序是页面顺序，360/神马常把广告和导航排前面。"""
    terms = _query_terms(query)
    if not terms:
        return rows

    def hits(r):
        blob = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
        return sum(1 for t in terms if t in blob)

    return sorted(rows, key=hits, reverse=True)


def _mark_ok(engine: str) -> None:
    state = _load_state()
    state[f"{engine}.last_ok"] = time.time()
    state.pop(f"{engine}.blocked_until", None)
    _save_state(state)


def _health_ordered(order: list[str]) -> list[str]:
    """最近成功过的排前面；冷却中的挪到最后（不删：实在没别的了还得试）。"""
    now = time.time()
    state = _load_state()

    def key(idx_en):
        idx, en = idx_en
        cooling = 0 if _cooldown_left(en) <= 0 else 1
        try:
            ok_at = float(state.get(f"{en}.last_ok", 0))
        except (TypeError, ValueError):
            ok_at = 0.0
        warm = 0 if now - ok_at < 900 else 1
        return (cooling, warm, idx)

    return [en for _, en in sorted(enumerate(order), key=key)]


# ─────────────────────────── 主搜索入口 ───────────────────────────

def search(query: str, limit: int = 10, engine: str = "auto",
           proxy: str = "auto", retries: int = 2, budget: float | None = None) -> dict:
    """返回 {query, engine, via, results[], problems[], relevance}。

    budget：总时间预算（秒），默认 25s。超了就不再试新引擎，把已有原因原样报出去 ——
    搜索工具最怕的不是失败，是「卡 40 秒然后给一堆不相干的东西」。
    """
    budget = float(os.environ.get("DSH_WEB_SEARCH_BUDGET", budget or 25))
    max_engines = int(os.environ.get("DSH_WEB_MAX_ENGINES", "3"))
    deadline = time.time() + budget
    cached = _result_cache_get(query, limit)
    if cached is not None and engine == "auto":
        cached["cached"] = True
        cached.setdefault("problems", []).append("（来自 6 小时内的结果缓存，没有再打引擎）")
        return cached

    order = [engine] if engine != "auto" else _health_ordered(_auto_order(query))
    order = [e for e in order if e in ENGINES]
    problems: list[str] = []
    best = None  # (rel, engine, via, rows)

    if engine == "auto":
        cooling = [e for e in order if _cooldown_left(e) > 0]
        if cooling:
            problems.append("冷却中（刚被反爬拦过，稍后再试更稳）：" + "、".join(
                f"{e}(剩{int(_cooldown_left(e))}s)" for e in cooling))

    tried = 0
    for en in order:
        if time.time() > deadline:
            problems.append(f"超出 {int(budget)}s 预算，剩下的引擎没试（{en} 起）")
            break
        if tried >= max_engines:
            problems.append(f"这次已经试过 {tried} 个引擎，不再扩大试探（剩下的没试："
                            + "、".join(order[order.index(en):]) + "）")
            break
        if _cooldown_left(en) > 0 and tried > 0:
            continue                     # 冷却中的引擎不做「顺位试」，省一次白撞
        tried += 1
        spec = ENGINES[en]
        headers = {"User-Agent": UA_DESKTOP if spec.get("ua") == "desktop" else UA,
                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        rows, via, blocked, doc = [], None, None, ""
        for route in _routes_for_engine(spec, proxy):
            if time.time() > deadline:
                break
            for attempt in range(1, retries + 1):
                final = ""
                try:
                    _polite(en)
                    if spec["kind"] == "api":
                        rows = spec["fetch"](query, limit, route)
                        via, blocked = route.label, None
                    else:
                        url = spec["url"].format(q=urllib.parse.quote(query), n=limit)
                        raw, ctype, final, via = get(url, route, headers=headers)
                        doc = decode(raw, ctype)
                        blocked = _looks_blocked(doc)
                        rows = [] if blocked else spec["parse"](doc, limit)
                        if not rows and not blocked:
                            rows = _parse_generic(doc, limit)
                            if rows:
                                problems.append(f"{en}/{route.label}: 专用解析器没匹配到，吃了通用兜底")
                        if rows and spec.get("resolve"):
                            rows = _resolve_redirects(rows, route, limit)
                        rows = [r for r in rows if r.get("url") and _ok_host(r["url"])]
                except urllib.error.HTTPError as e:
                    problems.append(f"{en}/{route.label}: HTTP {e.code}")
                    blocked = f"HTTP {e.code}"
                    if e.code in (429, 503) and attempt < retries:
                        time.sleep(1.2 * attempt + random.random())
                        continue
                    break
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{en}/{route.label}: {type(e).__name__} {str(e)[:110]}".strip())
                    if attempt < retries:
                        time.sleep(0.5 * attempt + random.random() * 0.3)
                        continue
                    break

                rows = _dedupe(rows, limit)
                if rows:
                    rows = _rank_rows(rows, query)
                    score = _relevance(rows, query)
                    if score >= spec.get("min_rel", 0.34):
                        _mark_ok(en)
                        payload = {"query": query, "engine": en, "via": via, "final": final,
                                   "results": rows, "problems": problems, "relevance": round(score, 2)}
                        _result_cache_put(query, limit, payload)
                        return payload
                    problems.append(f"{en}/{route.label}: 命中但跑题（相关度 {score:.0%}），换引擎")
                    if best is None or score > best[0]:
                        best = (score, en, via, rows)
                    break
                if blocked:
                    problems.append(f"{en}/{route.label}: 被反爬拦了（{blocked}）")
                    _set_cooldown(en, 90 if "antispider" in str(blocked) else 45)
                    if attempt < retries and time.time() < deadline:
                        time.sleep(1.2 * attempt + random.random())
                        continue
                elif spec["kind"] == "api":
                    problems.append(f"{en}/{route.label}: 接口 0 结果")
                elif any(m in doc.lower() for m in _EMPTY_MARKERS):
                    problems.append(f"{en}/{route.label}: 真的 0 结果")
                else:
                    problems.append(f"{en}/{route.label}: 页面里没解析到结果（结构可能改了）")
                break            # 这条通路没戏，换通路
            if rows:
                break                # 这条引擎已经给出结论，别再换通路试了

    # 兜底再来一轮：验证页经常是「刚才打太急」，歇 4 秒回头试一次往往就放行了
    if time.time() + 6 < deadline and order:
        retry_en = "ddg" if "ddg" in order else order[0]
        problems.append(f"全部失败，歇 4s 回头再试一次 {retry_en}")
        time.sleep(4)
        spec = ENGINES[retry_en]
        try:
            _polite(retry_en)
            if spec["kind"] == "api":
                rows = spec["fetch"](query, limit, routes_for("auto")[0])
                via = "retry"
            else:
                url = spec["url"].format(q=urllib.parse.quote(query), n=limit)
                raw, ctype, final, via = get(url, _routes_for_engine(spec, proxy)[0],
                                             headers={"User-Agent": UA_DESKTOP if spec.get("ua") == "desktop" else UA,
                                                      "Accept-Language": "zh-CN,zh;q=0.9"})
                doc = decode(raw, ctype)
                blocked = _looks_blocked(doc)
                rows = [] if blocked else (spec["parse"](doc, limit) or _parse_generic(doc, limit))
                if rows and spec.get("resolve"):
                    rows = _resolve_redirects(rows, route=routes_for("auto")[0], limit=limit)
                rows = [r for r in rows if r.get("url") and _ok_host(r["url"])]
            rows = _dedupe(rows, limit)
            if rows:
                score = _relevance(rows, query)
                if score >= spec.get("min_rel", 0.34):
                    _mark_ok(retry_en)
                    payload = {"query": query, "engine": retry_en, "via": via, "final": "",
                               "results": rows, "problems": problems, "relevance": round(score, 2)}
                    _result_cache_put(query, limit, payload)
                    return payload
                problems.append(f"重试 {retry_en}: 仍然跑题（相关度 {score:.0%}）")
        except Exception as e:  # noqa: BLE001
            problems.append(f"重试 {retry_en}: {type(e).__name__} {str(e)[:80]}")

    # 宁缺毋滥：相关度不到 50% 的「最接近」也不返回，直接把原因交出去
    if best is not None and best[0] >= 0.5:
        score, en, via, rows = best
        problems.append(f"没有完全对上的引擎，返回最接近的一组（{en}，相关度 {score:.0%}）")
        return {"query": query, "engine": en, "via": via, "final": "",
                "results": rows, "problems": problems, "relevance": round(score, 2)}

    problems.append(
        "所有可用引擎都没交出合格结果。这多半是出口 IP 被反爬盯上了（本机走的是共享代理 IP），"
        "等 1~2 分钟再试通常就恢复；或换一条代理线路；"
        "急的话直接 `python3 websearch.py fetch <已知网址>` 抓具体页面。")
    return {"query": query, "engine": None, "via": None, "results": [],
            "problems": problems, "relevance": 0.0}

# ─────────────────────────── 抓取 ───────────────────────────

def _cache_path(url: str) -> str:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, key + ".json")


def fetch(url: str, maxchars: int = 12000, proxy: str = "auto",
          raw: bool = False, use_cache: bool = True) -> dict:
    """返回 {url, final, status, text|raw_text, via, chars, cached}。"""
    if use_cache:
        p = _cache_path(url)
        try:
            if time.time() - os.path.getmtime(p) < CACHE_TTL:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                d["cached"] = True
                return d
        except (OSError, ValueError):
            pass

    problems = []
    # 有些站点直连必然被墙：先走代理，省掉一次「干等 9 秒」。
    # 实测维基百科直连会一直等到超时（一次抓取白掉 50 秒，超过 dsh-tool-web 的 30s 上限）。
    host = (urllib.parse.urlsplit(url).netloc or "").lower()
    direct_blocked = any(host == h or host.endswith("." + h) for h in _PROXY_ONLY_HOSTS)
    routes = routes_for(proxy)
    if direct_blocked and proxy in ("auto", ""):
        routes = [Route(DEFAULT_PROXY), Route(None)]
    for route in routes:
        try:
            body, ctype, final, via = get(url, route)
        except urllib.error.HTTPError as e:
            problems.append(f"{route.label}: HTTP {e.code}")
            continue
        except Exception as e:  # noqa: BLE001
            problems.append(f"{route.label}: {type(e).__name__} {e}".strip())
            continue
        doc = decode(body, ctype)
        text = doc if raw else html_to_text(doc)
        out = {
            "url": url, "final": final, "via": via,
            "status": 200, "chars": len(text),
            "text": text[:maxchars], "problems": problems, "cached": False,
        }
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(_cache_path(url), "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
        except OSError:
            pass
        return out

    if not problems:
        problems.append("两条通路都没拿到内容")
    return {"url": url, "final": url, "via": None, "status": 0, "chars": 0,
            "text": "", "problems": problems, "cached": False}


def batch(path: str, outdir: str, proxy: str = "auto", sleep: float = 0.8,
          maxchars: int = 20000) -> int:
    """批量抓取一个 URL 清单（每行一个，# 开头是注释）。已存在的跳过 ⇒ 可续跑。"""
    urls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    os.makedirs(outdir, exist_ok=True)
    ok = fail = skip = 0
    for i, url in enumerate(urls, 1):
        name = hashlib.sha1(url.encode()).hexdigest()[:12]
        dest = os.path.join(outdir, name + ".txt")
        if os.path.exists(dest) and os.path.getsize(dest) > 120:
            skip += 1
            continue
        r = fetch(url, maxchars=maxchars, proxy=proxy)
        if r["status"] == 200 and r["chars"] > 80:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(f"# URL: {r['final']}\n# VIA: {r['via']}\n\n{r['text']}")
            ok += 1
            print(f"[{i}/{len(urls)}] OK   {r['chars']:>6} 字  {url}", file=sys.stderr)
        else:
            fail += 1
            print(f"[{i}/{len(urls)}] FAIL {url}  {'; '.join(r['problems'])}", file=sys.stderr)
        time.sleep(sleep)
    print(f"\n完成：成功 {ok} · 跳过 {skip} · 失败 {fail} · 输出目录 {outdir}")
    return 0 if fail == 0 else 1


# ─────────────────────────── CLI ───────────────────────────

def _print_search(res: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    if not res["results"]:
        print(f"✗ 没搜到：{res['query']}")
        for p in res["problems"]:
            print(f"  · {p}")
        return
    print(f"== {res['engine']} · {len(res['results'])} 条 · via {res['via']} · {res['query']}")
    for i, r in enumerate(res["results"], 1):
        print(f"{i}. {r['title']}")
        print(f"   {r['url']}")
        if r.get("snippet"):
            print(f"   {r['snippet']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="websearch", description="网页检索/抓取兜底链路")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="搜索引擎检索")
    s.add_argument("query")
    s.add_argument("-n", "--num", type=int, default=10)
    s.add_argument("-e", "--engine", default="auto",
                   choices=["auto", *ENGINES.keys()])
    s.add_argument("--proxy", default="auto")
    s.add_argument("--json", action="store_true")

    f = sub.add_parser("fetch", help="抓单个网页")
    f.add_argument("url")
    f.add_argument("--max", type=int, default=12000)
    f.add_argument("--proxy", default="auto")
    f.add_argument("--raw", action="store_true", help="输出原始 HTML 而不是纯文本")
    f.add_argument("--no-cache", action="store_true")
    f.add_argument("--json", action="store_true")

    b = sub.add_parser("batch", help="批量抓 URL 清单（可断点续跑）")
    b.add_argument("file")
    b.add_argument("--out", required=True)
    b.add_argument("--proxy", default="auto")
    b.add_argument("--sleep", type=float, default=0.8)
    b.add_argument("--max", type=int, default=20000)

    p = sub.add_parser("probe", help="探测直连 / 代理哪条通")
    p.add_argument("--proxy", default="auto")

    a = ap.parse_args(argv)

    if a.cmd == "search":
        res = search(a.query, a.num, a.engine, a.proxy)
        _print_search(res, a.json)
        return 0 if res["results"] else 2

    if a.cmd == "fetch":
        r = fetch(a.url, a.max, a.proxy, a.raw, not a.no_cache)
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
        elif r["status"] == 200:
            print(f"# URL: {r['final']}\n# VIA: {r['via']}  CHARS: {r['chars']}\n")
            print(r["text"])
        else:
            print(f"✗ 抓取失败 {a.url}")
            for x in r["problems"]:
                print(f"  · {x}")
        return 0 if r["status"] == 200 else 2

    if a.cmd == "batch":
        return batch(a.file, a.out, a.proxy, a.sleep, a.max)

    if a.cmd == "probe":
        # 逐引擎实测：用的就是搜索时那条通路，所以看到的结果＝真实可用性
        q = "泉州 到 厦门 高铁 时刻表"
        print(f"探测查询：{q}\n{'引擎':<14}{'通路':<12}{'结果':>4}  {'相关度':>5}  {'耗时':>6}")
        for en in ("sogou", "sm", "so360", "quark", "ddg", "ddg-lite", "bing", "baidu"):
            spec = ENGINES[en]
            route = _routes_for_engine(spec, a.proxy)[0]
            t0 = time.time()
            try:
                url = spec["url"].format(q=urllib.parse.quote(q), n=10)
                raw, ctype, final, via = get(url, route, headers={
                    "User-Agent": UA_DESKTOP if spec.get("ua") == "desktop" else UA,
                    "Accept-Language": "zh-CN,zh;q=0.9"})
                doc = decode(raw, ctype)
                if _looks_blocked(doc):
                    print(f"{en:<14}{route.label:<12}{'被拦':>4}  {'-':>5}  {int((time.time()-t0)*1000):>5}ms  ({_looks_blocked(doc)})")
                    continue
                rows = spec["parse"](doc, 10)
                rel = _relevance(rows, q) if rows else 0
                print(f"{en:<14}{route.label:<12}{len(rows):>4}  {rel:>5.2f}  {int((time.time()-t0)*1000):>5}ms")
            except Exception as e:  # noqa: BLE001
                print(f"{en:<14}{route.label:<12}{'FAIL':>4}  {'-':>5}  {int((time.time()-t0)*1000):>5}ms  {type(e).__name__} {str(e)[:50]}")
        for en in ("wikipedia", "stackexchange", "github"):
            spec = ENGINES[en]
            t0 = time.time()
            try:
                rows = spec["fetch"](q, 5, _routes_for_engine(spec, a.proxy)[0])
                print(f"{en:<14}{'接口':<12}{len(rows):>4}  {'-':>5}  {int((time.time()-t0)*1000):>5}ms")
            except Exception as e:  # noqa: BLE001
                print(f"{en:<14}{'接口':<12}{'FAIL':>4}  {'-':>5}  {int((time.time()-t0)*1000):>5}ms  {type(e).__name__} {str(e)[:50]}")
        print("\n提示：被拦的引擎会自己冷却 45~90 秒；连续探测本身也会触发限流，别一直跑。")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
