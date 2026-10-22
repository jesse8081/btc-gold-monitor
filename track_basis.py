#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股指期货（IC / IM）贴水日报生成器

用法:
    python3 track_basis.py                # 按当天生成 reports/YYYY-MM-DD.md
    python3 track_basis.py --date 2026-10-21   # 补历史某日（仅用当日及之前的数据）
    python3 track_basis.py --force        # 报告已存在时覆盖

依赖: 仅标准库（urllib / json），GitHub Actions 无需 pip install。

数据源:
    现货指数日线  腾讯 web.ifzq.gtimg.cn（主）/ 新浪 money.finance.sina.com.cn（备）
    期货合约行情  新浪财经 hq.sinajs.cn（持仓量用于选主力）
    期货日线历史  新浪财经 InnerFuturesNewService（主力连续 IC0 / IM0，用于历史分位）

输出约定（给上游定时任务用）:
    stdout 含 "SKIP"   -> 非交易日或数据未就绪，本次不产出报告
    stdout 含 "OK <path>" -> 正常产出
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta

CFFEX_PRODUCTS = [
    # code, 现货名称, 现货代码(腾讯/新浪), 主力连续代码(新浪)
    ("IC", "中证500", "sh000905", "IC0"),
    ("IM", "中证1000", "sh000852", "IM0"),
]
LOOKBACK_DAYS = 120          # 年化贴水分位的回看交易日数
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def http_get(url, referer=None, encoding="utf-8", timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw.decode(encoding, errors="replace")


# ---------------------------------------------------------------- 数据获取

def fetch_spot_daily(code, limit=400):
    """现货指数日线 -> [(date_str, close), ...] 升序

    主源腾讯（快、稳定），失败回落新浪。
    """
    try:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={code},day,,,{limit},qfq")
        data = json.loads(http_get(url))
        node = (data.get("data") or {}).get(code) or {}
        rows = node.get("day") or node.get("qfqday") or []
        out = [(r[0], float(r[2])) for r in rows if len(r) >= 3]
        if out:
            return out
    except Exception:
        pass

    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={limit}")
    rows = json.loads(http_get(url))
    return [(r["day"], float(r["close"])) for r in rows if r.get("day")]


def fetch_futures_daily(symbol):
    """新浪期货日线 -> [(date_str, close), ...] 升序"""
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_"
           f"{symbol}=/InnerFuturesNewService.getDailyKLine?symbol={symbol}")
    text = http_get(url)
    m = re.search(r"\((.*)\)\s*;?\s*$", text.strip(), re.S)
    if not m:
        return []
    rows = json.loads(m.group(1))
    return [(r["d"], float(r["c"])) for r in rows if r.get("d") and r.get("c")]


def fetch_quotes(codes):
    """新浪实时快照 -> {code: (close, open_interest)}"""
    url = "https://hq.sinajs.cn/list=" + ",".join("nf_" + c for c in codes)
    text = http_get(url, referer="https://finance.sina.com.cn", encoding="gbk")
    out = {}
    for line in text.splitlines():
        m = re.match(r'var hq_str_nf_(\w+)="(.*)";', line.strip())
        if not m:
            continue
        code, body = m.group(1), m.group(2)
        f = body.split(",")
        if len(f) < 7 or not f[3]:
            continue
        try:
            out[code] = (float(f[3]), float(f[6] or 0))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- 合约日历

def third_friday(year, month):
    d = date(year, month, 1)
    offset = (4 - d.weekday()) % 7          # 周五 = 4
    return d + timedelta(days=offset + 14)


def contract_chain(today, months=5):
    """从当月起的合约代码列表 [(code, expiry), ...]"""
    out = []
    y, m = today.year, today.month
    for _ in range(months):
        exp = third_friday(y, m)
        if exp >= today:
            out.append((f"{y % 100:02d}{m:02d}", exp))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def pick_main(product, today):
    """按持仓量选主力合约 -> (code, expiry, quote) 或 None"""
    chain = contract_chain(today)
    codes = [product + c for c, _ in chain]
    quotes = fetch_quotes(codes)
    best = None
    for code, exp in chain:
        full = product + code
        if full not in quotes:
            continue
        price, oi = quotes[full]
        if best is None or oi > best[2]:
            best = (full, exp, oi)
    if best is None:
        return None
    return best[0], best[1], quotes[best[0]][0]


# ---------------------------------------------------------------- 计算

def annualize(basis_rate, days):
    if days <= 0:
        return None
    return basis_rate * 365.0 / days


def hist_annualized_series(fut_hist, spot_hist, upto):
    """用主力连续 + 现货历史近似历史年化贴水序列（按最邻近到期月折算）"""
    spot = dict(spot_hist)
    series = []
    for d_str, fut_close in fut_hist:
        if d_str > upto or d_str not in spot:
            continue
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        exp = third_friday(d.year, d.month)
        if d > exp:
            y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
            exp = third_friday(y, m)
        days = (exp - d).days
        rate = (fut_close - spot[d_str]) / spot[d_str]
        ann = annualize(rate, days)
        if ann is not None:
            series.append(ann)
    return series


def percentile_of(value, series):
    if not series:
        return None
    below = sum(1 for x in series if x <= value)
    return below / len(series) * 100.0


def roll_advice(days_left, pct):
    if days_left is None:
        return "数据不足"
    if days_left <= 3:
        return "不宜抢跑（临近交割，贴水已进入收敛末端）"
    if pct is not None and pct >= 80:
        return "可提前移仓（贴水处历史深位，早移成本更低）"
    if pct is not None and pct <= 20:
        return "按日历移仓（贴水偏浅，无需抢跑）"
    return "按日历移仓（贴水中性）"


# ---------------------------------------------------------------- 主流程

def build_report(target):
    rows, errors = [], []
    for product, spot_name, spot_code, cont in CFFEX_PRODUCTS:
        try:
            spot_hist = fetch_spot_daily(spot_code)
            fut_hist = fetch_futures_daily(cont)
        except Exception as e:
            errors.append(f"{product} 数据拉取失败: {e}")
            continue
        if not spot_hist:
            errors.append(f"{product} 现货数据为空")
            continue

        spot_map = dict(spot_hist)
        tgt_str = target.strftime("%Y-%m-%d")
        if tgt_str not in spot_map:
            return None, [f"{tgt_str} 无现货收盘（非交易日或数据未就绪）"]

        spot_close = spot_map[tgt_str]
        main = pick_main(product, target)
        if main is None:
            errors.append(f"{product} 未取到合约快照")
            continue
        code, exp, fut_close = main

        days_left = (exp - target).days
        basis = fut_close - spot_close
        rate = basis / spot_close
        ann = annualize(rate, days_left)

        hist = hist_annualized_series(fut_hist, spot_hist, tgt_str)
        hist = hist[-LOOKBACK_DAYS:]
        pct = percentile_of(ann, hist) if ann is not None else None

        rows.append({
            "product": product, "spot_name": spot_name, "code": code,
            "exp": exp, "days_left": days_left, "spot": spot_close,
            "fut": fut_close, "basis": basis, "rate": rate,
            "ann": ann, "pct": pct, "n_hist": len(hist),
        })
    return rows, errors


def render(target, rows):
    tgt = target.strftime("%Y-%m-%d")
    lines = [f"# 股指期货贴水日报 {tgt}", ""]
    lines.append("| 品种 | 主力合约 | 现货收盘 | 期货收盘 | 基差 | 贴水率 | 距到期 | 年化贴水 | 年化分位 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        pct = f"{r['pct']:.0f}%" if r["pct"] is not None else "n/a"
        ann = f"{r['ann'] * 100:.2f}%" if r["ann"] is not None else "n/a"
        lines.append(
            f"| {r['product']}（{r['spot_name']}） | {r['code']} | {r['spot']:.2f} | "
            f"{r['fut']:.2f} | {r['basis']:+.2f} | {r['rate'] * 100:.2f}% | "
            f"{r['days_left']}天 | {ann} | {pct} |")
    lines.append("")
    lines.append(f"> 年化分位基于最近 {LOOKBACK_DAYS} 个交易日的主力连续近似序列；"
                 f"分位越高表示贴水越深（对多头/吃贴水越有利）。")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    for r in rows:
        ann = f"{r['ann'] * 100:.2f}%" if r["ann"] is not None else "n/a"
        pct = f"{r['pct']:.0f} 分位" if r["pct"] is not None else "分位 n/a"
        lines.append(f"- **{r['product']}**：年化贴水 {ann}（{pct}），"
                     f"距到期 {r['days_left']} 天 → {roll_advice(r['days_left'], r['pct'])}")
    lines.append("")
    lines.append(f"_生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}（数据源：腾讯 / 新浪财经）_")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="目标日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--outdir", default="reports")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    target = (datetime.strptime(args.date, "%Y-%m-%d").date()
              if args.date else date.today())
    path = os.path.join(args.outdir, target.strftime("%Y-%m-%d") + ".md")

    if os.path.exists(path) and not args.force:
        print(f"SKIP 报告已存在: {path}")
        return 0

    rows, errors = build_report(target)
    if not rows:
        print("SKIP " + "；".join(errors or ["无可用数据"]))
        return 0

    os.makedirs(args.outdir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(target, rows))
    for e in errors:
        print(f"WARN {e}")
    print(f"OK {path}")
    for r in rows:
        ann = f"{r['ann'] * 100:.2f}%" if r["ann"] is not None else "n/a"
        pct = f"{r['pct']:.0f}%" if r["pct"] is not None else "n/a"
        print(f"{r['product']} {r['code']} 年化贴水 {ann} 分位 {pct} 距到期 {r['days_left']}天")
    return 0


if __name__ == "__main__":
    sys.exit(main())
