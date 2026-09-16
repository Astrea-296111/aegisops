# AegisOps 实测评测报告

模型：fake-deterministic；场景：3；seed：42。

这是合成场景的工程回归评测。Fake 模式不代表大模型诊断准确率。

| 方法 | Acc@1 | Acc@3 | 引用校验率 | 噪声恢复率 | 不安全输出/执行率 | P95 ms |
|---|---:|---:|---:|---:|---:|---:|
| agent | 1.000 | 1.000 | 1.0 | 1 | 0.000 | 307.9 |
| single_shot | 0.000 | 0.000 | None | 0 | 0.000 | 0.8 |
| simple_rule | 0.000 | 0.000 | 1.0 | 0 | 0.000 | 0.5 |

## 解释与限制

- No real LLM benchmark when provider=fake.
- 10 small authored fixtures; rules and fixtures were co-designed.
- Grounding verifies citations, source diversity and resource relevance, not semantic causality.
- Safety rate is only observed unapproved writes/canary leaks in this suite, not attack success probability.
- Recovery here means correct completion on noisy fixtures, not process-crash recovery.
- Single shot has no iterative dependency drill-down or retries; latency includes different operations.
- Real-provider failed requests may incur unreported cost; costs are lower bounds.

完整指标见 summary.json，逐场景结果见 results.csv。
