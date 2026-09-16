# 安全机制和明确边界

## 授权

本地 Bearer token 映射 viewer/operator。viewer 可创建和读取调查、请求取消；operator 才可做审批决定。两种 token 初始化时随机生成，HTTP 层常数时间比较。它不是生产身份系统，不支持用户级隔离、令牌轮转、OIDC/JWT issuer validation 或审计到具体员工。

`request_remediation=true` 只代表用户希望看到修复建议。它不会授予模型执行权限；批准动作仍必须由 operator 单独完成。

## 不可信遥测

`ToolRequest` 拒绝多余字段：没有 URL、command、文件路径或任意查询语言输入。服务名是枚举，时间窗口 60–900 秒，每页最多 50 条。所有网络地址来自服务端配置，httpx 禁止重定向，并关闭从环境隐式继承代理。

输出先扫描风险和脱敏，再裁剪；Bearer、常见 key/password 字段和演示 canary 会被移除。模型提示中的 JSON 明确标记 `UNTRUSTED_DATA`。模型响应的公开字段再做一次脱敏，日志仅记录安全类别，不落完整异常响应。

这些正则无法检测所有混淆、编码或跨多轮注入，不能作为唯一防线。未命中的攻击仍只能请求已暴露的只读能力；写动作要过独立 policy。

## 审批与修复

审批记录包括 run ID、精确动作结构、参数哈希、有效期、角色和决定。执行还检查当前状态、取消标记、scope 与资源 revision。批准 A 无法直接用于执行 B；资源更新后旧审批失效。

两个动作只更新 DemoResource 中的 fault/version。Execution 的唯一键和同事务提交防止重复数据库效果。已经成功的重复请求只返回已有回执。没有外部“执行中但结果未知”的写入路径；未来加入外部执行器时必须增加 reconciliation，不能用该回执实现冒充外部 exactly once。

## 审计

事件 hash 包含 run、序号、actor、事件类别、时间、payload 与上一 hash。校验包含当前 run.event_head，可以检测截断与简单历史修改。拥有数据库写权限的人能重算整链；生产应接独立签名／远端只追加存储，必要时 WORM。

## 运行范围

默认 API 绑定回环地址；Compose 暴露端口同样只绑定 127.0.0.1。demo 管理接口需要 DEMO_ONLY 和独立 token。观测组件无生产鉴权且未配置 TLS，只用于本机演示。真实模型供应商会收到脱敏后的数据，需要你自行选择可接受的提供商。

