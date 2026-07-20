# LLM 质量评测全 0 根因修复报告

日期：2026-07-20  
分支：`feat/llm-quality-baseline`  
工作目录：`D:\Projects\llm_customer_service_llm_quality`

## 结论

“LLM 质量评测各项都是 0”不是单纯的模型效果差，而是多层工程接线问题叠加导致：

1. 当前运行环境缺少 `langchain-openai` 等依赖，LLM 命令生成器没有真正稳定调用模型。
2. 自定义 action loader 触发 `actions/__init__.py` 循环导入，导致部分业务 action 注册失败。
3. action loader 扫描并重载 `actions/db.py`，覆盖了 integration harness 对临时评测库的 patch，业务 action 查错数据库。
4. 明显越界问题被 LLM 判为知识问答/闲聊，没有稳定拒答。
5. 用户首句里的订单号在 `start flow` 后没有保留到 `order_id` 槽位，写操作容易卡在“请选择订单”。
6. 售后 flow 收集 `postsale_order_id`，但售后 action 实际读取 `order_id`，flow/action 契约断裂。
7. 修改收货信息 flow 使用匿名嵌套 collect 步骤，但当前 `FlowExecutor` 只能跳转到具名 step id，导致 flow 中途结束。
8. 写操作虽然已经落库成功，但取消/售后/改地址回复没有统一包含“成功”，导致完成率评分仍偏低。

## 修复内容

- 修复 `Agent._load_custom_actions()`：
  - 预注册 `actions` package，避免相对导入触发循环导入。
  - 只扫描 `action_*.py`，避免重载 `db.py`、`security.py` 等支持模块。
- 修复明显越界 guard：
  - 天气、编程、闲聊、金融投资等明显非客服请求在调用 LLM 前直接返回 `CannotHandleCommand`。
  - 拒答回复统一引导到订单、物流、售后范围。
- 修复 LLM 命令生成器：
  - 当 LLM 输出 `start flow ...` 且用户原文包含形如 `eval-order-cancel` 的订单号时，自动追加 `SetSlotCommand(order_id=...)`。
- 修复售后 flow：
  - `apply_postsale` 从 `postsale_order_id` 改为收集 `order_id`。
- 修复修改收货信息 flow：
  - 把 `then:` 中匿名嵌套 collect 列表改成具名 step 链：`collect_receiver_name`、`collect_receiver_phone`、`collect_receive_province`、`collect_receive_city`、`collect_receive_district`、`collect_receive_street_address`。
- 修复写操作成功反馈：
  - 取消订单：`订单已取消成功...`
  - 修改收货信息：`订单收货信息已修改成功`
  - 申请售后：`您的xxx申请已成功提交！`

## 新增/更新测试

- action loader：
  - 相对导入不再触发 package cycle。
  - loader 不再重载已 patch 的支持模块 `actions.db`。
- LLM 边界：
  - 明显越界客服请求不会调用 LLM。
  - 边界拒答回复必须包含订单/物流/售后引导。
- LLM 槽位保留：
  - 用户首句包含订单号并启动订单 flow 时，必须追加 `order_id`。
- 生产 flow 契约：
  - 售后 flow 必须收集 action 使用的 `order_id`。
  - 修改收货信息分支必须跳转到已注册具名 step。
- action 审计：
  - 取消、改地址、售后写操作继续验证归属、幂等、审计，并新增“成功”反馈断言。

## Fresh verification 量化结果

完整 LLM 聊天评测命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_llm_chat_evaluation.py -q -s -m integration
```

最新结果：

```text
llm_chat_eval total_cases=14 scenario_completion_rate=1.0000 business_fact_accuracy=1.0000 boundary_refusal_rate=1.0000 average_turns_to_completion=3.40
1 passed
```

对比修复前历史 baseline：

```text
total_cases=14
scenario_completion_rate=0.0000
business_fact_accuracy=0.0000
boundary_refusal_rate=0.0000
average_turns_to_completion=0.00
```

## 剩余风险

- 这次评测覆盖订单查询、物流查询、改地址、取消订单、售后申请、越界拒答，但仍是固定 14 条用例；上线前仍需要补充更大规模的刁钻输入、压力测试、并发会话、失败注入和监控指标。
- Docker Desktop/MySQL 在开发过程中曾出现 daemon 不稳定；上线环境需要明确健康检查、重试和告警。
- Neo4j/GraphRAG 连接 warning 当前不影响订单/物流/售后主链路，但如果后续打开知识库问答能力，需要单独做知识检索质量评测。
