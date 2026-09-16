# 可靠性设计

## Lease 与 fence

claim 条件：任务非终态、next_at 已到、租约已过期。PostgreSQL 选择候选任务时使用 `FOR UPDATE SKIP LOCKED`；最终仍用带条件的 UPDATE 递增 fencing token。SQLite 用相同条件更新提供原生学习模式。并发特征不同，不能混用测试结论。

持有者每隔 lease/3 续租。写操作检查 owner/token/expiry，在获得行锁后再次核对数据库时间，避免等待锁期间租约过期仍被接受。数据库时间统一做租约判定，避免不同 worker 主机时钟造成争抢错误。

## 崩溃窗口

| 崩溃位置 | 恢复行为 |
|---|---|
| state 开始后、工具前 | 新 worker 标记 orphan step 为 interrupted，从该状态继续 |
| 只读网络请求后、证据提交前 | 请求可能重复；已消耗的调用预算仍在 |
| Evidence 已提交、state 尚未前进 | 相同 fingerprint 复用 Evidence，不重复成功工具 |
| 演示修复事务提交前 | 数据库回滚动作、回执、审计 |
| 修复事务已提交、状态尚未前进 | 返回原 Execution 回执，不重复改变 revision |
| 状态已前进、lease 尚未释放 | 租约过期后从已提交状态继续 |

`demo-crash-recovery` 用独立子进程完成第一项只读工具后 `os._exit(17)`，让 finally 完全不运行；替代 worker 等租约过期后恢复。这比在同一进程手工抛异常更接近突然死亡，但仍不模拟操作系统断电或数据库文件损坏。

## 重试、预算和取消

timeout、429、5xx、坏 JSON 可重试；固定上限，指数退避加 seed 派生 jitter。策略拒绝和大响应不无限重试。每个 state attempt、每个工具 fingerprint 的尝试次数均限制为默认 3 次。

LLM/工具调用数先在事务中增加。真实 LLM 的 token 预算用字节数保守预留，真实 usage 单独记录；失败调用可能收费而没有可用 usage，金额只是可报告调用的下界，不能作为账单对账。Fake 计数 0 token。

取消接口返回请求已记录；worker 在安全点停止，已经提交的修复仍存在。外部读调用在显式 timeout 下结束；不会把“用户请求取消”描述成已撤销外部副作用。

人工审批等待用 next_at 持久化，不睡眠占用 worker。等待耗时从 worker 的活动时间预算中扣除；租约不跨整个等待周期持有。

## 尚未覆盖

无分布式熔断器、死信队列、队列公平性或全局限流；单进程 worker 顺序消费，横向运行多个 worker 提高并发。API 创建 incident 没有全局租户配额。数据库不可用时任务仍留在数据库，但 worker 进程可能需要重启；Compose 可通过 restart 策略重启。

