# LLM 聊天评测收口报告

日期：2026-07-19；当前复核日期：2026-07-20

## 评测范围

这组评测面向真实认证电商客服链路，而不是 mock Agent：

- 真实 FastAPI auth 路由和 chat 路由；
- 真实 Redis Session 状态；
- 真实 MySQL 业务数据断言；
- 真实 `Agent.load(ECS_DEMO, ...)`；
- 独立临时 MySQL 数据库，用于 auth schema migration 和确定性业务 fixture。

它的用途是留下一个可追踪的 LLM 客服质量基线，方便后续持续改进；它不是“当前智能客服质量已经达到上线标准”的证明。

## 历史基线指标

2026-07-19 的已有 evidence 显示，评测 harness 曾完整跑通，但质量指标全部为 0：

| 指标 | 结果 | 样本 |
| --- | ---: | --- |
| 总用例数 | `14` | 10 个业务用例 + 4 个边界用例 |
| 场景完成率 | `0.0000` | `0 / 10` |
| 业务事实准确率 | `0.0000` | `0 / 10` |
| 边界拒答率 | `0.0000` | `0 / 4` |
| 平均完成轮数 | `0.00` | 没有完成的业务用例 |

结论很朴素：评测框架有价值，但当前 Agent 的客服质量基线很弱，不能据此宣布质量上线合格。

## 当前复跑结果

2026-07-20 我在当前 Codex runtime 里复跑：

```powershell
C:\Users\frozenfish\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest tests/integration/test_llm_chat_evaluation.py -q -s -m integration
```

实际结果：失败，失败点不是评测断言，而是当前本机缺少可用 MySQL：

```text
AssertionError: Local MySQL is unavailable for integration tests
```

因此当前只能把 2026-07-19 的结果作为历史 baseline，把 2026-07-20 的复跑状态标记为“环境阻塞，未得到新的通过结果”。这个阻塞不影响本轮生产 wiring 的 unit/API 验证，但会影响 LLM 质量基线的 fresh integration 复验。

## 观察到的质量问题

历史 evidence 支持这些具体问题：

1. 业务查询没有可靠落到 fixture 数据上。
   - 例如 `order_query_basic` 请求种子订单，真实响应未包含预期订单事实。
   - 这导致场景完成率和业务事实准确率都是 `0 / 10`。

2. 边界问题没有返回评测集要求的服务范围拒答。
   - 例如编程、天气、金融等非客服场景没有稳定给出“只处理订单、物流、售后”等范围说明。
   - 这导致边界拒答率是 `0 / 4`。

3. 写操作流程没有完成。
   - 修改地址、取消订单、申请售后等流程均未满足完成和事实断言。
   - 最长尝试到 3 轮，仍未达到评测完成条件。

## 证据文件

| 证据 | 结果 |
| --- | --- |
| `docs/reports/integration/evidence/llm-chat-evaluation.txt` | 2026-07-19 历史评测：`1 passed`，指标全 0 |
| `docs/reports/integration/evidence/llm-chat-evaluation-rerun-2026-07-20.txt` | 2026-07-20 当前复跑：MySQL 不可用，未通过 |
| `docs/reports/integration/evidence/llm-chat-evaluation-secret-scan.txt` | 作用域 secret 扫描：未发现密钥 |

## 上线影响

- 可以演示“真实认证 + 真实聊天入口 + 评测框架存在”。
- 不应演示成“智能客服已经能稳定解决订单、物流、售后问题”。
- 上线前如果要把 LLM 质量作为验收门槛，需要先恢复 MySQL/Redis integration 环境，然后重新跑这组评测；指标仍为 0 时，应进入 Agent prompt、意图路由、业务 action grounding 的专项修复。
