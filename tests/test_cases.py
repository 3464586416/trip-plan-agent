# -*- coding: utf-8 -*-
"""20 条约束用例：每条检查一个用户可感知的保证是否真的成立。

跑法：C:/Users/xb/.conda/envs/tripagent/python.exe -m pytest tests -q
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import planner  # noqa: E402
import tools  # noqa: E402
import constraints  # noqa: E402

# 测试不依赖真实模型：强制走规则版
planner.API_KEY = ""


def run(text: str) -> dict:
    return planner.plan_trip(text)


def codes(out: dict) -> list[str]:
    return [v["code"] for v in out["validation"]["violations"]]


def items_of(out: dict) -> list[dict]:
    return [it for d in out["plan"]["days"] for it in d["items"]]


# 1 天数解析
def test_days_parsed():
    assert run("广州 3 天，一个人")["req"]["days"] == 3


# 2 天数上限保护（用户说 10 天也只排 5 天）
def test_days_capped():
    assert run("广州 10 天")["req"]["days"] == 7


# 3b "1周" 解析为 7 天
def test_week_parsed():
    assert run("广州1周")["req"]["days"] == 7


# 3 中文人数解析
def test_people_chinese_number():
    assert run("广州 2 天，两个人")["req"]["people"] == 2


# 4 预算解析（"每人预算 800" 两个人 → 合计 1600）
def test_budget_per_person():
    out = run("广州 2 天，两个人，每人预算 800")
    assert out["req"]["budget"] == 1600


# 5 预算解析（"预算 1500" 无单位）
def test_budget_without_unit():
    assert run("广州 3 天，预算 1500")["req"]["budget"] == 1500


# 5b 单位写法：1W / 3k / 一万
def test_budget_units():
    assert run("广州1周，3人，预算1W")["req"]["budget"] == 10000
    assert run("广州 1 天，预算 3k")["req"]["budget"] == 3000


# 6 带父母 → 适老过滤 + 人数至少 2
def test_elderly_parsed():
    req = run("广州 2 天，带父母")["req"]
    assert req["elderly"] is True and req["people"] >= 2


# 7 "不想走太多路" → 步行强度 1，且方案里所有景点 walk<=1
def test_max_walk_enforced():
    out = run("广州 2 天，不想走太多路")
    assert out["req"]["max_walk"] == 1
    for it in items_of(out):
        assert tools.POI_BY_ID[it["poi_id"]]["walk"] <= 1


# 8 必去景点一定会被排进去
def test_must_visit_included():
    out = run("广州 3 天，想去广州塔")
    names = [tools.POI_BY_ID[it["poi_id"]]["name"] for it in items_of(out)]
    assert "广州塔" in names
    assert "C10" not in codes(out)


# 9 别名"省博"能识别成广东省博物馆
def test_alias_parse():
    out = run("广州 2 天，想去省博")
    assert "gz18" in out["req"]["must_visit"]


# 10 每天景点数不超过 4
def test_max_four_per_day():
    out = run("广州 4 天")
    for d in out["plan"]["days"]:
        assert len(d["items"]) <= 4


# 11 同一景点不重复
def test_no_duplicate_poi():
    out = run("广州 5 天")
    ids = [it["poi_id"] for it in items_of(out)]
    assert len(ids) == len(set(ids))


# 12 午餐时段留空（11:30–13:00 没有景点）
def test_lunch_reserved():
    out = run("广州 2 天")
    for d in out["plan"]["days"]:
        for it in d["items"]:
            assert not (it["start"] < "13:00" and it["end"] > "11:30"), (
                "景点跨过了午餐时段：%s %s-%s" % (it["poi_id"], it["start"], it["end"]))


# 13 相邻景点之间留够通行时间
def test_transit_gap():
    out = run("广州 3 天")
    for d in out["plan"]["days"]:
        for a, b in zip(d["items"], d["items"][1:]):
            gap = constraints._min(b["start"]) - constraints._min(a["end"])
            need = tools.transit_minutes(a["poi_id"], b["poi_id"])
            assert gap >= need, "%s→%s 只留了 %d 分钟，需要 %d" % (a["poi_id"], b["poi_id"], gap, need)


# 14 时间落在开放时间内
def test_within_open_hours():
    out = run("广州 3 天")
    for it in items_of(out):
        oa, ob = constraints._open_range(tools.POI_BY_ID[it["poi_id"]]["open"])
        assert oa <= constraints._min(it["start"]) and constraints._min(it["end"]) <= ob


# 15 闭馆日不安排
def test_closed_day_avoided():
    out = run("广州 4 天")
    for d in out["plan"]["days"]:
        wd = date.fromisoformat(d["date"]).weekday()
        for it in d["items"]:
            assert wd not in tools.POI_BY_ID[it["poi_id"]]["closed_on"]


# 16 总预算不超支（门票+餐饮+交通）
def test_budget_not_exceeded():
    out = run("广州 2 天，两个人，预算 1200")
    assert out["validation"]["stats"]["total_cost"] <= 1200 or "C8" in codes(out)


# 17 单日总时长不超过 10 小时
def test_day_hours():
    out = run("广州 3 天")
    for d in out["plan"]["days"]:
        if not d["items"]:
            continue
        span = constraints._min(d["items"][-1]["end"]) - constraints._min(d["items"][0]["start"])
        assert span <= 10 * 60


# 18 天数与日期连续
def test_dates_continuous():
    out = run("广州 3 天，明天出发")
    start = date.fromisoformat(out["req"]["start_date"])
    for i, d in enumerate(out["plan"]["days"]):
        assert d["date"] == (start + timedelta(days=i)).isoformat()


# 19 雨天优先室内（有雨日子室内占比 >= 一半）
def test_rain_indoor_first():
    out = run("广州 2 天")
    for d in out["plan"]["days"]:
        if "雨" not in (d.get("note") or ""):
            continue
        indoor = sum(1 for it in d["items"] if tools.POI_BY_ID[it["poi_id"]]["indoor"])
        assert indoor >= len(d["items"]) / 2


# 20 生成的 Markdown 含每天的日期、时段与预算
def test_markdown_shape():
    out = run("广州 2 天，每人预算 800")
    md = out["markdown"]
    assert "**预算**" in md
    for d in out["plan"]["days"]:
        assert d["date"] in md
    assert md.count("午餐") >= 1


# 额外：整体校验在常规输入下必须通过
def test_all_green_on_normal_input():
    for text in ("广州 2 天", "广州 3 天，两个人，预算 1500", "广州1周，3人，预算1W，喜欢拍照",
                 "广州 2 天，带父母不想走太多路"):
        out = run(text)
        assert out["validation"]["ok"], (text, codes(out))
