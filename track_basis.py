#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股指期货（IC / IM）贴水跟踪日报生成器

用法:
    python3 track_basis.py                     # 按当天生成 reports/YYYY-MM-DD.md
    python3 track_basis.py --date 2026-10-21   # 补历史某日（只用该日及之前的数据）
    python3 track_basis.py --force             # 报告已存在时覆盖
    python3 track_basis.py --lookback 60       # 分位窗口（默认 60 个交易日）

依赖: 仅标准库（urllib / json），GitHub Actions 无需 pip install。

数据源（全部为新浪财经）:
    现货指数日线  腾讯 web.ifzq.gtimg.cn（主）/ 新浪 getKLineData（备），两者实测一致
    合约日线与快照 新浪 InnerFuturesNewService.getDailyKLine（含收盘价 c / 成交量 v / 持仓量 p）
                   今日盘中回落到 hq.sinajs.cn 快照

口径:
    基差 = 期货 − 现货；基差率 = 基差 / 现货
    年化贴水 = 基差率 × 365 / 剩余自然日
    深度分位 = 近 N 个交易日中「比当前更浅」的天数占比，越高代表当前贴水越极端

输出约定（给上游定时任务用）:
    stdout 含 "SKIP"      -> 非交易日或数据未就绪，本次不产出报告
    stdout 含 "OK <path>" -> 正常产出
"""
import argparse
import json
import os
import re
import sys
import time
import traceback
import urllib.request
from datetime import date, datetime, timedelta, timezone

CFFEX_PRODUCTS = [
    # code, 现货名称, 现货代码
    ("IC", "中证500", "sh000905"),
    ("IM", "中证1000", "sh000852"),
]
LOOKBACK_DAYS = 60           # 年化贴水深度分位的回看交易日数
CN_TZ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def today_cn():
    """服务器多为 UTC，按北京时间取当日，避免跨零点算错日期"""
    return datetime.now(CN_TZ).date()


def http_get(url, referer=None, encoding="utf-8", timeout=20, retries=3):
    """带重试；非 2xx / 超时 / 空响应都会重试，最终抛最后一次异常。"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            if referer:
                req.add_header("Referer", referer)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            if not raw:
                raise ValueError("空响应")
            return raw.decode(encoding, errors="replace")
        except Exception as e:                       # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise last



# ---------------------------------------------------------------- 数据获取

def fetch_spot_daily(code, limit=400):
    """现货指数日线 -> {date_str: close}；主源腾讯，失败回落新浪。"""
    try:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={code},day,,,{limit},qfq")
        data = json.loads(http_get(url))
        node = (data.get("data") or {}).get(code) or {}
        rows = node.get("day") or node.get("qfqday") or []
        out = {r[0]: float(r[2]) for r in rows if len(r) >= 3}
        if out:
            return out
    except Exception:
        pass

    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={limit}")
    rows = json.loads(http_get(url))
    return {r["day"]: float(r["close"]) for r in rows if r.get("day")}


def fetch_spot_realtime(code):
    """现货指数实时价 -> float；收盘后（15:00 之后）即为当日收盘价。

    用于当日日线尚未发布时的兜底，避免 15:10 跑出来只是 SKIP、报告却没生成。
    """
    text = http_get(f"https://hq.sinajs.cn/list={code}",
                    referer="https://finance.sina.com.cn", encoding="gbk")
    m = re.search(r'"([^"]*)"', text)
    if not m:
        return None
    f = m.group(1).split(",")
    if len(f) < 4:
        return None
    try:
        v = float(f[3])
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def fetch_contract_daily(symbol):
    """单合约日线 -> {date_str: (close, oi, vol)}

    新浪在部分网络（如境外 runner）会返回 null / 空 / 非 JSON，一律按空处理，
    由调用方决定降级，不能让单合约把整个进程打挂。
    """
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_"
           f"{symbol}=/InnerFuturesNewService.getDailyKLine?symbol={symbol}")
    text = http_get(url)
    m = re.search(r"\((.*)\)\s*;?\s*$", text.strip(), re.S)
    if not m:
        return {}
    try:
        rows = json.loads(m.group(1))
    except (ValueError, TypeError):
        return {}
    if not isinstance(rows, list):
        return {}
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        d, c = r.get("d"), r.get("c")
        if not d or not c:
            continue
        try:
            out[d] = (float(c), float(r.get("p") or 0), float(r.get("v") or 0))
        except (TypeError, ValueError):
            continue
    return out


def fetch_snapshot(codes):
    """新浪期货快照 -> {code: (last, oi, vol)}，仅当日盘中/收盘后补齐用。"""
    url = "https://hq.sinajs.cn/list=" + ",".join("nf_" + c for c in codes)
    text = http_get(url, referer="https://finance.sina.com.cn", encoding="gbk")
    out = {}
    for line in text.splitlines():
        m = re.match(r'var hq_str_nf_(\w+)="(.*)";', line.strip())
        if not m:
            continue
        code, body = m.group(1), m.group(2)
        f = body.split(",")
        if len(f) < 8 or not f[3]:
            continue
        try:
            out[code] = (float(f[3]), float(f[6] or 0), float(f[4] or 0))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- 合约日历

def third_friday(year, month):
    """CFFEX 股指期货最后交易日：合约月第三个周五"""
    d = date(year, month, 1)
    offset = (4 - d.weekday()) % 7          # 周五 = 4
    return d + timedelta(days=offset + 14)


def _add_months(year, month, n):
    m = month - 1 + n
    return year + m // 12, m % 12 + 1


def listed_contracts(today, product):
    """当月 / 下月 / 下季 / 隔季 -> [(full_code, expiry), ...]"""
    y, m = today.year, today.month
    # 定位当月：本月合约已到期则顺延一个月
    for _ in range(3):
        if third_friday(y, m) >= today:
            break
        y, m = _add_months(y, m, 1)

    picks = [(y, m), _add_months(y, m, 1)]
    qy, qm = picks[-1]
    for _ in range(2):
        qy, qm = _add_months(qy, qm, 1)
        while qm % 3 != 0:                   # 季月：03 / 06 / 09 / 12
            qy, qm = _add_months(qy, qm, 1)
        picks.append((qy, qm))

    out = []
    for yy, mm in picks:
        exp = third_friday(yy, mm)
        if exp >= today:
            out.append((f"{product}{yy % 100:02d}{mm:02d}", exp))
    return out


# ---------------------------------------------------------------- 计算

def annualize(basis_rate, days):
    return None if days <= 0 else basis_rate * 365.0 / days


def depth_percentile(value, series):
    """近 N 日「比当前更浅」的天数占比（越高 = 当前越深）"""
    if not series or value is None:
        return None
    shallower = sum(1 for x in series if x > value)
    return shallower / len(series) * 100.0


def bar(pct, width=10):
    filled = 0 if pct is None else max(0, min(width, int(round(pct / 100.0 * width))))
    return "█" * filled + "░" * (width - filled)


def band_of(pct):
    if pct is None:
        return "数据不足"
    if pct >= 70:
        return "贴水偏深 ★"
    if pct <= 30:
        return "贴水偏浅"
    return "中性"


def per_contract_series(closes, spot_map, exp, upto, lookback):
    """单合约年化贴水序列（只用 upto 及之前）"""
    out = []
    for d_str in sorted(closes):
        if d_str > upto or d_str not in spot_map:
            continue
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (exp - d).days
        if days <= 0:
            continue
        rate = (closes[d_str][0] - spot_map[d_str]) / spot_map[d_str]
        ann = annualize(rate, days)
        if ann is not None:
            out.append((d_str, ann))
    return out[-lookback:]


def roll_advice(days_left, target_pct):
    if days_left <= 8:
        return "已进移仓窗口：本周内完成"
    if target_pct is not None and target_pct >= 70:
        return "可提前移仓（目标贴水处深位）"
    if target_pct is not None and target_pct <= 30:
        return "不宜抢跑（目标贴水偏浅）"
    return "按日历：到期前 5–8 个交易日移仓"


# ---------------------------------------------------------------- 主流程

def build_report(target, lookback):
    tgt = target.strftime("%Y-%m-%d")
    products, errors = [], []

    for product, spot_name, spot_code in CFFEX_PRODUCTS:
        try:
            spot_map = fetch_spot_daily(spot_code)
        except Exception as e:
            errors.append(f"{product} 现货拉取失败: {e}")
            continue
        if not spot_map:
            errors.append(f"{product} 现货数据为空")
            continue
        if tgt not in spot_map:
            # 当日日线还没发布（15:10 跑时常见）→ 用实时价兜底，仅在收盘后启用
            filled = False
            if target == today_cn() and datetime.now(CN_TZ).hour * 60 + datetime.now(CN_TZ).minute >= 15 * 60:
                try:
                    v = fetch_spot_realtime(spot_code)
                except Exception:                    # noqa: BLE001
                    v = None
                if v:
                    spot_map[tgt] = v
                    filled = True
                    errors.append(f"{product} 现货日线未更新，已用 {v:.2f} 兜底（收盘后实时价）")
            if not filled:
                return None, [f"{tgt} 无现货收盘（非交易日或数据未就绪）"]

        spot = spot_map[tgt]
        chain = listed_contracts(target, product)
        codes = [c for c, _ in chain]

        # 当日行情：目标日是今天 → 优先一次快照拿全 4 个合约（价 / 持仓 / 成交）；
        # 补历史日期时快照无效，只能逐合约取日线。
        snap, daily, failed = {}, {}, []
        if target == today_cn():
            try:
                snap.update(fetch_snapshot(codes))
            except Exception as e:                   # noqa: BLE001
                failed.append(f"snapshot({type(e).__name__})")

        for code in codes:
            if code in snap:
                continue
            try:
                time.sleep(0.25)                     # 避免连续请求触发限流
                daily[code] = fetch_contract_daily(code)
            except Exception as e:                   # noqa: BLE001
                daily[code] = {}
                failed.append(f"{code}({type(e).__name__})")
            if tgt in daily[code]:
                snap[code] = daily[code][tgt]

        if not snap:
            errors.append(f"{product} 未取到合约行情" + (f"，失败: {', '.join(failed)}" if failed else ""))
            continue
        if failed:
            errors.append(f"{product} 部分数据失败: {', '.join(failed)}")

        main_code, main_exp = max((ce for ce in chain if ce[0] in snap),
                                  key=lambda ce: snap[ce[0]][1])

        rows_chain = []
        for code, exp in chain:
            if code not in snap:
                continue
            close, oi, vol = snap[code]
            days = (exp - target).days
            rate = (close - spot) / spot
            rows_chain.append({
                "code": code, "exp": exp, "days": days, "close": close,
                "rate": rate, "ann": annualize(rate, days), "oi": oi, "vol": vol,
            })

        # 移仓目标：下季合约（链上第 3 个）
        target_row = rows_chain[2] if len(rows_chain) >= 3 else None

        # 历史序列只对「主力」和「目标」两个合约取日线（省请求、降限流风险）
        n_failed = len(failed)
        for code in {main_code, target_row["code"] if target_row else main_code}:
            if code in daily:
                continue
            try:
                time.sleep(0.25)
                daily[code] = fetch_contract_daily(code)
            except Exception as e:                   # noqa: BLE001
                daily[code] = {}
                failed.append(f"{code}历史({type(e).__name__})")
        if len(failed) > n_failed:
            errors.append(f"{product} 历史数据缺失: {', '.join(failed[n_failed:])}")

        # 主力合约的自身年化序列（近 N 日）
        main_row = next(r for r in rows_chain if r["code"] == main_code)
        series = per_contract_series(daily.get(main_code, {}), spot_map, main_exp, tgt, lookback)
        cur_ann = main_row["ann"]
        pct = depth_percentile(cur_ann, [a for _, a in series])
        prev_ann = next((a for d, a in reversed(series) if d < tgt), None)
        chg = (cur_ann - prev_ann) * 100 if (prev_ann is not None and cur_ann is not None) else None

        spot_prev = next((spot_map[d] for d in sorted(spot_map, reverse=True) if d < tgt), None)
        spot_chg = (spot / spot_prev - 1) if spot_prev else None   # 小数，渲染时再转百分比

        tgt_pct = tgt_series = None
        if target_row:
            tgt_series = per_contract_series(
                daily.get(target_row["code"], {}), spot_map, target_row["exp"], tgt, lookback)
            tgt_pct = depth_percentile(target_row["ann"], [a for _, a in tgt_series])
            gap = target_row["days"] - main_row["days"]
            spread = (main_row["close"] - target_row["close"]) / main_row["close"]
            target_row["spread"] = spread
            target_row["spread_ann"] = annualize(spread, gap)

        if cur_ann is None:
            errors.append(f"{product} 主力合约年化无法计算（剩余天数 {main_row['days']}）")
            continue

        products.append({
            "product": product, "spot_name": spot_name, "spot": spot,
            "spot_chg": spot_chg, "main": main_row, "chain": rows_chain,
            "pct": pct, "chg": chg, "series": series,
            "hist_min": min((a for _, a in series), default=None),
            "hist_max": max((a for _, a in series), default=None),
            "hist_med": sorted(a for _, a in series)[len(series) // 2] if series else None,
            "target": target_row, "target_pct": tgt_pct,
            "advice": roll_advice(main_row["days"], tgt_pct),
            "band": band_of(pct),
        })

    return products, errors


def _pct(v, signed=False, digits=1):
    if v is None:
        return "n/a"
    return f"{v * 100:+.{digits}f}%" if signed else f"{v * 100:.{digits}f}%"


def render(target, products, lookback):
    tgt = target.strftime("%Y-%m-%d")
    L = [f"# 股指期货贴水跟踪日报 · {tgt}", "", ""]
    L.append(f"数据源：新浪财经（收盘价 + 持仓量）｜口径：基差 = 期货 − 现货；"
             f"年化贴水 = 基差率 × 365 / 剩余天数｜分位窗口：近 {lookback} 个交易日")
    L.append("")

    # ---- 结论
    L += ["## 结论", ""]
    deep = [p for p in products if p["pct"] is not None and p["pct"] >= 70]
    names = "、".join(p["product"] for p in products)
    if deep:
        d = deep[0]
        L.append(f"今日 {names} 中 {d['product']} 贴水偏深（深度分位 {d['pct']:.0f}%），"
                 f"{d['advice']}。")
    else:
        bits = "、".join(f"{p['product']} {p['pct']:.0f}%" for p in products if p["pct"] is not None) or "分位数据不足"
        L.append(f"今日 {names} 贴水均处中性区间（{bits}），无明确移仓信号。")
    L.append("")

    # ---- 一、主力合约贴水快照
    L += ["## 一、主力合约贴水快照", ""]
    L.append("| 品种 | 主力合约 | 到期日 | 期货收盘 | 现货收盘 | 现货涨跌 | 基差 | 基差率 | 年化贴水 | 年化变化 | 深度分位 | 状态 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for p in products:
        m = p["main"]
        chg = f"{p['chg']:+.2f}pct" if p["chg"] is not None else "n/a"
        pct = f"{p['pct']:.0f}%" if p["pct"] is not None else "n/a"
        L.append(
            f"| {p['product']} | {m['code']} | {m['exp'].strftime('%Y-%m-%d')} | "
            f"{m['close']:.1f} | {p['spot']:.1f} | {_pct(p['spot_chg'], True, 2)} | "
            f"{m['close'] - p['spot']:+.1f} | {_pct(m['rate'], True, 2)} | {_pct(m['ann'], True)} | "
            f"{chg} | {pct} | {p['band']} |")
    L += ["", ""]

    # ---- 二、年化贴水历史区间
    L += [f"## 二、年化贴水历史区间（近{lookback}日）", ""]
    L.append("| 品种 | 当前 | 最浅 | 中位 | 最深 | 深度分位 |")
    L.append("|---|---|---|---|---|---|")
    for p in products:
        if p["pct"] is None:
            L.append(f"| {p['product']} | {_pct(p['main']['ann'], True)} | "
                     f"{_pct(p['hist_max'], True)} | {_pct(p['hist_med'], True)} | "
                     f"{_pct(p['hist_min'], True)} | n/a |")
        else:
            L.append(f"| {p['product']} | {_pct(p['main']['ann'], True)} | {_pct(p['hist_max'], True)} | "
                     f"{_pct(p['hist_med'], True)} | {_pct(p['hist_min'], True)} | "
                     f"{bar(p['pct'])} {p['pct']:.0f}% |")
    L += ["", "", "分位线均为年化贴水取值，越负代表贴水越深。深度分位越高 = 当前贴水相对近"
          f"{lookback}日越极端（对多头越有利）。", ""]

    # ---- 三、期限结构
    L += ["## 三、期限结构（当日全部在市合约）", ""]
    L.append("| 品种 | 合约 | 到期日 | 剩余天数 | 收盘 | 基差率 | 年化 | 持仓量 | 成交量 | 标记 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for p in products:
        for r in p["chain"]:
            if r["code"] == p["main"]["code"]:
                tag = "主力"
            elif r["days"] <= 35:
                tag = "近月"
            else:
                tag = ""
            L.append(f"| {p['product']} | {r['code']} | {r['exp'].strftime('%Y-%m-%d')} | {r['days']} | "
                     f"{r['close']:.1f} | {_pct(r['rate'], True, 2)} | {_pct(r['ann'], True)} | "
                     f"{r['oi']:,.0f} | {r['vol']:,.0f} | {tag} |")
    L += ["", ""]

    # ---- 四、近20个交易日
    L += ["## 四、主力合约年化贴水 · 近20个交易日", ""]
    dates = sorted({d for p in products for d, _ in p["series"]})[-20:]
    L.append("| 日期 | " + " | ".join(p["product"] for p in products) + " |")
    L.append("|---" * (len(products) + 1) + "|")
    for d in dates:
        cells = []
        for p in products:
            v = dict(p["series"]).get(d)
            cells.append(_pct(v, True) if v is not None else "n/a")
        L.append(f"| {d} | " + " | ".join(cells) + " |")
    L += ["", ""]

    # ---- 五、滚贴水 · 移仓窗口
    L += ["## 五、滚贴水 · 移仓窗口", ""]
    L.append("| 品种 | 主力 | 主力票息 | 主力分位 | 目标 | 目标票息 | 目标分位 | 跨期价差 | 建议 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for p in products:
        t = p["target"]
        if not t:
            continue
        sp = (f"{t['spread'] * 100:+.2f}%（{t['spread_ann'] * 100:+.1f}%）"
              if t.get("spread") is not None else "n/a")
        tp = f"{p['target_pct']:.0f}%" if p["target_pct"] is not None else "n/a"
        mp = f"{p['pct']:.0f}%" if p["pct"] is not None else "n/a"
        ma = f"{abs(p['main']['ann']) * 100:.1f}%" if p["main"]["ann"] is not None else "n/a"
        ta = f"{abs(t['ann']) * 100:.1f}%" if t["ann"] is not None else "n/a"
        L.append(f"| {p['product']} | {p['main']['code']} | {ma} | "
                 f"{mp} | {t['code']} | {ta} | "
                 f"{tp} | {sp} | {p['advice']} |")
    L += ["", ""]

    # ---- 口径说明
    L += ["口径说明", ""]
    L.append("主力年化：主力合约贴水持有到期收敛对应的年化收益，即多头滚贴水的「票息」，越厚越好。")
    L.append("")
    L.append(f"主力分位：该票息相对主力合约自身近{lookback}日的位置。≥70% 表示当前是深贴水时段。")
    L.append("")
    L.append(f"目标分位：移仓目标合约的贴水相对其自身近{lookback}日的位置。这是「要不要现在移仓」"
             "的核心——移仓本质是买入目标合约的贴水，买在分位高位才划算。")
    L.append("")
    L.append("跨期价差：主力与目标的价差占主力价格比例及年化。多头从近月滚远月，价差为正说明"
             "「高卖低买」，但票息会被摊薄，需与目标分位一起看。")
    L.append("")
    for p in products:
        t = p["target"]
        if not t:
            continue
        thin = abs(p["main"]["ann"]) - abs(t["ann"])     # 小数尺度，阈值 0.002 = 0.2pct
        band = "中位" if 30 < (p["target_pct"] or 50) < 70 else ("高位" if (p["target_pct"] or 0) >= 70 else "低位")
        if thin > 0.002:
            head = f"目标票息比主力薄 {thin * 100:.1f}pct，提前移仓会摊薄收益"
        elif thin < -0.002:
            head = f"目标票息比主力厚 {abs(thin) * 100:.1f}pct，远月更划算"
        else:
            head = "目标票息与主力基本持平，移仓摩擦是主要成本"
        L.append(f"{p['product']}：{head}；目标合约贴水处近{lookback}日 "
                 f"{p['target_pct']:.0f}% 分位（{band}）。" if p["target_pct"] is not None else
                 f"{p['product']}：{head}。")
    L += ["", ""]
    L.append("日历基准：CFFEX 股指期货最后交易日为合约月第三个周五（遇节假日顺延）。近月流动性通常在"
             "到期前 5–8 个交易日衰减，此时完成移仓最稳。")
    L.append("贴水来源：IC/IM 成分股分红较少，贴水主要来自中性策略对冲需求与融券成本，因此分位比绝对值更有参考意义。")
    L.append("")
    L.append("本报告由本地脚本自动生成，仅供跟踪参考，不构成投资建议。")
    L.append("")
    L.append("#股指期货贴水跟踪 #滚贴水 #移仓窗口")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="目标日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--outdir", default="reports")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    target = (datetime.strptime(args.date, "%Y-%m-%d").date()
              if args.date else today_cn())
    path = os.path.join(args.outdir, target.strftime("%Y-%m-%d") + ".md")

    # 先建目录：即使当日 SKIP 也要让 reports/ 存在，
    # 否则 CI 里 `git add reports` 会因 pathspec 不存在直接 exit 128。
    os.makedirs(args.outdir, exist_ok=True)

    if os.path.exists(path) and not args.force:
        print(f"SKIP 报告已存在: {path}")
        return 0

    try:
        products, errors = build_report(target, args.lookback)
    except Exception as e:                           # noqa: BLE001
        print(f"FAIL {target} 生成失败: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1
    if not products:
        msg = "；".join(errors or ["无可用数据"])
        if any("无现货收盘" in e for e in errors):
            print("SKIP " + msg)          # 非交易日 / 数据未就绪
            return 0
        print("FAIL " + msg)              # 数据源异常：要让 CI 变红，不能静默
        return 1

    with open(path, "w", encoding="utf-8") as f:
        f.write(render(target, products, args.lookback))
    for e in errors:
        print(f"WARN {e}")
    print(f"OK {path}")
    for p in products:
        m, t = p["main"], p["target"]
        tp = f"{p['target_pct']:.0f}%" if p["target_pct"] is not None else "-"
        print(f"{p['product']} {m['code']} 年化 {_pct(m['ann'], True)} "
              f"深度分位 {p['pct']:.0f}% 剩 {m['days']}天 | "
              f"目标 {t['code'] if t else '-'} {_pct(t['ann'], True) if t else '-'} "
              f"分位 {tp} | {p['advice']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
