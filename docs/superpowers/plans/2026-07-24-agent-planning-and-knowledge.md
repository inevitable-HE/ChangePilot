---
change: add-agent-planning-and-knowledge
design-doc: docs/superpowers/specs/2026-07-24-agent-planning-and-knowledge-design.md
base-ref: 6a1ede224f00718b8dfeb695adba0ef88ce7004c
---

# Agent 规划与知识检索实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将自然语言研发变更和版本化 Runbook 转换为带证据、经过确定性校验且可交给第一阶段工作流内核的结构化计划。

**Architecture:** 新增 `changepilot.planning` 分层包；LangGraph 只编排有限规划状态图，DeepSeek 和本地知识库通过端口接入。模型输出经过 Pydantic、规划策略和现有 `WorkflowDefinition.from_mapping()` 三层校验，任何节点都不直接执行有副作用工具。

**Tech Stack:** Python 3.12、Pydantic 2、LangGraph 1.x、OpenAI Python SDK、SQLAlchemy 2、Alembic、SQLite FTS5、可选 Sentence Transformers/BGE、Pytest。

## Global Constraints

- 默认测试必须离线运行，不调用 DeepSeek，不下载嵌入模型。
- DeepSeek API Key 只能从环境变量读取，不能写入仓库、日志和审计记录。
- LangGraph 修复环最多执行一次；策略错误不得进入修复环。
- Runbook 内容一律按不可信数据处理，不能修改系统规则或触发工具。
- 高风险步骤必须要求审批、引用可信证据并满足规划工具策略的补偿要求。
- 开发期间运行定向测试；全量测试和覆盖率集中在 Task 8。
- 不修改第一阶段工作流持久化模型，通过定义身份和摘要关联规划记录。

---

### Task 1: 建立规划领域模型和模型端口

**Files:**
- Modify: `pyproject.toml`
- Create: `src/changepilot/planning/__init__.py`
- Create: `src/changepilot/planning/domain/__init__.py`
- Create: `src/changepilot/planning/domain/models.py`
- Create: `src/changepilot/planning/domain/failures.py`
- Create: `src/changepilot/planning/ports/__init__.py`
- Create: `src/changepilot/planning/ports/models.py`
- Test: `tests/planning/unit/test_models.py`

**Interfaces:**
- Produces: `ChangeRequest`, `EvidenceRef`, `RetrievedEvidence`, `PlanStep`, `ChangePlan`, `PreparedWorkflow`, `PlanningResult`
- Produces: `ModelRequest`, `ModelResponse`, `ModelUsage`, `ModelGateway`

- [x] **Step 1: 添加失败测试，锁定严格且不可变的边界模型**

```python
def test_change_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ChangeRequest(
            service_id="orders",
            current_version="1.0",
            target_version="2.0",
            change_summary="upgrade with schema migration",
            success_conditions=("health endpoint is ready",),
            unexpected=True,
        )


def test_plan_digest_is_stable_for_equivalent_content() -> None:
    assert make_plan().content_digest == make_plan().content_digest
```

- [x] **Step 2: 运行测试并确认因规划包不存在而失败**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\unit\test_models.py -q`

Expected: FAIL with `ModuleNotFoundError: changepilot.planning`

- [x] **Step 3: 实现领域模型、可辨识结果联合类型和模型网关协议**

```python
class PlanningBoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ModelGateway(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse:
        """Return one structured model response without executing tools."""
```

`ChangePlan` 在模型验证后计算规范化 SHA-256；`PlanningResult` 只能是 `ClarificationRequired | PlanReady | PlanningRejected | BudgetExhausted`。

- [x] **Step 4: 添加 LangGraph、OpenAI SDK 和可选 BGE 依赖**

```toml
dependencies = [
    # existing dependencies
    "langgraph>=1.0,<2",
    "openai>=2.0,<3",
]

[project.optional-dependencies]
retrieval = ["sentence-transformers>=5.0,<6"]
```

- [x] **Step 5: 运行模型测试和现有定义测试**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\unit\test_models.py tests\unit\test_definition_validation.py -q`

Expected: PASS

- [x] **Step 6: 提交领域契约**

```bash
git add pyproject.toml src/changepilot/planning tests/planning/unit/test_models.py docs/superpowers openspec/changes/add-agent-planning-and-knowledge
git commit -m "feat: define planning domain contracts"
```

### Task 2: 实现 Mock、DeepSeek、预算和缓存网关

**Files:**
- Create: `src/changepilot/planning/application/model_gateway.py`
- Create: `src/changepilot/planning/adapters/__init__.py`
- Create: `src/changepilot/planning/adapters/models/__init__.py`
- Create: `src/changepilot/planning/adapters/models/mock.py`
- Create: `src/changepilot/planning/adapters/models/deepseek.py`
- Create: `src/changepilot/planning/ports/persistence.py`
- Test: `tests/planning/unit/test_model_gateway.py`
- Test: `tests/planning/unit/test_deepseek_adapter.py`

**Interfaces:**
- Consumes: `ModelGateway.generate(ModelRequest) -> ModelResponse`
- Produces: `BudgetedModelGateway`, `MockModelGateway`, `DeepSeekModelGateway`, `ModelCache`, `UsageRecorder`

- [x] **Step 1: 写入预算、缓存、瞬时重试和密钥脱敏失败测试**

```python
def test_budget_stops_before_second_call() -> None:
    gateway = BudgetedModelGateway(
        inner=MockModelGateway([response_with_usage(total_tokens=50)]),
        budget=ModelBudget(max_calls=1, max_total_tokens=50),
    )
    gateway.generate(request())
    with pytest.raises(ModelBudgetExceeded):
        gateway.generate(request())


def test_same_request_and_knowledge_snapshot_hits_cache() -> None:
    gateway.generate(request())
    cached = gateway.generate(request())
    assert cached.cache_hit is True
    assert inner.call_count == 1
```

- [x] **Step 2: 运行测试并确认缺少网关装饰器**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\unit\test_model_gateway.py -q`

Expected: FAIL

- [x] **Step 3: 实现预算、SHA-256 缓存键、有限重试和使用量记录**

```python
class BudgetedModelGateway:
    def generate(self, request: ModelRequest) -> ModelResponse:
        cache_key = self._cache_key(request)
        if cached := self._cache.get(cache_key):
            return cached.model_copy(update={"cache_hit": True})
        self._budget.reserve_call()
        response = self._retry_transient(lambda: self._inner.generate(request))
        self._budget.record(response.usage)
        self._cache.put(cache_key, response)
        self._usage.record(request, response)
        return response
```

重试仅接受超时、429 和 5xx，最多使用配置值；请求摘要和日志不得包含 API Key 或完整 Prompt。

- [x] **Step 4: 实现可编排 Mock 和 DeepSeek JSON Output 适配器**

```python
client.chat.completions.create(
    model=config.model,
    messages=[message.model_dump() for message in request.messages],
    response_format={"type": "json_object"},
    max_tokens=request.max_output_tokens,
    timeout=config.timeout_seconds,
)
```

DeepSeek 配置从 `CHANGEPILOT_DEEPSEEK_API_KEY`、`CHANGEPILOT_LLM_MODEL` 和 `CHANGEPILOT_LLM_BASE_URL` 读取；构造和异常字符串不得泄露密钥。

- [x] **Step 5: 运行网关和适配器测试**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\unit\test_model_gateway.py tests\planning\unit\test_deepseek_adapter.py -q`

Expected: PASS，且 HTTP 测试使用注入的假客户端

- [x] **Step 6: 提交模型网关**

```bash
git add src/changepilot/planning tests/planning/unit
git commit -m "feat: add budgeted model gateway"
```

### Task 3: 实现 Runbook 摄取、稳定分块和知识领域端口

**Files:**
- Create: `src/changepilot/planning/domain/knowledge.py`
- Create: `src/changepilot/planning/ports/knowledge.py`
- Create: `src/changepilot/planning/application/ingestion.py`
- Test: `tests/planning/unit/test_ingestion.py`

**Interfaces:**
- Produces: `RunbookDocument`, `KnowledgeChunk`, `KnowledgeSnapshot`, `KnowledgeStore`, `EmbeddingProvider`
- Produces: `RunbookIngestionService.ingest(document) -> IngestionResult`

- [x] **Step 1: 写入重复摄取、版本变化和稳定片段 ID 测试**

```python
def test_identical_document_reuses_existing_chunks() -> None:
    first = service.ingest(runbook(version="1"))
    second = service.ingest(runbook(version="1"))
    assert second.snapshot_digest == first.snapshot_digest
    assert second.created_chunks == 0


def test_changed_content_creates_new_knowledge_version() -> None:
    old = service.ingest(runbook(version="1", content="# Rollback\nUse A"))
    new = service.ingest(runbook(version="2", content="# Rollback\nUse B"))
    assert new.snapshot_digest != old.snapshot_digest
```

- [x] **Step 2: 运行测试并确认摄取服务缺失**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\unit\test_ingestion.py -q`

Expected: FAIL

- [x] **Step 3: 实现按标题和字符上限分块、内容摘要和可信级别**

```python
chunk_id = sha256(
    f"{document.document_id}:{document.version}:{position}:{content_digest}".encode()
).hexdigest()
```

片段必须保存标题路径、字符偏移、顺序、文档来源和内容摘要。相同身份和摘要不得重复写入或重新生成向量。

- [x] **Step 4: 运行摄取测试**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\unit\test_ingestion.py -q`

Expected: PASS

- [x] **Step 5: 提交摄取领域**

```bash
git add src/changepilot/planning tests/planning/unit/test_ingestion.py
git commit -m "feat: add versioned runbook ingestion"
```

### Task 4: 实现 SQLite FTS5、向量存储和混合检索

**Files:**
- Create: `alembic/versions/0004_planning_knowledge.py`
- Create: `src/changepilot/planning/adapters/knowledge/__init__.py`
- Create: `src/changepilot/planning/adapters/knowledge/schema.py`
- Create: `src/changepilot/planning/adapters/knowledge/sqlite.py`
- Create: `src/changepilot/planning/adapters/knowledge/bge.py`
- Create: `src/changepilot/planning/application/retrieval.py`
- Test: `tests/planning/unit/test_hybrid_retrieval.py`
- Test: `tests/planning/integration/test_sqlite_knowledge.py`

**Interfaces:**
- Consumes: `KnowledgeStore`, `EmbeddingProvider`
- Produces: `SQLiteKnowledgeStore`, `HybridRetriever`, `BgeEmbeddingProvider`

- [x] **Step 1: 写入 FTS、向量、RRF、引用和冲突失败测试**

```python
def test_hybrid_result_keeps_stable_citation() -> None:
    result = retriever.retrieve("订单服务 schema 迁移回滚", top_k=3)
    assert result.evidence[0].reference.document_id == "schema-migration"
    assert result.evidence[0].reference.chunk_id


def test_active_policies_with_different_rule_hashes_are_conflicts() -> None:
    result = retriever.retrieve("schema migration approval")
    assert result.conflicts[0].policy_key == "schema-migration-approval"
```

- [x] **Step 2: 运行测试并确认 SQLite 知识适配器缺失**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\unit\test_hybrid_retrieval.py tests\planning\integration\test_sqlite_knowledge.py -q`

Expected: FAIL

- [x] **Step 3: 添加知识表和 FTS5 虚拟表迁移**

迁移创建 `knowledge_documents`、`knowledge_chunks`、`knowledge_chunk_embeddings`、`planning_sessions`、`planning_attempts`、`planning_plans`、`model_usage`、`model_cache`，并通过 raw SQL 创建和删除 `knowledge_fts`。

- [x] **Step 4: 实现 BM25、精确余弦和 RRF**

```python
def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[str]], *, k: int = 60
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores
```

向量保存模型 ID 和维度；BGE 适配器延迟导入 `sentence_transformers`，缺少可选依赖时返回清晰配置错误。

- [x] **Step 5: 运行迁移与检索测试**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\unit\test_hybrid_retrieval.py tests\planning\integration\test_sqlite_knowledge.py tests\contract\test_sqlite_uow.py -q`

Expected: PASS

- [x] **Step 6: 提交知识存储和检索**

```bash
git add alembic src/changepilot tests/planning
git commit -m "feat: add hybrid runbook retrieval"
```

### Task 5: 实现规划策略、确定性校验和工作流映射

**Files:**
- Create: `src/changepilot/planning/domain/policies.py`
- Create: `src/changepilot/planning/application/validation.py`
- Create: `src/changepilot/planning/application/mapping.py`
- Test: `tests/planning/unit/test_plan_validation.py`
- Test: `tests/planning/unit/test_workflow_mapping.py`

**Interfaces:**
- Produces: `PlanningToolPolicy`, `PlanValidator.validate(plan, context) -> ValidationReport`
- Produces: `WorkflowDefinitionMapper.prepare(plan, registry) -> PreparedWorkflow`

- [x] **Step 1: 写入未知工具、循环、风险降级、补偿和证据测试**

```python
@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (use_unknown_tool, "unknown_tool"),
        (create_cycle, "dependency_cycle"),
        (downgrade_schema_write, "risk_downgrade"),
        (remove_rollback, "missing_compensation"),
        (cite_untrusted_only, "insufficient_trusted_evidence"),
    ],
)
def test_policy_violation_is_not_repairable(mutator, code) -> None:
    report = validator.validate(mutator(valid_plan()), context())
    assert report.errors[0].code == code
    assert report.errors[0].repairable is False
```

- [x] **Step 2: 运行校验测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\unit\test_plan_validation.py tests\planning\unit\test_workflow_mapping.py -q`

Expected: FAIL

- [x] **Step 3: 实现固定顺序的确定性校验**

最终风险取模型风险、`ToolDescriptor.risk` 和 `PlanningToolPolicy.minimum_risk` 中最高值。高风险强制审批；策略要求补偿时必须引用已注册补偿工具。引用必须存在于当前知识快照且满足最低可信级别。

- [x] **Step 4: 映射并调用第一阶段定义校验**

```python
definition = WorkflowDefinition.from_mapping(payload, registry)
return PreparedWorkflow(
    plan_id=plan.plan_id,
    plan_version=plan.version,
    knowledge_snapshot_digest=plan.knowledge_snapshot_digest,
    definition_id=definition.definition_id,
    definition_version=definition.version,
    definition_digest=definition.digest,
    payload=payload,
)
```

- [x] **Step 5: 运行规划校验和现有工具注册测试**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\unit\test_plan_validation.py tests\planning\unit\test_workflow_mapping.py tests\unit\test_tool_registry.py -q`

Expected: PASS

- [x] **Step 6: 提交安全校验和映射**

```bash
git add src/changepilot/planning tests/planning/unit
git commit -m "feat: validate and prepare change plans"
```

### Task 6: 实现 LangGraph 规划流程和会话持久化

**Files:**
- Create: `src/changepilot/planning/application/state.py`
- Create: `src/changepilot/planning/application/nodes.py`
- Create: `src/changepilot/planning/application/graph.py`
- Create: `src/changepilot/planning/application/services.py`
- Create: `src/changepilot/planning/adapters/persistence.py`
- Test: `tests/planning/integration/test_planning_graph.py`
- Test: `tests/planning/integration/test_plan_freshness.py`

**Interfaces:**
- Consumes: model gateway、retriever、validator、mapper、planning repository
- Produces: `build_planning_graph(dependencies)`, `PlanningService.start()`, `PlanningService.answer()`, `PlanningService.prepare_workflow()`

- [x] **Step 1: 写入澄清、成功、一次修复、拒绝和预算耗尽测试**

```python
def test_missing_target_version_returns_clarification_without_model_call() -> None:
    result = service.start(request_without_target())
    assert result.kind == "clarification_required"
    assert model.call_count == 0


def test_schema_error_is_repaired_only_once() -> None:
    model.script([invalid_json_response(), valid_plan_response()])
    result = service.start(complete_request())
    assert result.kind == "plan_ready"
    assert model.call_count == 2
```

- [x] **Step 2: 运行图测试并确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\integration\test_planning_graph.py -q`

Expected: FAIL

- [x] **Step 3: 实现 TypedDict 状态、节点和条件边**

```python
builder = StateGraph(PlanningState)
builder.add_edge(START, "normalize_request")
builder.add_edge("normalize_request", "check_required_context")
builder.add_conditional_edges("check_required_context", route_after_context)
builder.add_edge("retrieve_knowledge", "generate_plan")
builder.add_conditional_edges("validate_plan", route_after_validation)
builder.add_edge("repair_plan", "validate_repaired_plan")
```

图中不注册执行工具，不配置开放循环，也不启用 LangGraph checkpointer。

- [x] **Step 4: 保存会话、澄清、尝试和计划版本**

`PlanningService.answer()` 必须创建新尝试和新计划版本；旧计划状态改为不可提交。`prepare_workflow()` 重新核对知识快照、工具策略版本和最新计划版本。

- [x] **Step 5: 运行规划图和知识失效测试**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\integration\test_planning_graph.py tests\planning\integration\test_plan_freshness.py -q`

Expected: PASS

- [x] **Step 6: 提交规划 Agent**

```bash
git add src/changepilot/planning tests/planning/integration
git commit -m "feat: add bounded planning agent"
```

### Task 7: 建立订单服务升级端到端验收场景

**Files:**
- Create: `examples/runbooks/order-service-upgrade.md`
- Create: `examples/runbooks/schema-migration.md`
- Create: `examples/runbooks/rollback-policy.md`
- Create: `tests/planning/support.py`
- Create: `tests/planning/acceptance/test_order_upgrade_planning.py`
- Create: `tests/planning/security/test_prompt_injection.py`

**Interfaces:**
- Consumes: `PlanningService`, `PreparedWorkflow`, existing `WorkflowService`
- Produces: 可重复的自然语言到审批屏障验收路径

- [x] **Step 1: 写入端到端失败测试**

```python
def test_order_upgrade_plan_reaches_runtime_approval_barrier(tmp_path) -> None:
    result = planning.start(order_upgrade_request())
    assert result.kind == "plan_ready"
    prepared = planning.prepare_workflow(result.plan.plan_id)
    run_id = runtime.workflow.create_run(dict(prepared.payload))
    runtime.selected_run[0] = run_id
    drive_until_state(runtime, "waiting_approval")
    assert runtime.query.get_run(run_id).pending_approval.step_id == "migrate-schema"
```

- [x] **Step 2: 添加包含来源、版本、可信级别和补偿规范的示例 Runbook**

每份文档使用项目约定的元数据头，订单升级文档引用迁移、部署、健康检查和回滚要求。

- [x] **Step 3: 添加 Prompt 注入文档并验证其不能绕过审批**

```python
assert planning.start(request_with_injected_runbook()).kind == "planning_rejected"
assert model_tools_executed == []
```

- [x] **Step 4: 运行端到端和安全测试**

Run: `.venv\Scripts\python.exe -m pytest tests\planning\acceptance tests\planning\security -q`

Expected: PASS

- [x] **Step 5: 提交验收场景**

```bash
git add examples tests/planning
git commit -m "test: add planning agent acceptance scenario"
```

### Task 8: 文档、可选真实 API 冒烟测试和阶段验证

**Files:**
- Modify: `README.md`
- Create: `docs/architecture/agent-planning-and-knowledge.md`
- Create: `tests/planning/live/test_deepseek_smoke.py`
- Modify: `openspec/changes/add-agent-planning-and-knowledge/tasks.md`

**Interfaces:**
- Produces: 安装、知识更新、Mock 演示、DeepSeek 配置、成本控制和验证说明

- [x] **Step 1: 添加默认跳过的真实 DeepSeek 冒烟测试**

```python
pytestmark = pytest.mark.skipif(
    os.getenv("CHANGEPILOT_RUN_LIVE_LLM") != "1",
    reason="live DeepSeek test is opt-in",
)
```

测试最多调用一次，限制输出 token；缺少 API Key 时跳过，不能回退到无限重试。

- [x] **Step 2: 更新 README 和架构文档**

文档必须包含：

- 默认与 `[retrieval]` 安装方式
- Runbook 摄取和更新命令
- Mock LLM 订单升级演示
- DeepSeek 环境变量和预算上限
- LangGraph 与第一阶段内核职责边界
- 默认测试不会消耗 API 额度

- [x] **Step 3: 运行规划模块定向测试**

Run: `.venv\Scripts\python.exe -m pytest tests\planning -q -p no:cacheprovider`

Expected: PASS，live 测试为 skipped

- [x] **Step 4: 运行全量测试和覆盖率**

Run: `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`

Expected: 原有和新增测试全部 PASS

Run: `.venv\Scripts\python.exe -m pytest --cov=changepilot --cov-report=term-missing --cov-fail-under=85 -q -p no:cacheprovider`

Expected: PASS，statement coverage >= 85%

- [x] **Step 5: 运行迁移、OpenSpec 和差异校验**

Run: `.venv\Scripts\python.exe -m alembic upgrade head`

Expected: exit 0

Run: `openspec validate add-agent-planning-and-knowledge --strict --json --no-interactive`

Expected: `valid: true`

Run: `git diff --check`

Expected: no output

- [x] **Step 6: 勾选 OpenSpec tasks 并提交阶段结果**

确认每个任务具有对应实现和测试证据后，将 `tasks.md` 的 17 项全部勾选。

```bash
git add README.md docs src tests examples alembic pyproject.toml openspec/changes/add-agent-planning-and-knowledge
git commit -m "docs: complete planning agent workflow"
```

## OpenSpec Coverage

- Tasks 1-2 覆盖 OpenSpec 1.1-1.4。
- Tasks 3-4 覆盖 OpenSpec 2.1-2.4。
- Tasks 5-6 覆盖 OpenSpec 3.1-3.5。
- Tasks 7-8 覆盖 OpenSpec 4.1-4.4。
