"""
居间小助手 · 后端服务
========================
FastAPI 提供：
    GET  /              返回单页前端
    POST /api/search    接收企业名单 + 关注部门 + 引擎 + 上限，返回结构化联系人

启动（在项目目录）：
    .workbuddy/binaries/python/envs/default/Scripts/python.exe -m uvicorn app:app --port 8000
或直接：
    python app.py
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from scraper import search_multi

BASE_DIR = Path(__file__).resolve().parent
INDEX = BASE_DIR / "templates" / "index.html"

app = FastAPI(title="居间小助手 · 公开联系人挖掘")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(str(INDEX))


@app.post("/api/search")
async def api_search(payload: dict):
    raw = payload.get("companies", "")
    if isinstance(raw, list):
        companies = [c.strip() for c in raw if str(c).strip()]
    else:
        companies = [c.strip() for c in str(raw).splitlines() if c.strip()]

    focus = str(payload.get("focus", "")).strip()
    engine = str(payload.get("engine", "sogou")).strip() or "sogou"
    try:
        max_results = max(1, min(30, int(payload.get("max_results", 10))))
    except (TypeError, ValueError):
        max_results = 10

    mode = str(payload.get("mode", "general")).strip() or "general"
    if mode not in ("general", "mobile"):
        mode = "general"

    if not companies:
        return JSONResponse({"count": 0, "results": [], "error": "请至少输入一家企业"}, status_code=400)

    results = await search_multi(companies, focus, engine, max_results, mode)
    return {"count": len(results), "results": results}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
