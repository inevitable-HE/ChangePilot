# establish-reliable-workflow-core 验证报告

验证日期：2026-07-22  
验证模式：full  
代码审查模式：off（根据用户要求关闭逐任务及自动 reviewer；本报告承担项目完成后的统一验证记录）

## 结论

| 维度 | 结果 | 证据 |
| --- | --- | --- |
| 完整性 | PASS | OpenSpec 17/17 任务完成；9/9 项需求和 19/19 个场景均有实现与测试映射 |
| 正确性 | PASS | 全量测试 384 passed；固定订单升级场景、恢复、审批、重试、幂等和补偿均在测试中覆盖 |
| 一致性 | PASS | 实现保持 domain/application/ports/adapters 分层，并符合确定性 DAG、双事务工具调用和启动先恢复的设计 |
| 覆盖率 | PASS | 2942 条语句中 223 条未覆盖，总覆盖率 92%，超过 85% 门槛 |
| 规范校验 | PASS | `openspec validate --strict`：1 passed，0 failed，0 issues |
| 安全基线 | PASS | 未发现硬编码凭据、动态代码执行或 `shell=True`；密钥仅以引用/不可恢复占位符进入持久化边界 |

未发现 CRITICAL、WARNING 或 SUGGESTION 级验证问题，可以进入分支收尾。

## 需求映射

| OpenSpec 要求 | 主要实现 | 主要验证 |
| --- | --- | --- |
| 工作流定义结构校验 | `domain/definitions.py:67`、`domain/validation.py`、`application/tooling.py:35` | `tests/unit/test_definition_validation.py`、`tests/unit/test_tool_registry.py` |
| 依赖关系调度 | `application/scheduler.py:8`、`application/coordinator.py:107` | `tests/unit/test_scheduler.py`、`tests/integration/test_coordinator_retry.py` |
| 显式状态机 | `domain/runs.py:79`、`domain/runs.py:125`、`domain/states.py` | `tests/unit/test_state_machines.py` |
| 持久化与恢复 | `adapters/persistence/sqlite.py:773`、`application/recovery.py:34`、`application/services.py:294` | `tests/contract/uow_contract.py`、`tests/integration/test_recovery.py`、`tests/process/test_process_recovery.py` |
| 高风险审批 | `application/approvals.py:142`、`application/approvals.py:238` | `tests/unit/test_approval_binding.py`、`tests/integration/test_approval_barrier.py` |
| 幂等副作用恢复 | `application/tooling.py:140`、`ports/tools.py` | `tests/unit/test_idempotency.py`、`tests/integration/test_recovery.py` |
| 错误分类与有限重试 | `domain/failures.py`、`application/coordinator.py:107` | `tests/integration/test_coordinator_retry.py` |
| 显式补偿 | `application/scheduler.py:34`、`application/coordinator.py:107` | `tests/unit/test_compensation_order.py`、`tests/integration/test_compensation.py` |
| 审计与脱敏 | `domain/events.py:8`、`application/tooling.py:166`、`application/services.py:165` | `tests/unit/test_redaction.py`、`tests/unit/test_query_service.py`、订单升级验收测试 |

## 核心验收路径

固定订单服务升级 DAG 的三条端到端路径均已覆盖：

1. 前置检查完成后等待 Schema 变更审批，批准后迁移、部署、健康检查和冒烟测试成功。
2. 健康检查失败后，先恢复服务版本，再回滚 Schema，并保留原始错误。
3. 工具产生副作用后进程中断，重启时先 probe 已有结果，再继续调度且不重复副作用。

对应测试位于 `tests/integration/test_order_upgrade_acceptance.py` 和 `tests/process/test_process_recovery.py`。

## 执行证据

```text
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
384 passed in 23.32s

.venv\Scripts\python.exe -m coverage report --fail-under=85
TOTAL 2942 statements, 223 missed, 92% covered

openspec validate establish-reliable-workflow-core --strict --json --no-interactive
1 passed, 0 failed, 0 issues
```

## 保留边界

本 change 交付的是 ChangePilot Phase 1 的可靠执行内核，不包含 LLM 规划、RAG、真实生产工具、Web 控制台、多租户 RBAC 或分布式调度。这些是明确的非目标，不构成本次验证偏差。
