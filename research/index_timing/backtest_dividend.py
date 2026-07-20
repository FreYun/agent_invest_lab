"""中证红利四信号方法论(strategies/index-products/dividend.md)确定性回测。

严格按方法论字面规则:
  信号1 买入: 修正乖离业务分位<=0.05 → +20%;冷却带>0.10;
             首程(pos==0)无门槛;堆仓(目标仓位>50%)需绝对面闸(近5日未创30日新低+20日涨幅>=0)
  信号2 卖出: 拥挤业务分位>=0.90 → -20%;冷却带<0.80
  信号3 清空: 持仓期超额(922累计-985累计)>=+10% → 全清
  信号4 减档: 账户MDD触红线(-6%) 或 绝对价近5日创30日新低+浮亏扩大 → -20%;
             触发后锁信号1,直到收盘站回MA20
  优先级 3>4>2>1;样本数<150 只参考不触发;数据缺失保持不动。
执行: T收盘判定信号,T+1收盘价调仓(无未来函数);--t0 用T收盘成交做敏感性。
起始仓位: 主版本50%中枢;--coldstart 模拟bot实跑0仓等信号。
"""
import csv, sys, math
from pathlib import Path

DATA = Path(__file__).parent / "data" / "dividend_bt"
BT_START = "2024-01-01"
RED_LINE = -0.06          # bot3 USER.md 回撤红线
STEP = 0.2

def load_quote(name):
    out = {}
    for r in csv.DictReader((DATA / name).open()):
        d = r["日期"][:10]
        out[d] = float(r["收盘"])
    return out

def load_factor(name, qkey):
    out = {}
    for r in csv.DictReader((DATA / name).open()):
        if not r.get(qkey):
            continue
        try:
            out[r["date"]] = (float(r[qkey]), int(float(r.get("样本数") or 0)))
        except ValueError:
            continue
    return out

def run(coldstart=False, t_exec_lag=1, red_line=RED_LINE, verbose=True):
    px922 = load_quote("idx_000922.csv")
    px985 = load_quote("idx_000985.csv")
    dev = load_factor("corrected_deviation.csv", "修正乖离业务分位_由低至高")
    cong = load_factor("congestion.csv", "拥挤业务分位_由低至高")

    days = sorted(d for d in px922 if d in px985)
    bt_days = [d for d in days if d >= BT_START]
    idx0 = days.index(bt_days[0])

    pos = 0.0 if coldstart else 0.5
    nav, peak = 1.0, 1.0
    sig1_armed, sig2_armed = True, True
    sig4_lock = False
    last_s4_mdd, last_s4_low = None, None   # 信号4去重:要更深的回撤/更低的新低才再触发
    t0_i = None if coldstart else idx0      # 持仓周期起点(days 下标)
    cost = None if coldstart else px922[days[idx0]]
    pending = None                          # (target_pos, tag) 待执行
    log_rows, navs = [], []
    trig_count = {1: 0, 2: 0, 3: 0, 4: 0}

    for i in range(idx0, len(days)):
        d = days[i]
        c = px922[d]
        # --- 当日收益按昨日收盘时仓位计 ---
        if i > idx0:
            ret = c / px922[days[i - 1]] - 1
            nav *= 1 + pos * ret
        # --- 执行昨日信号(T+1收盘) ---
        if pending is not None and t_exec_lag == 1:
            new_pos, tag = pending
            if new_pos > pos and pos == 0:
                t0_i, cost = i, c
            elif new_pos > pos:
                add = new_pos - pos
                cost = (cost * pos + c * add) / new_pos
            if new_pos == 0:
                t0_i, cost = None, None
            if verbose:
                log_rows.append((d, tag, f"{pos:.0%}->{new_pos:.0%}", f"close={c:.0f}"))
            pos = new_pos
            pending = None
        peak = max(peak, nav)
        mdd_now = nav / peak - 1
        navs.append((d, nav, pos))

        # --- 收盘判信号 ---
        dq = dev.get(d)
        cq = cong.get(d)
        # 冷却带重臂
        if dq and dq[0] > 0.10:
            sig1_armed = True
        if cq and cq[0] < 0.80:
            sig2_armed = True
        # 信号4解锁: 站回MA20
        hist = [px922[days[j]] for j in range(max(0, i - 29), i + 1)]
        ma20 = sum(hist[-20:]) / min(20, len(hist))
        if sig4_lock and c > ma20:
            sig4_lock = False
        # 绝对面
        low5 = min(hist[-5:])
        low_prev = min(hist[:-5]) if len(hist) > 5 else low5
        new_low5 = low5 < low_prev                      # 近5日创30日新低
        falling = c < hist[-6] if len(hist) >= 6 else False
        chg20 = c / hist[-21] - 1 if len(hist) >= 21 else 0.0

        action = None
        # 信号3: 超额>=10% 清仓
        if pos > 0 and t0_i is not None:
            ex = (c / px922[days[t0_i]] - 1) - (px985[d] / px985[days[t0_i]] - 1)
            if ex >= 0.10:
                action = (0.0, f"S3清空(超额{ex:+.1%})")
                trig_count[3] += 1
        # 信号4: 保命减档
        if action is None and pos > 0:
            hit_line = mdd_now <= red_line and (last_s4_mdd is None or mdd_now < last_s4_mdd - 1e-9)
            floating_loss = cost is not None and c < cost
            hit_abs = new_low5 and falling and floating_loss and (last_s4_low is None or low5 < last_s4_low - 1e-9)
            if hit_line or hit_abs:
                action = (max(0.0, round(pos - STEP, 2)),
                          f"S4减档({'红线MDD%.1f%%' % (mdd_now*100) if hit_line else '绝对价新低'})")
                trig_count[4] += 1
                sig4_lock = True
                if hit_line: last_s4_mdd = mdd_now
                if hit_abs: last_s4_low = low5
        # 信号2: 拥挤减档
        if action is None and cq and cq[1] >= 150 and cq[0] >= 0.90 and sig2_armed and pos > 0:
            action = (max(0.0, round(pos - STEP, 2)), f"S2拥挤减(q={cq[0]:.2f})")
            trig_count[2] += 1
            sig2_armed = False
        # 信号1: 便宜加档
        if action is None and dq and dq[1] >= 150 and dq[0] <= 0.05 and sig1_armed and not sig4_lock and pos < 1.0:
            target = min(1.0, round(pos + STEP, 2))
            ok = True
            if target > 0.5 and pos > 0:                # 堆仓过绝对面闸
                ok = (not new_low5) and chg20 >= 0
            if ok:
                action = (target, f"S1加仓(q={dq[0]:.3f}{',堆仓' if target > 0.5 and pos > 0 else ',首程' if pos == 0 else ''})")
                trig_count[1] += 1
                sig1_armed = False
        if action:
            if t_exec_lag == 0:   # T收盘立即执行
                new_pos, tag = action
                if new_pos > pos and pos == 0:
                    t0_i, cost = i, c
                elif new_pos > pos:
                    add = new_pos - pos
                    cost = (cost * pos + c * add) / new_pos
                if new_pos == 0:
                    t0_i, cost = None, None
                if verbose:
                    log_rows.append((d, tag, f"{pos:.0%}->{new_pos:.0%}", f"close={c:.0f}"))
                pos = new_pos
            else:
                pending = action

    # 指标
    n_years = len(bt_days) / 243
    total = nav - 1
    ann = nav ** (1 / n_years) - 1
    peak2, mdd = 1.0, 0.0
    for _, v, _ in navs:
        peak2 = max(peak2, v)
        mdd = min(mdd, v / peak2 - 1)
    # 基准
    b0, e0 = px922[bt_days[0]], px922[bt_days[-1]]
    bh = e0 / b0
    # buyhold MDD
    pk, bmdd = 0, 0
    for d in bt_days:
        pk = max(pk, px922[d])
        bmdd = min(bmdd, px922[d] / pk - 1)
    half_nav, pk3, hmdd = 1.0, 1.0, 0.0
    prev = None
    for d in bt_days:
        if prev:
            half_nav *= 1 + 0.5 * (px922[d] / prev - 1)
        pk3 = max(pk3, half_nav)
        hmdd = min(hmdd, half_nav / pk3 - 1)
        prev = px922[d]
    return dict(nav=nav, total=total, ann=ann, mdd=mdd, trig=trig_count, log=log_rows, navs=navs,
                bh_total=bh - 1, bh_ann=bh ** (1 / n_years) - 1, bh_mdd=bmdd,
                half_total=half_nav - 1, half_ann=half_nav ** (1 / n_years) - 1, half_mdd=hmdd,
                n_days=len(bt_days))

if __name__ == "__main__":
    cold = "--coldstart" in sys.argv
    lag = 0 if "--t0" in sys.argv else 1
    r = run(coldstart=cold, t_exec_lag=lag)
    print(f"=== 红利四信号回测 {BT_START}~ ({r['n_days']}交易日, {'0仓冷启动' if cold else '50%中枢起步'}, T+{lag}执行) ===")
    print(f"策略:   总收益 {r['total']:+.2%}  年化 {r['ann']:+.2%}  MDD {r['mdd']:.2%}")
    print(f"BuyHold: 总收益 {r['bh_total']:+.2%}  年化 {r['bh_ann']:+.2%}  MDD {r['bh_mdd']:.2%}")
    print(f"恒定50%: 总收益 {r['half_total']:+.2%}  年化 {r['half_ann']:+.2%}  MDD {r['half_mdd']:.2%}")
    print(f"触发次数: S1={r['trig'][1]} S2={r['trig'][2]} S3={r['trig'][3]} S4={r['trig'][4]}")
    print("\n--- 调仓日志 ---")
    for row in r["log"]:
        print(" ", *row)
