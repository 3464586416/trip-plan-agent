# -*- coding: utf-8 -*-
"""行程规划 Agent 主循环。

流程：
  需求解析 → 模型带工具做规划（search_poi / get_weather / distance_between / budget_calc）
  → submit_plan → 代码硬校验 → 不通过把问题清单回灌 → 最多 3 轮
  → 模型不可用时降级到规则版排程（保证演示永远有输出）
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import date, timedelta

import constraints
import tools

_prog = threading.local()


def _p(pct: int, stage: str) -> None:
    """把当前阶段回报给调用方（Web 端用来画进度条）。"""
    cb = getattr(_prog, "cb", None)
    if cb:
        try:
            cb(int(pct), stage)
        except Exception:
            pass


BASE_URL = os.getenv("TRIP_LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
MODEL = os.getenv("TRIP_LLM_MODEL", "qwen-plus")
API_KEY = os.getenv("TRIP_LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")

MAX_TOOL_STEPS = 18
MAX_PLAN_ROUNDS = 3
DEBUG = os.getenv("TRIP_DEBUG") == "1"


def _dbg(*a):
    if DEBUG:
        print("[planner]", *a, file=sys.stderr)

SYSTEM_PROMPT = """你是"广州行程规划师"，用工具查数据、排行程，最后必须调用 submit_plan 提交。

硬性要求（会被代码校验，不通过会打回重排）：
1. 天数、日期严格按要求；相邻景点之间留够 distance_between 返回的通行时间；
2. 每个景点的时间必须落在开放时间内，避开闭馆日；
3. 单日最多 4 个景点，活动总时长不超过 10 小时；
4. 预算按 budget_calc 的结果控制在用户要求以内（预算默认是"每人"）；
5. 用户要求的步行强度、适老、室内偏好必须满足：优先调用 search_poi 的 elderly_only / max_walk / indoor_only 过滤，不要自己猜；
6. 每天 11:30–14:00 之间留出午餐空档；
6b. 同一天尽量只安排同一行政区或相邻区的景点；番禺/南沙/花都/从化/增城这类远郊点单独占一天；
6c. 每个景点在一趟行程里只能出现一次，第 2 天以后不要重复第 1 天用过的点；
6d. search_poi 返回的 closed_on 是闭馆星期（0=周一 … 6=周日），排之前先核对当天是周几，
    周一闭馆的博物馆不要排到周一，否则会被打回；
7. 天气有雨时，把室外项目换成室内备选；
8. 每天尽量排 3–4 个景点（受开放时间或用户限制排不满就少排，不要硬凑）。

时间一律 HH:MM。poi_id 必须来自工具返回结果，不要编造。
调工具要节制：search_poi 最多 3 次（每次拿到一批候选就够排了）、distance_between 只查你真正要连排的两三个景点、budget_calc 一次就够。
查完立刻调用 submit_plan 提交完整方案，不要反复查同样的东西。
提交被拒时，逐条解决反馈里的问题再提交，不要原样重交。"""



def _parse_date_range(text: str, today: date):
    """识别 12.3~12.9 / 12月3日-12月9日 / 12/3 至 12/9，返回 (start_iso, 天数)。"""
    sep = r"\s*(?:~|-|—|–|到|至|起)\s*"
    # 同月：12月3日-9日
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?" + sep + r"(\d{1,2})\s*[日号]", text)
    if m:
        mon, d1, d2 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mon <= 12 and 1 <= d1 <= 31 and 1 <= d2 <= 31 and d2 >= d1:
            y = _pick_year(today, mon, d1)
            start = date(y, mon, d1)
            return start.isoformat(), (date(y, mon, d2) - start).days + 1
    # 跨月：12.3~12.9 / 12月3日-12月9日 / 12/3 至 12/9
    m = re.search(r"(\d{1,2})\s*[.月/]\s*(\d{1,2})\s*[日号]?" + sep +
                  r"(\d{1,2})\s*[.月/]\s*(\d{1,2})\s*[日号]?", text)
    if m:
        m1, d1, m2, d2 = (int(m.group(i)) for i in range(1, 5))
        if all(1 <= x <= 12 for x in (m1, m2)) and 1 <= d1 <= 31 and 1 <= d2 <= 31:
            y1 = _pick_year(today, m1, d1)
            y2 = y1 + 1 if m2 < m1 else y1
            try:
                start = date(y1, m1, d1)
                end = date(y2, m2, d2)
            except ValueError:
                return None
            span = (end - start).days + 1
            if span <= 0:
                return None
            return start.isoformat(), span
    # 单日：12月3日 / 12.3（当作出发日，天数另算）
    return None


def _pick_year(today: date, month: int, day: int) -> int:
    """没写年份时：今年还没过就用今年，过了就用明年。"""
    try:
        this_year = date(today.year, month, day)
    except ValueError:
        return today.year
    return this_year.year if this_year >= today else today.year + 1

# ------------------------------------------------------------------ 需求解析

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn2num(tok: str) -> int:
    """把"三""十""十二""二十五"这类中文数字转成 int，认不出就当 1。"""
    if not tok:
        return 1
    if tok.isdigit():
        return int(tok)
    if tok == "十":
        return 10
    if "十" in tok:
        a, _, b = tok.partition("十")
        return (_CN_DIGITS.get(a, 1) if a else 1) * 10 + (_CN_DIGITS.get(b, 0) if b else 0)
    return _CN_DIGITS.get(tok[-1], 1)


def parse_request(text: str, today: date | None = None) -> dict:
    today = today or date.today()
    req = {"city": "广州", "days": 2, "people": 1, "budget": None, "start_date": None,
           "elderly": False, "max_walk": None, "indoor_pref": False,
           "must_visit": [], "interests": [], "raw": text}

    # ---------- 天数：3天 / 三天 / 1D / 2 day / 一周 / 1个星期 ----------
    explicit = {"days": False, "budget": False, "people": False}
    m = re.search(r"(?<![月\d])(\d+|[一二两三四五六七八九十]+)\s*(?:天|日|days|day|d|D)", text)
    if m:
        tok = m.group(1)
        n = int(tok) if tok.isdigit() else _cn2num(tok)
        req["days"] = max(1, min(n, 7))
        explicit["days"] = True
        if n > 7:
            req["hint"] = "你要排 %d 天，这边最多排 7 天，先给你 7 天的版本。" % n
    else:
        m = re.search(r"(\d+|[一二两三四五六七八九十]+|半)\s*(?:个)?\s*(?:星期|礼拜|周)", text)
        if m:
            tok = m.group(1)
            n = 0.5 if tok == "半" else (int(tok) if tok.isdigit() else _cn2num(tok))
            want = int(round(n * 7))
            req["days"] = max(1, min(want, 7))
            explicit["days"] = True
            if want > 7:
                req["hint"] = "你要排 %d 天，这边最多排 7 天，先给你 7 天的版本。" % want
        elif any(k in text for k in ("一日游", "当天往返", "当天来回")):
            req["days"] = 1
            explicit["days"] = True
        elif ("个月" in text or "一个月" in text
              or re.search(r"\d+\s*[mM](?![a-zA-Z])", text)):
            req["days"] = 7
            explicit["days"] = True
            req["hint"] = "行程最多排 7 天，先按 7 天给你。"
        elif "半天" in text or "半日" in text:
            req["days"] = 1
            explicit["days"] = True
        elif "周末" in text and req["days"] == 2:
            explicit["days"] = True

    # "2W / 3w" 歧义：附近没有金额词时按周理解（2W = 2 周）
    money_ctx = any(k in text for k in ("预算", "花费", "费用", "元", "块", "¥", "￥", "RMB", "rmb"))
    budget_text = text
    if not money_ctx and not explicit["days"]:
        m = re.search(r"(\d+|[一二两三四五六七八九十]+)\s*[wW](?![a-zA-Z])", text)
        if m:
            tok = m.group(1)
            n = int(tok) if tok.isdigit() else _cn2num(tok)
            want = n * 7
            req["days"] = max(1, min(want, 7))
            explicit["days"] = True
            if want > 7:
                req["hint"] = "你要排 %d 天（%d 周），这边最多排 7 天，先给你 7 天的版本。" % (want, n)
            budget_text = text[:m.start()] + " " + text[m.end():]

    # ---------- 预算：1W / 3k / 1万 / 8000元 / 800块 / 预算 1500 / ¥2000 / 1RMB ----------
    m = re.search(r"(?:预算|花费|费用|大概|大约|约|不超过|控制在|最多)?\s*"
                  r"(\d+(?:\.\d+)?)\s*(万|w|W|k|K|千|元|块|¥|￥|rmb|RMB|Rmb)", budget_text)
    if m:
        val = float(m.group(1))
        unit = m.group(2).lower()
        if unit in ("w", "万"):
            val *= 10000
        elif unit in ("k", "千"):
            val *= 1000
        req["budget"] = val
        explicit["budget"] = True
    else:
        m = re.search(r"预算\s*(?:是|为|大概|约)?\s*(\d+(?:\.\d+)?)", budget_text)
        if m:
            req["budget"] = float(m.group(1))
            explicit["budget"] = True

    # 前缀货币（¥1200 / $300）与英文 budget，以及"每人 500"这类裸数字
    if req["budget"] is None:
        m = re.search(r"[\u00a5\uffe5$]\s*(\d+(?:\.\d+)?)", text)
        if m:
            req["budget"] = float(m.group(1))
            explicit["budget"] = True
    if req["budget"] is None:
        m = re.search(r"(?:budget|Budget|BUDGET)\s*[:=]?\s*(\d+(?:\.\d+)?)", text)
        if m:
            req["budget"] = float(m.group(1))
            explicit["budget"] = True
    if req["budget"] is None:
        m = re.search(r"(?:每人|人均)\s*(?:大概|约)?\s*(\d+(?:\.\d+)?)", text)
        if m:
            req["budget"] = float(m.group(1))
            explicit["budget"] = True
            req["budget_per_person"] = True

    # ---------- 人数：3人 / 三个人 / 2位 / 两大一小 ----------
    m = re.search(r"([一二两三四五六七八九十\d]+)\s*大\s*([一二两三四五六七八九十\d]+)\s*小", text)
    if m:
        a = int(m.group(1)) if m.group(1).isdigit() else _cn2num(m.group(1))
        b = int(m.group(2)) if m.group(2).isdigit() else _cn2num(m.group(2))
        req["people"] = max(1, min(a + b, 8))
        explicit["people"] = True
    else:
        m = re.search(r"([一二两三四五六七八九十\d]+)\s*(?:个)?\s*(?:人|位|口|成人|大人)", text)
        if m:
            tok = m.group(1)
            n = int(tok) if tok.isdigit() else _cn2num(tok)
            if "父母" not in text and "老人" not in text:
                req["people"] = max(1, min(n, 8))
                explicit["people"] = True

    if any(k in text for k in ("父母", "老人", "爸妈", "长辈", "爷爷", "奶奶")):
        req["elderly"] = True
        req["people"] = max(req["people"], 2)
    # "每人/人均 800" 换算成整趟总预算
    if req["budget"] and any(k in text for k in ("每人", "人均", "一个人")):
        req["budget"] = req["budget"] * int(req["people"])
    if req["budget"] is None:
        for num in re.findall(r"(?<![\d.])(\d{3,6})(?![\d.])", text):
            v = float(num)
            if v >= 100 and v != req["days"] and v != req["people"]:
                req["budget"] = v
                explicit["budget"] = True
                break
    if req.get("budget_per_person") and req["budget"]:
        req["budget"] = req["budget"] * int(req["people"])
    req["explicit"] = explicit
    req["assumed_days"] = not explicit["days"]
    if any(k in text for k in ("不想走", "少走", "走不动", "腿脚", "轻松")):
        req["max_walk"] = 1
    elif any(k in text for k in ("爬山", "徒步", "暴走", "运动量", "走多点", "多走路", "多走点",
                                 "健身", "锻炼", "体力好", "不怕走", "能走")):
        req["max_walk"] = 3
    if any(k in text for k in ("下雨", "雨天", "室内", "避暑", "空调")):
        req["indoor_pref"] = True

    # 日期：区间 > 显式日期 > 明天 > 今天
    rng = _parse_date_range(text, today)
    if rng:
        req["start_date"], span = rng
        req["days"] = max(1, min(span, 7))
        explicit["days"] = True
        if span > 7:
            req["hint"] = "你选的时间跨度是 %d 天，这边最多排 7 天，先给你前 7 天的版本。" % span
    elif re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text):
        m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text)
        req["start_date"] = date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    elif re.search(r"(\d{1,2})\s*[.月/]\s*(\d{1,2})\s*[日号]?(?!\s*[.月/\d])", text):
        m = re.search(r"(\d{1,2})\s*[.月/]\s*(\d{1,2})\s*[日号]?", text)
        mon, day = int(m.group(1)), int(m.group(2))
        if 1 <= mon <= 12 and 1 <= day <= 31:
            req["start_date"] = date(_pick_year(today, mon, day), mon, day).isoformat()
        else:
            req["start_date"] = today.isoformat()
    elif "后天" in text:
        req["start_date"] = (today + timedelta(days=2)).isoformat()
    elif "明天" in text:
        req["start_date"] = (today + timedelta(days=1)).isoformat()
    elif "下周末" in text:
        days_ahead = (5 - today.weekday()) % 7 + 7
        req["start_date"] = (today + timedelta(days=days_ahead)).isoformat()
    elif "周末" in text:
        days_ahead = (5 - today.weekday()) % 7
        req["start_date"] = (today + timedelta(days=days_ahead or 7)).isoformat()
    else:
        req["start_date"] = today.isoformat()

    ask = []
    if not explicit["days"]:
        ask.append("打算玩几天？（说\"3 天\"就行）")
    if req["budget"] is None:
        ask.append("大概预算多少？人多的话给个总预算，我来均摊。")
    if not req["must_visit"] and not req["interests"]:
        ask.append("有特别想去的点吗？比如广州塔、省博，或\"想拍照\"\"带爸妈\"。")
    req["ask"] = ask[:2]
    if not explicit["days"] and req["days"] == 2:
        req["hint"] = "你没写玩几天，我先按 2 天排；告诉我\"3 天\"这种就能改。"
    ALIASES = {"省博": "广东省博物馆", "省博物馆": "广东省博物馆", "小蛮腰": "广州塔",
               "塔": "广州塔", "沙面岛": "沙面", "粤博": "广东省博物馆", "中大": "中山大学"}
    for poi in tools.POIS:
        if poi["name"] in text and poi["id"] not in req["must_visit"]:
            req["must_visit"].append(poi["id"])
    for alias, target in ALIASES.items():
        if alias in text:
            for poi in tools.POIS:
                if target in poi["name"] and poi["id"] not in req["must_visit"]:
                    req["must_visit"].append(poi["id"])
                    break
    TAG_GROUPS = {"历史": ("近代史", "考古", "古建", "西汉"),
                  "文化": ("岭南文化", "碑刻", "书院"),
                  "建筑": ("古建", "欧陆建筑", "骑楼", "哥特建筑", "建筑"),
                  "美食": ("小吃", "本地生活"),
                  "逛街": ("购物", "文创"),
                  "自然": ("公园", "自然", "花园"),
                  "拍照": ("拍照", "夜景", "江景")}
    for word, tags in TAG_GROUPS.items():
        if word in text:
            for t in tags:
                if t not in req["interests"]:
                    req["interests"].append(t)
    for tag in ("博物馆", "公园", "夜景", "古镇", "寺庙", "小吃", "购物", "动物园", "园林", "爬山"):
        if tag in text and tag not in req["interests"]:
            req["interests"].append(tag)
    return req


# ------------------------------------------------------------------ 模型主循环

def _client():
    from openai import OpenAI
    return OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=60)


def _requirement_block(req: dict) -> str:
    lines = ["用户需求：", req["raw"], "", "已解析约束：",
             "- 天数：%d，出发日期：%s" % (req["days"], req["start_date"]),
             "- 人数：%d" % req["people"]]
    if req["budget"]:
        lines.append("- 预算：每人 %s 元（合计 %s 元）" % (req["budget"], req["budget"] * req["people"]))
    if req["elderly"]:
        lines.append("- 同行有老人：只安排适老景点，步行强度不超过 2")
    if req["max_walk"]:
        lines.append("- 步行强度上限：%d（1 很轻松 / 2 一般 / 3 费腿）" % req["max_walk"])
    if req["must_visit"]:
        lines.append("- 必去：" + "、".join(tools.POI_BY_ID[i]["name"] for i in req["must_visit"]))
    if req["interests"]:
        lines.append("- 兴趣：" + "、".join(req["interests"]))
    lines.append("- 城市：广州（景点库已内置，用 search_poi 查）")
    return "\n".join(lines)


def plan_with_llm(req: dict) -> dict | None:
    if not API_KEY:
        return None
    try:
        client = _client()
    except Exception:
        return None

    _p(12, "查询天气与候选景点")
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _requirement_block(req)}]
    rounds, steps = 0, 0
    tool_use: dict[str, int] = {}
    fix_mode = False
    seen_ids: list[str] = []
    last_result = None
    while rounds < MAX_PLAN_ROUNDS and steps < MAX_TOOL_STEPS:
        steps += 1
        try:
            resp = client.chat.completions.create(model=MODEL, messages=messages,
                                                  tools=tools.TOOL_SCHEMAS, tool_choice="auto",
                                                  temperature=0.3)
        except Exception as exc:
            print("[planner] 模型调用失败：%s" % str(exc)[:160])
            return None
        msg = resp.choices[0].message
        _p(min(88, 15 + steps * 6), "模型规划第 %d 步" % steps)
        _dbg("step", steps, "round", rounds, "tools", [tc.function.name for tc in (msg.tool_calls or [])])
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])] or None})
        if not msg.tool_calls:
            messages.append({"role": "user", "content": "请继续：用工具查数据，最后调用 submit_plan 提交方案。"})
            continue

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            if name == "submit_plan":
                plan = args.get("plan") or {}
                _dbg("submit", json.dumps(plan, ensure_ascii=False)[:500])
                bad_ids = [it.get("poi_id") for d in (plan.get("days") or [])
                           for it in (d.get("items") or [])
                           if it.get("poi_id") not in tools.POI_BY_ID]
                if bad_ids:
                    allowed = "、".join("%s=%s" % (i, tools.POI_BY_ID[i]["name"]) for i in seen_ids[:40]) or \
                              "、".join("%s=%s" % (p["id"], p["name"]) for p in tools.POIS[:12])
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps({"ok": False,
                                                            "error": "这些 poi_id 不存在：%s" % "、".join(map(str, bad_ids)),
                                                            "allowed_ids": allowed,
                                                            "hint": "poi_id 必须原样复制 search_poi 返回的 id 字段（形如 gz01），不能自造"},
                                                            ensure_ascii=False)})
                    fix_mode = True
                    continue
                rounds += 1
                _p(78, "校验硬约束")
                plan.setdefault("title", "广州 %d 日行程" % req["days"])
                result = constraints.validate(plan, req)
                last_result = (plan, result, rounds)
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps({"ok": result["ok"],
                                                        "violations": result["violations"],
                                                        "stats": result["stats"]}, ensure_ascii=False)})
                if result["ok"]:
                    _annotate_lunch(plan)
                    return {"plan": plan, "validation": result, "rounds": rounds, "source": "llm"}
                used = "、".join("%s=%s" % (i, tools.POI_BY_ID[i]["name"])
                                 for d in (plan.get("days") or [])
                                 for it in (d.get("items") or [])
                                 for i in [it.get("poi_id")] if i in tools.POI_BY_ID)
                messages.append({"role": "user", "content": constraints.feedback(result) +
                                 "\n已用景点（不能重复使用）：" + used +
                                 "\n【本轮只做修复：不要再调用 search_poi / get_weather / distance_between / budget_calc，直接按上面的问题改完再调用 submit_plan】"})
                fix_mode = True
                continue
            if fix_mode:
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps({"error": "修复轮里不允许再查工具，请直接 submit_plan"}, ensure_ascii=False)})
                continue
            tool_use[name] = tool_use.get(name, 0) + 1
            if name == "search_poi" and tool_use[name] > 4:
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps({"error": "search_poi 额度已用完（4 次）。候选列表已经足够，"
                                                                 "请马上排程并调用 submit_plan。"}, ensure_ascii=False)})
                continue
            impl = tools.TOOL_IMPL.get(name)
            if impl is None:
                payload = {"error": "未知工具 %s" % name}
            else:
                try:
                    payload = impl(**args)
                    if name == "search_poi":
                        for item in (payload or {}).get("items", []):
                            if item.get("id") and item["id"] not in seen_ids:
                                seen_ids.append(item["id"])
                except Exception as exc:
                    payload = {"error": str(exc)[:160]}
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(payload, ensure_ascii=False)})
        if steps >= 10 and rounds == 0:
            # 步数快用完了还没提交：直接催它交方案
            messages.append({"role": "user", "content": "不要再查了，现在就调用 submit_plan 提交你手上的方案。"})
    _dbg("loop end", "rounds", rounds, "steps", steps, "last_result", bool(last_result))
    if last_result:
        plan, result, r = last_result
        _p(88, "自动校正方案")
        fixed = _repair_plan(plan, req)
        if fixed:
            fixed["rounds"] = r
            return fixed
        return {"plan": plan, "validation": result, "rounds": r, "source": "llm-partial"}
    return None


# ------------------------------------------------------------------ 规则降级

def _annotate_lunch(plan: dict) -> None:
    """模型给的方案里补上"午餐空档"标注，页面和 Markdown 才统一。"""
    for d in plan.get("days") or []:
        if d.get("lunch"):
            continue
        items = d.get("items") or []
        if not items:
            continue
        busy = [(constraints._min(i["start"]), constraints._min(i["end"])) for i in items]
        # 必须是完整的 11:30–13:00 空档，才标"午餐"
        free = constraints._gap_free(busy, 11 * 60 + 30, 13 * 60)
        if free and constraints._min(items[0]["start"]) < 13 * 60:
            d["lunch"] = "11:30–13:00"


def _slot_fit(poi: dict, start_min: int) -> tuple[int, int] | None:
    oa, ob = constraints._open_range(poi["open"])
    s = max(start_min, oa, 9 * 60)
    e = min(s + int(poi["duration_h"] * 60), ob)
    if e - s < int(poi["duration_h"] * 60 * 0.8):
        return None
    return s, e


def _repair_plan(plan: dict, req: dict) -> dict | None:
    """模型方案只差"重复景点/通行时间不够"时，用代码修掉，别让用户看兜底。

    规则：同一天按开始时间排序 → 去掉重复景点 → 从 09:00 起按真实通行时间重排，
    跨午餐时段（11:30–13:00）顺延到 13:00，超出开放时间或 22:00 的点直接丢弃。
    """
    seen: set[str] = set()
    days = []
    for d in plan.get("days") or []:
        try:
            wd = date.fromisoformat(d.get("date") or "").weekday()
        except Exception:
            wd = None
        items = sorted((d.get("items") or []), key=lambda x: x.get("start") or "09:00")
        kept, cursor, prev = [], None, None
        for it in items:
            pid = it.get("poi_id")
            poi = tools.POI_BY_ID.get(pid)
            if not poi or pid in seen:
                continue
            if wd is not None and wd in (poi.get("closed_on") or []):
                continue
            dur = int(poi["duration_h"] * 60)
            oa, ob = constraints._open_range(poi["open"])
            start = max(cursor or 0, oa, 9 * 60)
            # 别跨午饭：11:30 前没结束就挪到 13:00
            if start < 13 * 60 and start + dur > 11 * 60 + 30:
                start = max(start, 13 * 60)
            end = min(start + dur, ob)
            if end - start < dur * 0.8 or end > 22 * 60:
                continue
            gap = tools.transit_minutes(prev, pid) if prev else 0
            start = max(start, (cursor or 0) + gap)
            end = min(start + dur, ob)
            if end - start < dur * 0.8 or end > 22 * 60:
                continue
            kept.append({"poi_id": pid, "start": constraints._fmt(start), "end": constraints._fmt(end)})
            seen.add(pid)
            cursor, prev = end, pid
        days.append({"date": d.get("date"), "weekday": d.get("weekday"), "items": kept,
                     "lunch": d.get("lunch") or "", "note": d.get("note") or ""})
    fixed = {"title": plan.get("title") or "广州 %d 日行程" % req["days"],
             "summary": plan.get("summary") or "", "days": days}
    result = constraints.validate(fixed, req)
    if not result["ok"]:
        return None
    _annotate_lunch(fixed)
    return {"plan": fixed, "validation": result, "source": "llm-repaired"}


def plan_with_rules(req: dict) -> dict:
    start = date.fromisoformat(req["start_date"])
    weather = tools.get_weather("广州", req["start_date"], req["days"])
    rainy_days = {w["date"] for w in weather["days"] if (w.get("rain_prob") or 0) >= 60 or "雨" in w["weather"]}

    cand = tools.search_poi(elderly_only=req["elderly"],
                            max_walk=req["max_walk"],
                            indoor_only=req["indoor_pref"] or False,
                            exclude=[], limit=99)["items"]
    # 用户点名要去的，即使和适老/步行强度筛选项冲突也要补进来（硬约束 C10 优先）
    have = {p["id"] for p in cand}
    for mid in req["must_visit"]:
        if mid not in have:
            poi = tools.POI_BY_ID.get(mid)
            if poi:
                cand.append(tools._slim(poi))
    # 优先安排必去
    ordered = [p for p in cand if p["id"] in req["must_visit"]] + \
              [p for p in cand if p["id"] not in req["must_visit"]]
    ordered.sort(key=lambda p: (-(p["id"] in req["must_visit"]), p["walk"], p["ticket"]))

    used: set[str] = set()
    days = []
    LUNCH_S, LUNCH_E = 11 * 60 + 30, 13 * 60
    for i in range(req["days"]):
        d = start + timedelta(days=i)
        dstr = d.isoformat()
        is_rain = dstr in rainy_days
        pool = [p for p in ordered if p["id"] not in used and
                (not is_rain or (p["indoor"] or p["walk"] <= 1 or p["id"] in req["must_visit"]))]
        if is_rain:
            # 雨天把室内景点提到前面，但用户点名的必去优先
            pool.sort(key=lambda p: (p["id"] not in req["must_visit"], not p["indoor"], p["walk"], p["ticket"]))
        items, prev_id = [], None
        wd = d.weekday()
        interests = set(req.get("interests") or [])

        def score(p: dict) -> int:
            return len(interests & set(p.get("tags") or []))

        if interests:
            pool.sort(key=lambda p: (-(p["id"] in req["must_visit"]), -score(p), p["walk"], p["ticket"]))

        def fill(until: int, cursor: int, budget: int):
            """从 cursor 排到 until，返回新的 cursor/prev_id/可排数量"""
            nonlocal prev_id
            n = 0
            while n < budget:
                picked = None
                for p in pool:
                    if p["id"] in used:
                        continue
                    if wd in (p.get("closed_on") or []):
                        continue
                    gap = tools.transit_minutes(prev_id, p["id"]) if prev_id else 0
                    s = cursor + gap
                    dur = int(round(p["duration_h"] * 60))
                    if s + dur > until:
                        continue
                    fit = _slot_fit(p, s)
                    if fit is None or fit[1] > until:
                        # 景点 09:30 才开门这类情况会把结束时间顶出时段，重新挑
                        continue
                    picked = (p, fit)
                    break
                if not picked:
                    break
                p, (s, e) = picked
                items.append({"poi_id": p["id"], "start": constraints._fmt(s), "end": constraints._fmt(e),
                              "note": "室内" if p["indoor"] else ""})
                used.add(p["id"])
                pool.remove(p)
                cursor, prev_id = e, p["id"]
                n += 1
            return cursor

        # 上午：必须在 11:30 前结束
        fill(LUNCH_S, 9 * 60, 2)
        lunch = ""
        if items:
            night = bool(interests & {"夜景", "拍照", "观景"})
            # 下午：从 13:00 开始；要看夜景就留一个名额给晚上
            fill(17 * 60 + 30, max(LUNCH_E, 13 * 60), (3 if night else 4) - len(items))
            if night and len(items) < 4:
                night_tags = {"夜景", "观景", "江景"}
                candidates = [p for p in pool
                              if p["id"] not in used and night_tags & set(p.get("tags") or [])]
                if candidates:
                    saved = pool[:]
                    pool[:] = candidates
                    fill(21 * 60 + 30, 19 * 60, 1)
                    pool[:] = saved
            if len(items) > 0:
                lunch = "%s–%s" % (constraints._fmt(LUNCH_S), constraints._fmt(LUNCH_E))
        note = "有雨，安排室内为主" if is_rain else ""
        days.append({"date": dstr, "weekday": tools.WEEKDAY_CN[d.weekday()], "items": items,
                     "lunch": lunch, "note": note})

    plan = {"title": "广州 %d 日行程（规则版）" % req["days"],
            "summary": "按适老/步行强度/天气自动排布，午餐时段留空。",
            "days": days}
    return plan


# ------------------------------------------------------------------ 对外入口

def plan_trip(text: str, progress=None) -> dict:
    _prog.cb = progress
    try:
        return _plan_trip(text)
    finally:
        _prog.cb = None


def _plan_trip(text: str) -> dict:
    _p(6, "解析需求")
    req = parse_request(text)
    llm = plan_with_llm(req) if API_KEY else None
    if llm and llm["validation"]["ok"]:
        out = {"req": req, "source": llm["source"], "rounds": llm["rounds"],
               "plan": llm["plan"], "validation": llm["validation"]}
    elif llm:
        # 模型 3 轮仍不达标：规则版若更靠谱就换掉，避免把违约方案给出去
        plan = plan_with_rules(req)
        v = constraints.validate(plan, req)
        if v["ok"]:
            out = {"req": req, "source": "rules(fallback)", "rounds": llm["rounds"],
                   "plan": plan, "validation": v}
        else:
            out = {"req": req, "source": llm["source"], "rounds": llm["rounds"],
                   "plan": llm["plan"], "validation": llm["validation"]}
    else:
        _p(35, "规则排程")
        plan = plan_with_rules(req)
        out = {"req": req, "source": "rules", "rounds": 0, "plan": plan,
               "validation": constraints.validate(plan, req)}
    _p(94, "生成页面文案")
    out["markdown"] = tools.export_markdown(out["plan"])["markdown"]
    return out


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]) or "广州 2 天，每人预算 800，带父母不想走太多路"
    r = plan_trip(text)
    print(r["markdown"])
    print("\n[来源] %s，轮数 %s，校验 %s" % (r["source"], r["rounds"], r["validation"]["ok"]))
    for x in r["validation"]["violations"]:
        print("  ✗ [%s] %s: %s" % (x["code"], x["where"], x["msg"]))
