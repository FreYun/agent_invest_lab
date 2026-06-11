import sqlite3
con = sqlite3.connect('/home/ubuntu/rooot/agent_invest_lab/data/fund.db')
rows = con.execute("SELECT nav_date, nav FROM fund_nav WHERE fund_code='513100' AND nav IS NOT NULL ORDER BY nav_date").fetchall()
dates=[r[0] for r in rows]; nav=[r[1] for r in rows]; n=len(nav)
ma200=[None]*n; hi252=[None]*n; s=0.0
for i in range(n):
    s+=nav[i]
    if i>=200: s-=nav[i-200]
    if i>=199: ma200[i]=s/200
    hi252[i]=max(nav[max(0,i-251):i+1])
below=0; fired=[]
state=False
for i in range(n):
    if ma200[i] is None: continue
    below = below+1 if nav[i]<ma200[i] else 0
    slope_down = ma200[i-20] is not None and ma200[i]<ma200[i-20]
    dd = nav[i]/hi252[i]-1
    cond = below>=10 and slope_down and dd<=-0.18
    if cond and not state:
        fired.append((dates[i], round(dd*100,1)))
        state=True
    if nav[i]>ma200[i]:
        state=False
print("保险丝三条件首次全亮的日期（每轮一次）:")
for d,dd in fired: print(f"  {d}  回撤 {dd}%")
# 各熊市窗口的净值口径最大回撤
for (a,b,tag) in [("2015-06-01","2016-06-30","2015-16"),("2018-09-01","2019-03-31","2018Q4"),
                  ("2020-01-01","2020-12-31","2020covid"),("2021-11-01","2023-01-31","2022熊市"),
                  ("2025-01-01","2025-12-31","2025关税")]:
    idx=[i for i,d in enumerate(dates) if a<=d<=b]
    peak=0; mdd=0
    for i in idx:
        peak=max(peak,nav[i]); mdd=min(mdd,nav[i]/peak-1)
    print(f"{tag:12s} 净值口径最大回撤 {mdd:.1%}")
