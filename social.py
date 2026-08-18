"""
居间小助手 · 社媒来源适配器 (social.py)
========================================

四大社媒源的「公开联系方式」采集适配器。设计原则（合规优先）：
  * 只抓「企业/商家官方账号 / 公开搜索结果页」里**自行公开**的业务联系方式。
  * 不突破任何平台的登录墙 / 付费墙，不规避验证码——被拦就优雅返回空，绝不强行绕过。
  * 所有请求都会经过 compliance 的 robots / 礼貌 / 拒收 / 审计护栏。
  * 微信(搜狗) 最可行；微博/抖音/小红书为「尽力而为」，受反爬与 ToS 限制，可能返回空，属正常。

每个适配器：输入 (client, company, focus, max_results) -> 返回候选页面 URL 列表，
交给 scraper._fetch_pages 统一抽取联系人（复用同一套正则与合规逻辑）。
"""

from __future__ import annotations

import re
from urllib.parse import quote

from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------------------
# 微信 · 搜狗微信搜索（最可行）
# ---------------------------------------------------------------------------

async def wechat_sogou(client, company: str, focus: str, max_results: int) -> list[str]:
    """
    搜狗微信搜索（type=2=文章）。返回公众号文章链接，文章里常留企业业务手机。
    结果链接是 /link?url= 中转，client 跟随 302 到 mp.weixin.qq.com 正文。
    """
    q = company + (f" {focus}" if focus else "")
    urls: list[str] = []
    try:
        r = await client.get(
            "https://weixin.sogou.com/search",
            params={"type": "2", "query": q, "ie": "utf8"},
            timeout=20,
        )
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select(".news-list h3 a, .txt-box h3 a, .results h3 a"):
            href = a.get("href", "")
            if not href:
                continue
            if href.startswith("/"):
                href = "https://weixin.sogou.com" + href
            if href.startswith("http"):
                urls.append(href)
            if len(urls) >= max_results:
                break
    except Exception as e:
        print(f"[warn] 微信搜狗失败: {e}")
    return urls


# ---------------------------------------------------------------------------
# 微博 · 蓝V 公开搜索（尽力而为）
# ---------------------------------------------------------------------------

async def weibo(client, company: str, focus: str, max_results: int) -> list[str]:
    """
    微博公开搜索页。若被登录墙拦截，extract_contacts 自然拿不到内容（优雅降级）。
    直接把搜索结果页作为来源，抽取帖子里公开的业务联系方式。
    """
    q = company + (" 联系电话 业务" if not focus else f" {focus}")
    try:
        r = await client.get(
            "https://s.weibo.com/weibo",
            params={"q": q, "type": "all"},
            timeout=20,
            headers={"User-Agent": HEADERS["User-Agent"]},
        )
        return [r.url or f"https://s.weibo.com/weibo?q={quote(q)}"]
    except Exception as e:
        print(f"[warn] 微博搜索失败(可能需登录/被反爬): {e}")
        return []


# ---------------------------------------------------------------------------
# 抖音 · 企业号公开搜索（尽力而为，ToS 风险较高）
# ---------------------------------------------------------------------------

async def douyin(client, company: str, focus: str, max_results: int) -> list[str]:
    q = company + (" 联系方式" if not focus else f" {focus}")
    try:
        r = await client.get(
            "https://www.douyin.com/search/" + quote(q),
            timeout=20,
            headers={"User-Agent": HEADERS["User-Agent"]},
        )
        return [r.url or f"https://www.douyin.com/search/{quote(q)}"]
    except Exception as e:
        print(f"[warn] 抖音搜索失败(反爬强/需登录): {e}")
        return []


# ---------------------------------------------------------------------------
# 小红书 · 商家号公开搜索（尽力而为，ToS 明确禁止抓取，风险高）
# ---------------------------------------------------------------------------

async def xiaohongshu(client, company: str, focus: str, max_results: int) -> list[str]:
    q = company + (" 联系方式" if not focus else f" {focus}")
    try:
        r = await client.get(
            "https://www.xiaohongshu.com/search_result",
            params={"keyword": q},
            timeout=20,
            headers={"User-Agent": HEADERS["User-Agent"]},
        )
        return [r.url or f"https://www.xiaohongshu.com/search_result?keyword={quote(q)}"]
    except Exception as e:
        print(f"[warn] 小红书搜索失败(反爬极强/ToS禁止): {e}")
        return []


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

SOCIAL_SOURCES = {
    "wechat": {
        "label": "微信公众号(搜狗)",
        "fetch": wechat_sogou,
        "source_type": "social:wechat",
        "reliable": True,
    },
    "weibo": {
        "label": "微博蓝V",
        "fetch": weibo,
        "source_type": "social:weibo",
        "reliable": False,
    },
    "douyin": {
        "label": "抖音企业号",
        "fetch": douyin,
        "source_type": "social:douyin",
        "reliable": False,
    },
    "xiaohongshu": {
        "label": "小红书商家号",
        "fetch": xiaohongshu,
        "source_type": "social:xiaohongshu",
        "reliable": False,
    },
}
