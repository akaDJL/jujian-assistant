"""
居间小助手 · 公开联系人挖掘爬虫
================================

设计目标（对应工程居间人的真实需求）：
    输入一批目标企业（或关键词）-> 突破搜索引擎反爬 -> 抓取公开网页 ->
    抽取业务部门联系人 / 电话 / 邮箱 / 关键人 -> 结构化返回。

反爬策略（来自上一轮调研结论，轻量优先）：
    1. TLS 指纹伪装：curl_cffi 的 impersonate=chrome，过 JA3/TLS 检测，无需浏览器。
    2. 真实 UA + 中文 Accept-Language，避免被认成脚本。
    3. 并发受控（Semaphore）+ 随机延时，不对单站打爆。
    4. 可选代理（SCRAPER_PROXY 环境变量），需要时挂住宅/机房代理换 IP。
    5. 自动跟随重定向（Baidu 的 link?url= 中转）。

合规边界（务必遵守）：
    * 仅采集你有权访问的「公开」数据；不碰登录态、不破付费墙、不爬隐私字段。
    * 遵守目标站点 robots.txt 与 ToS；控制频率，别把对方服务器压垮。
    * 本工具定位为「公开信息聚合辅助」，输出仅供参考，请自行核实真实性。
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from urllib.parse import quote

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 代理：默认不走 WorkBuddy 透明代理，直连；如需换 IP 设 SCRAPER_PROXY=http://host:port
PROXY: str | None = os.getenv("SCRAPER_PROXY") or None

# 企业库登录态 Cookie（可选，进阶）：用你自己的企查查/天眼查账号登录后复制 Cookie 粘贴到环境变量，
# 可解锁「法定代表人 + 联系电话」详情页。仅建议在合法授权范围内、针对你有正当商务利益的企业使用，
# 不采集隐私数据、不破付费墙。未设置则跳过这两家，只走免登录的爱企查。
QCC_COOKIE: str = os.getenv("QCC_COOKIE") or ""
TYCC_COOKIE: str = os.getenv("TYCC_COOKIE") or ""

# 随机延时区间（秒），模拟真人节奏（已优化：原 0.6~1.8 太保守）
DELAY_MIN, DELAY_MAX = 0.3, 0.8

# 并发抓取页数上限
CONCURRENCY = 4

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}

# 搜索引擎定义（默认 Sogou：对中文企业结果最相关，且对机房 IP 不降级）
SEARCH_ENGINES = {
    "sogou": {
        "base": "https://www.sogou.com/web",
        "params": lambda q: {"query": q},
        "parse": "sogou",
    },
    "bing": {
        "base": "https://cn.bing.com/search",
        "params": lambda q: {"q": q, "count": 20, "mkt": "zh-CN", "setlang": "zh-CN"},
        "parse": "bing",
    },
    "baidu": {
        "base": "https://www.baidu.com/s",
        "params": lambda q: {"wd": q, "rn": 20},
        "parse": "baidu",
    },
}

# 低价值/纯百科-UGC 域名：仅当页面确实提到目标公司时才保留
LOW_VALUE_HOSTS = {
    "baike.baidu.com", "zhihu.com", "map.baidu.com", "baike.baidu.com",
    "wikipedia.org", "visitbeijing.com.cn", "thepaper.cn",
}


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""

# ---------------------------------------------------------------------------
# 正则
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# 手机号：11 位，兼容 +86 / 86 前缀与中间空格/短横分隔（如 138 0013 8000）
MOBILE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?(1[3-9](?:[\s\-]?\d){9})(?!\d)")

# 座机：0 区号 + 7~8 位（与手机以"1"开头区分，互不冲突）
LANDLINE_RE = re.compile(r"(?<!\d)(0\d{2,3}[-\s]?\d{7,8})(?!\d)")

# 关键人启发式：标签词 + 2~4 个汉字的人名
NAME_LABEL_RE = re.compile(
    r"(?:联系人|负责人|项目经理|项目主管|商务经理|销售经理|采购经理|"
    r"项目联系人|对接人|招标联系人|法人代表|执行事务合伙人)\s*[:：]?\s*([\u4e00-\u9fa5]{2,4})"
)
NAME_ROLE_RE = re.compile(
    r"(?<![经主部主任总商销采工])([\u4e00-\u9fa5]{2,3})\s*[(（]\s*(?:项目经理|项目主管|负责人|"
    r"商务|销售|采购|总|经理|主任|部长|工程师)\s*[)）]"
)

# 法定代表人（单独抽取，便于在结果中标注「法人」身份 —— 居间最看重的决策联系人）
LEGAL_RE = re.compile(r"法定代表人\s*[:：]?\s*([\u4e00-\u9fa5]{2,4})")


# ---------------------------------------------------------------------------
# 查询构造
# ---------------------------------------------------------------------------

# 公司名归一化：去掉常见省市前缀与组织后缀，提取核心词用于相关性匹配
PREFIXES = [
    "北京市", "上海市", "广东省", "江苏省", "浙江省", "山西省", "山东省", "四川省",
    "河北省", "河南省", "北京市", "中国", "北京", "上海", "广东", "江苏", "浙江",
    "山西", "山东", "四川", "河北", "河南", "省", "市", "县",
]
SUFFIXES = [
    "股份有限公司", "有限责任公司", "有限公司", "股份公司", "集团有限公司",
    "科技有限公司", "技术有限公司", "科技股份有限公司", "科技公司", "技术公司",
    "环保科技有限公司", "环保工程有限公司", "建设工程有限公司", "工程有限公司",
    "投资有限公司", "控股有限公司", "发展有限公司", "实业有限公司",
    "装备制造有限公司", "装备股份有限公司", "能源科技有限公司", "电气有限公司",
    "集团有限公司", "集团", "公司",
]


def company_tokens(name: str) -> list[str]:
    """提取用于相关性匹配的公司核心词（含 3-gram 滑窗）。"""
    n = name.strip()
    for p in PREFIXES:
        if n.startswith(p) and len(n) > len(p) + 1:
            n = n[len(p):]
    for s in SUFFIXES:
        if n.endswith(s) and len(n) > len(s) + 1:
            n = n[: -len(s)]
            break
    core = n
    toks = {core}
    if len(core) >= 3:
        toks.add(core[:3])
    if len(core) >= 4:
        toks.add(core[:4]); toks.add(core[-4:])
    for i in range(len(core) - 2):
        toks.add(core[i:i + 3])
    return [t for t in toks if len(t) >= 2]


def build_queries(company: str, focus: str = "") -> list[str]:
    """
    构造多意图查询（覆盖联系方式全维度，重点强化「联系人 + 手机号」）：
      1) 公司名 + 联系电话/邮箱/地址/联系人 —— 命中官网与工商展示页
      2) 公司名 + 项目部/负责人 + 招标公告 —— 居间最想要的「项目对接人」
      3) 公司名 + 联系人 + 手机号 —— 专门挖个人移动电话
      4) 公司名 + 业务经理/招商 + 手机 —— 命中 B2B/招商页里的业务联系人手机
    """
    c = company.strip()
    qs = [
        f'"{c}" 联系电话 邮箱 地址 联系人',
        f'"{c}" 项目部 负责人 招标公告',
        f'"{c}" 联系人 手机号',
        f'"{c}" 业务经理 招商 手机',
    ]
    f = (focus or "").strip()
    if f:
        qs.append(f'"{c}" {f} 项目负责人 手机')
    else:
        qs.append(f'"{c}" 采购部 联系人 手机')
    return qs


def build_mobile_queries(company: str, focus: str = "") -> list[str]:
    """
    手机专项查询（新思路：钉死手机号高命中公开源）：
      ① 招标/采购公告 —— 手机号金矿（招标人/代理/项目负责人必留手机）
      ② 官网联系页 + B2B/黄页（慧聪/顺企）—— 中小企业业务员私人手机密集
      ③ 微信搜狗（公众号/文章常留业务手机，无需登录）
    """
    c = company.strip()
    qs = [
        f'"{c}" 招标公告 代理 联系人 手机',
        f'"{c}" 中标 项目经理 联系电话 手机',
        f'"{c}" 官网 联系我们 业务合作 手机',
        f'"{c}" 慧聪网 联系人 手机',
        f'"{c}" 顺企网 业务经理 手机',
    ]
    f = (focus or "").strip()
    if f:
        qs.append(f'"{c}" {f} 手机 联系电话')
    return qs


def build_enterprise_queries(company: str) -> list[str]:
    """
    新思路④：直接面向企业库的查询意图（爱企查/企查查/天眼查），
    钉死「法定代表人 + 联系电话/手机」这一最值钱的公开工商字段。
    """
    c = company.strip()
    return [
        f'"{c}" 法定代表人 联系电话',
        f'"{c}" 法定代表人 手机 联系方式',
        f'"{c}" 工商信息 注册资本 联系电话',
    ]


def relevance(rec: dict, tokens: list[str]) -> int:
    """排序分：公司核心词命中越多越高，联系信息越丰富越高（手机号/配对联系人权重最高）。"""
    hay = (rec.get("snippet", "") + " " + rec["source"]).lower()
    score = sum(1 for t in tokens if t.lower() in hay)
    score += len(rec.get("emails", [])) + len(rec.get("phones", []))
    score += 2 * len(rec.get("mobiles", []))
    score += 2 * len(rec.get("names", []))
    score += 3 * len(rec.get("contacts", []))  # 已配对「人名+手机」价值最高
    return score


# ---------------------------------------------------------------------------
# SERP 解析
# ---------------------------------------------------------------------------

def parse_serp(engine: str, html: str, max_results: int) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []

    if engine == "bing":
        for li in soup.select("li.b_algo"):
            h2 = li.find("h2")
            a = h2.find("a") if h2 else None
            href = a.get("href") if a else None
            if href and href.startswith("http"):
                urls.append(href)
            if len(urls) >= max_results:
                break

    elif engine == "baidu":
        for div in soup.select("div.result, div.c-container"):
            a = div.find("a", href=True)
            href = a.get("href") if a else None
            # Baidu 真实地址在中转链接里，靠后续请求跟随重定向解析
            if href and ("http" in href):
                urls.append(href)
            if len(urls) >= max_results:
                break

    elif engine == "sogou":
        anchors = soup.select(
            ".rb h3 a, .vrwrap h3 a, .results h3 a, a[data-mdurl]"
        )
        if not anchors:
            anchors = soup.select("h3 a")
        for a in anchors:
            href = a.get("href", "")
            if not href:
                continue
            # Sogou 中转链接 /link?url=...，需拼绝对地址后由 client 跟随 302 到真实页
            if href.startswith("/"):
                href = "https://www.sogou.com" + href
            if href.startswith("http"):
                urls.append(href)
            if len(urls) >= max_results:
                break

    # 兜底：页面里所有外链去重截取
    if not urls:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and "bing.com" not in href and "baidu.com" not in href:
                urls.append(href)
            if len(urls) >= max_results:
                break

    return urls[:max_results]


# ---------------------------------------------------------------------------
# 联系人抽取
# ---------------------------------------------------------------------------

def extract_contacts(html: str, source: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    if not text:
        return None

    emails = sorted(set(EMAIL_RE.findall(text)))

    # 手机号单独抽取（清洗掉 +86 / 空格 / 短横）
    mobiles_raw = MOBILE_RE.findall(text)
    mobiles = sorted({re.sub(r"[\s\-]", "", m) for m in mobiles_raw})

    # 座机（与手机号分离，避免混淆）
    phones = sorted(set(LANDLINE_RE.findall(text)))

    names: set[str] = set()
    for m in NAME_LABEL_RE.finditer(text):
        names.add(m.group(1))
    for m in NAME_ROLE_RE.finditer(text):
        names.add(m.group(1))

    # 法定代表人（单独抽取，便于标注身份；从普通 names 中剔除避免重复）
    legal: set[str] = set()
    for m in LEGAL_RE.finditer(text):
        legal.add(m.group(1))
    names -= legal

    # 人名 + 手机号就近配对（同一页面内"张三 138..."自动关联）
    contacts = _pair_contacts(text, names)

    # 仅保留「真的挖到点东西」的页面，减少噪音
    if not (emails or mobiles or phones or names or legal):
        return None

    # 生成一句摘要：取第一个出现联系信息的片段
    snippet = make_snippet(text, emails, mobiles, phones, names)

    return {
        "source": source,
        "emails": emails,
        "mobiles": mobiles,
        "phones": phones,
        "names": sorted(names),
        "legal": sorted(legal),
        "contacts": contacts,
        "snippet": snippet,
    }


def _pair_contacts(text: str, names: set[str]) -> list[dict]:
    """
    最佳努力关联：在同一页面文本里，把「人名」与「就近出现的手机号」配对。
    仅当人名与手机号距离 < 40 字符时才配对，避免乱点鸳鸯。
    返回 [{"name":..., "mobile":...}, ...]
    """
    if not names:
        return []
    # 人名出现位置
    name_spans: list[tuple[int, str]] = []
    for rx in (NAME_LABEL_RE, NAME_ROLE_RE):
        for m in rx.finditer(text):
            name_spans.append((m.start(), m.group(1)))
    if not name_spans:
        return []
    # 手机号出现位置（已清洗）
    mob_spans: list[tuple[int, str]] = [
        (m.start(), re.sub(r"[\s\-]", "", m.group(1))) for m in MOBILE_RE.finditer(text)
    ]
    if not mob_spans:
        return []

    used: set[int] = set()
    out: list[dict] = []
    for np_, name in name_spans:
        best = None
        best_d = 40  # 关联窗口（字符）
        for mp_, mob in mob_spans:
            if mp_ in used:
                continue
            d = abs(mp_ - np_)
            if d < best_d:
                best_d = d
                best = (mp_, mob)
        if best:
            used.add(best[0])
            out.append({"name": name, "mobile": best[1]})
    return out


def make_snippet(text: str, emails, mobiles, phones, names) -> str:
    markers = []
    if emails:
        markers.append(emails[0])
    if mobiles:
        markers.append(mobiles[0])
    if phones:
        markers.append(phones[0])
    if names:
        markers.append(next(iter(names)))
    if not markers:
        return text[:120]
    pos = min(text.find(m) for m in markers if m in text)
    if pos < 0:
        return text[:120]
    start = max(0, pos - 40)
    return text[start : start + 160]


# ---------------------------------------------------------------------------
# 核心抓取
# ---------------------------------------------------------------------------

async def _fetch_serp(client, eng: dict, q: str, max_results: int, retries: int = 2) -> list[str]:
    """拉搜索结果页并解析 URL；遇反爬空结果自动重试。"""
    for attempt in range(retries + 1):
        try:
            resp = await client.get(eng["base"], params=eng["params"](q), timeout=20)
            urls = parse_serp(eng["parse"], resp.text, max_results)
            if urls:
                return urls
        except Exception as e:
            print(f"[warn] SERP {eng['parse']} 第{attempt}次失败: {e}")
        await asyncio.sleep(1.2)
    return []


async def _fetch_enterprise(client, company: str, max_results: int) -> list[dict]:
    """
    新思路④：直接打企业库站点，挖「法定代表人 + 联系电话」。
      - 爱企查（百度）：免登录，搜索卡片常直接露出法定代表人与联系电话，最稳。
      - 企查查/天眼查：若设置了 QCC_COOKIE / TYCC_COOKIE（你自己的登录态），
        可解锁详情页里的联系电话；未设置则跳过。
    返回已抽取联系信息的记录列表（可能为空）。
    """
    tokens = company_tokens(company)
    recs: list[dict] = []

    # 1) 爱企查（免登录）
    try:
        r = await client.get(
            "https://aiqicha.baidu.com/s", params={"q": company, "t": 0}, timeout=20
        )
        sp = extract_contacts(r.text, r.url or "https://aiqicha.baidu.com/s")
        if sp:
            recs.append(sp)
        detail = re.findall(r'href="(/company_detail[^"?]+)"', r.text)
        detail = ["https://aiqicha.baidu.com" + u for u in dict.fromkeys(detail)][:max_results]
        if detail:
            recs.extend(await _fetch_pages(client, detail, tokens))
    except Exception as e:
        print(f"[warn] 爱企查查询失败: {e}")

    # 2) 企查查（需登录态 Cookie）
    if QCC_COOKIE:
        try:
            r = await client.get(
                "https://www.qcc.com/web/search", params={"key": company},
                headers={"Cookie": QCC_COOKIE}, timeout=20,
            )
            detail = re.findall(r'href="(/company/[^"?]+\.html)"', r.text)
            detail = ["https://www.qcc.com" + u for u in dict.fromkeys(detail)][:max_results]
            if detail:
                recs.extend(await _fetch_pages(client, detail, tokens, headers={"Cookie": QCC_COOKIE}))
        except Exception as e:
            print(f"[warn] 企查查查询失败: {e}")

    # 3) 天眼查（需登录态 Cookie）
    if TYCC_COOKIE:
        try:
            r = await client.get(
                "https://www.tianyancha.com/search", params={"key": company},
                headers={"Cookie": TYCC_COOKIE}, timeout=20,
            )
            detail = re.findall(r'href="(/company/\d+\.html)"', r.text)
            detail = ["https://www.tianyancha.com" + u for u in dict.fromkeys(detail)][:max_results]
            if detail:
                recs.extend(await _fetch_pages(client, detail, tokens, headers={"Cookie": TYCC_COOKIE}))
        except Exception as e:
            print(f"[warn] 天眼查查询失败: {e}")

    return recs


async def search_company(
    company: str,
    focus: str = "",
    engine: str = "sogou",
    max_results: int = 10,
    mode: str = "general",
) -> list[dict]:
    """
    对单家企业：
      general 模式 —— 多引擎回退（任一引擎拿到含联系信息页面即返回，快）
      mobile   模式 —— 多引擎累积 + 手机专项查询，含手机号的结果优先置顶（更全）
    """
    build = build_mobile_queries if mode == "mobile" else build_queries
    queries = build(company, focus)
    tokens = company_tokens(company)
    engines = [engine] + [e for e in ("sogou", "bing", "baidu") if e != engine]

    client = AsyncSession(
        impersonate="chrome",
        headers=HEADERS,
        proxy=PROXY,
        timeout=20,
        verify=False,
    )
    collected: list[dict] = []
    try:
        for eng_name in engines:
            eng = SEARCH_ENGINES[eng_name]
            url_set: list[str] = []
            for q in queries:
                urls = await _fetch_serp(client, eng, q, max_results)
                for u in urls:
                    if u not in url_set:
                        url_set.append(u)
                if len(url_set) >= max_results:
                    break
            url_set = url_set[:max_results]
            if not url_set:
                continue
            found = await _fetch_pages(client, url_set, tokens)
            if mode != "mobile" and found:  # 通用模式：拿到即返（原行为，快）
                return found
            collected.extend(found)
        if mode == "mobile":
            # 新思路④：直接打企业库（爱企查免登录 + 企查查/天眼查可选登录态）
            collected.extend(await _fetch_enterprise(client, company, max_results))
            # 去重 + 排序：高置信 > 含手机号 > 相关度
            seen: set[str] = set()
            out: list[dict] = []
            for r in collected:
                if r["source"] not in seen:
                    seen.add(r["source"])
                    out.append(r)
            out.sort(
                key=lambda r: (
                    r["confidence"] == "high",
                    len(r.get("mobiles", [])) > 0,
                    relevance(r, tokens),
                ),
                reverse=True,
            )
            return out
        return []
    finally:
        await client.close()


async def _fetch_pages(client, url_set: list[str], tokens: list[str], headers: dict | None = None) -> list[dict]:
    """并发抓取结果页并抽取 / 过滤 / 排序联系人。headers 用于企业库登录态 Cookie。"""
    sem = asyncio.Semaphore(CONCURRENCY)

    async def worker(url: str) -> dict | None:
        async with sem:
            try:
                pr = await client.get(url, timeout=15, headers=headers or {})
                rec = extract_contacts(pr.text, pr.url or url)
            except Exception:
                rec = None
            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            return rec

    tasks = [asyncio.create_task(worker(u)) for u in url_set]
    raw: list[dict] = []
    for done in asyncio.as_completed(tasks):
        rec = await done
        if rec:
            raw.append(rec)

    kept: list[dict] = []
    for r in raw:
        hay = (r.get("snippet", "") + " " + r["source"]).lower()
        hit = any(t.lower() in hay for t in tokens)
        r["confidence"] = "high" if hit else "low"
        # 低价值域名（百科/UGC/政府门户）且未命中公司名 -> 视为噪声丢弃
        if r["confidence"] == "low" and _host_of(r["source"]) in LOW_VALUE_HOSTS:
            continue
        kept.append(r)
    kept.sort(key=lambda r: (r["confidence"] == "high", relevance(r, tokens)), reverse=True)
    return kept


async def search_multi(
    companies: list[str],
    focus: str = "",
    engine: str = "sogou",
    max_results: int = 10,
    mode: str = "general",
) -> list[dict]:
    """批量企业并发搜索，结果按来源去重。mode=mobile 时启用手机专项策略。"""
    companies = [c for c in (companies or []) if c.strip()]
    if not companies:
        return []

    results = await asyncio.gather(
        *[search_company(c, focus, engine, max_results, mode) for c in companies]
    )

    merged: list[dict] = []
    for r in results:
        merged.extend(r)

    seen: set[str] = set()
    out: list[dict] = []
    for r in merged:
        if r["source"] not in seen:
            seen.add(r["source"])
            out.append(r)
    return out


# 供直接命令行调试：python scraper.py "山西某环保公司"
if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "碧水源 环保"
    data = asyncio.run(search_multi([q], "", "bing", 8))
    print(f"核心词: {company_tokens(q)}")
    for d in data:
        print(f"\n来源: {d['source']}")
        print(f"  邮箱: {d['emails']}")
        print(f"  电话: {d['phones']}")
        print(f"  关键人: {d['names']}")
        print(f"  摘要: {d['snippet']}")
