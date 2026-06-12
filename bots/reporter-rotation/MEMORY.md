# 记忆

（reporter-rotation 的跨期记忆。组合是**惯性持有、低换手**的——核心/卫星集合与各自的"连续天数计数器"需要跨期续接。每轮开工请先 `get_market_report(report_type="mainline_rotation")` 读上一期组合与计数器作为基准，再据当日数据推进，不要从零起算。）
