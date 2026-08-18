"""
居间小助手 · 合规护栏模块 (compliance.py)
=========================================

本模块把「合规」做成可执行的代码，而不是一句口号。所有抓取在落库/返回前都必须经过这里：

  1. robots.txt 遵守        —— 目标站点 Disallow 的 path 一律不抓（带 24h 文件缓存）。
  2. 礼貌抓取 (politeness)  —— per-host 最小请求间隔，不对单站打爆。
  3. 拒收名单 (DNC)         —— 个人要求「不联系/删除」的号码，永不采集、永不返回。
  4. 采集审计 (audit)       —— 每条手机号记录：哈希 + 来源 + 时间 + 法律基础 + 场景分级。
  5. 用途 / 法律基础声明     —— 使用前必须确认《使用须知》，记录同意版本与时间。
  6. 上下文分级             —— 区分「企业业务线 / 商务场景个人号 / 未知个人」，高风险默认排除。

法律边界（中国大陆）：
  * 手机号属于个人信息，《个人信息保护法》(PIPL) 适用。
  * 本工具依据 PIPL 第 13 条第 1 款第 6 项 + 第 27 条：在「合理范围」内处理
    个人「自行公开或者其他已经合法公开」的信息；个人明确拒绝的，停止处理并删除。
  * 因此：仅采集「商业场景下公开的联系方式」；个人一旦进入拒收名单即永久屏蔽；
    无任何商务场景佐证的纯个人号码默认不采集（需显式开启且自担风险）。
  * 不突破任何平台的登录墙 / 付费墙，不规避反爬验证码（那会越过「合法公开」边界）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 路径与存储
# ---------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

DNC_FILE = DATA / "dnc.json"
AUDIT_FILE = DATA / "audit.log"
CONSENT_FILE = DATA / "consent.json"
ROBOTS_CACHE = DATA / "robots_cache.json"

# 审计日志里对个人号码做盐值哈希，避免明文落盘
_AUDIT_SALT = b"jujian-compliance-v1"

USAGE_TERMS_VERSION = "2026-08-18"
USAGE_TERMS = (
    "《居间小助手 · 公开联系人采集使用须知》\n"
    "1. 本工具仅聚合「已在互联网上公开披露」的企业/商务联系方式，用于合法的工程居间、"
    "B2B 商务对接等正当目的。\n"
    "2. 我承诺：不突破任何网站的登录墙 / 付费墙，不规避反爬验证码，遵守目标站点 "
    "robots.txt 与用户协议（ToS），控制访问频率。\n"
    "3. 我理解：手机号属于个人信息，受《个人信息保护法》(PIPL) 保护。本工具依据 "
    "PIPL 第 27 条在「合理范围」内处理已合法公开的信息；对任何「无商务场景佐证的纯个人号码」"
    "默认不采集。\n"
    "4. 我承诺：建立并维护「拒收名单」，对任何要求「不联系 / 删除」的个人立即屏蔽并删除其信息；"
    "不将采集到的号码用于骚扰、诈骗、非法营销或转售。\n"
    "5. 采集结果仅供参考，商务对接前我会自行核实真实性，并自行承担使用后果与法律责任。"
)

# 业务场景关键词：出现则视为「商务场景公开的联系方式」
BUSINESS_KEYWORDS = [
    "招标", "中标", "采购", "招商", "业务", "联系", "项目", "负责人", "代理",
    "公司", "企业", "总机", "客服", "销售", "商务", "供需", "合作", "供应",
    "工程", "环保", "能源", "建设", "投资", "股份", "有限", "经理", "厂长",
    "主任", "部长", "对接", "询价", "投标", "发包", "业主", "发包人",
]

# per-host 礼貌间隔（秒）：两次对同一主机请求的最小间隔
POLITE_INTERVAL = 2.5

_host_last_hit: dict[str, float] = {}
_polite_locks: dict[str, asyncio.Lock] = {}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_mobile(mobile: str) -> str:
    return hashlib.sha256(_AUDIT_SALT + mobile.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 礼貌抓取
# ---------------------------------------------------------------------------

async def polite_wait(url: str) -> None:
    """
    按 per-host 最小间隔限速。同一主机的并发请求会被串行化，避免把对方压垮。
    用法：在发起 client.get 之前 `await polite_wait(url)`。
    """
    host = _host(url)
    if not host:
        return
    lock = _polite_locks.setdefault(host, asyncio.Lock())
    async with lock:
        last = _host_last_hit.get(host, 0.0)
        wait = POLITE_INTERVAL - (time.monotonic() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        _host_last_hit[host] = time.monotonic()


# ---------------------------------------------------------------------------
# robots.txt 遵守（带 24h 文件缓存）
# ---------------------------------------------------------------------------

def _load_robots_cache() -> dict:
    try:
        if ROBOTS_CACHE.exists():
            return json.loads(ROBOTS_CACHE.read_text("utf-8"))
    except Exception:
        pass
    return {}


def _save_robots_cache(cache: dict) -> None:
    try:
        ROBOTS_CACHE.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
    except Exception:
        pass


def _path_allowed(disallows: list[str], path: str) -> bool:
    for rule in disallows:
        if rule == "/":
            return False
        if rule and path.startswith(rule):
            return False
    return True


async def robots_allowed(client, url: str) -> bool:
    """
    检查 url 是否被目标站点 robots.txt 禁止。允许返回 True，禁止返回 False。
    解析失败时（无 robots / 网络错误）保守地视为「允许」（不主动封禁合法采集），
    但记录一次未知，便于排查。
    """
    host = _host(url)
    if not host:
        return True
    scheme = "https"
    try:
        scheme = urlparse(url).scheme or "https"
    except Exception:
        pass
    robots_url = f"{scheme}://{host}/robots.txt"

    cache = _load_robots_cache()
    entry = cache.get(host)
    now = time.time()
    disallows: list[str] = []
    if entry and now - entry.get("ts", 0) < 24 * 3600:
        disallows = entry.get("disallow", [])
    else:
        try:
            r = await client.get(robots_url, timeout=10,
                                 headers={"User-Agent": "Mozilla/5.0"})
            text = r.text or ""
            # 仅解析面向所有爬虫的 Disallow（不针对特定 UA 精细化）
            in_star = True
            cur_disallow: list[str] = []
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                if line.lower().startswith("user-agent:"):
                    ua = line.split(":", 1)[1].strip().lower()
                    in_star = (ua == "*")
                    continue
                if in_star and line.lower().startswith("disallow:"):
                    rule = line.split(":", 1)[1].strip()
                    if rule:
                        cur_disallow.append(rule)
            disallows = cur_disallow
            cache[host] = {"ts": now, "disallow": disallows}
            _save_robots_cache(cache)
        except Exception:
            # 取不到 robots 不阻断（视为允许），但缓存空结果 1h 避免反复请求
            cache[host] = {"ts": now, "disallow": []}
            _save_robots_cache(cache)

    path = urlparse(url).path or "/"
    return _path_allowed(disallows, path)


# ---------------------------------------------------------------------------
# 拒收名单 (Do-Not-Contact)
# ---------------------------------------------------------------------------

def load_dnc() -> dict:
    try:
        if DNC_FILE.exists():
            return json.loads(DNC_FILE.read_text("utf-8"))
    except Exception:
        pass
    return {"blocked": {}}


def _save_dnc(d: dict) -> None:
    DNC_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")


def dnc_blocked(mobile: str) -> bool:
    d = load_dnc()
    return mobile in d.get("blocked", {})


def dnc_add(mobile: str, reason: str = "", by: str = "") -> None:
    d = load_dnc()
    d.setdefault("blocked", {})
    d["blocked"][mobile] = {
        "added_at": _now_iso(),
        "reason": reason or "本人要求不联系/删除",
        "by": by or "unknown",
    }
    _save_dnc(d)


def dnc_remove(mobile: str) -> bool:
    d = load_dnc()
    if mobile in d.get("blocked", {}):
        del d["blocked"][mobile]
        _save_dnc(d)
        return True
    return False


def dnc_list() -> list[dict]:
    d = load_dnc()
    return [
        {"mobile": m, **meta} for m, meta in d.get("blocked", {}).items()
    ]


# ---------------------------------------------------------------------------
# 使用须知 / 同意
# ---------------------------------------------------------------------------

def record_consent() -> None:
    CONSENT_FILE.write_text(
        json.dumps(
            {"version": USAGE_TERMS_VERSION, "acknowledged_at": _now_iso()},
            ensure_ascii=False, indent=2,
        ),
        "utf-8",
    )


def consent_ok() -> bool:
    try:
        if CONSENT_FILE.exists():
            data = json.loads(CONSENT_FILE.read_text("utf-8"))
            return data.get("version") == USAGE_TERMS_VERSION
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# 采集审计
# ---------------------------------------------------------------------------

def audit_log(mobile: str, source: str, context: str, legal_basis: str,
              risk: str, source_type: str = "") -> None:
    """
    追加一行审计日志（个人号码以哈希存储，不落明文）。
    字段：时间 / 号码哈希 / 来源 / 场景 / 风险 / 法律基础 / 来源类型。
    """
    entry = {
        "ts": _now_iso(),
        "mobile_hash": _hash_mobile(mobile),
        "source": source,
        "context": context,
        "risk": risk,
        "legal_basis": legal_basis,
        "source_type": source_type,
    }
    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def audit_summary() -> dict:
    total = 0
    by_risk: dict[str, int] = {}
    by_context: dict[str, int] = {}
    try:
        with AUDIT_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                total += 1
                by_risk[e.get("risk", "unknown")] = by_risk.get(e.get("risk", "unknown"), 0) + 1
                by_context[e.get("context", "unknown")] = by_context.get(e.get("context", "unknown"), 0) + 1
    except Exception:
        pass
    return {"total": total, "by_risk": by_risk, "by_context": by_context}


# ---------------------------------------------------------------------------
# 上下文分级
# ---------------------------------------------------------------------------

# 角色提示词：出现「联系人/负责人/招标」等，即视为商务场景（即使无其它业务关键词）
ROLE_HINT_RE = re.compile(
    r"联系人|负责人|项目经理|项目主管|招标|中标|采购|代理|法人代表|法人|"
    r"商务|销售|对接人|发包人|业主|询价|投标|招商|总机|客服",
    re.IGNORECASE,
)


def classify_context(text: str, has_name_pair: bool = False) -> dict:
    """
    根据页面文本与是否「人名+手机」配对，判定号码场景与风险：
      - business_line         企业业务线/总机/客服 —— 低风险，法律基础：企业公开业务联系方式
      - personal_in_business  商务场景下的个人号（如招标联系人手机）—— 中风险，PIPL 第 27 条
      - personal_unknown      无任何商务场景佐证的纯个人号 —— 高风险，建议不采集
    返回 {context, business_context, is_personal, risk, legal_basis}
    """
    hay = (text or "").lower()
    business_context = any(kw.lower() in hay for kw in BUSINESS_KEYWORDS) or bool(
        ROLE_HINT_RE.search(text or "")
    )
    is_personal = bool(has_name_pair)
    if business_context:
        if is_personal:
            return {
                "context": "personal_in_business",
                "business_context": True,
                "is_personal": True,
                "risk": "medium",
                "legal_basis": "已合法公开·合理处理(PIPL第27条)·含商务场景个人号",
            }
        return {
            "context": "business_line",
            "business_context": True,
            "is_personal": False,
            "risk": "low",
            "legal_basis": "企业公开业务联系方式",
        }
    # 无商务场景佐证
    if is_personal:
        return {
            "context": "personal_unknown",
            "business_context": False,
            "is_personal": True,
            "risk": "high",
            "legal_basis": "无法确定公开商务场景·默认不采集",
        }
    return {
        "context": "personal_unknown",
        "business_context": False,
        "is_personal": False,
        "risk": "high",
        "legal_basis": "无法确定公开商务场景·默认不采集",
    }
