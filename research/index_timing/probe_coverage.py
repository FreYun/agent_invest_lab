import sys,json,urllib.request
sys.path.insert(0,"/home/ubuntu/rooot/.local/share/uv/tools/vibe-trading-ai/lib/python3.11/site-packages")
URL="http://127.0.0.1:18078/mcp";H={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
def call(m,p,sid=None):
    h=dict(H)
    if sid:h["Mcp-Session-Id"]=sid
    r=urllib.request.urlopen(urllib.request.Request(URL,data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),headers=h),timeout=90)
    sid=r.headers.get("Mcp-Session-Id",sid);b=r.read().decode()
    for ln in b.splitlines():
        if ln.startswith("data:"):return json.loads(ln[5:].strip()),sid
    return json.loads(b),sid
_,sid=call("initialize",{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"cov","version":"1"}})
# 用户权威清单 name -> 候选代码（多后缀，服务端只回可用的）
NAMES=[
 ("中证A500",["000510.CSI","000510.SH"]),("沪深300",["000300.SH"]),("上证50",["000016.SH"]),
 ("中证500",["000905.SH"]),("中证1000",["000852.SH"]),("北证50",["899050.BJ","899050.CSI"]),
 ("创业板指",["399006.SZ"]),("科创50",["000688.SH"]),("科创创业50",["931643.CSI"]),
 ("中国互联网50",["H30533.CSI"]),("港股通创新药",["987018.CSI","987018.HK"]),
 ("通信设备",["931160.CSI"]),("通信技术",["931144.CSI"]),("创业板人工智能",["970070.CNI","970070.SZ"]),
 ("CS人工智",["930713.CSI"]),("科创芯片",["000685.SH","000685.CSI"]),("国证芯片",["980017.SZ","980017.CNI"]),
 ("中证半导",["931865.CSI"]),("半导体材料设备",["931743.CSI"]),("云计算",["930851.CSI"]),
 ("动漫游戏",["930901.CSI"]),("中证军工",["399967.SZ"]),("卫星通信",["980018.SZ","980018.CNI"]),
 ("机器人",["H30590.CSI"]),("CS电池",["931719.CSI"]),("CS新能车",["399976.SZ","399976.CSI"]),
 ("绿色电力",["931897.CSI"]),("电力指数",["H30199.CSI"]),("光伏产业",["931151.CSI"]),
 ("电网设备主题",["931994.CSI"]),("细分化工",["000813.SH","000813.CSI"]),("稀土产业",["930598.CSI"]),
 ("工业有色",["H11059.CSI"]),("CS稀金属",["930632.CSI"]),("中证酒",["399987.SZ"]),
 ("800消费",["000932.SH","000932.CSI"]),("生物医药",["399441.SZ"]),("中证医疗",["399989.SZ"]),
 ("保险主题",["399809.SZ"]),("中证银行",["399986.SZ"]),("证券公司",["399975.SZ"]),
 ("自由现金流",["980092.CNI","980092.SZ"]),("中证红利",["000922.SH","000922.CSI"]),("红利低波",["H30269.CSI"]),
 ("SSH黄金股票",["931238.CSI"]),
]
OVERSEAS=[("恒生科技","HSTECH"),("恒生指数","HSI"),("标普500","SPX"),("纳斯达克100","NDX100"),("黄金9999","AU9999"),("标普A股红利低波50","SPCLLHCP")]
allsyms=[s for _,v in NAMES for s in v]
res,_=call("tools/call",{"name":"market_index_quote","arguments":{"market":"cn","symbols":allsyms,"start_date":"2014-01-01","end_date":"2026-06-05","simulated_datetime":"2026-06-05 15:00:00"}},sid)
items={it.get("指数标识"):it for it in json.loads(res["result"]["content"][0]["text"]).get("items",[])}
ok=[];bad=[]
print("=== A股指数 simworld 可用性（用户权威清单）===")
for nm,syms in NAMES:
    hit=None
    for s in syms:
        it=items.get(s)
        if it and it.get("是否可用") and it.get("行情记录"):
            rec=it["行情记录"];hit=(s,len(rec),rec[0]["日期"],rec[-1]["日期"]);break
    if hit: ok.append(nm);print("  ✓ %-16s %-12s rows=%-5d %s..%s"%(nm,hit[0],hit[1],hit[2],hit[3]))
    else:   bad.append(nm);print("  ✗ %-16s 不可用（候选:%s）"%(nm,",".join(syms)))
print("\n=== 海外/黄金（market=cn 通常不覆盖，需另验 market=hk/us 或专用工具）===")
for nm,s in OVERSEAS: print("  ?  %-16s %s"%(nm,s))
print("\n小计：A股可用 %d / %d，不可用 %d：%s"%(len(ok),len(NAMES),len(bad),"、".join(bad)))
