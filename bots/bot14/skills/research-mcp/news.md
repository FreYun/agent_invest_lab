# 新闻与研报工具详细参数

## 搜索

### `news_search` — 新闻搜索（语义匹配）
- `query*`: 搜索关键词，如 "黄金 价格"、"A股 市场"
- `top_k`: 返回条数，默认 3
- `search_day_ago`: 搜索多少天内的新闻
- 返回：[文章ID, 摘要文本, 相关度评分]

### `research_search` — 研报搜索（语义匹配）
- `query*`: 如 "半导体 行业"、"消费 复苏"
- `top_k`: 返回条数，默认 3
- `search_day_ago`: 搜索多少天内的研报
- 返回：[研报ID, 研报全文/摘要, 相关度评分]

### `search_rerank` — 底层搜索（一般不需要直接用）
`news_search` 和 `research_search` 是它的简化包装。直接用上面两个即可。
- `query*`, `search_type`("news"/"research"), `top_k`, `search_day_ago`, `score_threshold`, `fields`

## 实体识别

### `query_segment` — 从文本提取股票/基金实体
- `user_inputs*`: 用户输入文本，如 "帮我查一下贵州茅台和宁德时代的持仓"
- 返回：识别出的股票代码、基金代码、基金经理、主题等

适合在处理用户自然语言请求时，先用此工具提取结构化实体，再调用对应数据工具。
