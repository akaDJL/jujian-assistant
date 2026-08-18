"""
居间小助手 · 后端服务（合规升级版）
====================================
FastAPI 提供：
    GET  /                      返回单页前端
    POST /api/search            接收企业名单 + 关注部门 + 引擎 + 上限 + 社媒源 + 合规开关
    GET  /api/terms             返回《使用须知》文本与是否已同意
    POST /api/consent           记录同意（使用前必须）
    GET  /api/dnc               返回拒收名单
    POST /api/dnc               加入拒收名单 {mobile, reason?}
    DELETE /api/dnc             移出拒收名单 {mobile}
    GET  /api/audit             返回采集审计概览（计数，不含明文号码）

启动：
    .workbuddy/binaries/python/envs/default/Scripts/python.exe -m uvicorn app:app --port 8000
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from scraper import search_multi
import compliance as compliance
from social import SOCIAL_SOURCES

BASE_DIR = Path(__file__).resolve().parent
INDEX = BASE_DIR / "templates" / "index.html"

app = FastAPI(title="居间小助手 · 公开联系人挖掘（合规版）")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(INDEX))


@app.get("/api/terms")
async def api_terms():
    return {
        "version": compliance.USAGE_TERMS_VERSION,
        "acknowledged": compliance.consent_ok(),
        "terms": compliance.USAGE_TERMS,
        "social_sources": [
            {"key": k, "label": v["label"], "reliable": v["reliable"]}
            for k, v in SOCIAL_SOURCES.items()
        ],
    }


@app.post("/api/consent")
async def api_consent():
    compliance.record_consent()
    return {"ok": True, "acknowledged_at": compliance.consent_ok() and _consent_time()}


def _consent_time() -> str:
    try:
        import json
        from pathlib import Path
        p = BASE_DIR / "data" / "consent.json"
        if p.exists():
            return json.loads(p.read_text("utf-8")).get("acknowledged_at", "")
    except Exception:
        pass
    return ""


@app.post("/api/search")
async def api_search(payload: dict):
    # 合规前置：必须先确认《使用须知》
    if not compliance.consent_ok():
        return JSONResponse(
            {"error": "请先阅读并勾选同意《使用须知》后再进行采集。", "need_consent": True},
            status_code=403,
        )

    raw = payload.get("companies", "")
    if isinstance(raw, list):
        companies = [c.strip() for c in raw if str(c).strip()]
    else:
        companies = [c.strip() for c in str(raw).splitlines() if c.strip()]

    focus = str(payload.get("focus", "")).strip()
    engine = str(payload.get("engine", "bing")).strip() or "bing"
    try:
        max_results = max(1, min(30, int(payload.get("max_results", 10))))
    except (TypeError, ValueError):
        max_results = 10

    mode = str(payload.get("mode", "general")).strip() or "general"
    if mode not in ("general", "mobile"):
        mode = "general"

    social = payload.get("social") or []
    if isinstance(social, str):
        social = [social]
    social = [s for s in social if s in SOCIAL_SOURCES]

    include_personal = bool(payload.get("include_personal", False))

    if not companies:
        return JSONResponse({"count": 0, "results": [], "error": "请至少输入一家企业"}, status_code=400)

    try:
        results = await asyncio.wait_for(
            search_multi(companies, focus, engine, max_results, mode, social, include_personal),
            timeout=150,
        )
    except asyncio.TimeoutError:
        return JSONResponse(
            {"count": 0, "results": [], "error": "检索超时（150s），请减少企业数量或每家企业页数后重试。"},
            status_code=504,
        )

    # 合规元信息随结果一并返回，便于前端展示与留痕
    meta = {
        "audit_total": compliance.audit_summary()["total"],
        "dnc_count": len(compliance.dnc_list()),
        "social_used": social,
        "include_personal": include_personal,
    }
    return {"count": len(results), "results": results, "compliance": meta}


@app.get("/api/dnc")
async def api_dnc_list():
    return {"count": len(compliance.dnc_list()), "blocked": compliance.dnc_list()}


@app.post("/api/dnc")
async def api_dnc_add(payload: dict):
    mobile = str(payload.get("mobile", "")).strip()
    from scraper import normalize_mobile
    norm = normalize_mobile(mobile)
    if not norm:
        return JSONResponse({"ok": False, "error": "手机号格式不正确"}, status_code=400)
    compliance.dnc_add(norm, str(payload.get("reason", "")), str(payload.get("by", "user")))
    return {"ok": True, "mobile": norm, "message": "已加入拒收名单，后续采集将永久屏蔽该号码"}


@app.delete("/api/dnc")
async def api_dnc_del(payload: dict):
    mobile = str(payload.get("mobile", "")).strip()
    from scraper import normalize_mobile
    norm = normalize_mobile(mobile)
    if not norm:
        return JSONResponse({"ok": False, "error": "手机号格式不正确"}, status_code=400)
    removed = compliance.dnc_remove(norm)
    return {"ok": removed, "mobile": norm, "message": "已从拒收名单移除" if removed else "该号码不在拒收名单中"}


@app.get("/api/audit")
async def api_audit():
    return compliance.audit_summary()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
