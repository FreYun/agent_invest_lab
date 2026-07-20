#!/usr/bin/env python3
"""组装「场外 C 份额」候选宇宙目标清单（一次性脚本）。

目标：让 fund_db 覆盖当前候选宇宙里每个跟踪指数的「场外 C 载体」。
两个来源，均以【跟踪指数代码】为最终核验键：
  ① A→C：fund_info 里场外 A，按东财全量表「名字去 A 加 C」精确匹配出 C 代码
          （同基金 A/C 必跟同指数，无需再核验）。
  ② ETF→联接C：当前 fund_db 里场内 ETF（代码 5/159/56）的跟踪指数，若 fund_db 尚无
          场外 C 覆盖 → 在东财表按 ETF 主题词召回候选场外 C，再用 ttjj basic-info
          核验候选的「跟踪指数代码」== 该 ETF 的指数码，命中才采纳。

输出：
  /tmp/otc_c_to_ingest.json   待 ingest 的新场外 C 代码清单（去重、剔除已在库）
  /tmp/otc_c_index_gap.json   去掉 ETF 后仍无场外 C 载体的指数缺口清单
  并打印汇总。

用法：python3.12 scripts/assemble_otc_c_universe.py
"""
from __future__ import annotations
import json, re, sqlite3, time
import requests

DB = "/home/rooot/agent_invest_lab/data/fund.db"
EM_TABLE = "/tmp/fundcode_search.js"
BASIC_INFO = "http://ttjj-data-api.jijinmima.cn/api/fund/basic-info"

def is_etf(code: str) -> bool:
    return code.startswith("5") or code.startswith("159") or code.startswith("56")

def load_em():
    txt = open(EM_TABLE, encoding="utf-8").read()
    m = re.search(r"=\s*(\[.*\]);?\s*$", txt.strip(), re.S)
    arr = json.loads(m.group(1))
    by_name = {}
    for x in arr:
        by_name.setdefault(x[2], x[0])          # 名称 -> 代码（首个）
    return arr, by_name

def basic_info_batch(codes):
    """ttjj basic-info：{code: {跟踪指数代码, 基金名称, 基金规模_亿元}}。分块 50，串行。"""
    out = {}
    for i in range(0, len(codes), 50):
        batch = codes[i:i+50]
        try:
            r = requests.post(BASIC_INFO, json={"fund_codes": batch}, timeout=60)
            r.raise_for_status()
            for it in (r.json().get("items") or []):
                c = str(it.get("基金代码") or "").strip()
                if c:
                    out[c] = {"idx": it.get("跟踪指数代码"),
                              "name": it.get("基金名称"),
                              "scale": it.get("基金规模_亿元") or 0}
        except Exception as e:
            print(f"  basic-info 批失败 {batch[0]}..: {e}")
        time.sleep(0.1)
    return out

def main():
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT fund_code, fund_name, track_index_code, track_index_name FROM fund_info").fetchall()
    existing = set(c for c, *_ in rows)
    arr, by_name = load_em()

    # 现有「场外C 载体」覆盖的指数集合。
    # 判据 = 名字尾 'C'（场外联接C 与 LOF-C 都算；纯场内ETF 永不以 C 结尾）——
    # 比按代码前缀更准，避免把 (LOF)C 误判成场内ETF。
    otc_c_idx_have = set()
    for c, n, ic, _ in rows:
        if ic and n and n.endswith("C"):
            otc_c_idx_have.add(ic)

    # ── 来源①：A→C 精确名匹配（A 份额按名字尾，含 LOF-A）──
    a2c = []          # (Acode, Ccode, Cname)
    for c, n, ic, _ in rows:
        if not n or not n.endswith("A"):
            continue
        cname = n[:-1] + "C"
        ccode = by_name.get(cname)
        if ccode and ccode not in existing:
            a2c.append((c, ccode, cname))

    # ── 来源②：纯场内ETF→联接C（按指数码核验）──
    # 纯场内ETF = 名字不以 A/C 结尾（单一份额）；只处理 fund_db 尚无场外C覆盖的指数
    etf_idx = {}      # index_code -> 代表ETF名
    for c, n, ic, iname in rows:
        if ic and n and not n.endswith("A") and not n.endswith("C") and ic not in otc_c_idx_have:
            etf_idx.setdefault(ic, n or iname or "")
    print(f"场内ETF 指数 {len(etf_idx)} 个尚无场外C覆盖，召回候选...")

    # 召回：东财表里场外(非5/159/56) 名字含主题词且含'联接'且尾C 的基金
    def keyword(etf_name: str) -> str:
        return etf_name.split("ETF")[0].strip() if etf_name else ""
    cand_codes = set()
    cand_for_idx = {}     # index_code -> [候选code...]
    for ic, en in etf_idx.items():
        kw = keyword(en)
        if not kw:
            continue
        hits = [x[0] for x in arr
                if (not is_etf(x[0])) and x[2].endswith("C") and "联接" in x[2] and kw in x[2]]
        if not hits:   # 放宽：去掉"联接"要求
            hits = [x[0] for x in arr
                    if (not is_etf(x[0])) and x[2].endswith("C") and kw in x[2]]
        cand_for_idx[ic] = hits
        cand_codes.update(hits)
    cand_codes -= existing
    print(f"召回候选场外C {len(cand_codes)} 只，basic-info 核验跟踪指数码...")
    info = basic_info_batch(sorted(cand_codes))

    # 核验：候选的指数码 == 该ETF指数码 才采纳；每个指数挑规模最大的一只
    etf2c = []        # (index_code, Ccode, Cname)
    gap = []          # 无场外C载体的指数
    for ic, en in etf_idx.items():
        matched = [(cc, info[cc]) for cc in cand_for_idx.get(ic, [])
                   if cc in info and info[cc]["idx"] == ic]
        if matched:
            best = max(matched, key=lambda kv: float(kv[1]["scale"] or 0))
            etf2c.append((ic, best[0], best[1]["name"]))
        else:
            gap.append({"index_code": ic, "etf_name": en})

    # ── 汇总待 ingest（去重）──
    to_ingest = {}
    for ac, cc, cn in a2c:
        to_ingest[cc] = {"code": cc, "name": cn, "source": "A->C", "from": ac}
    for ic, cc, cn in etf2c:
        if cc not in to_ingest:
            to_ingest[cc] = {"code": cc, "name": cn, "source": "ETF->联接C", "index": ic}
    to_ingest = {c: v for c, v in to_ingest.items() if c not in existing}

    json.dump(list(to_ingest.values()), open("/tmp/otc_c_to_ingest.json", "w"), ensure_ascii=False, indent=1)
    json.dump(gap, open("/tmp/otc_c_index_gap.json", "w"), ensure_ascii=False, indent=1)

    print("="*60)
    print(f"来源① A→C 待补:           {len(a2c)} 只")
    print(f"来源② ETF→联接C 待补:      {len(etf2c)} 只")
    print(f"去重后待 ingest 新场外C:   {len(to_ingest)} 只  → /tmp/otc_c_to_ingest.json")
    print(f"仍无场外C载体的指数(缺口): {len(gap)} 个  → /tmp/otc_c_index_gap.json")
    con.close()

if __name__ == "__main__":
    main()
