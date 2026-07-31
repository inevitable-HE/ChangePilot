import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BarChart3,
  BookOpenCheck,
  BrainCircuit,
  Check,
  ChevronDown,
  CircleDot,
  Clock3,
  Download,
  FileCheck2,
  Filter,
  FlaskConical,
  GitBranch,
  HelpCircle,
  History,
  Languages,
  ListChecks,
  LoaderCircle,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  X,
  XCircle,
} from "lucide-react";

const CHINESE_COPY = Object.freeze({
  "Operations console": "运营控制台",
  Runs: "运行",
  Evaluations: "评测",
  "{count} active": "{count} 个运行中",
  "{count} approvals": "{count} 个待审批",
  local: "本地",
  "No change runs yet": "暂无变更运行",
  "Submit an isolated order-service upgrade to review the plan and approval boundary.": "提交一个隔离的订单服务升级，审阅执行计划与审批边界。",
  "New change": "新建变更",
  "Structured request": "结构化请求",
  Close: "关闭",
  "Change summary": "变更说明",
  "Upgrade the order service and database schema.": "升级订单服务与数据库 Schema。",
  Service: "服务",
  Version: "版本",
  "Execution scenario": "执行场景",
  "Successful upgrade": "成功升级",
  "Migrate, deploy and validate V2.": "迁移 Schema、部署并验证 V2。",
  "Health failure": "健康检查失败",
  "Compensate service and schema to V1.": "将服务与 Schema 补偿回 V1。",
  "Restart recovery": "重启恢复",
  "Recover a committed migration result.": "恢复已经提交的迁移结果。",
  "Readiness assessment": "就绪度检查",
  "Inspect current service and database state without changes.": "只读检查当前服务和数据库状态，不执行变更。",
  "Assess order service and database readiness.": "评估订单服务与数据库的变更就绪度。",
  "Service and database readiness is reported.": "输出服务与数据库就绪度报告。",
  "Perform read-only checks only.": "仅允许执行只读检查。",
  Cancel: "取消",
  "Create plan": "创建计划",
  Workspace: "工作区",
  "Change runs": "变更运行",
  "All states": "全部状态",
  "Waiting approval": "等待审批",
  Succeeded: "成功",
  Compensated: "已补偿",
  "Manual intervention": "人工处理",
  "Loading runs": "正在加载运行",
  "No runs match this filter.": "没有符合当前筛选条件的运行。",
  "Execution plan": "执行计划",
  "Select a run": "选择一个运行",
  "policy guarded": "策略保护",
  "{count} events": "{count} 条事件",
  entry: "入口",
  "after {steps}": "依赖 {steps}",
  "high risk": "高风险",
  Intent: "目的",
  Validation: "验证",
  Evidence: "证据",
  "No external evidence required.": "无需外部证据。",
  Compensation: "补偿",
  "Recovery required": "需要恢复",
  "Approval gate": "审批门禁",
  "Reconcile unknown result": "核对未知执行结果",
  "High-risk schema change requires operator approval.": "高风险 Schema 变更需要操作者审批。",
  Tool: "工具",
  Binding: "绑定摘要",
  Reject: "拒绝",
  Approve: "批准",
  "Recover run": "恢复运行",
  "Authoritative log": "权威日志",
  "Audit timeline": "审计时间线",
  "Download report": "下载报告",
  "No events recorded.": "暂无审计事件。",
  "Event #{sequence}": "事件 #{sequence}",
  workflow: "工作流",
  "Quality evidence": "质量证据",
  "Evaluation runs": "评测运行",
  "Offline by default": "默认离线",
  "No persisted evaluations": "暂无持久化评测",
  "Run the offline core suite to establish the first comparable baseline.": "运行离线核心套件，建立首个可比较基线。",
  History: "历史记录",
  Dataset: "数据集",
  "safety targets": "安全指标",
  "Evaluation metrics": "评测指标",
  "Regression set": "回归集",
  "Case results": "样例结果",
  "{passed} passed · {failed} failed": "{passed} 个通过 · {failed} 个失败",
  "No errors": "无错误",
  "Budget telemetry": "预算统计",
  "Baseline delta": "基线变化",
  "Initial baseline. No prior run comparison.": "初始基线，暂无历史结果可比较。",
  Regressions: "回归项",
  "No threshold regressions": "没有指标低于阈值",
  "approval effective rate": "审批生效率",
  "compensation success rate": "补偿成功率",
  "completion rate": "端到端完成率",
  "dangerous action block rate": "危险操作阻断率",
  "evidence completeness rate": "证据完整率",
  "forbidden action absence rate": "禁止操作未出现率",
  "idempotent replay rate": "幂等重放成功率",
  "plan schema rate": "计划 Schema 通过率",
  "recovery success rate": "恢复成功率",
  "risk identification rate": "风险识别率",
  "required step coverage": "必需步骤覆盖率",
  "average latency ms": "平均耗时（毫秒）",
  "model calls": "模型调用次数",
  "total tokens": "Token 总量",
  "cache hits": "缓存命中",
  "estimated cost": "预估费用",
  development: "开发集",
  holdout: "保留集",
  "Refresh authoritative state": "刷新权威状态",
  "Reviewed evidence and compensation policy.": "已审阅证据与补偿策略。",
  "Rejected from operations console.": "由运营控制台拒绝。",
  "Capture the deployed service version before making changes.": "在变更前记录当前部署的服务版本。",
  "Service reports the expected V1 baseline.": "服务返回预期的 V1 基线。",
  "Capture the schema version and immutable database fingerprint.": "记录 Schema 版本与不可变数据库指纹。",
  "Schema reports V1 and a valid precondition fingerprint.": "Schema 返回 V1 及有效的前置条件指纹。",
  "Verify the service and database form a supported upgrade baseline.": "确认服务与数据库构成受支持的升级基线。",
  "All compatibility and migration prerequisites pass.": "全部兼容性与迁移前置条件通过。",
  "Apply the versioned V1 to V2 order-schema migration.": "执行版本化的订单 Schema V1 到 V2 迁移。",
  "Migration ledger records one committed V2 transition.": "迁移台账记录一次已提交的 V2 迁移。",
  "Switch the isolated order service to the V2 contract.": "将隔离订单服务切换到 V2 契约。",
  "V2 starts only after the V2 schema is available.": "仅在 V2 Schema 可用后启动 V2 服务。",
  "Check service and database compatibility after deployment.": "部署后检查服务与数据库兼容性。",
  "The V2 health contract returns success.": "V2 健康检查契约返回成功。",
  "Exercise order reads and the versioned business contract.": "验证订单读取与版本化业务契约。",
  "Orders remain readable and the V2 contract is valid.": "订单保持可读，且 V2 契约有效。",
  "V2 health and order smoke checks pass": "V2 健康检查和订单冒烟测试通过",
  "Require approval before schema migration": "Schema 迁移前必须经过审批",
  "Current decision": "当前结论",
  "Next action": "下一步",
  "Change target": "变更目标",
  "Success condition": "成功条件",
  Constraints: "约束条件",
  "Active safeguards": "已生效的安全控制",
  "Agent planning trace": "Agent 规划轨迹",
  "RAG evidence": "RAG 检索证据",
  "The request was normalized, grounded in runbooks, converted into a tool-bound plan, and validated before execution.": "请求经过规范化、操作手册检索、工具绑定计划生成和执行前校验。",
  "Planning stages": "规划阶段",
  "Validation passed": "校验通过",
  "Model calls": "模型调用",
  "Tokens": "Token",
  "Plan version": "计划版本",
  "Knowledge snapshot": "知识快照",
  "No retrieved evidence was recorded.": "未记录检索证据。",
  "lexical rank {rank}": "关键词排序 #{rank}",
  "vector rank {rank}": "向量排序 #{rank}",
  "normalize request": "规范化请求",
  "check required context": "检查必要上下文",
  "retrieve knowledge": "检索知识",
  "generate plan": "生成计划",
  "validate plan": "校验计划",
  "repair plan": "修复计划",
  "validate repaired plan": "校验修复后计划",
  "Waiting for migration approval": "等待数据库迁移审批",
  "The requested change completed successfully": "请求的变更已成功完成",
  "The failed change was compensated": "失败的变更已完成补偿",
  "Execution result must be reconciled": "需要核对未知执行结果",
  "The change was cancelled": "变更已取消",
  "Manual attention is required": "需要人工处理",
  "The guarded workflow is executing": "受保护的工作流正在执行",
  "Review migration evidence, then approve or reject the high-risk step.": "审阅迁移证据，然后批准或拒绝高风险步骤。",
  "Download the terminal report and review the final audit record.": "下载终态报告，并审阅最终审计记录。",
  "Inspect the original failure and reverse compensation evidence.": "检查原始失败原因与反向补偿证据。",
  "Recover the run so the engine can probe the committed effect without repeating it.": "恢复运行，由引擎探测已提交副作用，避免重复执行。",
  "Review the rejection evidence; no protected side effect was executed.": "审阅拒绝证据；受保护的副作用没有执行。",
  "Inspect read-only diagnostics before choosing a manual remediation.": "先检查只读诊断证据，再决定人工处置方案。",
  "Follow authoritative events while the workflow advances automatically.": "工作流自动推进时，关注权威审计事件。",
  Request: "请求",
  "Plan review": "计划审阅",
  Precheck: "前置检查",
  Approval: "审批",
  Execution: "执行",
  Verification: "验证",
  Outcome: "结果",
  complete: "完成",
  current: "当前",
  attention: "需关注",
  "Local sandbox only": "仅限本地沙箱",
  "Fixed tool contracts": "固定工具契约",
  "Approval before migration": "迁移前强制审批",
  "Idempotent side effects": "副作用幂等",
  "Reverse compensation available": "支持反向补偿",
  "Durable audit trail": "持久化审计轨迹",
  English: "English",
  Chinese: "中文",
  "Switch language": "切换语言",
  Guide: "使用指南",
  "How ChangePilot works": "ChangePilot 如何工作",
  "ChangePilot turns a service and database change into a reviewable, guarded, and auditable workflow. This demo runs only against an isolated local sandbox.": "ChangePilot 将服务与数据库变更转化为可审阅、受保护、可审计的工作流。本演示仅在隔离的本地沙箱中运行。",
  "Create a change": "创建变更",
  "Choose a demo scenario and describe the intended outcome.": "选择演示场景，并描述期望结果。",
  "Review the generated plan": "审阅生成的计划",
  "Check dependencies, evidence, validation intent, risk, and compensation before execution.": "执行前检查依赖、证据、验证目标、风险和补偿方案。",
  "Decide at the approval gate": "在审批门禁做出决定",
  "The schema migration cannot run until an operator approves its bound tool and arguments.": "只有操作者批准已绑定的工具和参数后，Schema 迁移才会执行。",
  "Follow the outcome": "跟踪执行结果",
  "Read the current decision and next action; inspect compensation or recover an unknown result when needed.": "查看当前结论和下一步；必要时检查补偿，或恢复未知执行结果。",
  "What to try": "建议体验",
  "Success completes migration, deployment, verification, and reporting.": "成功场景完成迁移、部署、验证和报告。",
  "Health failure demonstrates reverse compensation to V1.": "健康检查失败场景演示反向补偿回 V1。",
  "Restart recovery demonstrates reconciliation without applying the migration twice.": "重启恢复场景演示在不重复迁移的前提下核对执行结果。",
  "Current boundary": "当前边界",
  "This is a control-plane prototype, not a production deployment platform. Real use still needs enterprise identity, authorization, secrets, infrastructure adapters, backups, and observability.": "这是控制平面原型，并非生产发布平台。真实使用仍需接入企业身份认证、权限、密钥、基础设施适配器、备份和可观测能力。",
  "Start guided demo": "开始引导演示",
  pending: "待处理",
  ready: "就绪",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
  compensated: "已补偿",
  compensation_failed: "补偿失败",
  manual_intervention: "人工处理",
  waiting_approval: "等待审批",
  result_unknown: "结果未知",
  compensating: "补偿中",
});

const LanguageContext = createContext({
  language: "en",
  setLanguage: () => {},
  t: (text) => text,
});

function interpolate(text, values = {}) {
  return Object.entries(values).reduce(
    (result, [key, value]) => result.replaceAll(`{${key}}`, String(value)),
    text,
  );
}

function useLanguage() {
  return useContext(LanguageContext);
}

const TERMINAL = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "compensated",
  "compensation_failed",
  "manual_intervention",
]);

const GUIDANCE_HEADLINES = {
  awaiting_approval: "Waiting for migration approval",
  change_completed: "The requested change completed successfully",
  change_compensated: "The failed change was compensated",
  recovery_needed: "Execution result must be reconciled",
  change_cancelled: "The change was cancelled",
  manual_attention: "Manual attention is required",
  execution_in_progress: "The guarded workflow is executing",
};

const GUIDANCE_ACTIONS = {
  review_migration_approval: "Review migration evidence, then approve or reject the high-risk step.",
  download_run_report: "Download the terminal report and review the final audit record.",
  inspect_failure_and_compensation: "Inspect the original failure and reverse compensation evidence.",
  recover_unknown_result: "Recover the run so the engine can probe the committed effect without repeating it.",
  review_rejection_audit: "Review the rejection evidence; no protected side effect was executed.",
  inspect_diagnostic_evidence: "Inspect read-only diagnostics before choosing a manual remediation.",
  monitor_authoritative_events: "Follow authoritative events while the workflow advances automatically.",
};

const STAGE_LABELS = {
  request: "Request",
  plan: "Plan review",
  precheck: "Precheck",
  approval: "Approval",
  execution: "Execution",
  verification: "Verification",
  outcome: "Outcome",
};

const SAFETY_LABELS = {
  local_sandbox: "Local sandbox only",
  fixed_tool_contracts: "Fixed tool contracts",
  approval_before_migration: "Approval before migration",
  idempotent_effects: "Idempotent side effects",
  compensation_available: "Reverse compensation available",
  audit_persisted: "Durable audit trail",
};

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function toneFor(state = "") {
  if (state === "succeeded") return "success";
  if (state === "compensated") return "cyan";
  if (state.includes("fail") || state === "manual_intervention") return "danger";
  if (state === "waiting_approval" || state === "result_unknown") return "warning";
  if (state === "running" || state === "compensating") return "active";
  return "neutral";
}

function Status({ value }) {
  const { t } = useLanguage();
  return <span className={`status status-${toneFor(value)}`}>{t(value, {}, value.replaceAll("_", " "))}</span>;
}

function IconButton({ label, children, ...props }) {
  return (
    <button className="icon-button" title={label} aria-label={label} {...props}>
      {children}
    </button>
  );
}

function EmptyState({ onCreate }) {
  const { t } = useLanguage();
  return (
    <section className="empty-state">
      <div className="empty-icon"><GitBranch size={24} /></div>
      <h2>{t("No change runs yet")}</h2>
      <p>{t("Submit an isolated order-service upgrade to review the plan and approval boundary.")}</p>
      <button className="button button-primary" onClick={onCreate}>
        <Plus size={16} /> {t("New change")}
      </button>
    </section>
  );
}

function GuideDialog({ open, onClose, onStart }) {
  const { t } = useLanguage();
  if (!open) return null;
  const steps = [
    ["1", "Create a change", "Choose a demo scenario and describe the intended outcome."],
    ["2", "Review the generated plan", "Check dependencies, evidence, validation intent, risk, and compensation before execution."],
    ["3", "Decide at the approval gate", "The schema migration cannot run until an operator approves its bound tool and arguments."],
    ["4", "Follow the outcome", "Read the current decision and next action; inspect compensation or recover an unknown result when needed."],
  ];
  return (
    <div className="dialog-backdrop" role="presentation">
      <section className="dialog guide-dialog" role="dialog" aria-modal="true" aria-labelledby="guide-title">
        <header className="dialog-header">
          <div>
            <span className="eyebrow">ChangePilot</span>
            <h2 id="guide-title">{t("How ChangePilot works")}</h2>
          </div>
          <IconButton label={t("Close")} type="button" onClick={onClose}><X size={18} /></IconButton>
        </header>
        <p className="guide-intro">{t("ChangePilot turns a service and database change into a reviewable, guarded, and auditable workflow. This demo runs only against an isolated local sandbox.")}</p>
        <ol className="guide-steps">
          {steps.map(([number, title, detail]) => (
            <li key={number}>
              <span>{number}</span>
              <div><strong>{t(title)}</strong><p>{t(detail)}</p></div>
            </li>
          ))}
        </ol>
        <section className="guide-scenarios">
          <span className="eyebrow">{t("What to try")}</span>
          <ul>
            <li><Check size={14} /> {t("Success completes migration, deployment, verification, and reporting.")}</li>
            <li><RotateCcw size={14} /> {t("Health failure demonstrates reverse compensation to V1.")}</li>
            <li><RefreshCw size={14} /> {t("Restart recovery demonstrates reconciliation without applying the migration twice.")}</li>
          </ul>
        </section>
        <aside className="guide-boundary">
          <AlertTriangle size={17} />
          <div><strong>{t("Current boundary")}</strong><p>{t("This is a control-plane prototype, not a production deployment platform. Real use still needs enterprise identity, authorization, secrets, infrastructure adapters, backups, and observability.")}</p></div>
        </aside>
        <footer className="dialog-actions">
          <button type="button" className="button" onClick={onClose}>{t("Close")}</button>
          <button type="button" className="button button-primary" onClick={onStart}><Play size={16} /> {t("Start guided demo")}</button>
        </footer>
      </section>
    </div>
  );
}

function NewChangeDialog({ open, onClose, onCreated }) {
  const { t } = useLanguage();
  const [scenario, setScenario] = useState("success");
  const [summary, setSummary] = useState(() => t("Upgrade the order service and database schema."));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) {
      setScenario("success");
      setSummary(t("Upgrade the order service and database schema."));
    }
  }, [open, t]);
  if (!open) return null;

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const readiness = scenario === "readiness";
      const result = await api("/api/requests", {
        method: "POST",
        body: JSON.stringify({
          service_id: "order-service",
          current_version: "v1",
          target_version: readiness ? "v1" : "v2",
          change_summary: summary,
          success_conditions: [t(readiness ? "Service and database readiness is reported." : "V2 health and order smoke checks pass")],
          constraints: [t(readiness ? "Perform read-only checks only." : "Require approval before schema migration")],
          scenario,
        }),
      });
      if (!result.run_id) throw new Error(result.errors?.join("; ") || "Request needs clarification");
      onCreated(result.run_id);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation">
      <form className="dialog" onSubmit={submit}>
        <header className="dialog-header">
          <div>
            <span className="eyebrow">{t("Structured request")}</span>
            <h2>{t("New change")}</h2>
          </div>
          <IconButton label={t("Close")} type="button" onClick={onClose}><X size={18} /></IconButton>
        </header>
        <label>
          {t("Change summary")}
          <textarea value={summary} onChange={(event) => setSummary(event.target.value)} rows={3} />
        </label>
        <div className="fixed-fields">
          <label>{t("Service")}<input value="order-service" readOnly /></label>
          <label>{t("Version")}<input value={scenario === "readiness" ? "v1 (read only)" : "v1 → v2"} readOnly /></label>
        </div>
        <fieldset>
          <legend>{t("Execution scenario")}</legend>
          {[
            ["success", "Successful upgrade", "Migrate, deploy and validate V2."],
            ["compensation", "Health failure", "Compensate service and schema to V1."],
            ["recovery", "Restart recovery", "Recover a committed migration result."],
            ["readiness", "Readiness assessment", "Inspect current service and database state without changes."],
          ].map(([value, title, detail]) => (
            <label className="scenario-option" key={value}>
              <input
                type="radio"
                name="scenario"
                value={value}
                checked={scenario === value}
                onChange={() => {
                  setScenario(value);
                  setSummary(t(value === "readiness" ? "Assess order service and database readiness." : "Upgrade the order service and database schema."));
                }}
              />
              <span><strong>{t(title)}</strong><small>{t(detail)}</small></span>
            </label>
          ))}
        </fieldset>
        {error && <div className="inline-error"><AlertTriangle size={16} />{error}</div>}
        <footer className="dialog-actions">
          <button type="button" className="button" onClick={onClose}>{t("Cancel")}</button>
          <button className="button button-primary" disabled={busy || !summary.trim()}>
            {busy ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
            {t("Create plan")}
          </button>
        </footer>
      </form>
    </div>
  );
}

function RunList({ runs, selected, filter, setFilter, onSelect, onCreate, loading }) {
  const { t } = useLanguage();
  const filtered = runs.filter((run) => filter === "all" || run.state === filter);
  return (
    <aside className="run-panel">
      <div className="panel-heading">
        <div><span className="eyebrow">{t("Workspace")}</span><h2>{t("Change runs")}</h2></div>
        <IconButton label={t("New change")} onClick={onCreate}><Plus size={18} /></IconButton>
      </div>
      <div className="filter-row">
        <Filter size={14} />
        <select value={filter} onChange={(event) => setFilter(event.target.value)}>
          <option value="all">{t("All states")}</option>
          <option value="waiting_approval">{t("Waiting approval")}</option>
          <option value="succeeded">{t("Succeeded")}</option>
          <option value="compensated">{t("Compensated")}</option>
          <option value="manual_intervention">{t("Manual intervention")}</option>
        </select>
      </div>
      <div className="run-list">
        {loading && <div className="panel-loading"><LoaderCircle className="spin" size={18} /> {t("Loading runs")}</div>}
        {!loading && filtered.length === 0 && <p className="quiet">{t("No runs match this filter.")}</p>}
        {filtered.map((run) => (
          <button className={`run-item ${selected === run.run_id ? "selected" : ""}`} key={run.run_id} onClick={() => onSelect(run.run_id)}>
            <div className="run-item-top"><strong>{run.definition_id}</strong><Status value={run.state} /></div>
            <span className="mono">{run.run_id.slice(0, 8)}</span>
            <div className="run-progress"><span style={{ width: `${Math.min(100, (run.last_event_sequence / 44) * 100)}%` }} /></div>
          </button>
        ))}
      </div>
    </aside>
  );
}

function RunOrientation({ guidance }) {
  const { t } = useLanguage();
  if (!guidance) return <div className="orientation-loading"><LoaderCircle className="spin" size={17} /></div>;
  return (
    <section className="run-orientation">
      <div className="journey-rail" aria-label={t("Execution plan")}>
        {guidance.stages.map((stage) => (
          <div className={`journey-stage journey-${stage.state}`} key={stage.stage_id}>
            <span className="journey-marker">{stage.state === "complete" ? <Check size={13} /> : stage.state === "attention" ? <AlertTriangle size={12} /> : <CircleDot size={12} />}</span>
            <span>{t(STAGE_LABELS[stage.stage_id])}</span>
            <small>{t(stage.state)}</small>
          </div>
        ))}
      </div>
      <div className="orientation-body">
        <div className="decision-summary">
          <span className="eyebrow">{t("Current decision")}</span>
          <h2>{t(GUIDANCE_HEADLINES[guidance.headline_code], {}, guidance.headline_code)}</h2>
          <p><ArrowRight size={15} /> <span><strong>{t("Next action")}:</strong> {t(GUIDANCE_ACTIONS[guidance.next_action_code], {}, guidance.next_action_code)}</span></p>
        </div>
        <dl className="change-context">
          <div><dt>{t("Change target")}</dt><dd><strong>{guidance.service_id}</strong><span>{guidance.current_version} → {guidance.target_version}</span></dd></div>
          <div><dt>{t("Change summary")}</dt><dd>{guidance.goal}</dd></div>
          <div><dt>{t("Success condition")}</dt><dd>{guidance.success_conditions.join(" · ") || "—"}</dd></div>
          <div><dt>{t("Constraints")}</dt><dd>{guidance.constraints.join(" · ") || "—"}</dd></div>
        </dl>
      </div>
      <div className="safeguard-strip">
        <span className="eyebrow">{t("Active safeguards")}</span>
        <div>{guidance.safety_controls.map((control) => <span key={control}><ShieldCheck size={13} /> {t(SAFETY_LABELS[control], {}, control)}</span>)}</div>
      </div>
    </section>
  );
}

function PlanningTracePanel({ planning }) {
  const { t } = useLanguage();
  if (!planning) return <div className="planning-loading"><LoaderCircle className="spin" size={17} /></div>;
  return (
    <section className="planning-trace">
      <header className="planning-heading">
        <div className="planning-title">
          <BrainCircuit size={18} />
          <div>
            <span className="eyebrow">{t("Agent planning trace")}</span>
            <p>{t("The request was normalized, grounded in runbooks, converted into a tool-bound plan, and validated before execution.")}</p>
          </div>
        </div>
        <Status value={planning.validation_status === "passed" ? "succeeded" : "failed"} />
      </header>
      <dl className="planning-metrics">
        <div><dt>{t("Plan version")}</dt><dd>v{planning.plan_version ?? "—"}</dd></div>
        <div><dt>{t("Model calls")}</dt><dd>{planning.model_usage.calls}</dd></div>
        <div><dt>{t("Tokens")}</dt><dd>{planning.model_usage.total_tokens}</dd></div>
        <div><dt>{t("Knowledge snapshot")}</dt><dd className="mono">{planning.knowledge_snapshot_digest.slice(0, 10) || "—"}</dd></div>
      </dl>
      <div className="planning-stages">
        <span className="eyebrow">{t("Planning stages")}</span>
        <div>
          {planning.stages.map((stage) => (
            <span key={stage}><Check size={12} /> {t(stage.replaceAll("_", " "))}</span>
          ))}
        </div>
      </div>
      <div className="evidence-list">
        <span className="eyebrow"><BookOpenCheck size={13} /> {t("RAG evidence")} ({planning.evidence.length})</span>
        {planning.evidence.length === 0 && <p className="quiet">{t("No retrieved evidence was recorded.")}</p>}
        {planning.evidence.slice(0, 3).map((evidence) => (
          <article key={evidence.chunk_id}>
            <div>
              <strong>{evidence.document_id}@{evidence.document_version}</strong>
              <code>{evidence.location}</code>
            </div>
            <p>{evidence.excerpt}</p>
            <small>
              {evidence.lexical_rank && t("lexical rank {rank}", { rank: evidence.lexical_rank })}
              {evidence.vector_rank && ` · ${t("vector rank {rank}", { rank: evidence.vector_rank })}`}
            </small>
          </article>
        ))}
      </div>
    </section>
  );
}

function PlanWorkspace({ snapshot, plan, planning, guidance, activeStep, setActiveStep }) {
  const { t } = useLanguage();
  const stateById = new Map(snapshot?.steps?.map((step) => [step.step_id, step.state]));
  return (
    <main className="plan-workspace">
      <header className="workspace-heading">
        <div>
          <span className="eyebrow">{t("Execution plan")}</span>
          <h1>{snapshot?.summary.definition_id || t("Select a run")}</h1>
        </div>
        {snapshot && <Status value={snapshot.summary.state} />}
      </header>
      <div className="plan-meta">
        <span><GitBranch size={15} /> v{plan?.definition_version ?? "—"}</span>
        <span><ShieldCheck size={15} /> {t("policy guarded")}</span>
        <span><History size={15} /> {t("{count} events", { count: snapshot?.summary.last_event_sequence ?? 0 })}</span>
      </div>
      <RunOrientation guidance={guidance} />
      <PlanningTracePanel planning={planning} />
      <section className="step-list" aria-label={t("Execution plan")}>
        {plan?.steps.map((step, index) => {
          const state = stateById.get(step.step_id) || "pending";
          return (
            <button className={`step-row ${activeStep === step.step_id ? "active" : ""}`} key={step.step_id} onClick={() => setActiveStep(step.step_id)}>
              <span className={`step-index step-${toneFor(state)}`}>
                {state === "succeeded" ? <Check size={15} /> : index + 1}
              </span>
              <span className="step-copy">
                <span className="step-title"><strong>{step.step_id}</strong><Status value={state} /></span>
                <span>{step.tool.name}</span>
              </span>
              <span className="step-deps">{step.depends_on.length ? t("after {steps}", { steps: step.depends_on.join(", ") }) : t("entry")}</span>
              {step.approval_required && <span className="risk-flag"><AlertTriangle size={14} /> {t("high risk")}</span>}
              <ChevronDown size={16} />
            </button>
          );
        })}
      </section>
      {activeStep && plan && <StepDetail step={plan.steps.find((item) => item.step_id === activeStep)} />}
    </main>
  );
}

function StepDetail({ step }) {
  const { t } = useLanguage();
  if (!step) return null;
  return (
    <section className="step-detail">
      <div className="detail-column">
        <span className="eyebrow">{t("Intent")}</span>
        <p>{t(step.rationale)}</p>
        <span className="eyebrow">{t("Validation")}</span>
        <p>{t(step.validation_intent)}</p>
      </div>
      <div className="detail-column">
        <span className="eyebrow">{t("Evidence")}</span>
        {step.evidence_refs.length ? step.evidence_refs.map((ref) => <code key={ref}>{ref}</code>) : <p className="quiet">{t("No external evidence required.")}</p>}
        {step.compensation_tool && <><span className="eyebrow">{t("Compensation")}</span><p><RotateCcw size={14} /> {step.compensation_tool.name}</p></>}
      </div>
    </section>
  );
}

function ApprovalPanel({ snapshot, onDecision, onRecover, busy }) {
  const { t } = useLanguage();
  const approval = snapshot?.pending_approval;
  const recoverable = snapshot?.steps.some((step) => step.state === "result_unknown");
  if (!approval && !recoverable) return null;
  return (
    <section className="approval-panel">
      <div className="approval-title"><ShieldCheck size={18} /><div><span className="eyebrow">{t(recoverable ? "Recovery required" : "Approval gate")}</span><h3>{recoverable ? t("Reconcile unknown result") : approval.step_id}</h3></div></div>
      {approval && <>
        <p>{approval.risk_reasons.join(" · ") || t("High-risk schema change requires operator approval.")}</p>
        <dl><dt>{t("Tool")}</dt><dd>{approval.tool_name}@{approval.tool_version}</dd><dt>{t("Binding")}</dt><dd className="mono">{approval.binding_digest.slice(0, 12)}…</dd></dl>
        <div className="approval-actions">
          <button className="button button-danger" disabled={busy} onClick={() => onDecision("rejected")}><XCircle size={16} /> {t("Reject")}</button>
          <button className="button button-primary" disabled={busy} onClick={() => onDecision("approved")}><Check size={16} /> {t("Approve")}</button>
        </div>
      </>}
      {recoverable && <button className="button button-primary full" disabled={busy} onClick={onRecover}><RefreshCw size={16} /> {t("Recover run")}</button>}
    </section>
  );
}

function AuditPanel({ snapshot, events, selectedEvent, setSelectedEvent, onDecision, onRecover, busy }) {
  const { t } = useLanguage();
  return (
    <aside className="audit-panel">
      <ApprovalPanel snapshot={snapshot} onDecision={onDecision} onRecover={onRecover} busy={busy} />
      <div className="panel-heading audit-heading">
        <div><span className="eyebrow">{t("Authoritative log")}</span><h2>{t("Audit timeline")}</h2></div>
        {snapshot && TERMINAL.has(snapshot.summary.state) && <a className="icon-button" title={t("Download report")} aria-label={t("Download report")} href={`/api/runs/${snapshot.summary.run_id}/report.md`} target="_blank"><Download size={17} /></a>}
      </div>
      <div className="audit-list">
        {events.length === 0 && <p className="quiet">{t("No events recorded.")}</p>}
        {[...events].reverse().map((event) => (
          <button className={`audit-event ${selectedEvent?.sequence === event.sequence ? "selected" : ""}`} key={event.sequence} onClick={() => setSelectedEvent(event)}>
            <span className="event-line" />
            <span className="event-dot"><CircleDot size={13} /></span>
            <span className="event-content"><strong>{event.event_type}</strong><small>#{event.sequence} · {event.step_id || t("workflow")}</small></span>
          </button>
        ))}
      </div>
      {selectedEvent && <div className="event-inspector"><span className="eyebrow">{t("Event #{sequence}", { sequence: selectedEvent.sequence })}</span><pre>{JSON.stringify(selectedEvent.payload, null, 2)}</pre></div>}
    </aside>
  );
}

function EvaluationView({ items }) {
  const { t } = useLanguage();
  const [selectedId, setSelectedId] = useState(null);
  const selected = items.find((item) => item.run_id === selectedId) || items[0];
  const percentageMetrics = Object.entries(selected?.aggregate_metrics || {})
    .filter(([name]) => name.endsWith("_rate"));
  const operationalMetrics = Object.entries(selected?.aggregate_metrics || {})
    .filter(([name]) => !name.endsWith("_rate"));
  const failedCases = selected?.case_results.filter((item) => !item.passed) || [];

  return (
    <section className="evaluation-view">
      <header className="evaluation-heading">
        <div><span className="eyebrow">{t("Quality evidence")}</span><h1>{t("Evaluation runs")}</h1></div>
        <div className="evaluation-mode"><FlaskConical size={15} /> {t("Offline by default")}</div>
      </header>
      {items.length === 0 ? (
        <div className="empty-state compact">
          <FileCheck2 size={28} />
          <h2>{t("No persisted evaluations")}</h2>
          <p>{t("Run the offline core suite to establish the first comparable baseline.")}</p>
        </div>
      ) : (
        <div className="evaluation-layout">
          <aside className="evaluation-history">
            <span className="eyebrow">{t("History")}</span>
            {items.map((item) => (
              <button
                className={`evaluation-run ${selected?.run_id === item.run_id ? "selected" : ""}`}
                key={item.run_id}
                onClick={() => setSelectedId(item.run_id)}
              >
                <span><strong>{item.metadata.dataset_id}</strong><Status value={item.passed ? "succeeded" : "failed"} /></span>
                <small>v{item.metadata.dataset_version} · {item.metadata.code_version}</small>
                <code>{item.run_id.slice(0, 8)}</code>
              </button>
            ))}
          </aside>
          <main className="evaluation-result">
            <div className="evaluation-summary">
              <div>
                <span className="eyebrow">{t("Dataset")}</span>
                <h2>{selected.metadata.dataset_id}@{selected.metadata.dataset_version}</h2>
                <p>{selected.metadata.model} · metrics {selected.metadata.metrics_version}</p>
              </div>
              <div className="quality-score">
                <BarChart3 size={19} />
                <strong>{percentageMetrics.filter(([, value]) => value >= 1).length}/{percentageMetrics.length}</strong>
                <span>{t("safety targets")}</span>
              </div>
            </div>
            <section className="metric-grid" aria-label={t("Evaluation metrics")}>
              {percentageMetrics.map(([name, value]) => (
                <div className="metric-item" key={name}>
                  <span>{t(name.replaceAll("_", " "))}</span>
                  <strong>{Math.round(value * 100)}%</strong>
                  <div><i style={{ width: `${Math.min(100, value * 100)}%` }} /></div>
                </div>
              ))}
            </section>
            <section className="evaluation-section">
              <div className="section-title">
                <div><span className="eyebrow">{t("Regression set")}</span><h2>{t("Case results")}</h2></div>
                <span>{t("{passed} passed · {failed} failed", { passed: selected.case_results.length - failedCases.length, failed: failedCases.length })}</span>
              </div>
              <div className="case-table">
                {selected.case_results.map((item) => (
                  <div className="case-row" key={item.case_id}>
                    <Status value={item.passed ? "succeeded" : "failed"} />
                    <strong>{item.case_id}</strong>
                    <span>{t(item.split)}</span>
                    <span>{item.latency_ms} ms</span>
                    <span>{item.errors.join("; ") || t("No errors")}</span>
                  </div>
                ))}
              </div>
            </section>
            <section className="evaluation-footer">
              <div>
                <span className="eyebrow">{t("Budget telemetry")}</span>
                {operationalMetrics.map(([name, value]) => (
                  <p key={name}><span>{t(name.replaceAll("_", " "))}</span><strong>{value}</strong></p>
                ))}
              </div>
              <div>
                <span className="eyebrow">{t("Baseline delta")}</span>
                {Object.keys(selected.baseline_delta).length === 0 ? <p className="quiet">{t("Initial baseline. No prior run comparison.")}</p> :
                  Object.entries(selected.baseline_delta).map(([name, value]) => (
                    <p key={name}><span>{t(name.replaceAll("_", " "))}</span><strong className={value < 0 ? "negative" : ""}>{value > 0 ? "+" : ""}{value}</strong></p>
                  ))}
              </div>
              <div>
                <span className="eyebrow">{t("Regressions")}</span>
                {selected.regressions.length === 0 ? <p className="evaluation-pass"><Check size={15} /> {t("No threshold regressions")}</p> :
                  selected.regressions.map((name) => <p className="negative" key={name}>{t(name.replaceAll("_", " "))}</p>)}
              </div>
            </section>
          </main>
        </div>
      )}
    </section>
  );
}

export default function App() {
  const [language, setLanguage] = useState(() => {
    const stored = window.localStorage.getItem("changepilot-language");
    if (stored === "en" || stored === "zh") return stored;
    return window.navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
  });
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState(null);
  const selectedRef = useRef(null);
  const [snapshot, setSnapshot] = useState(null);
  const [plan, setPlan] = useState(null);
  const [planning, setPlanning] = useState(null);
  const [guidance, setGuidance] = useState(null);
  const [events, setEvents] = useState([]);
  const [activeStep, setActiveStep] = useState(null);
  const [selectedEvent, setSelectedEvent] = useState(null);
  const [filter, setFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState(false);
  const [guide, setGuide] = useState(
    () => window.localStorage.getItem("changepilot-guide-seen") !== "1",
  );
  const [view, setView] = useState("runs");
  const [evaluations, setEvaluations] = useState([]);
  const t = useCallback((text, values = {}, fallback = text) => {
    const translated = language === "zh" ? CHINESE_COPY[text] ?? fallback : fallback;
    return interpolate(translated, values);
  }, [language]);
  const languageContext = useMemo(
    () => ({ language, setLanguage, t }),
    [language, t],
  );

  const loadRuns = useCallback(async () => {
    try {
      const data = await api("/api/runs");
      setRuns(data);
      setSelected((current) => current || data[0]?.run_id || null);
    } catch (failure) { setError(failure.message); }
    finally { setLoading(false); }
  }, []);

  const loadSelected = useCallback(async (runId) => {
    if (!runId) return;
    try {
      const [nextSnapshot, nextPlan, nextPlanning, nextGuidance, nextEvents] = await Promise.all([
        api(`/api/runs/${runId}`),
        api(`/api/runs/${runId}/plan`),
        api(`/api/runs/${runId}/planning`),
        api(`/api/runs/${runId}/guidance`),
        api(`/api/runs/${runId}/events`),
      ]);
      if (selectedRef.current !== runId) return;
      setSnapshot(nextSnapshot);
      setPlan(nextPlan);
      setPlanning(nextPlanning);
      setGuidance(nextGuidance);
      setEvents(nextEvents.events);
      setActiveStep(nextPlan.steps[0]?.step_id || null);
      setError("");
    } catch (failure) { setError(failure.message); }
  }, []);

  useEffect(() => { loadRuns(); api("/api/evaluations").then(setEvaluations).catch(() => {}); }, [loadRuns]);
  useEffect(() => {
    selectedRef.current = selected;
    setGuidance(null);
    setPlanning(null);
    loadSelected(selected);
  }, [selected, loadSelected]);
  useEffect(() => {
    window.localStorage.setItem("changepilot-language", language);
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    document.title = language === "zh" ? "ChangePilot 运营控制台" : "ChangePilot Operations";
  }, [language]);
  useEffect(() => {
    if (!selected || snapshot && TERMINAL.has(snapshot.summary.state)) return undefined;
    const cursor = events.at(-1)?.sequence || 0;
    const source = new EventSource(`/api/runs/${selected}/events/stream?after=${cursor}`);
    source.addEventListener("audit", () => { loadSelected(selected); loadRuns(); });
    source.onerror = () => source.close();
    const fallback = window.setInterval(() => loadSelected(selected), 4000);
    return () => { source.close(); window.clearInterval(fallback); };
  }, [selected, snapshot?.summary.state, events.length, loadSelected, loadRuns]);

  async function decide(decision) {
    setBusy(true);
    try {
      const approval = snapshot.pending_approval;
      await api(`/api/runs/${selected}/approval`, {
        method: "POST",
        body: JSON.stringify({
          decision,
          expected_version: approval.version,
          binding_digest: approval.binding_digest,
          actor: "console-operator",
          reason: t(decision === "approved" ? "Reviewed evidence and compensation policy." : "Rejected from operations console."),
        }),
      });
      await Promise.all([loadSelected(selected), loadRuns()]);
    } catch (failure) { setError(`${failure.message}. ${t("Refresh authoritative state")}.`); await loadSelected(selected); }
    finally { setBusy(false); }
  }

  async function recover() {
    setBusy(true);
    try {
      await api(`/api/runs/${selected}/recover`, { method: "POST", body: JSON.stringify({ expected_run_revision: snapshot.summary.revision }) });
      await Promise.all([loadSelected(selected), loadRuns()]);
    } catch (failure) { setError(failure.message); }
    finally { setBusy(false); }
  }

  function closeGuide() {
    window.localStorage.setItem("changepilot-guide-seen", "1");
    setGuide(false);
  }

  const totals = useMemo(() => ({
    active: runs.filter((run) => !TERMINAL.has(run.state)).length,
    approvals: runs.filter((run) => run.pending_approval).length,
  }), [runs]);

  return (
    <LanguageContext.Provider value={languageContext}>
      <div className="app-shell">
        <header className="topbar">
          <div className="brand"><span className="brand-mark">CP</span><div><strong>ChangePilot</strong><small>{t("Operations console")}</small></div></div>
          <nav className="view-tabs">
            <button className={view === "runs" ? "active" : ""} onClick={() => setView("runs")}><Activity size={16} /> {t("Runs")}</button>
            <button className={view === "evaluations" ? "active" : ""} onClick={() => setView("evaluations")}><ListChecks size={16} /> {t("Evaluations")}</button>
          </nav>
          <div className="top-actions">
            <div className="top-stats"><span><Clock3 size={14} /> {t("{count} active", { count: totals.active })}</span><span><ShieldCheck size={14} /> {t("{count} approvals", { count: totals.approvals })}</span><span className="connection"><span /> {t("local")}</span></div>
            <button className="top-help" title={t("Guide")} onClick={() => setGuide(true)}><HelpCircle size={14} /> {t("Guide")}</button>
            <div className="language-switch" role="group" aria-label={t("Switch language")}>
              <Languages size={14} />
              <button className={language === "en" ? "active" : ""} onClick={() => setLanguage("en")} aria-pressed={language === "en"}>{t("English")}</button>
              <button className={language === "zh" ? "active" : ""} onClick={() => setLanguage("zh")} aria-pressed={language === "zh"}>{t("Chinese")}</button>
            </div>
          </div>
        </header>
        {error && <div className="error-banner"><AlertTriangle size={16} /><span>{error}</span><button onClick={() => setError("")}><X size={16} /></button></div>}
        {view === "evaluations" ? <EvaluationView items={evaluations} /> : (
          runs.length === 0 && !loading ? <EmptyState onCreate={() => setDialog(true)} /> :
          <div className="operations-grid">
            <RunList runs={runs} selected={selected} filter={filter} setFilter={setFilter} onSelect={setSelected} onCreate={() => setDialog(true)} loading={loading} />
            <PlanWorkspace snapshot={snapshot} plan={plan} planning={planning} guidance={guidance} activeStep={activeStep} setActiveStep={setActiveStep} />
            <AuditPanel snapshot={snapshot} events={events} selectedEvent={selectedEvent} setSelectedEvent={setSelectedEvent} onDecision={decide} onRecover={recover} busy={busy} />
          </div>
        )}
        <GuideDialog open={guide} onClose={closeGuide} onStart={() => { closeGuide(); setDialog(true); }} />
        <NewChangeDialog open={dialog} onClose={() => setDialog(false)} onCreated={(runId) => { setDialog(false); setSelected(runId); loadRuns(); }} />
      </div>
    </LanguageContext.Provider>
  );
}
