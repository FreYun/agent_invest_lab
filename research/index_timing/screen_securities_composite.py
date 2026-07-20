"""券商 399975.SZ 组合因子扫描：把方法论/mem0 里已经在用的三个辅助信号
（板块风险分 / macro_50etf_vix / PE 分位）与 mom_20 做组合，看能不能在
IS+OOS Calmar 上稳超 mom_20 单因子（当前唯一绿灯）。

数据可用起点：
  - 券商 quote 2007-06 起（用于 mom_20 与回测标的收益）
  - PE 分位 2014-12 起（market_index_val 只给 PE + PE_pctl，无 PB）
  - 板块风险分 2015-01 起（sector_factor BK000128）
  - macro_50etf_vix 2015 起
组合期从 2015-01-01 开始，样本共 ~11 年。IS/OOS 切在 2021-12-31。

统计验证：bootstrap Sharpe CI 600 + walk-forward 5 窗；5bp 单边成本 + T+1。
产物：STDOUT 汇总 + 保存到 runs/securities_composite/summary.json。
"""
import json, os, sys, urllib.request, math, hashlib
sys.path.insert(0, "/home/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
import numpy as np
import pandas as pd
from backtest.validation import bootstrap_sharpe_ci, walk_forward_analysis

URL = "http://127.0.0.1:18078/mcp"
H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
OUT_DIR = os.path.join(os.path.dirname(__file__), "runs", "securities_composite")
os.makedirs(OUT_DIR, exist_ok=True)
CACHE = os.path.join(OUT_DIR, "data_cache.pkl")


import time
def _raw(method, params, sid):
    h = dict(H)
    if sid: h["Mcp-Session-Id"] = sid
    r = urllib.request.urlopen(
        urllib.request.Request(URL, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(), headers=h),
        timeout=180,
    )
    sid2 = r.headers.get("Mcp-Session-Id", sid)
    body = r.read().decode()
    for ln in body.splitlines():
        if ln.startswith("data:"):
            return json.loads(ln[5:].strip()), sid2
    return json.loads(body), sid2

def new_session():
    _, sid = _raw("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sec-cmp", "version": "1"}}, None)
    return sid

def tool_call(name, args, sid=[None], max_retry=3):
    """带重试、错误自愈的 tool 调用。sid 用 list 做外层共享。"""
    for i in range(max_retry):
        try:
            if sid[0] is None:
                sid[0] = new_session()
            r, sid[0] = _raw("tools/call", {"name": name, "arguments": args}, sid[0])
            if "result" not in r:
                # 服务端错误：重置 sid 再试
                err = r.get("error") or r
                if i < max_retry - 1:
                    time.sleep(0.5 + i)
                    sid[0] = None
                    continue
                raise RuntimeError(f"no result: {str(err)[:200]}")
            return r
        except Exception as e:
            if i == max_retry - 1: raise
            time.sleep(1 + i)
            sid[0] = None
    raise RuntimeError("unreachable")


def fetch_all():
    if os.path.exists(CACHE):
        return pd.read_pickle(CACHE)
    SID = [None]

    # 分段拉券商 quote（跨度大用分段避免超时）
    quote_frames = []
    for y0 in range(2007, 2027, 3):
        y1 = min(y0 + 3, 2027)
        r = tool_call("market_index_quote", {
            "market": "cn", "symbols": ["399975.SZ"],
            "start_date": f"{y0}-01-01", "end_date": f"{y1}-01-01",
            "simulated_today": "2026-06-29", "simulated_datetime": "2026-06-29 15:00:00",
        }, SID)
        items = json.loads(r["result"]["content"][0]["text"])["items"]
        if items and items[0].get("行情记录"):
            for rec in items[0]["行情记录"]:
                quote_frames.append(rec)
    q = pd.DataFrame(quote_frames)
    q["date"] = pd.to_datetime(q["日期"]).dt.normalize()
    q = q.set_index("date").sort_index()
    q["close"] = pd.to_numeric(q["收盘"], errors="coerce")
    q = q[["close"]].dropna()
    q = q[~q.index.duplicated(keep="last")]

    # PE + PE 分位（2014 起）
    val_frames = []
    # market_index_val 一次给一天，改成月末点探（组合信号只需较低频，不会引入未来）
    dates = pd.date_range("2014-12-31", "2026-06-30", freq="B")
    # 效率考虑：拉月末 + 每周一（同时 forward-fill 至日频）
    sample_dates = sorted(set(list(pd.date_range("2014-12-31", "2026-06-30", freq="W-MON")) + list(pd.date_range("2014-12-31", "2026-06-30", freq="BME"))))
    for d in sample_dates:
        ds = d.strftime("%Y-%m-%d")
        try:
            r = tool_call("market_index_val", {
                "symbols": ["399975.SZ"], "start_date": ds, "end_date": ds,
                "simulated_today": ds, "simulated_datetime": f"{ds} 15:00:00",
            }, SID)
            items = json.loads(r["result"]["content"][0]["text"])["items"]
            if items and items[0].get("估值快照"):
                snap = items[0]["估值快照"]
                val_frames.append({"date": pd.to_datetime(snap["交易日期"]).normalize(),
                                   "pe": snap.get("市盈率_TTM"),
                                   "pe_pctl": snap.get("历史百分位")})
        except Exception as e:
            print(f"val {ds} err {e}")
    v = pd.DataFrame(val_frames).drop_duplicates(subset=["date"]).set_index("date").sort_index()

    # 板块风险分（2015 起，分段拉）
    sec_frames = []
    for y0 in range(2015, 2027, 2):
        y1 = min(y0 + 2, 2027)
        try:
            r = tool_call("sector_factor", {
                "sec_codes": ["BK000128"],
                "start_date": f"{y0}-01-01", "end_date": f"{y1}-01-01",
                "simulated_today": "2026-06-29", "simulated_datetime": "2026-06-29 15:00:00",
            }, SID)
            items = json.loads(r["result"]["content"][0]["text"])["items"]
            if items and items[0].get("记录"):
                for rec in items[0]["记录"]:
                    sec_frames.append(rec)
        except Exception as e:
            print(f"sector {y0} err {e}")
    s = pd.DataFrame(sec_frames)
    s["date"] = pd.to_datetime(s["日期"]).dt.normalize()
    s = s.set_index("date").sort_index()
    s["risk_score"] = pd.to_numeric(s["风险分"], errors="coerce")
    s["sector_mom"] = pd.to_numeric(s["动量"], errors="coerce")
    s = s[["risk_score", "sector_mom"]]
    s = s[~s.index.duplicated(keep="last")]

    # VIX（2015 起，分段拉）
    vix_frames = []
    for y0 in range(2015, 2027, 2):
        y1 = min(y0 + 2, 2027)
        try:
            r = tool_call("macro_50etf_vix", {
                "start_date": f"{y0}-01-01", "end_date": f"{y1}-01-01",
                "simulated_today": "2026-06-29", "simulated_datetime": "2026-06-29 15:00:00",
            }, SID)
            items = json.loads(r["result"]["content"][0]["text"])["items"]
            for rec in items:
                vix_frames.append(rec)
        except Exception as e:
            print(f"vix {y0} err {e}")
    x = pd.DataFrame(vix_frames)
    x["date"] = pd.to_datetime(x["交易日期"]).dt.normalize()
    x = x.set_index("date").sort_index()
    x["ivix"] = pd.to_numeric(x["ivix"], errors="coerce")
    x = x[["ivix"]]
    x = x[~x.index.duplicated(keep="last")]

    # merge
    df = q.copy()
    df = df.join(v, how="left")
    df = df.join(s, how="left")
    df = df.join(x, how="left")
    df["pe"] = df["pe"].ffill(limit=30)
    df["pe_pctl"] = df["pe_pctl"].ffill(limit=30)
    df["risk_score"] = df["risk_score"].ffill(limit=5)
    df["sector_mom"] = df["sector_mom"].ffill(limit=5)
    df["ivix"] = df["ivix"].ffill(limit=5)

    df.to_pickle(CACHE)
    return df


# ---------- 信号构造 ----------

def sig_buyhold(df):
    return pd.Series(1.0, index=df.index)

def sig_mom20(df, thr=0.0):
    r20 = df["close"].pct_change(20)
    return (r20 > thr).astype(float)

def sig_mom20_and_risk(df, risk_thr=0.5):
    """mom_20>0 AND 板块风险分>阈值 → 满仓；否则空"""
    r20 = df["close"].pct_change(20)
    s = ((r20 > 0) & (df["risk_score"] > risk_thr)).astype(float)
    return s

def sig_mom20_or_risk(df, risk_thr=0.7):
    """mom_20>0 OR 板块风险分>阈值 → 满仓"""
    r20 = df["close"].pct_change(20)
    s = ((r20 > 0) | (df["risk_score"] > risk_thr)).astype(float)
    return s

def sig_mom20_gated_risk(df, risk_thr=0.5):
    """mom_20 决定方向，风险分只用来 veto 极端弱势（<阈值→强制空仓）"""
    r20 = df["close"].pct_change(20)
    s = (r20 > 0).astype(float)
    s = s.where(df["risk_score"].fillna(1.0) > risk_thr, 0.0)
    return s

def sig_mom20_plus_vixspike(df, vix_pctl_hi=0.95, vix_win=252*3, hold=20):
    """mom_20 承重 + VIX 极高分位（>95% 分位）时逆向加仓开关：VIX 高分位后 hold 日强制多头。"""
    r20 = df["close"].pct_change(20)
    base = (r20 > 0).astype(float)
    vix = df["ivix"]
    def _pct_now(a):
        # a 是滚动窗口的 numpy array；如果全 NaN 返回 nan
        if len(a) < 2: return np.nan
        cur = a[-1]
        if np.isnan(cur): return np.nan
        past = a[:-1]
        past = past[~np.isnan(past)]
        if len(past) == 0: return np.nan
        return float((cur > past).mean())
    pctl = vix.rolling(vix_win, min_periods=252).apply(_pct_now, raw=True)
    trig = (pctl >= vix_pctl_hi)
    force = pd.Series(0.0, index=df.index)
    idxs = np.where(trig.fillna(False).to_numpy())[0]
    for i in idxs:
        j = min(i + hold, len(df))
        force.iloc[i:j] = 1.0
    return pd.Series(np.maximum(base.values, force.values), index=df.index)

def sig_pepctl_baseline(df, lo=10.0, hi=30.0, baseline=0.3):
    """PE 分位<lo → baseline 底仓; mom_20>0 满仓; PE 分位>hi 时 mom_20 说话（不给底仓）。"""
    r20 = df["close"].pct_change(20)
    s = pd.Series(0.0, index=df.index)
    s = s.where(~(r20 > 0), 1.0)
    # 深熊底仓（PE 分位<lo）
    pe = df["pe_pctl"]
    deep = pe < lo
    s = s.where(~(deep & (r20 <= 0)), baseline)
    return s

def sig_mom20_x_sector_mom(df, sector_thr=0.0):
    """mom_20 与板块动量同号才在场"""
    r20 = df["close"].pct_change(20)
    s = ((r20 > 0) & (df["sector_mom"] > sector_thr)).astype(float)
    return s


# ---------- 回测 ----------

COST = 5e-4  # 5bp 单边

def backtest(df, sig, start):
    idx = df.index
    r = df["close"].pct_change().fillna(0)
    pos = sig.clip(0, 1).reindex(idx).fillna(0).shift(1).fillna(0)  # T+1
    turn = pos.diff().abs().fillna(0)
    net = pos * r - turn * COST
    m = idx >= pd.Timestamp(start)
    eq = (1 + net.loc[m]).cumprod()
    if len(eq) == 0:
        return None
    ann_days = 252
    n = len(eq)
    total = eq.iloc[-1] - 1
    ann_ret = eq.iloc[-1] ** (ann_days / n) - 1
    dd = (eq / eq.cummax() - 1).min()
    cal = ann_ret / abs(dd) if dd < 0 else 0
    sharpe = net.loc[m].mean() / net.loc[m].std() * math.sqrt(ann_days) if net.loc[m].std() > 0 else 0
    flips = int((pos.diff().abs() > 0).loc[m].sum())
    exp = float(pos.loc[m].mean())
    return {"equity": eq, "total": total, "ann_ret": ann_ret, "dd": dd, "cal": cal, "sharpe": sharpe, "flips": flips, "exposure": exp}


def val_stats(eq):
    ci = bootstrap_sharpe_ci(eq, n_bootstrap=600, confidence=0.95)
    wf = walk_forward_analysis(eq, [], n_windows=5)
    shs = [w.get("sharpe") for w in (wf.get("windows") or wf.get("per_window") or [])]
    pos_windows = sum(1 for x in shs if x and x > 0)
    return {
        "ci_lower": float(ci.get("ci_lower", 0)),
        "ci_upper": float(ci.get("ci_upper", 0)),
        "prob_positive": float(ci.get("prob_positive", 0)),
        "wf_positive": pos_windows,
    }


def grade(ci_lower, wf_pos):
    if ci_lower > 0 and wf_pos >= 4:
        return "✅绿灯"
    if wf_pos >= 4:
        return "🟢近绿"
    return "🟡黄灯"


def main():
    df = fetch_all()
    print(f"data rows={len(df)}, from={df.index[0].date()} to={df.index[-1].date()}")
    print(f"  risk_score notna={df['risk_score'].notna().sum()}  pe_pctl notna={df['pe_pctl'].notna().sum()}  ivix notna={df['ivix'].notna().sum()}")
    # 组合信号从 2015-01-01 起（辅助数据都有值），IS 到 2021-12-31，OOS 2022-01-01→末
    START = "2015-01-01"
    IS_END = "2021-12-31"
    OOS_START = "2022-01-01"
    df = df.loc[START:].copy()

    # 只保留辅助数据都有值的日子做组合（否则 shift 前的 signal 会退化）
    df_full = df.dropna(subset=["risk_score", "ivix", "pe_pctl", "sector_mom"])
    print(f"full-support window rows={len(df_full)}  {df_full.index[0].date()} → {df_full.index[-1].date()}")

    # 参考基线 mom_20 单因子（用 full window 便于横比）
    signals = {
        "buyhold": sig_buyhold,
        "mom_20 (baseline)": sig_mom20,
        "mom_20 AND risk>0.5": lambda d: sig_mom20_and_risk(d, 0.5),
        "mom_20 AND risk>0.7": lambda d: sig_mom20_and_risk(d, 0.7),
        "mom_20 OR risk>0.7": lambda d: sig_mom20_or_risk(d, 0.7),
        "mom_20 gated risk<0.3=0": lambda d: sig_mom20_gated_risk(d, 0.3),
        "mom_20 gated risk<0.5=0": lambda d: sig_mom20_gated_risk(d, 0.5),
        "mom_20 + VIX>95% hold20": lambda d: sig_mom20_plus_vixspike(d, 0.95, 252*3, 20),
        "mom_20 + VIX>90% hold20": lambda d: sig_mom20_plus_vixspike(d, 0.90, 252*3, 20),
        "PE_pctl<10 baseline0.3": lambda d: sig_pepctl_baseline(d, 10, 30, 0.3),
        "PE_pctl<10 baseline0.5": lambda d: sig_pepctl_baseline(d, 10, 30, 0.5),
        "mom_20 AND sector_mom>0": lambda d: sig_mom20_x_sector_mom(d, 0.0),
    }

    rows = []
    for name, fn in signals.items():
        try:
            sig = fn(df_full)
            m_is = backtest(df_full.loc[:IS_END], sig.loc[:IS_END], START)
            m_os = backtest(df_full.loc[OOS_START:], sig.loc[OOS_START:], OOS_START)
            if m_is is None or m_os is None:
                continue
            # validation 用全 window equity（贯通 IS+OOS 更稳）
            m_full = backtest(df_full, sig, START)
            v = val_stats(m_full["equity"])
            rows.append({
                "name": name,
                "is_cal": m_is["cal"], "is_dd": m_is["dd"], "is_ann": m_is["ann_ret"],
                "os_cal": m_os["cal"], "os_dd": m_os["dd"], "os_ann": m_os["ann_ret"], "os_shp": m_os["sharpe"],
                "flips_os": m_os["flips"], "exp_os": m_os["exposure"],
                "ci_lower": v["ci_lower"], "ci_upper": v["ci_upper"], "prob_pos": v["prob_positive"], "wf_pos": v["wf_positive"],
                "verdict": grade(v["ci_lower"], v["wf_positive"]),
            })
        except Exception as e:
            print(f"{name}: err {e}")

    bh_is = next((r for r in rows if r["name"] == "buyhold"), None)
    bh_os_cal = bh_is["os_cal"] if bh_is else 0
    is_cal_bh = bh_is["is_cal"] if bh_is else 0

    # 输出
    print(f"\nBH: IS_cal={is_cal_bh:.2f}  OOS_cal={bh_os_cal:.2f}  OOS_dd={bh_is['os_dd']*100:.0f}%")
    print(f"{'signal':32} {'IS_cal':>7} {'OS_cal':>7} {'ΔOS':>6} {'OSdd%':>7} {'exp':>5} {'flip':>5} {'CI[lo,hi]':>20} {'wf':>4} verdict")
    rows.sort(key=lambda r: -(r["os_cal"] - bh_os_cal) if r["name"] != "buyhold" else 999)
    for r in rows:
        d_os = r["os_cal"] - bh_os_cal
        print(f"{r['name']:32} {r['is_cal']:+7.2f} {r['os_cal']:+7.2f} {d_os:+6.2f} {r['os_dd']*100:+7.1f} {r['exp_os']:5.2f} {r['flips_os']:5d} [{r['ci_lower']:+.2f},{r['ci_upper']:+.2f}] {r['wf_pos']:>3}/5 {r['verdict']}")

    with open(os.path.join(OUT_DIR, "summary.json"), "w") as f:
        json.dump({"rows": rows, "bh_os_cal": bh_os_cal, "bh_is_cal": is_cal_bh,
                   "sample_start": df_full.index[0].strftime("%Y-%m-%d"),
                   "sample_end": df_full.index[-1].strftime("%Y-%m-%d"),
                   "is_end": IS_END, "oos_start": OOS_START}, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nsaved -> {os.path.join(OUT_DIR, 'summary.json')}")


if __name__ == "__main__":
    main()
