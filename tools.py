# -*- coding: utf-8 -*-
"""行程规划 Agent 的工具集：只做确定性计算，不调用大模型。

设计原则：
1. 每个工具都是纯函数（天气除外），可单测；
2. 返回结构化 dict，直接喂给 LLM 做决策；
3. 预算、路程、开放时间这类"硬约束"必须由代码算，不能交给模型心算。
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

MEAL_COST = {"breakfast": 15, "lunch": 45, "dinner": 55}
TRANSIT_FARE = 8          # 市内每次换乘的平均打车/地铁成本（元）
TRANSIT_SPEED_KMH = 18    # 含等车、步行进出的平均速度
TRANSIT_OVERHEAD_MIN = 10

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 广州气候常年值（用于天气 API 不可用时的降级，来源：公开气候常年的粗略整理）
CLIMATE_FALLBACK = {
    1: (18, 10, "多云"), 2: (19, 11, "多云"), 3: (22, 14, "小雨"),
    4: (26, 18, "小雨"), 5: (29, 22, "雷雨"), 6: (31, 24, "雷雨"),
    7: (33, 25, "雷雨"), 8: (33, 25, "雷雨"), 9: (31, 24, "多云"),
    10: (28, 21, "晴"), 11: (24, 16, "晴"), 12: (20, 12, "多云"),
}


def _load_pois() -> list[dict]:
    path = os.path.join(DATA_DIR, "pois_gz.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["pois"]


POIS = _load_pois()
POI_BY_ID = {p["id"]: p for p in POIS}


def _slim(p: dict) -> dict:
    return {k: p[k] for k in ("id", "name", "district", "ticket", "open", "closed_on", "indoor",
                              "duration_h", "walk", "elderly", "tags")}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def transit_minutes(a_id: str, b_id: str) -> int:
    a, b = POI_BY_ID[a_id], POI_BY_ID[b_id]
    km = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
    return int(round(TRANSIT_OVERHEAD_MIN + km / TRANSIT_SPEED_KMH * 60))


# ---------------------------------------------------------------- tools

def search_poi(tags: list[str] | None = None, max_ticket: float | None = None,
               indoor_only: bool = False, elderly_only: bool = False,
               max_walk: int | None = None, district: str | None = None,
               exclude: list[str] | None = None, limit: int = 12) -> dict:
    """按条件筛选候选景点。返回精简字段，避免把整张表塞进上下文。"""
    exclude = set(exclude or [])
    hits = []
    for p in POIS:
        if p["id"] in exclude:
            continue
        if tags and not (set(tags) & set(p["tags"])):
            continue
        if max_ticket is not None and p["ticket"] > max_ticket:
            continue
        if indoor_only and not p["indoor"]:
            continue
        if elderly_only and not p["elderly"]:
            continue
        if max_walk is not None and p["walk"] > max_walk:
            continue
        if district and p["district"] != district:
            continue
        hits.append(p)
    hits.sort(key=lambda x: (-x["elderly"], x["walk"], x["ticket"]))
    return {"count": len(hits), "items": [_slim(p) for p in hits[:limit]]}


def get_weather(city: str, start_date: str, days: int) -> dict:
    """查询未来天气（Open-Meteo，无需 key）；失败时降级到气候常年值。"""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    days = max(1, min(int(days), 7))
    lat, lon = 23.13, 113.26
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&timezone=Asia%%2FShanghai&forecast_days=7" % (lat, lon))
    try:
        import urllib.request
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "trip-agent"}), timeout=8) as r:
            raw = json.loads(r.read().decode())
        daily = raw["daily"]
        idx = {d: i for i, d in enumerate(daily["time"])}
        out = []
        for i in range(days):
            d = (start + timedelta(days=i)).isoformat()
            if d in idx:
                j = idx[d]
                out.append({
                    "date": d, "weekday": WEEKDAY_CN[date.fromisoformat(d).weekday()],
                    "weather": _code_cn(daily["weather_code"][j]),
                    "temp_max": daily["temperature_2m_max"][j],
                    "temp_min": daily["temperature_2m_min"][j],
                    "rain_prob": daily["precipitation_probability_max"][j],
                    "source": "open-meteo",
                })
        if out:
            return {"city": city, "days": out}
    except Exception:  # 网络不可用时不能让整个规划失败
        pass
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        hi, lo, wx = CLIMATE_FALLBACK[d.month]
        out.append({"date": d.isoformat(), "weekday": WEEKDAY_CN[d.weekday()],
                    "weather": wx, "temp_max": hi, "temp_min": lo, "rain_prob": None,
                    "source": "climate-fallback"})
    return {"city": city, "days": out, "warning": "天气接口不可用，已使用气候常年值"}


def _code_cn(code: int) -> str:
    if code == 0:
        return "晴"
    if code in (1, 2):
        return "多云"
    if code == 3:
        return "阴"
    if code in (45, 48):
        return "雾"
    if code in (51, 53, 55, 56, 57):
        return "小雨"
    if code in (61, 63, 66):
        return "中雨"
    if code in (65, 67, 80, 81, 82):
        return "大雨"
    if code in (95, 96, 99):
        return "雷雨"
    return "多云"


def budget_calc(plan: dict, people: int = 1) -> dict:
    """按行程算门票、餐饮、市内交通三块成本。"""
    days = plan.get("days", [])
    ticket = 0.0
    for d in days:
        for it in d.get("items", []):
            poi = POI_BY_ID.get(it.get("poi_id"))
            if poi:
                ticket += poi["ticket"]
    meals = sum(MEAL_COST.values()) * len(days)
    hops = sum(max(0, len(d.get("items", [])) - 1) + 1 for d in days)
    transit = hops * TRANSIT_FARE
    total = (ticket + meals + transit) * people
    return {"people": people, "ticket_per_person": ticket, "meals_per_person": meals,
            "transit_per_person": transit, "total": total,
            "breakdown": {"门票": ticket * people, "餐饮": meals * people, "交通": transit * people}}


def export_markdown(plan: dict) -> dict:
    """把行程渲染成可保存的 Markdown。"""
    title = plan.get("title") or "广州行程"
    lines = ["# " + title, ""]
    if plan.get("summary"):
        lines += [plan["summary"], ""]
    for d in plan.get("days", []):
        lines.append("## %s %s" % (d.get("date", ""), d.get("weekday", "")))
        if d.get("note"):
            lines.append("> " + d["note"])
        rows = []
        for it in d.get("items", []):
            poi = POI_BY_ID.get(it.get("poi_id"), {})
            rows.append((it.get("start", ""), "- **%s–%s** %s（%s，门票 %s 元）%s" % (
                it.get("start", ""), it.get("end", ""), poi.get("name", it.get("poi_id")),
                poi.get("district", ""), poi.get("ticket", "?"),
                ("　" + it["note"]) if it.get("note") else "")))
        if d.get("lunch"):
            rows.append((d["lunch"].split("–")[0], "- **%s** 午餐（自行安排，预算已计入）" % d["lunch"]))
        rows.sort(key=lambda r: r[0])
        lines.extend(r[1] for r in rows)
        lines.append("")
    if plan.get("budget"):
        b = plan["budget"]
        people = max(1, int(b.get("people") or 1))
        lines.append("**预算**：%s 人合计 %s 元（人均 %s 元；门票 %s + 餐饮 %s + 交通 %s）" % (
            people, round(float(b.get("total") or 0)),
            round(float(b.get("total") or 0) / people),
            b.get("ticket_per_person"), b.get("meals_per_person"), b.get("transit_per_person")))
    return {"markdown": "\n".join(lines)}


# ---------------------------------------------------------------- schemas

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_poi",
            "description": "按标签/门票/室内外/适老/步行强度筛选广州景点候选",
            "parameters": {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"},
                             "description": "如 博物馆/公园/夜景/古镇"},
                    "max_ticket": {"type": "number"},
                    "indoor_only": {"type": "boolean"},
                    "elderly_only": {"type": "boolean"},
                    "max_walk": {"type": "integer", "description": "1很轻松 2一般 3费腿"},
                    "district": {"type": "string"},
                    "exclude": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询广州未来天气，含降雨概率",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "days": {"type": "integer"},
                },
                "required": ["city", "start_date", "days"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "distance_between",
            "description": "两个景点之间的直线距离与预计通行时间",
            "parameters": {
                "type": "object",
                "properties": {"a_id": {"type": "string"}, "b_id": {"type": "string"}},
                "required": ["a_id", "b_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "budget_calc",
            "description": "计算一份行程的门票、餐饮、交通总花费",
            "parameters": {
                "type": "object",
                "properties": {"plan": {"type": "object"}, "people": {"type": "integer"}},
                "required": ["plan"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_plan",
            "description": "提交最终行程方案，等待系统校验。校验不通过会返回问题清单，需要修改后重新提交。",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "object",
                        "description": "完整行程。days 的每一项对应一天，items 里按时间顺序列出当天景点。",
                        "properties": {
                            "title": {"type": "string"},
                            "summary": {"type": "string"},
                            "days": {
                                "type": "array",
                                "description": "每天一项，天数必须和用户要求一致，日期从出发日连续",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                                        "items": {
                                            "type": "array",
                                            "description": "当天的景点，按开始时间升序；午餐时段(11:30-13:00)不要排景点",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "poi_id": {"type": "string", "description": "来自 search_poi 返回的 id"},
                                                    "start": {"type": "string", "description": "HH:MM"},
                                                    "end": {"type": "string", "description": "HH:MM"},
                                                    "note": {"type": "string"}
                                                },
                                                "required": ["poi_id", "start", "end"]
                                            }
                                        }
                                    },
                                    "required": ["date", "items"]
                                }
                            }
                        },
                        "required": ["days"]
                    },
                    "reasoning": {"type": "string", "description": "一句话说明排布思路"},
                },
                "required": ["plan"],
            },
        },
    },
]

TOOL_IMPL = {
    "search_poi": search_poi,
    "get_weather": get_weather,
    "distance_between": lambda a_id, b_id: {"km": round(haversine_km(POI_BY_ID[a_id]["lat"], POI_BY_ID[a_id]["lon"], POI_BY_ID[b_id]["lat"], POI_BY_ID[b_id]["lon"]), 2), "minutes": transit_minutes(a_id, b_id)},
    "budget_calc": budget_calc,
}
