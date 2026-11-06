# -*- coding: utf-8 -*-
"""硬约束校验器：行程好不好看交给模型，做不做得到由代码说了算。

每个约束都有稳定编号（C1..C11），被拒时把编号和证据一起回传给模型，
模型据此重排——这是"Agent 自检循环"的核心，而不是靠提示词说"请仔细检查"。
"""
from __future__ import annotations

from datetime import date, timedelta

import tools

SOLID = [
    ("C1", "天数必须等于用户要求"),
    ("C2", "日期必须连续且从出发日开始"),
    ("C3", "不能重复安排同一景点"),
    ("C4", "行程时间必须落在景点开放时间内"),
    ("C5", "不能在闭馆日安排该景点"),
    ("C6", "相邻景点之间要留够通行时间"),
    ("C7", "单日活动时长与步行强度不能超限"),
    ("C8", "总预算不能超"),
    ("C9", "每天景点数不超过 4 个"),
    ("C10", "必须包含用户点名要去的景点"),
    ("C11", "活动时间只能落在 08:00–22:00"),
]
SOFT_CODES = {"S1"}


def _min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _fmt(minutes: int) -> str:
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def _open_range(spec: str) -> tuple[int, int]:
    a, b = spec.split("-")
    return _min(a), _min(b)


def validate(plan: dict, req: dict) -> dict:
    """返回 {ok, violations:[{code, where, msg}], warnings:[...], stats:{...}}"""
    v: list[dict] = []
    w: list[str] = []

    def bad(code: str, where: str, msg: str) -> None:
        v.append({"code": code, "where": where, "msg": msg})

    days = plan.get("days") or []
    # C1
    if len(days) != int(req["days"]):
        bad("C1", "整体", "要求 %d 天，方案给了 %d 天" % (int(req["days"]), len(days)))

    # C2
    start = date.fromisoformat(req["start_date"])
    for i, d in enumerate(days):
        expect = (start + timedelta(days=i)).isoformat()
        if d.get("date") != expect:
            bad("C2", "第%d天" % (i + 1), "日期应为 %s，实际 %s" % (expect, d.get("date")))

    seen: dict[str, str] = {}
    total_items = 0
    for i, d in enumerate(days):
        where_day = "第%d天(%s)" % (i + 1, d.get("date"))
        items = d.get("items") or []
        total_items += len(items)
        # C9
        if len(items) > 4:
            bad("C9", where_day, "安排了 %d 个景点，单日上限 4 个" % len(items))

        prev_end, prev_id = None, None
        day_minutes = 0
        for it in items:
            pid = it.get("poi_id")
            poi = tools.POI_BY_ID.get(pid)
            if not poi:
                bad("C3", where_day, "未知景点 id：%s" % pid)
                continue
            # C3
            if pid in seen:
                bad("C3", where_day, "%s 已在%s出现过" % (poi["name"], seen[pid]))
            else:
                seen[pid] = where_day

            try:
                s, e = _min(it["start"]), _min(it["end"])
            except Exception:
                bad("C4", where_day, "%s 时间格式不合法（要 HH:MM）" % poi["name"])
                continue
            if e <= s:
                bad("C4", where_day, "%s 结束时间不晚于开始时间" % poi["name"])
                continue

            # C11
            if s < 8 * 60 or e > 22 * 60:
                bad("C11", where_day, "%s 的 %s–%s 超出 08:00–22:00" % (poi["name"], it["start"], it["end"]))

            # C4
            oa, ob = _open_range(poi["open"])
            if s < oa or e > ob:
                bad("C4", where_day, "%s 开放时间 %s，安排成了 %s–%s" % (poi["name"], poi["open"], it["start"], it["end"]))

            # C5
            try:
                wd = date.fromisoformat(d["date"]).weekday()
            except Exception:
                wd = None
            if wd is not None and wd in poi.get("closed_on", []):
                bad("C5", where_day, "%s 在%s闭馆" % (poi["name"], tools.WEEKDAY_CN[wd]))

            # C6
            if prev_end is not None:
                need = tools.transit_minutes(prev_id, pid)
                gap = s - prev_end
                if gap < need:
                    bad("C6", where_day, "%s→%s 需 %d 分钟，只留了 %d 分钟" % (
                        tools.POI_BY_ID[prev_id]["name"], poi["name"], need, gap))
            prev_end, prev_id = e, pid
            day_minutes += e - s

        # C7 活动总时长（含景点内时长）不能超过 10 小时
        if day_minutes > 10 * 60:
            bad("C7", where_day, "单日活动 %d 小时，超过 10 小时上限" % round(day_minutes / 60))
        if req.get("max_walk") is not None:
            must = set(req.get("must_visit") or [])
            heavy = [it["poi_id"] for it in items
                     if tools.POI_BY_ID.get(it.get("poi_id"), {}).get("walk", 1) > req["max_walk"]
                     and it["poi_id"] not in must]
            if heavy:
                bad("C7", where_day, "步行强度超限（要求≤%d）：%s" % (
                    req["max_walk"], "、".join(tools.POI_BY_ID[h]["name"] for h in heavy)))

        # S1 午餐：11:30–13:00 要有完整空档
        busy = [( _min(it["start"]), _min(it["end"])) for it in items]
        if not _gap_free(busy, 11 * 60 + 30, 13 * 60):
            w.append("%s 11:30–13:00 没有完整留出午餐时间" % where_day)

    # C10
    for pid in req.get("must_visit") or []:
        if pid not in seen:
            bad("C10", "整体", "用户点名要去的 %s 没有安排" % tools.POI_BY_ID.get(pid, {}).get("name", pid))

    # C8
    b = tools.budget_calc(plan, people=int(req.get("people", 1)))
    if req.get("budget") and b["total"] > float(req["budget"]) * int(req.get("people", 1)):
        bad("C8", "整体", "预算 %s 元（%s人），超了 %s 元" % (
            round(b["total"]), req.get("people", 1),
            round(b["total"] - float(req["budget"]) * int(req.get("people", 1)))))

    plan["budget"] = b
    return {"ok": not v, "violations": v, "warnings": w,
            "stats": {"days": len(days), "items": total_items, "total_cost": b["total"]}}


def _gap_free(busy: list[tuple[int, int]], start: int, end: int) -> bool:
    for s, e in busy:
        if s < end and start < e:
            return False
    return True


def feedback(result: dict) -> str:
    """把校验结果转成给模型看的中文反馈。"""
    if result["ok"]:
        return "校验通过。"
    lines = ["方案未通过校验，请按下列问题修改后重新调用 submit_plan："]
    for x in result["violations"]:
        lines.append("- [%s] %s：%s" % (x["code"], x["where"], x["msg"]))
    if result.get("warnings"):
        lines.append("另注意（不强制）：" + "；".join(result["warnings"]))
    lines.append("参考：预算合计 %s 元。" % round(result["stats"]["total_cost"]))
    return "\n".join(lines)
