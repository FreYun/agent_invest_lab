"""sw_industry_sync 单测: fetch 仅留当前在册 + upsert 幂等. 全程临时库, 不触网不打生产库."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sw_industry_sync as s


def _fake_call(name, offset=0, limit=3000, **kw):
    assert name == "index_member_all"
    page1 = {"has_more": True, "rows": [
        {"ts_code": "600000.SH", "l1_code": "801780.SI", "l1_name": "银行",
         "l2_code": "801781.SI", "l2_name": "股份制银行", "out_date": None},
        {"ts_code": "000001.SZ", "l1_code": "801780.SI", "l1_name": "银行",
         "l2_code": "801781.SI", "l2_name": "股份制银行", "out_date": "20200101"},  # 已调出, 应剔除
    ]}
    page2 = {"has_more": False, "rows": [
        {"ts_code": "300750.SZ", "l1_code": "801730.SI", "l1_name": "电力设备",
         "l2_code": "801737.SI", "l2_name": "电池", "out_date": None},
    ]}
    return page1 if offset == 0 else page2


def test_fetch_filters_out_dated():
    rows = s.fetch_current_members(call=_fake_call)
    codes = {r[0] for r in rows}
    assert codes == {"600000.SH", "300750.SZ"}      # 000001.SZ (out_date 非空) 被剔除
    bank = [r for r in rows if r[0] == "600000.SH"][0]
    assert bank == ("600000.SH", "801780.SI", "银行", "801781.SI", "股份制银行")


def test_upsert_idempotent_and_updates(tmp_path):
    db = str(tmp_path / "m.db")
    c = sqlite3.connect(db)
    try:
        n1 = s.upsert_members(c, [("600000.SH", "801780.SI", "银行", "801781.SI", "股份制银行")])
        assert n1 == 1
        # 再次 upsert 同一 ts_code, 改 L1 名 -> 写入行数仍为 1(含 upsert), 物理行数不增, 值更新
        n2 = s.upsert_members(c, [("600000.SH", "801780.SI", "银行Ⅱ", "801781.SI", "股份制银行")])
        assert n2 == 1  # 返回写入(含 upsert)行数, 非插入增量
        rows = c.execute("SELECT ts_code, l1_name FROM sw_industry_member").fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "银行Ⅱ"
    finally:
        c.close()
