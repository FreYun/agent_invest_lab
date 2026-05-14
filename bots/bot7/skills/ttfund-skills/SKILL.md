---
name: ttfund-skills
description: 天天基金 Skills 集合 — 基金信息、基金经理、条件选基、持仓查询、黄金分析、投顾策略、指数详情、净值查询、模拟组合、自选查询等10个技能。
---

# 天天基金 Skills 集合

## 前提条件

- 已配置环境变量 `TTFUND_APIKEY`
- 网关地址：`https://skills.tiantianfunds.com/ai-smart-skill-service/openapi/skill/invoke`
- 请求方式：`POST`
- 鉴权 Header：`X-API-Key`

## 统一调用方式

```bash
curl --location 'https://skills.tiantianfunds.com/ai-smart-skill-service/openapi/skill/invoke' \
--header "X-API-Key: $TTFUND_APIKEY" \
--header 'Content-Type: application/json' \
--data '{
  "skill_id": "<SKILL_ID>",
  "_skill_version": "<VERSION>",
  ...其他参数
}'
```

## Skill 列表

| 序号 | 技能名称 | skill_id | version | 说明 |
|------|----------|----------|---------|------|
| 1 | 天天基金信息 | FUND_BASE_INFOS | 1.1.0 | 基金基础信息查询 |
| 2 | 基金经理查询 | FUND_MANAGER_INFO | 1.0.0 | 基金经理信息、在管产品、历史业绩 |
| 3 | 天天条件选基 | FUND_CONDITION_SELECT | 1.1.0 | 条件筛选基金 |
| 4 | 基金持仓查询 | FUND_HOLDING_INFO | 1.0.0 | 基金持仓、重仓股债、行业配置 |
| 5 | 天天黄金查询 | FUND_HUAAN_GOLD_INFO | 1.0.0 | 黄金行情、宏观、风险、资讯 |
| 6 | 投顾策略查询 | FUND_TG_STRATEGY_INFO | 1.0.0 | 投顾策略信息、业绩、风险 |
| 7 | 指数详情查询 | FUND_INDEX_INFO | 1.0.0 | 指数点位、估值、成分 |
| 8 | 基金净值查询 | FUND_NAV_INFO | 1.0.0 | 基金净值历史、累计净值 |
| 9 | 模拟组合管理 | MODEL_PORTFOLIO | 1.0.0 | 模拟组合列表/详情/持仓/交易 |
| 10 | 天天基金自选 | FUND_FAVOR_ZX | 1.0.0 | 用户自选基金列表 |

## 调用示例

### 1. 查询基金基础信息

```bash
curl --location 'https://skills.tiantianfunds.com/ai-smart-skill-service/openapi/skill/invoke' \
--header "X-API-Key: $TTFUND_APIKEY" \
--header 'Content-Type: application/json' \
--data '{
  "skill_id": "FUND_BASE_INFOS",
  "_skill_version": "1.1.0",
  "fcode": "000001"
}'
```

### 2. 查询基金经理

```bash
curl --location 'https://skills.tiantianfunds.com/ai-smart-skill-service/openapi/skill/invoke' \
--header "X-API-Key: $TTFUND_APIKEY" \
--header 'Content-Type: application/json' \
--data '{
  "skill_id": "FUND_MANAGER_INFO",
  "_skill_version": "1.0.0",
  "manager_name": "张坤"
}'
```

### 3. 条件选基（近1年收益率最高的5只基金）

```bash
curl --location 'https://skills.tiantianfunds.com/ai-smart-skill-service/openapi/skill/invoke' \
--header "X-API-Key: $TTFUND_APIKEY" \
--header 'Content-Type: application/json' \
--data '{
  "skill_id": "FUND_CONDITION_SELECT",
  "_skill_version": "1.1.0",
  "pageIndex": 1,
  "pageNum": 5,
  "pageType": 1,
  "orderField": "5_6_-1"
}'
```

### 4. 查询基金持仓

```bash
curl --location 'https://skills.tiantianfunds.com/ai-smart-skill-service/openapi/skill/invoke' \
--header "X-API-Key: $TTFUND_APIKEY" \
--header 'Content-Type: application/json' \
--data '{
  "skill_id": "FUND_HOLDING_INFO",
  "_skill_version": "1.0.0",
  "fund_id": "000001",
  "holding_type": "all"
}'
```

### 5. 查询黄金信息

```bash
curl --location 'https://skills.tiantianfunds.com/ai-smart-skill-service/openapi/skill/invoke' \
--header "X-API-Key: $TTFUND_APIKEY" \
--header 'Content-Type: application/json' \
--data '{
  "skill_id": "FUND_HUAAN_GOLD_INFO",
  "_skill_version": "1.0.0",
  "query_scope": "all"
}'
```

### 6. 查询指数详情

```bash
curl --location 'https://skills.tiantianfunds.com/ai-smart-skill-service/openapi/skill/invoke' \
--header "X-API-Key: $TTFUND_APIKEY" \
--header 'Content-Type: application/json' \
--data '{
  "skill_id": "FUND_INDEX_INFO",
  "_skill_version": "1.0.0",
  "index_id": "沪深300",
  "query_scope": "all",
  "time_range": "1y"
}'
```

### 7. 查询基金净值

```bash
curl --location 'https://skills.tiantianfunds.com/ai-smart-skill-service/openapi/skill/invoke' \
--header "X-API-Key: $TTFUND_APIKEY" \
--header 'Content-Type: application/json' \
--data '{
  "skill_id": "FUND_NAV_INFO",
  "_skill_version": "1.0.0",
  "fund_id": "000001",
  "range": "n"
}'
```

## 返回结果格式

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "skill_id": "FUND_MANAGER_INFO",
    "skill_name": "基金经理查询",
    "raw_result": {
      "status_code": 200,
      "body": {}
    },
    "field_interpretations": []
  }
}
```

## 错误处理

- 若缺少 `TTFUND_APIKEY`，提示用户前往天天基金 App 搜索 skills 获取 apikey
- 若 HTTP 请求失败，提示服务暂时不可用
- 若业务返回 `errorCode != 0`，视为业务失败

## 注意事项

1. 每次请求必须携带 `skill_id` 和 `_skill_version`
2. `FUND_BASE_INFOS` 和 `FUND_CONDITION_SELECT` 使用 version `1.1.0`
3. 其他 skill 使用 version `1.0.0`
