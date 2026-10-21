#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# BTC 整数关口监控 v2
# 穿越触发 + 迟滞带 + 当日去重 + 噪音熔断 + 单日上限
import json
import os
import sys
from datetime import datetime, timedelta

import requests

BASE = os.environ.get("STATE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
os.makedirs(BASE, exist_ok=True)
ST = os.path.join(BASE, "state.json")
LG = os.path.join(BASE, "alerts.log")
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


STEP = 1000        # 关口网格
HYST = 0.0015      # 迟滞带 ±0.15%
GAP = 45           # 最小间隔（分钟）
MOVE = 3.0         # 相对上次提醒价波动 %
NOISE = 3          # 熔断阈值
RANGE = 0.04       # 观察窗口


def px():
    return float(requests.get("https://blockchain.info/ticker", timeout=10).json()["USD"]["last"])
    except Exception:
        pass

    try:
        j = requests.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=15
        ).json()
        return float(j["price"])
    except Exception:
        pass

    try:
        j = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=15).json()
        return float(j["data"]["amount"])
    except Exception:
        pass

    raise RuntimeError("所有取价源均失败")


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
        if p >= lv * (1 + HYST):
            cur = "up"
        elif p <= lv * (1 - HYST):
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
        mk = "（强）" if lv % 5000 == 0 else ""
        push(
            f"BTC {dr}{lv:,}{mk}，现价{p:,.0f}",
            f"{dr}整数关口 {lv:,}（{'强关口' if mk else '千元关口'}）\n"
            f"现价 {p:,.0f}\n今日第 {bud()['used'] + 1} 条 · {now:%H:%M}",
        )
        st["a"] += 1
        take()
        s["ts"] = now.isoformat()
        s["ref"] = p
        ev.append(f"alert{lv}")

    if s["ref"] and abs(p / s["ref"] - 1) * 100 >= MOVE and bud()["used"] < CAP:
        if not s["ts"] or now - datetime.fromisoformat(s["ts"]) >= timedelta(minutes=GAP):
            c = (p / s["ref"] - 1) * 100
            push(
                f"BTC {'急涨' if c > 0 else '急跌'}{abs(c):.1f}%，现价{p:,.0f}",
                f"较上次提醒价 {s['ref']:,.0f} 变动 {c:+.1f}%\n现价 {p:,.0f}\n{now:%H:%M}",
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
