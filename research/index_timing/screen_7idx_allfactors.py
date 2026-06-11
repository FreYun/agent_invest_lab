"""对 7 份新 methodology 的 9 条候选指数跑 local_sweep 全 44 因子 IS/OOS + validation（2026-06-11）。
沿用 screen_ne6_allfactors.py；路径改为迁移后 HOME（/home/ubuntu/rooot），urllib 绕代理。
跑法：cd research/index_timing && /home/ubuntu/rooot/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/bin/python3.11 screen_7idx_allfactors.py
"""
import sys, json, urllib.request
sys.path.insert(0, "/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np, pandas as pd
import local_sweep as L
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis

URL = "http://127.0.0.1:18078/mcp"; H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def call(m, p, sid=None):
    h = dict(H)
    if sid: h["Mcp-Session-Id"] = sid
    r = opener.open(urllib.request.Request(URL, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode(), headers=h), timeout=120)
    sid = r.headers.get("Mcp-Session-Id", sid); b = r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"): return json.loads(ln[5:].strip()), sid
    return json.loads(b), sid

_, sid = call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "i7f", "version": "1"}})

IDX = [("通信设备", "931160.CSI"), ("通信技术", "931144.CSI"),
       ("CS人工智", "930713.CSI"), ("创业板人工智能", "970070.SZ"),
       ("科创芯片", "000685.SH"), ("国证芯片", "980017.SZ"),
       ("中证半导", "931865.CSI"), ("云计算", "930851.CSI"), ("动漫游戏", "930901.CSI")]
syms = [c for _, c in IDX]
res, _ = call("tools/call", {"name": "market_index_quote", "arguments": {
    "market": "cn", "symbols": syms, "start_date": "2012-01-01", "end_date": "2026-06-10",
    "simulated_datetime": "2026-06-10 15:00:00"}}, sid)
items = {it.get("指数标识"): it for it in json.loads(res["result"]["content"][0]["text"]).get("items", [])}

def to_df(rec):
    df = pd.DataFrame(rec); df["date"] = pd.to_datetime(df["日期"]); df = df.set_index("date").sort_index()
    out = pd.DataFrame(index=df.index)
    out["close"] = pd.to_numeric(df["收盘"], errors="coerce")
    for src, dst in [("最高", "high"), ("最低", "low"), ("开盘", "open"), ("成交量", "volume")]:
        out[dst] = pd.to_numeric(df[src], errors="coerce") if src in df.columns else np.nan
    # 缺 high/low 用 close 代理（donchian 退化为 close 版；chandelier ATR 偏小，结果仅参考）
    out["high"] = out["high"].fillna(out["close"]); out["low"] = out["low"].fillna(out["close"])
    return out.dropna(subset=["close"])

def equity(df, sig, s):
    sig = sig.clip(0, 1).fillna(0).reindex(df.index).fillna(0); ret = df["close"].pct_change().fillna(0)
    pos = sig.shift(1).fillna(0); cost = pos.diff().abs().fillna(0) * 0.0005
    m = df.index >= pd.Timestamp(s); return (1 + (pos * ret - cost).loc[m]).cumprod()

for nm, code in IDX:
    it = items.get(code)
    if not it or not it.get("是否可用") or not it.get("行情记录"):
        print("\n#### %s %s 不可用" % (nm, code)); continue
    df = to_df(it["行情记录"])
    has_hl = it["行情记录"][0].get("最高") is not None
    d0 = df.index[0]; IS_S = str(d0.date()); IS_E = "2021-12-31"; OOS_S = "2022-01-01"; OOS_E = "2026-06-10"
    short_is = (pd.Timestamp(IS_E) - d0).days < 3 * 365
    wis = L.slice_range(df, IS_S, IS_E, 260); woos = L.slice_range(df, OOS_S, OOS_E, 260)
    bh_is = L.backtest(wis, L.s_buyhold(wis), IS_S); bh_os = L.backtest(woos, L.s_buyhold(woos), OOS_S)
    rows = []
    for name, fn, kw in L.GRID:
        if name == "buyhold": continue
        try:
            mis = L.backtest(wis, fn(wis, **kw), IS_S); mos = L.backtest(woos, fn(woos, **kw), OOS_S)
            rows.append((name, fn, kw, mis, mos))
        except Exception:
            pass
    winners = [r for r in rows if r[3]["calmar"] > bh_is["calmar"] and r[4]["calmar"] > bh_os["calmar"]]
    winners.sort(key=lambda r: r[4]["calmar"] - bh_os["calmar"], reverse=True)
    print("\n#### %s (%s) rows=%d %s起 OHLC=%s%s | BH: IS_cal=%.2f OOS_cal=%.2f OOS_dd=%.0f%%" % (
        nm, code, len(df), IS_S, has_hl, "（IS<3y 探索性）" if short_is else "", bh_is["calmar"], bh_os["calmar"], bh_os["max_drawdown"] * 100))
    if not winners:
        # 没有双赢者时给出 OOS 最优 3 个做参考（近-miss 上下文）
        rows.sort(key=lambda r: r[4]["calmar"] - bh_os["calmar"], reverse=True)
        print("  无因子 IS+OOS 同时击败 buyhold。OOS 端最好的 3 个（参考）：")
        for name, fn, kw, mis, mos in rows[:3]:
            print("    %-18s IS_dCal=%+.2f OOS_dCal=%+.2f OOSdd=%.0f%% flips=%d" % (
                name, mis["calmar"] - bh_is["calmar"], mos["calmar"] - bh_os["calmar"], mos["max_drawdown"] * 100, mos["trade_flips"]))
        continue
    for name, fn, kw, mis, mos in winners[:6]:
        eq = equity(df, fn(df, **kw), IS_S)  # 全周期 equity 做 validation
        ci = bootstrap_sharpe_ci(eq, n_bootstrap=600, confidence=0.95)
        wf = walk_forward_analysis(eq, [], n_windows=5); shs = [x.get("sharpe") for x in (wf.get("windows") or wf.get("per_window") or [])]; pos = sum(1 for x in shs if x and x > 0)
        cl = ci.get("ci_lower", 0); pl = ci.get("prob_positive", 0) * 100
        verd = "✅绿灯" if (cl > 0 and pos >= 4) else ("🟢近绿" if (pl >= 90 and pos >= 4) else "🟡黄灯")
        print("  %-18s IS_dCal=%+.2f OOS_cal=%.2f(dCal%+.2f) OOSdd=%.0f%% exp=%.2f flips=%d | CI[%.2f,%.2f]p+%.0f%% wf%d/5 %s" % (
            name, mis["calmar"] - bh_is["calmar"], mos["calmar"], mos["calmar"] - bh_os["calmar"], mos["max_drawdown"] * 100, mos["avg_exposure"], mos["trade_flips"], cl, ci.get("ci_upper", 0), pl, pos, verd))
