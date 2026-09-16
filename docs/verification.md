# 交付验收记录

环境：Linux-6.18.44-x86_64-with-glibc2.39；Python 3.12.14；日期：2026-09-16。

| 检查 | 结果 | 证据 |
|---|---|---|
| Ruff | PASS | ruff check . |
| mypy | PASS | 28 个源码文件 |
| pytest | 52 passed / 1 skipped | artifacts/verification/pytest.xml |
| Alembic schema check | PASS | No new upgrade operations detected |
| wheel / sdist 构建 | PASS | python -m build --no-isolation |
| 新目录、带空格和中文路径初始化 | PASS | fresh-install.json |
| 独立 API + worker 进程的 HTTP 调查 | PASS | fresh-install.json |
| 子进程异常退出后恢复 | PASS | crash-recovery.json、crash-events.json |
| replay eval | 10 个 Agent 场景完成，2 个基线各 10 个完成 | artifacts/eval/latest/ |
| live 工具协议适配 | PASS，使用 mock HTTP | test_live_contracts.py |
| demo 故障控制和业务响应 | PASS，真实数据库 + mock 日志导出 | test_live_contracts.py |
| Windows 依赖解析 | PASS，55 个原始 wheel 随包 | wheelhouse/SHA256SUMS.txt |
| Windows 实际执行 | NOT_MEASURED | 当前宿主非 Windows，提供 Windows CI job |
| PostgreSQL 实际执行 | NOT_MEASURED | 当前无可用服务，专用测试被 skip；提供独立 CI job |
| Docker Compose live 端到端 | NOT_MEASURED | 当前无 Docker，提供 live CI job |
| 真实模型调用 | NOT_MEASURED | 未提供 API 凭据，仅测试 HTTP 兼容协议 |

## 需求覆盖与变化

调查、证据、重试、预算、取消、持久化租约、fencing、审批绑定、资源 revision、数据库内幂等和哈希链均有实现及对应测试。原 prompt 中 4,000–7,000 行是建议规模；本项目没有为了达到行数而填充重复代码。

SQLite 是新增的 Windows 原生入口，未替代 PostgreSQL 实现。两个演示动作只修改数据库配置；修复后的业务流量恢复未测量。MCP 和 SSE 为可选项未实现；分布式熔断／全局限流、自动分页、多租户、外部写入 reconciliation 没有实现。细节参阅 design-notes.md、security.md、reliability.md。

## 不能声称的事情

不能说“已经在 Windows 实机与生产 PostgreSQL 部署验收”；不能说“真实模型准确率 100%”；不能说“完全防御 Prompt Injection”或“任意外部动作 exactly once”。本次可信的结论是：Linux 上的 Python 回放工程通过了上述测试，并交付了 Windows 安装依赖、脚本与验证流程。
