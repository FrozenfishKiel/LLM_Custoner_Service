# Action 归属、事务、幂等和审计设计

## 背景

当前 chat 授权已经把登录账号解析为可信 `account_id` 和业务 `user_id`，并通过 Agent metadata 传给 Action。Redis TrackerStore 也已经让生产会话状态落到 `tracker:account:{account_id}`。但业务 Action 仍存在课程 demo 遗留问题：部分 Action 从 tracker 槽读取 `user_id`，缺失时回退到 `"1001"`；部分订单、物流和售后查询只按 `order_id` 查；写操作缺少统一事务、幂等和审计。

本 slice 覆盖 PRD B-01 至 B-10，以及 D-05 的业务数据隔离。它不实现限流、前端、管理员审计查看页、模拟数据初始化或生产监控。

## 领域约束

- 消费者身份只能来自登录态，Action 不得相信模型、消息、按钮 payload 或 tracker 槽提供的 `user_id`。
- 业务对象归属以 `OrderInfo.user_id == current_user_id` 为主线。物流必须通过当前用户订单关联得到；收货信息必须属于当前业务用户；售后必须通过订单明细回溯到当前用户订单。
- 查询 Action 对越权对象必须返回与不存在相同的用户提示，不泄露对象是否属于别人。
- 关键写操作包括修改收货信息、取消订单和创建售后。它们必须在单个数据库事务中完成业务校验、状态修改和审计记录。
- 幂等不是前端防抖。重复提交同一关键写操作时，数据库结果不能重复产生副作用，响应应稳定。

## 采用方案

采用“共享 Action 安全上下文 + 业务守卫函数”的方案：

1. 新增 `ecs_demo/actions/security.py`，集中定义：
   - `ActionSecurityError`
   - `ActionUserContext`
   - `current_action_user(**kwargs)`
   - `owned_order_query(session, user_id, order_id)`
   - `record_action_audit(session, context, ...)`
2. Action 运行时优先从 `kwargs["user_id"]` 和 `kwargs["account_id"]` 获取可信身份。缺失时默认 fail closed；只有显式 demo 模式才允许回退 tracker 槽。
3. 查询类 Action 使用守卫查询：
   - 订单详情：`order_id + current_user_id`
   - 物流：通过当前用户订单查询物流
   - 售后资格/原因：先校验订单归属，再读取订单明细和商品类目
4. 写类 Action 使用事务边界：
   - 修改收货信息：校验订单归属、订单状态仍允许修改、目标地址属于当前用户或在同事务中新建当前用户地址。
   - 取消订单：校验订单归属、状态仍允许取消；已取消时返回幂等成功，不重复修改。
   - 创建售后：校验订单归属和售后资格；对同一订单、同一售后类型和原因重复提交时返回已有申请，不重复创建售后记录。
5. 审计复用现有 `audit_event` 表和 `AccountRepository.record_audit()` 的字段约束；业务 Action 写入 `event_type` 使用稳定前缀：
   - `business.address.change`
   - `business.order.cancel`
   - `business.postsale.apply`

## 不采用的方案

### 方案 A：只在 HTTP chat route 做一次校验

这个方案不能满足 PRD B-01 至 B-10，因为 Action 仍可能根据被污染的槽位或按钮 payload 访问别人的业务对象。它只能保护入口，不能保护业务写入点。

### 方案 B：把所有业务 Action 重写成新服务层

长期更干净，但当前范围过大，会同时牵动 Flow、按钮 payload、课程数据和展示文案。这个 slice 选择在现有 Action 内建立统一守卫，先把越权风险封住。

## 错误处理

- 缺少可信身份：生产返回稳定失败提示，并记录 warning；不得回退 `"1001"`。
- 对象不存在或不属于当前用户：返回“未找到该订单/物流/售后信息”，不泄露越权事实。
- 写操作状态不允许：返回业务状态提示，不执行修改，必要时记录 failure 审计。
- 数据库异常：回滚事务，返回“请稍后重试”，记录脱敏日志和 failure 审计；不得在响应或日志中输出密码、Redis URL、Session token、CSRF token。

## 测试策略

- 单元测试：用 SQLite 或 fake tracker 覆盖身份解析、缺少身份 fail closed、越权 order_id 不返回数据、写操作幂等。
- MySQL 集成测试：在真实临时 MySQL 库中创建两个业务用户的数据，验证用户 A 无法读取/修改用户 B 的订单、物流、地址和售后。
- HTTP/Agent 边界回归：继续验证 chat route 只注入服务端可信 metadata，客户端 identity 字段被清理。
- 故障测试：模拟写操作中途异常，确认事务回滚且不会留下部分售后或错误订单状态。
- 审计测试：关键写操作成功/失败都写入 `audit_event`，metadata 只包含非敏感业务字段。

## 上线影响

完成后，即使用户或模型把别人的 `order_id`、`receive_id` 或 `user_id` 放进消息、metadata、tracker 槽或按钮 payload，业务 Action 也只能处理当前登录业务用户的数据。写操作具备可追踪审计和基本幂等，继续降低公网化后的越权和重复提交风险。

仍未完成的上线项包括：正式限流、浏览器 E2E、生产监控告警、备份恢复、压测、LLM 评测、发布回滚演练和管理员审计查看页面。
