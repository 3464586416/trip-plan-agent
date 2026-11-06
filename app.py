# -*- coding: utf-8 -*-
"""Web 入口：把行程规划 Agent 包成一个能直接被陌生人使用的页面。

启动：python app.py  （默认 http://127.0.0.1:8000）
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import planner

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_REQ_PER_MIN = int(os.getenv("TRIP_RATE_PER_MIN", "6"))
DAILY_CAP = int(os.getenv("TRIP_DAILY_CAP", "300"))   # 公网演示：每天的模型调用总量上限

app = FastAPI(title="广州行程规划 Agent", docs_url=None, redoc_url=None)

# 极简滑动窗口限流：公网免费档必须防别人刷爆 key
_hits: dict[str, deque] = {}
TASKS: dict[str, dict] = {}          # 异步任务：进度条靠它
TASK_TTL = 900                       # 15 分钟没人取就丢掉
_day = {"key": "", "n": 0}


def _daily_ok() -> bool:
    from datetime import date as _date
    today = _date.today().isoformat()
    if _day["key"] != today:
        _day["key"], _day["n"] = today, 0
    if _day["n"] >= DAILY_CAP:
        return False
    _day["n"] += 1
    return True


def _rate_ok(ip: str) -> bool:
    now = time.time()
    q = _hits.setdefault(ip, deque())
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= MAX_REQ_PER_MIN:
        return False
    q.append(now)
    return True


@app.get("/health")
def health():
    return {"ok": True, "llm": bool(planner.API_KEY)}


@app.get("/favicon.ico")
def favicon():
    # 页面里用的是内嵌 SVG 图标，这里给浏览器一个空响应，免得控制台报 404
    return Response(status_code=204)


@app.post("/api/plan")
async def api_plan(request: Request):
    ip = (request.headers.get("x-forwarded-for") or request.client.host or "-").split(",")[0].strip()
    if not _rate_ok(ip):
        return JSONResponse({"ok": False, "error": "请求太频繁了，等一分钟再试。"}, status_code=429)
    if not _daily_ok():
        return JSONResponse({"ok": False, "error": "今天的演示额度用完了，明天再来。"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    text = (body or {}).get("text", "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "请先描述你的行程需求"}, status_code=400)
    if len(text) > 300:
        return JSONResponse({"ok": False, "error": "需求描述请控制在 300 字以内"}, status_code=413)
    try:
        out = planner.plan_trip(text)
    except Exception as e:  # 兜底：不让页面白屏
        return JSONResponse({"ok": False, "error": "规划失败：%s" % e}, status_code=500)
    return {
        "ok": True,
        "source": out["source"],
        "rounds": out["rounds"],
        "req": out["req"],
        "plan": out["plan"],
        "markdown": out["markdown"],
        "validation": out["validation"],
        "pois": {p["id"]: {"name": p["name"], "district": p["district"], "ticket": p["ticket"],
                           "indoor": p["indoor"], "open": p["open"]}
                 for p in planner.tools.POIS},
    }


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")



@app.post("/api/plan_async")
async def plan_async(request: Request):
    """提交后立刻返回 task_id，前端轮询进度，避免干等。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "先说说想去哪、玩几天"}, status_code=400)
    if len(text) > 300:
        return JSONResponse({"ok": False, "error": "描述太长了，300 字以内"}, status_code=413)
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or \
        (request.client.host if request.client else "unknown")
    if not _rate_ok(ip):
        return JSONResponse({"ok": False, "error": "请求太频繁，歇一会儿再试"}, status_code=429)
    if not _daily_ok():
        return JSONResponse({"ok": False, "error": "今天的演示额度用完了，明天再来"}, status_code=429)

    tid = uuid.uuid4().hex
    TASKS[tid] = {"status": "running", "progress": 3, "stage": "开始", "t0": time.time()}

    def _run():
        try:
            res = planner.plan_trip(text, progress=lambda p, s: TASKS[tid].update(progress=p, stage=s))
            TASKS[tid].update(status="done", progress=100, stage="完成", result=res)
        except Exception as exc:  # noqa: BLE001
            TASKS[tid].update(status="error", error=str(exc)[:200])

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "task_id": tid}


@app.get("/api/plan_task/{tid}")
def plan_task(tid: str):
    now = time.time()
    for k in [k for k, v in TASKS.items() if now - v["t0"] > TASK_TTL]:
        TASKS.pop(k, None)
    t = TASKS.get(tid)
    if not t:
        return JSONResponse({"ok": False, "error": "任务不存在或已过期"}, status_code=404)
    if t["status"] == "error":
        TASKS.pop(tid, None)
        return {"ok": False, "error": t.get("error") or "规划失败"}
    if t["status"] != "done":
        return {"ok": True, "status": "running", "progress": t["progress"], "stage": t["stage"]}
    TASKS.pop(tid, None)
    out = {"ok": True, "status": "done", "progress": 100, "stage": "完成"}
    out.update(t["result"])
    return out


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

