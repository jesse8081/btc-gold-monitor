#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 金价整数关口监控 v2
# 穿越触发 + 迟滞带 + 当日去重 + 噪音熔断 + 单日上限
import json
import os
import sys
from datetime import datetime, timedelta

import requests

BASE = os.environ.get("STATE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
os.makedirs(BASE, exist_ok=True)
ST = os.path.join(BASE, "gold_state.json")
LG = os.path.join(BASE, "gold_alerts.log")
VERSION = "v2"

BUD = os.path.join(BASE, "budget.json")
CAP = 5            # 每日总推送上限（BTC + 金价共用）


def bud():
    d = datetime.now().strftime("%Y-%m-%d")
    try:
        b = json.load(open(BUD, encoding="utf-8"))
        if b.get("date") == d:
            return b
    except Exception:
        pass
    return {"date": d, "used": 0}


def take():
    b = bud()
    if b["used"] >= CAP:
        return False
    b["used"] += 1
    with open(BUD, "w", encoding="utf-8") as f:
        json.dump(b, f, ensure_ascii=False)
    return True


STEP = 50          # 关口网格 元/克
HYST = 3.0         # 迟滞带 ±3 元（绝对值）
GAP = 60           # 最小间隔（分钟）
MOVE = 20.0        # 相对上次提醒价波动（元）
NOISE = 3
RANGE = 0.06


def px():
    # 沪金 Au9999，单位 元/克。新浪偶发返回空，重试 3 次
    import time

    last = None
    for i in range(3):
        try:
            r = requests.get(
                "https://hq.sinajs.cn/list=SGE_AU9999",
                headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            r.encoding = "gbk"
            body = r.text.split('"')[1] if '"' in r.text else ""
            parts = body.split(",")
            if len(parts) >= 5:
                return float(parts[3])
            last = RuntimeError("返回为空")
        except Exception as e:
            last = e
        time.sleep(3)
    raise RuntimeError(f"取价失败: {last}")


def hook():
    k = os.environ.get("SERVERCHAN_KEY", "").strip()
    return f"https://sctapi.ftqq.com/{k}.send" if k else ""


def push(t, b):
    try:
        open(LG, "a", encoding="utf-8").write(f"[{datetime.now():%F %T}] {t}\n")
    except Exception:
        pass
    u = hook()
    if not u:
        return
    try:
        requests.post(u, data={"title": t, "desp": b}, timeout=10)
    except Exception as e:
        print("push fail", e, file=sys.stderr)


def load():
    try:
        s = json.load(open(ST, encoding="utf-8"))
        if s.get("v") == VERSION:
            return s
    except Exception:
        pass
    return {"v": VERSION, "date": "", "last": None, "ts": None, "ref": None, "lv": {}}


def save(s):
    tmp = ST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(tmp, ST)


def main():
    s = load()
    now = datetime.now()
    d = now.strftime("%Y-%m-%d")
    if s["date"] != d:
        s.update({"date": d, "lv": {}})

    try:
        p = px()
    except Exception as e:
        print(f"取价失败，本轮跳过: {e}")
        return
    ev = []
    lo, hi = p * (1 - RANGE), p * (1 + RANGE)
    for lv in range(int(lo // STEP * STEP), int(hi // STEP * STEP) + STEP, STEP):
        if lv <= 0:
            continue
        st = s["lv"].setdefault(str(lv), {"p": None, "a": 0, "c": 0})
        prev = st["p"]
        if p >= lv + HYST:
            cur = "up"
        elif p <= lv - HYST:
            cur = "dn"
        else:
            cur = prev
        if prev is None or cur == prev:
            st["p"] = cur
            continue
        st["p"] = cur
        st["c"] += 1
        if st["c"] > NOISE:
            ev.append(f"skip{lv}noise")
            continue
        if st["a"] > 0:
            ev.append(f"skip{lv}dup")
            continue
        if bud()["used"] >= CAP:
            ev.append("skipcap")
            continue
        if s["ts"] and now - datetime.fromisoformat(s["ts"]) < timedelta(minutes=GAP):
            ev.append("skipgap")
            continue
        dr = "上破" if cur == "up" else "下破"
        push(
            f"金价 {dr}{lv}元，现价{p:.1f}",
            f"{dr}整数关口 {lv} 元/克\n现价 {p:.2f} 元/克\n今日第 {bud()['used'] + 1} 条 · {now:%H:%M}",
        )
        st["a"] += 1
        take()
        s["ts"] = now.isoformat()
        s["ref"] = p
        ev.append(f"alert{lv}")

    if s["ref"] and abs(p - s["ref"]) >= MOVE and bud()["used"] < CAP:
        if not s["ts"] or now - datetime.fromisoformat(s["ts"]) >= timedelta(minutes=GAP):
            dm = p - s["ref"]
            push(
                f"金价 {'急涨' if dm > 0 else '急跌'}{abs(dm):.0f}元，现价{p:.1f}",
                f"较上次提醒价 {s['ref']:.2f} 变动 {dm:+.2f} 元\n现价 {p:.2f} 元/克\n{now:%H:%M}",
            )
            take()
            s["ts"] = now.isoformat()
            s["ref"] = p
            ev.append("alertmove")

    s["last"] = p
    save(s)
    print(" ".join(ev) if ev else "ok")


if __name__ == "__main__":
    main()
