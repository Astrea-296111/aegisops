# 威胁模型

资产：模型 API key、operator 权限、演示修复能力、遥测内容、审计历史。

信任边界：用户到 API；模型到工具；外部日志到模型；operator 到审批；worker 到数据库；服务到观测后端。服务器配置和代码被视为可信，遥测与模型输出不可信。

| 威胁 | 实施的防线 | 代码／测试 | 剩余风险 |
|---|---|---|---|
| 间接注入 | 数据 envelope、扫描标记、白名单、独立审批 | guards.py、registry.py、test_injection_does_not_execute_or_leak | 正则和模型可能仍被误导；不能保证诊断语义安全 |
| SSRF | 输入无 URL、服务枚举、配置地址、禁重定向 | Query、LiveDataSource、typed boundary tests | 可信配置被篡改、DNS／网络侧风险需 egress 控制 |
| Secret 外泄 | 递归去敏、canary 检查、错误消息分类 | redact、compact、security tests | 未匹配的秘密格式仍可能外发 |
| 越权修复 | operator 权限、审批二次检查 | app.py、remediation.py | operator token 被盗即具有该角色能力 |
| 参数替换／审批重放 | exact hash + run + expiry + revision | action_policy、mutation tests | 数据库或应用服务器完全失陷不在防御范围 |
| 重复副作用 | 数据库事务 + 唯一 Execution | idempotent execution test | 不推广到外部 HTTP／宿主机操作 |
| 旧 worker 写入 | lease + fencing + 数据库时间 | repository.locked、stale worker test | 数据库自身一致性和权限需外部保障 |
| 审计篡改 | 哈希链 + 持久化链头校验 | verify_chain、tampering test | 攻击者可重写整链，无外部锚定 |
| 钱包／资源耗尽 | 单 run 工具／LLM／token／时间预算 | worker.llm、registry.execute、budget test | 大量创建 run 仍需全局速率和配额限制 |
| 伪造证据 | id、非空、资源相关性、多信号校验 | grounded、self-correction test | 引用存在不证明因果；confidence 非统计校准概率 |
| 畸形／巨大返回 | bounded streaming、schema、裁剪 | bounded_json、HTTP failure tests | 压缩后过度丢失信息会影响诊断 |
| 故障控制被滥用 | 本机端口、DEMO_ONLY、独立 token | demo_service.py、demo control test | 本机恶意进程／配置误暴露需部署隔离 |

