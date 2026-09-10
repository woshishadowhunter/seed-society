"""Consciousness plugin manifest for the Agent Society runtime.

This module names the existing architecture through the Yogacara
(eight-consciousness) lens used by the project: each runtime module is declared
as a plugin with its consciousness role, what it provides, and what it depends
on. The registry is declarative metadata only: it never changes imports,
permissions, budgets, or acceptance criteria.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Consciousness(str, Enum):
    """The eight-consciousness mapping used by this project."""

    ALAYA = "alaya"          # 阿赖耶识：种子库、熏习、现行
    MANAS = "manas"          # 末那识：个体性（我执）、路由与晋升
    MANO = "mano"            # 意识：分别、造作与串联（双循环）
    PANCA = "panca"          # 前五识：根尘相接（工具、网络、工作区）
    SILA = "sila"            # 戒律：预算、审批、围栏、默认拒绝


@dataclass(frozen=True, slots=True)
class PluginManifest:
    name: str
    consciousness: Consciousness
    summary: str
    module: str
    provides: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    kind: str = "runtime"    # runtime | seed | bridge

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "consciousness": self.consciousness.value,
            "kind": self.kind,
            "summary": self.summary,
            "module": self.module,
            "provides": list(self.provides),
            "depends_on": list(self.depends_on),
        }


SOCIETY_MANIFESTS: tuple[PluginManifest, ...] = (
    # ── 阿赖耶识：种子库与熏习 ────────────────────────────────────────────
    PluginManifest(
        "seed-store",
        Consciousness.ALAYA,
        "持久化种子库：SQLite/PostgreSQL 两后端，JSON payload 旁挂索引列",
        "seed_society.storage",
        ("SQLiteRepository", "scheduler_now"),
    ),
    PluginManifest(
        "seed-store-postgres",
        Consciousness.ALAYA,
        "PostgreSQL 执行平面：JSONB + SKIP LOCKED 多主机发现",
        "seed_society.postgres_storage",
        ("PostgreSQLRepository",),
        depends_on=("seed-store",),
    ),
    PluginManifest(
        "seed-memory",
        Consciousness.ALAYA,
        "三类记忆：任务反馈、种子知识、任务型绩效（现行反熏）",
        "seed_society.memory",
        ("MemoryManager",),
        depends_on=("seed-store",),
    ),
    PluginManifest(
        "seed-experience",
        Consciousness.ALAYA,
        "熏习：把 reviewed attempts 蒸馏为可复用的 lessons",
        "seed_society.experience",
        ("ExperienceDistiller",),
        depends_on=("seed-store",),
    ),
    PluginManifest(
        "seed-consolidation",
        Consciousness.ALAYA,
        "睡眠回放巩固：RPE 显著性、遗忘曲线、异熟语义晋升门",
        "seed_society.consolidation",
        ("ConsolidationEngine", "ConsolidationPolicy"),
        depends_on=("seed-store", "seed-experience"),
    ),
    PluginManifest(
        "seed-revisions",
        Consciousness.ALAYA,
        "可审计种子修订史与确定性回滚（逆编辑重建，含必须失败反例）",
        "seed_society.revisions",
        ("SeedRollback", "RollbackReport"),
        depends_on=("seed-store",),
    ),
    PluginManifest(
        "seed-predicates",
        Consciousness.SILA,
        "纯谓词筛查：近重复阻断/相似告警、不可配置凭据筛查、双信号陈旧",
        "seed_society.predicates",
        ("screen_duplicate", "secret_reason", "is_stale"),
        depends_on=("seed-store",),
    ),
    PluginManifest(
        "seed-evolution",
        Consciousness.ALAYA,
        "种子相续：双亲基因组确定性重组出子代候选",
        "seed_society.evolution",
        ("GenomeRecombiner",),
        depends_on=("seed-store",),
    ),
    # ── 末那识：个体性与路由 ────────────────────────────────────────────
    PluginManifest(
        "manas-identity",
        Consciousness.MANAS,
        "个体性契约：AgentProfile/AgentGenome/绩效记录的不可变数据",
        "seed_society.domain",
        ("AgentProfile", "AgentGenome", "PerformanceRecord"),
    ),
    PluginManifest(
        "manas-selection",
        Consciousness.MANAS,
        "可解释的绩效加权路由；champion 缺失即阻塞，绝不静默回退",
        "seed_society.selection",
        ("PerformanceWeightedSelector",),
        depends_on=("manas-identity",),
    ),
    PluginManifest(
        "manas-promotion",
        Consciousness.MANAS,
        "冠军/挑战者门：不可变基准、晋升门槛、身份复检",
        "seed_society.evaluation",
        ("BenchmarkEvaluator", "PromotionPolicy"),
        depends_on=("manas-identity", "seed-store"),
    ),
    # ── 意识：分别、造作与串联 ────────────────────────────────────────────
    PluginManifest(
        "mano-engine",
        Consciousness.MANO,
        "双循环引擎：外循环推进任务图、内循环执行-评审-修复",
        "seed_society.engine",
        ("LoopEngine", "resolve_approval"),
        depends_on=("manas-identity", "manas-selection", "seed-memory"),
    ),
    PluginManifest(
        "mano-deterministic",
        Consciousness.MANO,
        "离线可复现的规划者/专家/评审者与量子杯场景",
        "seed_society.deterministic",
        ("QuantumMugPlanner", "TemplateWorker", "CriteriaReviewer"),
    ),
    PluginManifest(
        "mano-model-agents",
        Consciousness.MANO,
        "严格 JSON 的模型角色适配器（规划/执行/评审）",
        "seed_society.model_agents",
        ("ModelPlanner", "ModelWorker", "ModelReviewer"),
        depends_on=("panca-providers",),
    ),
    PluginManifest(
        "mano-scheduler",
        Consciousness.MANO,
        "租约与围栏契约：会话、claim、单调 fencing token",
        "seed_society.scheduler",
        ("WorkerSession", "TaskClaim", "run_scheduler_self_test"),
    ),
    PluginManifest(
        "mano-worker-service",
        Consciousness.MANO,
        "持久 worker：发现就绪任务、独立续租、审批暂停、优雅排水",
        "seed_society.worker_service",
        ("WorkerService", "WorkerServiceConfig"),
        depends_on=("mano-scheduler", "seed-store"),
    ),
    # ── 前五识：根尘相接 ──────────────────────────────────────────────────
    PluginManifest(
        "panca-providers",
        Consciousness.PANCA,
        "舌识：OpenAI 兼容模型边界（标准库 HTTP、密钥不落盘）",
        "seed_society.providers",
        ("OpenAICompatibleProvider",),
        depends_on=("panca-http",),
    ),
    PluginManifest(
        "panca-http",
        Consciousness.PANCA,
        "鼻识/身识基础：无重定向、限字节、限墙钟的 HTTP 传输",
        "seed_society.http_transport",
        ("post_bytes", "HTTPTransportError"),
    ),
    PluginManifest(
        "panca-tools",
        Consciousness.PANCA,
        "工具门：发现、schema 校验、风险策略与审批执行",
        "seed_society.tools",
        ("ToolRegistry", "ToolExecutor", "DefaultToolPolicy"),
        depends_on=("sila-approval",),
    ),
    PluginManifest(
        "panca-mcp",
        Consciousness.PANCA,
        "MCP stdio 适配：有界传输、工具发现、本地风险分级",
        "seed_society.mcp",
        ("MCPStdioClient", "MCPToolAdapter"),
    ),
    PluginManifest(
        "panca-workspace",
        Consciousness.PANCA,
        "眼/身识：有界检查、内容寻址写、恢复与命名检查",
        "seed_society.workspace_tools",
        ("WorkspaceReadFileTool", "WorkspaceWriteFileTool", "WorkspaceRunCheckTool"),
    ),
    PluginManifest(
        "panca-a2a",
        Consciousness.PANCA,
        "远程委托：钉住 Agent Card、有界轮询、歧义不重发",
        "seed_society.a2a",
        ("A2AHTTPClient", "A2ARemoteExecutor", "A2ARemoteWorker"),
        depends_on=("panca-http", "sila-governance"),
    ),
    PluginManifest(
        "panca-github",
        Consciousness.PANCA,
        "鼻识：有界只读 GitHub issue/PR 摄取",
        "seed_society.github",
        ("GitHubIssueClient", "GitHubPullRequestClient"),
    ),
    # ── 戒律：识的边界 ────────────────────────────────────────────────────
    PluginManifest(
        "sila-budget",
        Consciousness.SILA,
        "预算戒：max_actions/max_attempts/min_passing_score 全流程约束",
        "seed_society.domain",
        ("RunBudget", "transition_goal", "validate_task_graph"),
    ),
    PluginManifest(
        "sila-approval",
        Consciousness.SILA,
        "身业戒：写/执行工具需持久化审批，读工具即时现行",
        "seed_society.domain",
        ("ApprovalRequest", "ApprovalStatus", "ToolRisk"),
    ),
    PluginManifest(
        "sila-tracing",
        Consciousness.SILA,
        "观照：关联、计时、脱敏的执行 span",
        "seed_society.tracing",
        ("TraceRecorder",),
    ),
    PluginManifest(
        "sila-governance",
        Consciousness.SILA,
        "不妄作戒：A2A 默认拒绝、决策先于联网、TCK 证据",
        "seed_society.a2a_governance",
        ("DelegationPolicyEvaluator", "build_doctor_report"),
    ),
    PluginManifest(
        "sila-publication",
        Consciousness.SILA,
        "不偷盗戒：只发布 goal 拥有的路径，幂等状态机，永不 force-push",
        "seed_society.publication",
        ("WorkspacePublishPullRequestTool",),
    ),
    # ── 桥与操作面 ────────────────────────────────────────────────────────
    PluginManifest(
        "bridge-cli",
        Consciousness.MANO,
        "命令行操作面：运行、检查、运维的 30+ 子命令",
        "seed_society.cli",
        ("build_parser", "main"),
        kind="bridge",
    ),
    PluginManifest(
        "bridge-mcp-server",
        Consciousness.PANCA,
        "MCP stdio 桥：把 society 工具暴露给 DSH 的 mcp-client",
        "seed_society.mcp_server",
        ("serve", "society_tools"),
        kind="bridge",
    ),
    PluginManifest(
        "bridge-mneme",
        Consciousness.ALAYA,
        "dsh-mneme 桥：晋升种子下行推送、巩固产物上行导入（双向幂等、干跑优先）",
        "seed_society.mneme_bridge",
        ("push_seeds", "import_seeds"),
        depends_on=("seed-store",),
        kind="bridge",
    ),
    PluginManifest(
        "seed-skills",
        Consciousness.ALAYA,
        "DSH 技能种子：.agents/skills/yogacara-* 约束七识用法",
        "integrations.dsh.skills",
        ("yogacara-society", "yogacara-alaya", "yogacara-manas",
         "yogacara-mano", "yogacara-panca", "yogacara-sila"),
        kind="seed",
    ),
)


def list_plugins(*, consciousness: str | None = None) -> list[dict[str, Any]]:
    """Return the plugin manifest list, optionally filtered by consciousness."""
    manifests = SOCIETY_MANIFESTS
    if consciousness:
        normalized = str(consciousness).strip().casefold()
        valid = {item.value for item in Consciousness}
        if normalized not in valid:
            raise ValueError(
                f"unknown consciousness: {consciousness}; expected one of "
                f"{sorted(valid)}"
            )
        manifests = tuple(
            item for item in manifests if item.consciousness.value == normalized
        )
    return [item.to_dict() for item in manifests]


def describe_society() -> dict[str, Any]:
    """Render the machine-readable eight-consciousness map.

    This is the seed document the harness reads to understand which plugin
    speaks for which consciousness.
    """
    order = (
        Consciousness.ALAYA,
        Consciousness.MANAS,
        Consciousness.MANO,
        Consciousness.PANCA,
        Consciousness.SILA,
    )
    names = {
        Consciousness.ALAYA: "阿赖耶识（含藏种子 / 熏习 / 现行）",
        Consciousness.MANAS: "末那识（我执 / 个体性 / 路由与晋升）",
        Consciousness.MANO: "意识（分别 / 造作 / 双循环串联）",
        Consciousness.PANCA: "前五识（眼耳鼻舌身 / 根尘相接）",
        Consciousness.SILA: "戒律（预算 / 审批 / 围栏 / 默认拒绝）",
    }
    return {
        "society": "seed-society",
        "version": "1.3.0",
        "doctrine": (
            "以有限的阿赖耶识能力赋予独特种子，借七识拟合使用者："
            "技能文件约束能力与性格，综合上下文告知模型真正的需求。"
        ),
        "consciousnesses": [
            {
                "id": consciousness.value,
                "name": names[consciousness],
                "plugins": [
                    item.to_dict()
                    for item in SOCIETY_MANIFESTS
                    if item.consciousness == consciousness
                ],
            }
            for consciousness in order
        ],
    }
