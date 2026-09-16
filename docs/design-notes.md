# 设计记录与参考边界

核查日期：2026-09-16。需求来自原始 `prompt.md`，完整副本在 `original-prompt.md`。本轮追加要求是 Windows 可部署及初学者说明书。

## 参考项目

| 项目与核查来源 | 实际借鉴的思想 | 本项目没有照搬的部分 |
|---|---|---|
| [HolmesGPT](https://github.com/HolmesGPT/holmesgpt)；[工具集文档](https://holmesgpt.dev/dev/data-sources/custom-toolsets/) | 用观测工具调查故障；限制工具输出规模 | 不引入其代码、Kubernetes 权限或海量工具集 |
| [agentgateway](https://github.com/agentgateway/agentgateway) | 在模型外做权限、预算和可观测性控制 | 不实现通用 MCP/A2A 代理或 CEL 引擎 |
| [Microsoft MCP Gateway](https://github.com/microsoft/mcp-gateway) | 工具元数据与会话管理、控制面和数据面边界 | 不引入 Kubernetes 生命周期、内置 shell 工具或 .NET 运行时 |
| [AgentLedger](https://github.com/yaogdu/AgentLedger) | run/step/event、claim/lease/fencing、重试与恢复 | 不照搬通用多后端 runtime、调度器与 artifact 平台 |
| [AgentOps-Bench](https://github.com/kunwarshivam/agentops-bench) | clean/noisy/adversarial 分组、故障注入与成本指标 | 不使用其数据与数字作为本项目成绩 |
| [OTel SRE-Copilot](https://github.com/Wassbdr/otel-sre-copilot) | 多信号 RCA、回放、基线与证据评测 | 不引入 LangGraph、kind、15 个服务或其未完成的 benchmark 数字 |
| [Pydantic AI durable execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/) | 将推理步骤与持久化执行分开思考 | 不引入 Temporal/Pydantic AI 作为依赖，自研小型状态机便于学习 |

选择依据是设计问题与当前项目匹配，不是热度榜。上面部分项目规模很小，不能把它们统称为经过大规模生产验证的系统。AegisOps 源码为本任务重新实现；没有复制开源实现。

## 范围固定

两个服务（order/inventory）、五个只读工具、两个角色、两个数据库内演示动作、10 个合成场景。核心 Python 约 3,000 行量级，保持学习者能够逐文件阅读；未为了满足原 prompt 的行数建议而补无价值代码。

## 主要取舍

1. 增加 SQLite + aiosqlite 原生路线，解决 Windows 初次安装门槛。PostgreSQL + asyncpg 仍实现，部署配置与专用 CI 保留。
2. 不实现外部宿主机 restart。关闭故障／回退版本会改变 demo 服务下一次业务请求的行为；它们与回执共享数据库事务，原子性边界可以实际测试。
3. 写操作后仅校验持久化配置与 revision。业务恢复必须观测新流量，本版不伪造该结论。
4. Fake 是规则测试替身。Single Shot 和 Simple Rule 也得到两个服务的数据，所以当前精度持平；不为了让 Agent 胜出而弱化对照数据。
5. 工具输出 schema 统一，输入按白名单约束；未接 MCP，避免另一条可绕过策略的执行路径。
6. 临时错误在原状态保留 checkpoint，并通过 next_at 调度有限重试，审计为 `state.retry_scheduled`；没有额外 RETRYABLE_ERROR 状态，避免两个字段重复表达恢复位置。
7. 测试替代真实模型，并显式保留真实供应商 token/cost 的测量入口。
8. OTel span 默认不记录异常正文、prompt 或敏感遥测。跨状态用 run.id 关联；未做持久化 trace parent 恢复。

## 相比原 prompt 的范围变化

强制依赖都保留，新增 SQLite 仅为 Windows 入口。未实现的细项包括：生产级熔断器、全局 API 速率限制、自动分页循环、SSE、MCP。当前提供参数边界／page cursor、单 run 预算、步骤重试、服务端过滤和输出压缩。时间字段用 UTC epoch 秒保存以简化 SQLite/PostgreSQL 一致性；对外字段含义写在说明书。完整 live 栈因本次环境无 Docker 未执行，不能当已验收结果。

