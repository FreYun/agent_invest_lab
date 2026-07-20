#!/usr/bin/env python3
"""KYC 画像批量测试：从画像 JSON 生成 N 个 bot 克隆 + 一份批量 world config。

每个画像 → 一个独立 bot（bot_id=bot101t_<tag>，独立账户）。克隆自模板 bots/bot101t，
把该画像写进各自的 RISK_ASSIGNMENT.md（状态待建档）。bot 跑时第一步读 RISK_ASSIGNMENT、
调 update_my_user 据 KYC 推断风险等级写进 USER.md，再走投资流程定持仓。

所有 bot 列进一份 config，一个 run 一起跑 → 同 run_id、不同 bot_id 横向对比，
唯一变量=KYC 画像。bot_id 用中性名（k1/k2/...），不把风险倾向泄露给 bot。

用法：
  /usr/bin/python3.12 scripts/kyc_batch_setup.py \
      --profiles world/config/kyc/samples.json [--date 2026-06-23] [--clean]
然后按脚本打印的命令跑 world。
"""
from __future__ import annotations
import argparse, json, re, shutil, sys
from pathlib import Path

REPO = Path("/home/rooot/agent_invest_lab")
TEMPLATE_BOT = REPO / "bots" / "bot101t"          # 模板（已建好的单测分身）
BOTS_DIR = REPO / "bots"
BASE_CFG = REPO / "world" / "config" / "world-bot101t-test.yaml"
OUT_CFG = REPO / "world" / "config" / "world-bot101t-kyc-batch.yaml"
IDENTITY_FILES = ["IDENTITY.md", "TOOLS.md", "AGENTS.md", "USER.md"]


def render_risk_section(profile: dict) -> str:
    """USER.md「风险偏好」节内容（待建档 + KYC）。bot 开跑读到后调 update_my_user
    据 KYC 推断风险等级、把定式风险偏好写回本节、覆盖这块，再当日建仓。"""
    kyc = profile.get("KYC画像", {})
    return f"""## 风险偏好

待建档

> 以下是系统分配给本账户的用户 KYC 问卷结果。**开盘第一步**：调用 `update_my_user`，据下面的 KYC
> **自行推断风险等级（保守 / 中性 / 进取）**，把定式风险偏好写回本节、覆盖掉这块「待建档 + KYC」，
> 然后据此进行当日建仓与决策。风险等级由你从 KYC 推断，不在此直接给定。

- **本次投资金额**：{profile.get('本次投资金额', '未给定')}
- **本金变少时的反应**：{kyc.get('本金变少时的反应', '')}
- **收益回吐时的反应**：{kyc.get('收益回吐时的反应', '')}
- **画像摘要**：{kyc.get('画像摘要', '')}
"""


def write_user_md_risk(dest: Path, profile: dict) -> None:
    """把 dest/USER.md 的「## 风险偏好」节整段替换为含 KYC 的待建档版本（保留其余节）。"""
    p = dest / "USER.md"
    txt = p.read_text(encoding="utf-8")
    # 匹配 ## 风险偏好 ... 到下一个 ## 标题（或文末）
    new = re.sub(r"##\s*风险偏好.*?(?=\n##\s|\Z)", render_risk_section(profile).strip() + "\n\n", txt, count=1, flags=re.S)
    if new == txt:   # 模板没有该节（兜底）：插到文件头之后
        new = txt.rstrip() + "\n\n" + render_risk_section(profile)
    p.write_text(new, encoding="utf-8")


def make_clone(tag: str, label: str, profile: dict) -> str:
    bot_id = f"bot101t_{tag}"
    dest = BOTS_DIR / bot_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(TEMPLATE_BOT, dest)
    # 把模板里的 'bot101t' 令牌换成新 bot_id（account_id / 身份 / bot_id 引用）。
    # 'bot101t' 不会误伤 'bot101'（无 t），从干净模板复制故只替换一次。
    for fn in IDENTITY_FILES:
        p = dest / fn
        if p.exists():
            p.write_text(p.read_text(encoding="utf-8").replace("bot101t", bot_id), encoding="utf-8")
    # 把该画像的 KYC 写进 USER.md 的「风险偏好」节（待建档）——USER.md 是 research-loop 注入的
    # 标准文件，bot 开跑即读到 KYC，再调 update_my_user 落成定式风险偏好写回 USER.md。
    write_user_md_risk(dest, profile)
    return bot_id


def write_batch_config(bot_ids: list[str], *, replay_from: str | None, replay_to: str | None,
                       mode: str, concurrency: int, out_path: Path) -> None:
    import yaml
    def str_rep(dumper, data):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="'" if data.isdigit() else None)
    yaml.add_representer(str, str_rep, Dumper=yaml.SafeDumper)

    base = yaml.safe_load(BASE_CFG.read_text(encoding="utf-8"))
    buy = base["buyable_fund_codes"]
    model = base["bot_models"]["bot101t"]
    assign = {"strategy_id": "multi_equity_high", "buyable_fund_codes": buy}

    cfg = dict(base)
    cfg["bots"] = list(bot_ids)
    # KYC 现在走 USER.md（标准注入文件），不再需要 RISK_ASSIGNMENT.md
    if isinstance(cfg.get("shadow_include"), list):
        cfg["shadow_include"] = [x for x in cfg["shadow_include"] if x != "RISK_ASSIGNMENT.md"]
    if replay_from and replay_to:
        cfg["replay"] = {"from": replay_from, "to": replay_to}
    # 决策频率：trading_days（单日/逐日测试）或 weekly（周度回测，每周一决策）
    cfg["chat_step_mode"] = mode
    if mode == "weekly":
        cfg["chat_weekday"] = 1
        cfg.pop("chat_step_days", None)
    else:
        cfg["chat_step_days"] = 1
        cfg.pop("chat_weekday", None)
    cfg["concurrency"] = concurrency
    # 建档硬门多一步 update_my_user 调用 + 上游偶发重试，600s 易被掐 → 给余量。
    # 坑：weekly/首日决策日实际用的是 research_day_timeout（run.ts:999 useExtendedBudget，
    # 因 periodTradingDays>1），不是 per_bot_timeout → research_day_timeout 才是真正要 bump 的钮。
    cfg["per_bot_timeout_seconds"] = max(int(base.get("per_bot_timeout_seconds", 600) or 600), 900)
    cfg["research_day_timeout_seconds"] = max(int(base.get("research_day_timeout_seconds", 600) or 600), 1200)
    cfg["enable_user_self_edit"] = True
    cfg["bot_assignments"] = {b: dict(assign) for b in bot_ids}
    cfg["bot_models"] = {b: dict(model) for b in bot_ids}

    order = ["research_loop", "research_loop_rust_bin", "bots", "replay", "calendar", "concurrency",
             "per_bot_timeout_seconds", "research_day_every", "research_day_timeout_seconds",
             "chat_step_mode", "chat_step_days", "chat_weekday", "enable_user_self_edit", "shadow_include",
             "simworld_upstream_url", "fund_mcp_cli", "strategy_library_root",
             "bot_assignments", "bot_models", "simworld_tools", "buyable_fund_codes"]
    ordered = {k: cfg[k] for k in order if k in cfg}
    for k in cfg:
        ordered.setdefault(k, cfg[k])
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# KYC 测试配置（scripts/kyc_batch_setup.py 生成，mode={mode}）。每 bot 一个 KYC 画像，一个 run 横向对比。\n")
        yaml.dump(ordered, f, Dumper=yaml.SafeDumper, allow_unicode=True, sort_keys=False, width=200)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles", required=True, help="画像 JSON（{profiles:[{tag,label,profile}]}）")
    ap.add_argument("--date", default=None, help="单日测试交易日 YYYY-MM-DD（mode=trading_days 单日用）")
    ap.add_argument("--from", dest="from_date", default=None, help="回测窗口起 YYYY-MM-DD（与 --to 一起，做区间回测）")
    ap.add_argument("--to", dest="to_date", default=None, help="回测窗口末 YYYY-MM-DD")
    ap.add_argument("--mode", choices=["trading_days", "weekly"], default="trading_days", help="决策频率：trading_days 或 weekly（周度回测）")
    ap.add_argument("--concurrency", type=int, default=1, help="并发 bot 数（默认 1 顺序跑）")
    ap.add_argument("--out", default=None, help="输出 config 文件名（默认按 mode 命名）")
    ap.add_argument("--no-clones", action="store_true", help="只重生成 config，不动 bots/bot101t_* 克隆")
    ap.add_argument("--clean", action="store_true", help="先删掉所有已存在的 bots/bot101t_* 克隆")
    args = ap.parse_args()

    if args.clean:
        for d in BOTS_DIR.glob("bot101t_*"):
            if d.is_dir():
                shutil.rmtree(d)
                print(f"  删除旧克隆 {d.name}")

    # 回测窗口：优先 --from/--to；否则 --date 单日
    replay_from = args.from_date or args.date
    replay_to = args.to_date or args.date
    out_path = Path(args.out) if args.out else (
        BASE_CFG.parent / ("world-bot101t-kyc-weekly.yaml" if args.mode == "weekly" else "world-bot101t-kyc-batch.yaml"))
    if not out_path.is_absolute():
        out_path = BASE_CFG.parent / out_path

    data = json.loads(Path(args.profiles).read_text(encoding="utf-8"))
    profiles = data["profiles"]
    mapping, bot_ids = [], []
    for p in profiles:
        bot_id = f"bot101t_{p['tag']}"
        if not args.no_clones:
            make_clone(p["tag"], p["label"], p["profile"])
            print(f"  生成 {bot_id}  ({p['label']}, {p['profile'].get('本次投资金额','')})")
        bot_ids.append(bot_id)
        mapping.append((bot_id, p["label"], p["profile"].get("本次投资金额", "")))

    write_batch_config(bot_ids, replay_from=replay_from, replay_to=replay_to,
                       mode=args.mode, concurrency=args.concurrency, out_path=out_path)
    win = f"{replay_from}..{replay_to}" if replay_from else "base"
    run_id = f"kyc-{args.mode}-{(replay_from or 'base').replace('-', '')}"
    cfg_name = out_path.name
    print("\n" + "=" * 64)
    print(f"已生成 {len(bot_ids)} 个 bot + config：{out_path}（mode={args.mode}, 窗口={win}, concurrency={args.concurrency}）")
    print("bot_id → 画像映射：")
    for b, l, m in mapping:
        print(f"  {b}  =  {l}  ({m})")
    print("\n跑：")
    print(f"  cd world && npm start -- --config config/{cfg_name} --run-id {run_id} --world-dir runtime-test")
    print("\n对比（净值曲线/回撤/持仓，按 bot_id）：")
    print(f"""  sqlite3 -header -column data/fund.db "SELECT bot_id, trade_date, round(net_value,4) nav, round(cumulative_return_pct,2) ret_pct, round(equity_weight*100,1) eq_pct FROM fund_bot_daily_snapshots WHERE run_id='{run_id}' ORDER BY bot_id, trade_date;" """)
    print(f"""  # 各 bot 据 KYC 写的 USER.md：runtime-test/runs/{run_id}/users/<bot_id>.revisions.jsonl""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
