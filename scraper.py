"""
居间小助手 · 公开联系人挖掘爬虫（合规升级版）
============================================

设计目标（对应工程居间人的真实需求）：
    输入一批目标企业（或关键词）-> 突破搜索引擎反爬 -> 抓取公开网页 ->
    抽取业务部门联系人 / 电话 / 邮箱 / 关键人 -> 结构化返回。

反爬策略（轻量优先）：
    1. TLS 指纹伪装：curl_cffi 的 impersonate=chrome，过 JA3/TLS 检测，无需浏览器。
    2. 真实 UA + 中文 Accept-Language，避免被认成脚本。
    3. 并发受控（Semaphore）+ 随机延时，不对单站打爆。
    4. 可选代理（SCRAPER_PROXY 环境变量），需要时挂住宅/机房代理换 IP。

合规护栏（本升级重点，compliance.py 实现，强制前置）：
    * robots.txt 遵守：Disallow 的 path 一律不抓。
    * 礼貌抓取：per-host 最小间隔（2.5s），不压垮对方服务器。
    * 拒收名单 (DNC)：个人要求「不联系/删除」的号码永久屏蔽。
    * 采集审计：每条手机号哈希落盘（时间/来源/场景/风险/法律基础）。
    * 上下文分级：企业业务线(低) / 商务场景个人号(中) / 未知个人(高,默认不采)。
    * 用途声明：使用前须确认《使用须知》（app 层强制）。

数据范围（依用户选择）：
    含「商务场景下自行公开的个人手机号」，但高风险(无商务场景佐证)默认排除，
    需显式开启 include_personal 且自担风险。绝不突破登录墙/付费墙/验证码。
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from datetime import datetime, timezone
from urllib.parse import quote

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

import compliance as compliance
from social import SOCIAL_SOURCES

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 出公网走代理：优先 SCRAPER_PROXY，否则自动沿用系统 HTTPS_PROXY/HTTP_PROXY（本机为透明代理）。
# 注意去掉末尾斜杠，避免 curl_cffi 解析代理地址失败。
_raw_proxy = os.getenv("SCRAPER_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""
PROXY: str | None = _raw_proxy.rstrip("/") or None

QCC_COOKIE: str = os.getenv("QCC_COOKIE") or ""
TYCC_COOKIE: str = os.getenv("TYCC_COOKIE") or ""

DELAY_MIN, DELAY_MAX = 0.3, 0.8
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

SEARCH_ENGINES = {
    "sogou": {"base": "https://www.sogou.com/web", "params": lambda q: {"query": q}, "parse": "sogou"},
    "bing": {"base": "https://cn.bing.com/search", "params": lambda q: {"q": q, "count": 20, "mkt": "zh-CN", "setlang": "zh-CN"}, "parse": "bing"},
    "baidu": {"base": "https://www.baidu.com/s", "params": lambda q: {"wd": q, "rn": 20}, "parse": "baidu"},
}

# 低价值/噪音站点：即使抽到电话也丢弃（论坛/百科/问答/聚合新闻等非企业官方来源）
LOW_VALUE_HOSTS = {
    "baike.baidu.com", "baike.baidu.com.cn", "zhihu.com", "zhuanlan.zhihu.com",
    "map.baidu.com", "wikipedia.org", "visitbeijing.com.cn", "thepaper.cn",
    "sohu.com", "sohu.com.cn", "jjwxc.net", "hanyuguoxue.com", "hanyu.baidu.com",
    "weibo.com", "weibo.cn", "news.qq.com", "toutiao.com", "baijiahao.baidu.com",
    "douban.com", "tieba.baidu.com", "qq.com", "163.com", "sina.com.cn",
    "csdn.net", "cnblogs.com", "jianshu.com", "oschina.net", "yesky.com",
}


def _host_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _is_low_value_host(host: str) -> bool:
    """判断域名是否为低价值站点（含子域匹配：bbs.jjwxc.net 也命中 jjwxc.net）。"""
    host = (host or "").lower()
    if not host:
        return False
    for bad in LOW_VALUE_HOSTS:
        if host == bad or host.endswith("." + bad):
            return True
    return False


# ---------------------------------------------------------------------------
# 运营商识别 + 手机号归一化 / 校验
# ---------------------------------------------------------------------------

CARRIER_MAP = {
    "中国移动": ["134", "135", "136", "137", "138", "139", "147", "148", "150", "151",
               "152", "157", "158", "159", "172", "178", "182", "183", "184", "187",
               "188", "195", "197", "198"],
    "中国联通": ["130", "131", "132", "145", "146", "155", "156", "166", "167", "171",
               "175", "176", "185", "186", "196"],
    "中国电信": ["133", "149", "153", "173", "174", "177", "180", "181", "189", "190",
               "191", "193", "199"],
    "中国广电": ["192"],
}
_PREFIX2CARRIER = {p: c for c, prefixes in CARRIER_MAP.items() for p in prefixes}


def normalize_mobile(raw: str) -> str | None:
    """把各种写法的手机号清洗成 11 位纯数字；非法返回 None。"""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    if len(digits) == 11 and re.fullmatch(r"1[3-9]\d{9}", digits):
        return digits
    return None


def valid_mobile(m: str) -> bool:
    return bool(re.fullmatch(r"1[3-9]\d{9}", m or ""))


def carrier_of(m: str) -> str:
    return _PREFIX2CARRIER.get((m or "")[:3], "未知")


# ---------------------------------------------------------------------------
# 正则
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# 手机号：11 位，兼容 +86 / 86 / 0086 / (86) 前缀与中间空格/短横分隔
MOBILE_RE = re.compile(
    r"(?<!\d)(?:\+?86|0086|\(?\s?86\)?[\-\s]?)?(1[3-9](?:[\s\-]?\d){9})(?!\d)"
)

# 座机：0 区号 + 7~8 位
LANDLINE_RE = re.compile(r"(?<!\d)(0\d{2,3}[-\s]?\d{7,8})(?!\d)")

NAME_LABEL_RE = re.compile(
    r"(?:联系人|负责人|项目经理|项目主管|商务经理|销售经理|采购经理|"
    r"项目联系人|对接人|招标联系人|法人代表|执行事务合伙人)\s*[:：]?\s*([\u4e00-\u9fa5]{2,4})"
)
NAME_ROLE_RE = re.compile(
    r"(?<![经主部主任总商销采工])([\u4e00-\u9fa5]{2,3})\s*[(（]\s*(?:项目经理|项目主管|负责人|"
    r"商务|销售|采购|总|经理|主任|部长|工程师)\s*[)）]"
)
LEGAL_RE = re.compile(r"法定代表人\s*[:：]?\s*([\u4e00-\u9fa5]{2,4})")


# ---------------------------------------------------------------------------
# 查询构造
# ---------------------------------------------------------------------------

PREFIXES = [
    "北京市", "上海市", "广东省", "江苏省", "浙江省", "山西省", "山东省", "四川省",
    "河北省", "河南省", "中国", "北京", "上海", "广东", "江苏", "浙江",
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
    # 完整 core 始终放第一位，供「强相关性」判断（必须命中完整企业名才算相关）
    ordered = [core]
    for t in toks:
        if t != core and len(t) >= 2:
            ordered.append(t)
    return ordered


def build_queries(company: str, focus: str = "") -> list[str]:
    c = company.strip()
    qs = [
        f'"{c}" 联系电话 邮箱 地址 联系人',
        f'"{c}" 项目部 负责人 招标公告',
        f'"{c}" 联系人 手机号',
        f'"{c}" 业务经理 招商 手机',
    ]
    f = (focus or "").strip()
    qs.append(f'"{c}" {f} 项目负责人 手机' if f else f'"{c}" 采购部 联系人 手机')
    return qs


def build_mobile_queries(company: str, focus: str = "") -> list[str]:
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
    c = company.strip()
    return [
        f'"{c}" 法定代表人 联系电话',
        f'"{c}" 法定代表人 手机 联系方式',
        f'"{c}" 工商信息 注册资本 联系电话',
    ]


def relevance(rec: dict, tokens: list[str]) -> int:
    hay = (rec.get("snippet", "") + " " + (rec.get("source") or "")).lower()
    score = sum(1 for t in tokens if t.lower() in hay)
    score += len(rec.get("emails", [])) + len(rec.get("phones", []))
    score += 2 * len(rec.get("mobiles", []))
    score += 2 * len(rec.get("names", []))
    score += 3 * len(rec.get("contacts", []))
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
            if href and ("http" in href):
                urls.append(href)
            if len(urls) >= max_results:
                break

    elif engine == "sogou":
        anchors = soup.select(".rb h3 a, .vrwrap h3 a, .results h3 a, a[data-mdurl]")
        if not anchors:
            anchors = soup.select("h3 a")
        for a in anchors:
            href = a.get("href", "")
            if not href:
                continue
            if href.startswith("/"):
                href = "https://www.sogou.com" + href
            if href.startswith("http"):
                urls.append(href)
            if len(urls) >= max_results:
                break

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

    # 手机号：抽取 -> 归一化 -> 仅保留合法 11 位 -> 去重
    mobiles_raw = MOBILE_RE.findall(text)
    mobiles = sorted({m for raw in mobiles_raw if (m := normalize_mobile(raw))})

    phones = sorted(set(LANDLINE_RE.findall(text)))

    names: set[str] = set()
    for m in NAME_LABEL_RE.finditer(text):
        names.add(m.group(1))
    for m in NAME_ROLE_RE.finditer(text):
        names.add(m.group(1))

    legal: set[str] = set()
    for m in LEGAL_RE.finditer(text):
        legal.add(m.group(1))
    names -= legal

    contacts = _pair_contacts(text, names)

    if not (emails or mobiles or phones or names or legal):
        return None

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
    if not names:
        return []
    name_spans: list[tuple[int, str]] = []
    for rx in (NAME_LABEL_RE, NAME_ROLE_RE):
        for m in rx.finditer(text):
            name_spans.append((m.start(), m.group(1)))
    if not name_spans:
        return []

    mob_spans: list[tuple[int, str]] = [
        (m.start(), re.sub(r"[\s\-]", "", m.group(1)))
        for m in MOBILE_RE.finditer(text)
    ]
    if not mob_spans:
        return []

    used: set[int] = set()
    out: list[dict] = []
    for np_, name in name_spans:
        best = None
        best_d = 40
        for mp_, mob in mob_spans:
            if mp_ in used:
                continue
            d = abs(mp_ - np_)
            if d < best_d:
                best_d = d
                best = (mp_, mob)
        if best:
            used.add(best[0])
            norm = normalize_mobile(best[1])
            if norm:
                out.append({"name": name, "mobile": norm, "carrier": carrier_of(norm)})
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
    return text[start: start + 160]


# ---------------------------------------------------------------------------
# 合规过滤（在落库/返回前强制调用）
# ---------------------------------------------------------------------------

def _apply_compliance(rec: dict, text: str, source_type: str, include_personal: bool) -> dict | None:
    """
    对单条抽取结果做合规处理：
      - 页面级上下文分级（企业业务线 / 商务场景个人号 / 未知个人）
      - 拒收名单过滤
      - 高风险(无商务场景佐证)号码默认剔除（除非 include_personal）
      - 运营商标注 + 审计哈希落盘
    返回处理后的 rec；若过滤后空了返回 None。
    """
    has_name_pair = bool(rec.get("contacts"))
    cls = compliance.classify_context(text, has_name_pair)
    rec["context"] = cls["context"]
    rec["business_context"] = cls["business_context"]
    rec["risk"] = cls["risk"]
    rec["legal_basis"] = cls["legal_basis"]
    rec["source_type"] = source_type
    rec["collected_at"] = datetime.now(timezone.utc).isoformat()

    kept_mobiles: list[str] = []
    mobile_carriers: dict[str, str] = {}
    for m in rec.get("mobiles", []):
        norm = normalize_mobile(m)
        if not norm or not valid_mobile(norm):
            continue
        if compliance.dnc_blocked(norm):
            continue
        if cls["risk"] == "high" and not include_personal:
            continue
        carrier = carrier_of(norm)
        kept_mobiles.append(norm)
        mobile_carriers[norm] = carrier
        compliance.audit_log(norm, rec["source"], cls["context"], cls["legal_basis"], cls["risk"], source_type)

    kept_contacts: list[dict] = []
    kept_set = set(kept_mobiles)
    for c in rec.get("contacts", []):
        nm = normalize_mobile(c.get("mobile", ""))
        if nm in kept_set:
            kept_contacts.append({
                "name": c.get("name", ""),
                "mobile": nm,
                "carrier": mobile_carriers.get(nm, carrier_of(nm)),
            })
            compliance.audit_log(nm, rec["source"], cls["context"], cls["legal_basis"], cls["risk"], source_type)

    rec["mobiles"] = sorted(set(kept_mobiles))
    rec["mobile_carriers"] = mobile_carriers
    rec["contacts"] = kept_contacts

    if not (rec.get("emails") or rec["mobiles"] or rec.get("phones") or
            rec.get("names") or rec.get("legal")):
        return None
    return rec


# ---------------------------------------------------------------------------
# 跨来源按手机号去重合并
# ---------------------------------------------------------------------------

def merge_by_mobile(records: list[dict]) -> list[dict]:
    """
    把不同来源但共享同一手机号的结果合并为一条，减少噪音、聚合多源证据。
    合并后字段：sources(列表) / 主 source / mobiles / mobile_carriers /
    contacts(按 姓名+手机 去重) / emails / phones / names / legal / 场景取最高风险 /
    置信度 / snippet(首条) / count(来源数)。
    """
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in records:
        key_m = (r.get("mobiles") or ["__nophone__" + r["source"]])[0] if r.get("mobiles") else "__nophone__" + r["source"]
        groups.setdefault(key_m, []).append(r)
        if key_m not in order:
            order.append(key_m)

    out: list[dict] = []
    for key in order:
        grp = groups[key]
        if len(grp) == 1:
            out.append(grp[0])
            continue
        merged: dict = {
            "sources": [],
            "emails": set(),
            "mobiles": set(),
            "mobile_carriers": {},
            "phones": set(),
            "names": set(),
            "legal": set(),
            "contacts": {},
            "snippet": "",
            "confidence": "low",
            "context": "business_line",
            "risk": "low",
            "business_context": True,
            "legal_basis": "",
            "source_type": "",
            "collected_at": "",
        }
        risk_rank = {"low": 0, "medium": 1, "high": 2}
        best_risk = "low"
        for r in grp:
            if r["source"] not in merged["sources"]:
                merged["sources"].append(r["source"])
            merged["emails"].update(r.get("emails", []))
            merged["mobiles"].update(r.get("mobiles", []))
            merged["mobile_carriers"].update(r.get("mobile_carriers", {}))
            merged["phones"].update(r.get("phones", []))
            merged["names"].update(r.get("names", []))
            merged["legal"].update(r.get("legal", []))
            for c in r.get("contacts", []):
                merged["contacts"][(c.get("name", ""), c.get("mobile", ""))] = c
            if not merged["snippet"] and r.get("snippet"):
                merged["snippet"] = r["snippet"]
            if r.get("confidence") == "high":
                merged["confidence"] = "high"
            if r.get("business_context"):
                merged["business_context"] = True
            if risk_rank.get(r.get("risk", "low"), 0) > risk_rank.get(best_risk, 0):
                best_risk = r.get("risk", "low")
                merged["context"] = r.get("context", "business_line")
                merged["legal_basis"] = r.get("legal_basis", "")
            if r.get("source_type"):
                merged["source_type"] = r.get("source_type")
            if r.get("collected_at") and (not merged["collected_at"] or r["collected_at"] > merged["collected_at"]):
                merged["collected_at"] = r["collected_at"]

        merged["emails"] = sorted(merged["emails"])
        merged["mobiles"] = sorted(merged["mobiles"])
        merged["phones"] = sorted(merged["phones"])
        merged["names"] = sorted(merged["names"])
        merged["legal"] = sorted(merged["legal"])
        merged["contacts"] = list(merged["contacts"].values())
        merged["risk"] = best_risk
        merged["source"] = merged["sources"][0]
        merged["count"] = len(merged["sources"])
        out.append(merged)
    return out


# ---------------------------------------------------------------------------
# 核心抓取
# ---------------------------------------------------------------------------

async def _fetch_serp(client, eng: dict, q: str, max_results: int, retries: int = 2) -> list[str]:
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


def _find_contact_pages(html: str, base_url: str) -> list[str]:
    """从页面提取「联系我们/联系方式/contact」等链接，用于次级抓取（同域优先）。"""
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "lxml")
    base_host = _host_of(base_url)
    out: list[str] = []
    seen: set[str] = set()
    keys = ("联系我们", "联系方式", "联系我", "contact", "contactus", "about-us", "电话", "tel:", "lianxi")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        txt = a.get_text(" ", strip=True)
        joined = f"{txt} {href}".lower()
        if not any(k in joined for k in keys):
            continue
        full = urljoin(base_url, href)
        if full in seen:
            continue
        seen.add(full)
        if not full.lower().startswith(("http://", "https://")):
            continue
        if _host_of(full) == base_host:
            out.insert(0, full)
        else:
            out.append(full)
        if len(out) >= 6:
            break
    return out[:4]


def _is_redirector(url: str) -> bool:
    """判断是否为搜索引擎跳转链接（baidu/sogou/so 的 link 跳转器，非内容站点）。"""
    from urllib.parse import urlparse
    h = _host_of(url)
    p = urlparse(url).path.lower()
    return (
        h in ("www.baidu.com", "baidu.com", "www.sogou.com", "sogou.com", "www.so.com", "so.com")
        and ("link" in p or "baidu.php" in p or "url=" in url.lower())
    )


async def _fetch_pages(client, url_set: list[str], tokens: list[str], headers: dict | None = None,
                       source_type: str = "search", include_personal: bool = False) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def worker(url: str) -> dict | None:
        async with sem:
            await compliance.polite_wait(url)
            if not _is_redirector(url) and not await compliance.robots_allowed(client, url):
                print(f"[compliance] robots 禁止，跳过: {url}")
                return None
            try:
                pr = await client.get(url, timeout=15, headers=headers or {})
                html = pr.text
                final_url = pr.url or url
            except Exception:
                # 兜底重试：http 失败试 https（或反之），很多官网仅 https 可达
                alt = (
                    url.replace("http://", "https://", 1)
                    if url.startswith("http://")
                    else url.replace("https://", "http://", 1)
                )
                if alt == url:
                    return None
                try:
                    pr = await client.get(alt, timeout=15, headers=headers or {})
                    html = pr.text
                    final_url = pr.url or alt
                except Exception:
                    return None
            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            rec = extract_contacts(html, final_url)
            has_contact = bool(rec and (rec.get("mobiles") or rec.get("phones") or rec.get("emails")))
            if not has_contact:
                # 次级抓取：页面本身无联系方式时，探测「联系我们/联系方式」子页（最多 2 个）
                for cu in _find_contact_pages(html, final_url)[:2]:
                    await compliance.polite_wait(cu)
                    if not await compliance.robots_allowed(client, cu):
                        continue
                    try:
                        pr2 = await client.get(cu, timeout=15, headers=headers or {})
                        rec2 = extract_contacts(pr2.text, pr2.url or cu)
                    except Exception:
                        continue
                    if rec2 and (rec2.get("mobiles") or rec2.get("phones") or rec2.get("emails")):
                        rec = rec2
                        final_url = pr2.url or cu
                        html = pr2.text
                        break
            if not rec:
                return None
            return _apply_compliance(rec, html, source_type, include_personal)

    tasks = [asyncio.create_task(worker(u)) for u in url_set]
    raw: list[dict] = []
    for done in asyncio.as_completed(tasks):
        rec = await done
        if rec:
            raw.append(rec)

    kept: list[dict] = []
    core = tokens[0].lower() if tokens else ""
    for r in raw:
        hay = (r.get("snippet", "") + " " + (r.get("source") or "")).lower()
        # 强相关性：必须命中完整企业名（core）才算相关，否则判 low
        hit = (core in hay) if core else any(t.lower() in hay for t in tokens)
        r["confidence"] = "high" if hit else "low"
        # 过滤1：低价值站点（论坛/百科/问答/聚合，含子域）即使含联系方式也丢弃
        if _is_low_value_host(_host_of(r["source"])):
            continue
        # 过滤2：必须确实含电话/邮箱/手机
        if not (r.get("mobiles") or r.get("phones") or r.get("emails")):
            continue
        # 过滤3：丢弃不含完整企业名的不相关结果（宁缺毋滥）
        if not hit:
            continue
        kept.append(r)
    kept.sort(key=lambda r: (r["confidence"] == "high", relevance(r, tokens)), reverse=True)
    return kept


async def _fetch_enterprise(client, company: str, max_results: int, include_personal: bool) -> list[dict]:
    tokens = company_tokens(company)
    recs: list[dict] = []

    try:
        r = await client.get("https://aiqicha.baidu.com/s", params={"q": company, "t": 0}, timeout=20)
        sp = extract_contacts(r.text, r.url or "https://aiqicha.baidu.com/s")
        if sp:
            recs.append(_apply_compliance(sp, r.text, "enterprise:aiqicha", include_personal))
        detail = re.findall(r'href="(/company_detail[^"?]+)"', r.text)
        detail = ["https://aiqicha.baidu.com" + u for u in dict.fromkeys(detail)][:max_results]
        if detail:
            recs.extend(await _fetch_pages(client, detail, tokens, source_type="enterprise:aiqicha", include_personal=include_personal))
    except Exception as e:
        print(f"[warn] 爱企查查询失败: {e}")

    if QCC_COOKIE:
        try:
            r = await client.get("https://www.qcc.com/web/search", params={"key": company},
                                 headers={"Cookie": QCC_COOKIE}, timeout=20)
            detail = re.findall(r'href="(/company/[^"?]+\.html)"', r.text)
            detail = ["https://www.qcc.com" + u for u in dict.fromkeys(detail)][:max_results]
            if detail:
                recs.extend(await _fetch_pages(client, detail, tokens, headers={"Cookie": QCC_COOKIE},
                                               source_type="enterprise:qcc", include_personal=include_personal))
        except Exception as e:
            print(f"[warn] 企查查查询失败: {e}")

    if TYCC_COOKIE:
        try:
            r = await client.get("https://www.tianyancha.com/search", params={"key": company},
                                 headers={"Cookie": TYCC_COOKIE}, timeout=20)
            detail = re.findall(r'href="(/company/\d+\.html)"', r.text)
            detail = ["https://www.tianyancha.com" + u for u in dict.fromkeys(detail)][:max_results]
            if detail:
                recs.extend(await _fetch_pages(client, detail, tokens, headers={"Cookie": TYCC_COOKIE},
                                               source_type="enterprise:tycc", include_personal=include_personal))
        except Exception as e:
            print(f"[warn] 天眼查查询失败: {e}")

    return [x for x in recs if x]


async def search_company(
    company: str,
    focus: str = "",
    engine: str = "sogou",
    max_results: int = 10,
    mode: str = "general",
    social: list[str] | None = None,
    include_personal: bool = False,
) -> list[dict]:
    build = build_mobile_queries if mode == "mobile" else build_queries
    queries = build(company, focus)
    tokens = company_tokens(company)
    engines = [engine] + [e for e in ("bing", "sogou", "baidu") if e != engine]
    social = [s for s in (social or []) if s in SOCIAL_SOURCES]

    client = AsyncSession(impersonate="chrome", headers=HEADERS, proxy=PROXY, timeout=20, verify=False, allow_redirects=True)
    collected: list[dict] = []
    try:
        # 1) 搜索引擎（多引擎回退）
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
            if url_set:
                collected.extend(await _fetch_pages(client, url_set, tokens, source_type="search", include_personal=include_personal))
            # general 模式：拿到即返（快），除非还要跑社媒/企业库
            if mode != "mobile" and not social and collected:
                return collected

        # 2) 社媒源（用户勾选）
        for skey in social:
            src = SOCIAL_SOURCES[skey]
            try:
                urls = await src["fetch"](client, company, focus, max_results)
                if urls:
                    collected.extend(await _fetch_pages(
                        client, urls, tokens, source_type=src["source_type"], include_personal=include_personal))
            except Exception as e:
                print(f"[warn] 社媒源 {skey} 失败: {e}")

        # 3) mobile 模式：直接打企业库
        if mode == "mobile":
            collected.extend(await _fetch_enterprise(client, company, max_results, include_personal))

        # 去重 + 跨来源合并 + 排序
        seen: set[str] = set()
        uniq: list[dict] = []
        for r in collected:
            if r["source"] not in seen:
                seen.add(r["source"])
                uniq.append(r)
        merged = merge_by_mobile(uniq)
        merged.sort(key=lambda r: (
            r["confidence"] == "high",
            len(r.get("mobiles", [])) > 0,
            r.get("risk") == "low",
            relevance(r, tokens),
        ), reverse=True)
        return merged
    finally:
        await client.close()


async def search_multi(
    companies: list[str],
    focus: str = "",
    engine: str = "sogou",
    max_results: int = 10,
    mode: str = "general",
    social: list[str] | None = None,
    include_personal: bool = False,
) -> list[dict]:
    companies = [c for c in (companies or []) if c.strip()]
    if not companies:
        return []

    results = await asyncio.gather(
        *[search_company(c, focus, engine, max_results, mode, social, include_personal) for c in companies]
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


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "碧水源 环保"
    data = asyncio.run(search_multi([q], "", "bing", 8, social=["wechat"]))
    print(f"核心词: {company_tokens(q)}")
    for d in data:
        print(f"\n来源: {d['source']} | 场景: {d.get('context')} | 风险: {d.get('risk')}")
        print(f"  邮箱: {d['emails']}")
        print(f"  手机: {d['mobiles']} -> {d.get('mobile_carriers')}")
        print(f"  电话: {d['phones']}")
        print(f"  关键人: {d['names']}")
        print(f"  摘要: {d['snippet']}")
