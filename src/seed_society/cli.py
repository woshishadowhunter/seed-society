"""Command-line interface for running and inspecting agent societies."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shlex
import sys
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from threading import Event as ThreadEvent
from typing import Any, Sequence
from uuid import uuid4

from .a2a import (
    A2AHTTPClient,
    A2ALimits,
    A2ARemoteExecutor,
    A2ARemoteWorker,
    register_remote_agent,
)
from .a2a_governance import (
    DelegationPolicyEvaluator,
    build_doctor_report,
    parse_a2a_tck_report,
    parse_policy_document,
)
from .a2a_reliability import run_reliability_campaign
from .consolidation import ConsolidationEngine
from .deterministic import CriteriaReviewer, build_demo_engine
from .domain import (
    AgentGenome,
    AgentProfile,
    AgentSelfModel,
    BenchmarkCase,
    CandidateExecution,
    CandidateIdentity,
    CaseEvaluation,
    Goal,
    OutboxStatus,
    PolicyActivation,
    PolicyVerdict,
    RunBudget,
    SeedKind,
    Task,
    utc_now,
)
from .engine import LoopEngine, resolve_approval
from .evaluation import BenchmarkEvaluator, PromotionPolicy
from .evolution import GenomeRecombiner
from .experience import ExperienceDistiller
from .github import GitHubIssueClient, GitHubPullRequestClient
from .maintenance import (
    build_maintenance_engine,
    create_maintenance_goal,
    maintenance_goal_configuration,
    maintenance_publication_configuration,
)
from .memory import MemoryManager
from .model_runtime import doctor_model_runtime, load_model_runtime
from .outbox import (
    OutboxDispatcher,
    OutboxDispatchStatus,
    WebhookOutboxHandler,
)
from .operations import collect_health, collect_metrics
from .providers import OpenAICompatibleProvider
from .postgres_storage import PostgreSQLRepository
from .readiness import run_product_readiness_self_test
from .selection import PerformanceWeightedSelector
from .scheduler import parse_utc, run_scheduler_self_test
from .storage import SQLiteRepository
from .tracing import TraceRecorder
from .worker_service import (
    WorkerRunStatus,
    WorkerService,
    WorkerServiceConfig,
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _emit(value: Any, as_json: bool, human: str | None = None) -> None:
    if as_json:
        print(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(human if human is not None else value)


class SpecPlanner:
    def __init__(self, task_specs: Sequence[dict[str, Any]]):
        self.task_specs = list(task_specs)

    def plan(self, goal: Goal, context: dict[str, Any]) -> Sequence[Task]:
        tasks = []
        for index, spec in enumerate(self.task_specs, start=1):
            tasks.append(
                Task.create(
                    goal.goal_id,
                    str(spec["task_id"]),
                    str(spec["task_type"]),
                    str(spec["description"]),
                    assigned_role=str(spec.get("assigned_role", "worker")),
                    acceptance_criteria=dict(spec.get("acceptance_criteria", {})),
                    dependencies=tuple(spec.get("dependencies", ())),
                    context={
                        "output": str(spec.get("output", "")),
                        "repair_output": str(spec.get("repair_output", "")),
                    },
                    max_attempts=int(spec.get("max_attempts", 3)),
                    position=int(spec.get("position", index)),
                )
            )
        return tasks


class SpecWorker:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id

    def execute(self, task: Task, context: dict[str, Any]) -> str:
        use_repair = bool(context.get("review_feedback"))
        key = "repair_output" if use_repair else "output"
        output = task.context.get(key) or task.context.get("output")
        if not output:
            raise ValueError(f"task {task.task_id} has no {key}")
        return str(output)


def _load_spec(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid goal spec: {error}") from error
    required = ("title", "description", "tasks")
    if not isinstance(data, dict) or any(not data.get(field) for field in required):
        raise ValueError("invalid goal spec: title, description, and tasks are required")
    if not isinstance(data["tasks"], list) or not data["tasks"]:
        raise ValueError("invalid goal spec: tasks must be a non-empty list")
    task_required = ("task_id", "task_type", "description", "output")
    for index, task in enumerate(data["tasks"]):
        if not isinstance(task, dict) or any(field not in task for field in task_required):
            raise ValueError(
                f"invalid goal spec: task {index + 1} requires "
                + ", ".join(task_required)
            )
    return data


def _load_genome(agent_id: str, path: str) -> AgentGenome:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid genome file: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("invalid genome file: JSON object is required")
    self_model_data = data.get("self_model", {})
    if self_model_data and not isinstance(self_model_data, dict):
        raise ValueError("invalid genome file: self_model must be an object")
    self_model = None
    if self_model_data:
        self_model = AgentSelfModel.create(
            mission=str(self_model_data.get("mission", "")),
            success_signals=tuple(self_model_data.get("success_signals", ())),
            failure_modes=tuple(self_model_data.get("failure_modes", ())),
        )
    return AgentGenome.create(
        agent_id,
        base_model=str(data.get("base_model", "")),
        role_seed=str(data.get("role_seed", "")),
        self_model=self_model,
        traits=tuple(data.get("traits", ())),
        tool_profile=tuple(data.get("tool_profile", ())),
        memory_profile=dict(data.get("memory_profile", {})),
        risk_policy=str(data.get("risk_policy", "read_only")),
        parents=tuple(data.get("parents", ())),
        generation=int(data.get("generation", 1)),
    )


def _agent_id(task_type: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_type.casefold()).strip("-")
    return f"spec-{slug or 'worker'}"


def _build_spec_engine(
    repository: SQLiteRepository,
    spec: dict[str, Any],
    *,
    allow_remote: bool = False,
    remote_limits: A2ALimits | None = None,
) -> LoopEngine:
    workers = {}
    task_types: dict[str, set[str]] = {}
    for task in spec["tasks"]:
        agent_id = _agent_id(str(task["task_type"]))
        task_types.setdefault(agent_id, set()).add(str(task["task_type"]))
        workers.setdefault(agent_id, SpecWorker(agent_id))
    for agent_id, supported in task_types.items():
        repository.save_agent(
            AgentProfile(agent_id, "worker", "static-spec-v1", tuple(sorted(supported)))
        )
    if allow_remote:
        active_identities = {
            (deployment.champion_agent_id, deployment.champion_model_id)
            for deployment in repository.list_deployments()
        }
        limits = remote_limits or A2ALimits()
        for registration in repository.list_remote_agents():
            if (registration.agent_id, registration.model_id) not in active_identities:
                continue
            token = _registration_token(registration.auth_env)
            client = A2AHTTPClient(
                auth_token=token,
                limits=limits,
                allow_insecure_localhost=registration.allow_insecure_localhost,
            )
            workers[registration.agent_id] = A2ARemoteWorker(
                A2ARemoteExecutor(
                    repository,
                    registration,
                    client,
                    tracer=TraceRecorder(repository),
                    limits=limits,
                    policy_evaluator=DelegationPolicyEvaluator(repository),
                    require_policy=True,
                )
            )
    memory = MemoryManager(repository)
    return LoopEngine(
        planner=SpecPlanner(spec["tasks"]),
        workers=workers,
        reviewer=CriteriaReviewer(),
        repository=repository,
        memory=memory,
        selector=PerformanceWeightedSelector(),
        budget=RunBudget(max_actions=int(spec.get("max_actions", 100))),
    )


def _registration_token(auth_env: str) -> str:
    if not auth_env:
        return ""
    token = os.environ.get(auth_env, "")
    if not token:
        raise ValueError(f"remote authentication environment variable is missing: {auth_env}")
    return token


def _read_bounded(path: str, max_bytes: int, label: str) -> bytes:
    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as error:
        raise ValueError(f"cannot read {label}: {error}") from error
    if not raw or len(raw) > max_bytes:
        raise ValueError(f"{label} size is invalid")
    return raw


def _parse_skill_declarations(values: Sequence[str]) -> dict[str, str]:
    skills: dict[str, str] = {}
    for declaration in values:
        task_type, separator, skill_id = declaration.partition("=")
        task_type = task_type.strip()
        skill_id = skill_id.strip()
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", task_type)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", skill_id)
        ):
            raise ValueError("skills must use TASK_TYPE=SKILL_ID with safe identifiers")
        if task_type in skills:
            raise ValueError(f"duplicate remote task type: {task_type}")
        skills[task_type] = skill_id
    if not skills:
        raise ValueError("at least one --skill TASK_TYPE=SKILL_ID is required")
    return skills


def _status(repository: SQLiteRepository, goal_id: str) -> dict[str, Any]:
    goal = repository.get_goal(goal_id)
    if goal is None:
        raise KeyError(f"goal not found: {goal_id}")
    return {
        "goal": goal,
        "tasks": repository.list_tasks(goal_id),
        "attempts": repository.list_attempts(goal_id),
        "reviews": repository.list_reviews(goal_id),
        "artifacts": repository.list_artifacts(goal_id),
        "approvals": repository.list_approvals(goal_id),
        "spans": repository.list_spans(goal_id),
        "workspace_snapshots": repository.list_workspace_snapshots(goal_id),
        "verification_results": repository.list_verification_results(goal_id),
        "publication": repository.get_publication(goal_id),
    }


def _provider_from_environment() -> OpenAICompatibleProvider:
    api_key = os.environ.get("MODEL_API_KEY", "")
    model = os.environ.get("MODEL_ID", "")
    if not api_key or not model:
        raise ValueError("MODEL_API_KEY and MODEL_ID are required")
    return OpenAICompatibleProvider(
        api_key,
        os.environ.get("MODEL_BASE_URL", "https://api.openai.com/v1"),
        model,
        response_format={"type": "json_object"},
    )


class _ObservedBenchmarkRunner:
    def __init__(self, results: dict[tuple[str, str], dict[str, Any]]):
        self.results = results

    def __call__(
        self, candidate: CandidateIdentity, case: BenchmarkCase
    ) -> CandidateExecution:
        result = self.results[(case.case_id, candidate.agent_id)]
        return CandidateExecution(result, float(result["duration_ms"]))


def _score_observed_result(case: BenchmarkCase, output: Any) -> CaseEvaluation:
    if not isinstance(output, dict):
        raise ValueError("observed candidate result must be an object")
    return CaseEvaluation(bool(output["passed"]), float(output["score"]))


def _load_evaluation_spec(
    path: str,
) -> tuple[
    tuple[BenchmarkCase, ...],
    CandidateIdentity,
    CandidateIdentity,
    dict[tuple[str, str], dict[str, Any]],
]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid evaluation spec: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("invalid evaluation spec: cases must be a list")
    if not data["cases"]:
        raise ValueError("invalid evaluation spec: benchmark needs at least one case")
    task_type = str(data.get("task_type", "")).strip()
    identities = []
    for role in ("champion", "challenger"):
        value = data.get(role)
        if not isinstance(value, dict):
            raise ValueError(f"invalid evaluation spec: {role} must be an object")
        identities.append(
            CandidateIdentity(
                str(value.get("agent_id", "")), str(value.get("model_id", ""))
            )
        )
    champion, challenger = identities
    benchmark: list[BenchmarkCase] = []
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for index, value in enumerate(data["cases"], start=1):
        if not isinstance(value, dict):
            raise ValueError(f"invalid evaluation spec: case {index} must be an object")
        case = BenchmarkCase.create(
            str(value.get("case_id", "")),
            task_type,
            dict(value.get("input", {})),
            dict(value.get("acceptance_criteria", {})),
            context=dict(value.get("context", {})),
            critical=bool(value.get("critical", False)),
        )
        observed = value.get("results")
        if not isinstance(observed, dict):
            raise ValueError(f"invalid evaluation spec: case {case.case_id} needs results")
        for candidate in (champion, challenger):
            result = observed.get(candidate.agent_id)
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("passed"), bool)
                or not isinstance(result.get("score"), (int, float))
                or not isinstance(result.get("duration_ms"), (int, float))
            ):
                raise ValueError(
                    f"invalid evaluation result for {case.case_id}/{candidate.agent_id}"
                )
            CaseEvaluation(result["passed"], float(result["score"]))
            CandidateExecution(result, float(result["duration_ms"]))
            results[(case.case_id, candidate.agent_id)] = dict(result)
        benchmark.append(case)
    return tuple(benchmark), champion, challenger, results


def _ensure_evaluation_agent(
    repository: SQLiteRepository,
    candidate: CandidateIdentity,
    task_type: str,
) -> None:
    existing = repository.get_agent(candidate.agent_id)
    if existing is None:
        repository.save_agent(
            AgentProfile(candidate.agent_id, "worker", candidate.model_id, (task_type,))
        )
        return
    if (
        not existing.enabled
        or existing.role != "worker"
        or existing.model_id != candidate.model_id
        or not ("*" in existing.task_types or task_type in existing.task_types)
    ):
        raise ValueError(f"candidate identity conflicts with agent {candidate.agent_id}")


def _maintenance_goal_id(repository: str, issue: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", repository.casefold()).strip("-")
    return f"maintain-{slug}-{issue}"


def _parse_check_declarations(values: Sequence[str]) -> dict[str, tuple[str, ...]]:
    result = {}
    for declaration in values:
        name, separator, command_text = declaration.partition("=")
        name = name.strip()
        if not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ValueError("checks must use NAME=COMMAND with a safe short name")
        if name in result:
            raise ValueError(f"duplicate check: {name}")
        try:
            command = tuple(shlex.split(command_text, posix=True))
        except ValueError as error:
            raise ValueError(f"invalid check command for {name}: {error}") from error
        if not command:
            raise ValueError(f"check command must not be empty: {name}")
        result[name] = command
    return result


def _database_protected_paths(database: str, workspace: Path) -> tuple[str, ...]:
    database_path = Path(database).resolve()
    if not database_path.is_relative_to(workspace):
        return ()
    relative = database_path.relative_to(workspace).as_posix()
    return (relative, f"{relative}-shm", f"{relative}-wal")


def _add_postgres_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url")
    parser.add_argument("--postgres-schema", default="agent_society")


def _open_repository(args):
    database_url = getattr(args, "database_url", None) or os.environ.get(
        "SEED_SOCIETY_DATABASE_URL", ""
    )
    if database_url:
        supported = {
            "enqueue",
            "worker",
            "status",
            "events",
            "agents",
            "scheduler",
            "traces",
            "approvals",
            "approve",
            "reject",
            "health",
            "metrics",
            "outbox",
            "genome",
            "experience",
        }
        if args.command not in supported:
            raise ValueError(
                f"PostgreSQL execution backend does not support command: {args.command}"
            )
        return PostgreSQLRepository(
            database_url,
            schema=getattr(args, "postgres_schema", "agent_society"),
        )
    return SQLiteRepository(args.db)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed-society",
        description="Run auditable goal-driven societies of specialized agents.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    product = commands.add_parser("product", help="inspect product readiness")
    product_commands = product.add_subparsers(dest="product_command", required=True)
    product_self_test = product_commands.add_parser(
        "self-test", help="run install-time product readiness checks"
    )
    product_self_test.add_argument("--json", action="store_true")

    model = commands.add_parser("model", help="inspect model runtime compatibility")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_doctor = model_commands.add_parser(
        "doctor", help="probe configured OpenAI-compatible providers"
    )
    model_doctor.add_argument("config")
    model_doctor.add_argument("--json", action="store_true")

    plugins = commands.add_parser(
        "plugins", help="list the eight-consciousness plugin manifest"
    )
    plugins.add_argument(
        "--consciousness",
        choices=["alaya", "manas", "mano", "panca", "sila"],
    )
    plugins.add_argument(
        "--describe",
        action="store_true",
        help="render the full society doctrine map",
    )
    plugins.add_argument("--json", action="store_true")

    outbox = commands.add_parser("outbox", help="inspect and deliver outbox messages")
    outbox_commands = outbox.add_subparsers(dest="outbox_command", required=True)
    outbox_list = outbox_commands.add_parser("list", help="list durable outbox messages")
    outbox_list.add_argument(
        "--status",
        choices=[status.value for status in OutboxStatus],
    )
    outbox_list.add_argument("--limit", type=int, default=100)
    outbox_list.add_argument("--db", default="seed-society.db")
    _add_postgres_options(outbox_list)
    outbox_list.add_argument("--json", action="store_true")
    outbox_dispatch = outbox_commands.add_parser(
        "dispatch", help="deliver one outbox message to a webhook"
    )
    outbox_dispatch.add_argument("--worker-id", required=True)
    outbox_dispatch.add_argument("--topic", default="webhook")
    outbox_dispatch.add_argument("--webhook-url", required=True)
    outbox_dispatch.add_argument("--token-env")
    outbox_dispatch.add_argument("--allow-insecure-localhost", action="store_true")
    outbox_dispatch.add_argument("--lease", type=int, default=60)
    outbox_dispatch.add_argument("--retry-seconds", type=int, default=5)
    outbox_dispatch.add_argument("--timeout", type=float, default=30.0)
    outbox_dispatch.add_argument("--watch", action="store_true")
    outbox_dispatch.add_argument("--poll-interval", type=float, default=1.0)
    outbox_dispatch.add_argument("--db", default="seed-society.db")
    _add_postgres_options(outbox_dispatch)
    outbox_dispatch.add_argument("--json", action="store_true")
    outbox_purge = outbox_commands.add_parser(
        "purge", help="delete bounded terminal outbox history"
    )
    outbox_purge.add_argument("--before", required=True)
    outbox_purge.add_argument("--limit", type=int, default=1000)
    outbox_purge.add_argument("--db", default="seed-society.db")
    _add_postgres_options(outbox_purge)
    outbox_purge.add_argument("--json", action="store_true")

    for name, help_text in (
        ("health", "inspect database readiness and degraded runtime state"),
        ("metrics", "collect bounded operational counters"),
    ):
        operations = commands.add_parser(name, help=help_text)
        operations.add_argument("--db", default="seed-society.db")
        _add_postgres_options(operations)
        if name == "health":
            operations.add_argument("--worker-id")
        operations.add_argument("--json", action="store_true")

    demo = commands.add_parser("demo", help="run the offline quantum mug scenario")
    demo.add_argument("--db", default="seed-society.db")
    demo.add_argument("--goal-id", default="quantum-mug-demo")
    demo.add_argument("--json", action="store_true")

    run = commands.add_parser("run", help="run a JSON goal specification")
    run.add_argument("spec")
    run.add_argument("--db", default="seed-society.db")
    run.add_argument("--allow-remote", action="store_true")
    run.add_argument("--remote-timeout", type=float, default=60.0)
    run.add_argument("--remote-max-polls", type=int, default=20)
    run.add_argument("--remote-poll-interval", type=float, default=0.25)
    run.add_argument("--json", action="store_true")

    enqueue = commands.add_parser(
        "enqueue", help="plan a JSON goal specification for worker processes"
    )
    enqueue.add_argument("spec")
    enqueue.add_argument("--db", default="seed-society.db")
    _add_postgres_options(enqueue)
    enqueue.add_argument("--json", action="store_true")

    worker = commands.add_parser("worker", help="run lease-owned worker processes")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    worker_run = worker_commands.add_parser("run", help="claim and execute queued tasks")
    worker_run.add_argument("--worker-id", required=True)
    worker_run.add_argument("--session-id")
    agent_source = worker_run.add_mutually_exclusive_group(required=True)
    agent_source.add_argument("--agent-id", dest="agent_ids", action="append")
    agent_source.add_argument("--model-config")
    limit = worker_run.add_mutually_exclusive_group()
    limit.add_argument("--once", action="store_true")
    limit.add_argument("--max-tasks", type=int)
    worker_run.add_argument("--heartbeat-ttl", type=int, default=60)
    worker_run.add_argument("--lease", type=int, default=30)
    worker_run.add_argument("--renew-interval", type=float, default=10.0)
    worker_run.add_argument("--poll-interval", type=float, default=1.0)
    worker_run.add_argument("--db", default="seed-society.db")
    _add_postgres_options(worker_run)
    worker_run.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="inspect goal state and artifacts")
    status.add_argument("goal_id")
    status.add_argument("--db", default="seed-society.db")
    _add_postgres_options(status)
    status.add_argument("--json", action="store_true")

    events = commands.add_parser("events", help="inspect an ordered audit trail")
    events.add_argument("goal_id")
    events.add_argument("--db", default="seed-society.db")
    _add_postgres_options(events)
    events.add_argument("--json", action="store_true")

    agents = commands.add_parser("agents", help="inspect agents and social memory")
    agents.add_argument("--db", default="seed-society.db")
    _add_postgres_options(agents)
    agents.add_argument("--json", action="store_true")

    evaluate = commands.add_parser(
        "evaluate", help="evaluate a challenger against a benchmark"
    )
    evaluate.add_argument("spec")
    evaluate.add_argument("--db", default="seed-society.db")
    evaluate.add_argument("--json", action="store_true")

    evaluations = commands.add_parser(
        "evaluations", help="inspect evaluation runs and case outcomes"
    )
    evaluations.add_argument("run_id", nargs="?")
    evaluations.add_argument("--db", default="seed-society.db")
    evaluations.add_argument("--json", action="store_true")

    promote = commands.add_parser(
        "promote", help="explicitly promote a recommended challenger"
    )
    promote.add_argument("run_id")
    promote.add_argument("--by", required=True)
    promote.add_argument("--db", default="seed-society.db")
    promote.add_argument("--json", action="store_true")

    deployments = commands.add_parser(
        "deployments", help="inspect active task-type champions"
    )
    deployments.add_argument("--db", default="seed-society.db")
    deployments.add_argument("--json", action="store_true")

    scheduler = commands.add_parser(
        "scheduler", help="inspect and recover fenced task leases"
    )
    scheduler_commands = scheduler.add_subparsers(
        dest="scheduler_command", required=True
    )
    scheduler_workers = scheduler_commands.add_parser(
        "workers", help="list durable worker sessions"
    )
    scheduler_workers.add_argument("--at")
    scheduler_workers.add_argument("--db", default="seed-society.db")
    _add_postgres_options(scheduler_workers)
    scheduler_workers.add_argument("--json", action="store_true")
    scheduler_claims = scheduler_commands.add_parser(
        "claims", help="list task claim history"
    )
    scheduler_claims.add_argument("--goal-id")
    scheduler_claims.add_argument("--at")
    scheduler_claims.add_argument("--db", default="seed-society.db")
    _add_postgres_options(scheduler_claims)
    scheduler_claims.add_argument("--json", action="store_true")
    scheduler_reap = scheduler_commands.add_parser(
        "reap", help="recover task claims expired at an explicit UTC time"
    )
    scheduler_reap.add_argument("--at", required=True)
    scheduler_reap.add_argument("--db", default="seed-society.db")
    _add_postgres_options(scheduler_reap)
    scheduler_reap.add_argument("--json", action="store_true")
    scheduler_self_test = scheduler_commands.add_parser(
        "self-test", help="run the deterministic scheduler safety campaign"
    )
    scheduler_self_test.add_argument("--db", default=":memory:")
    scheduler_self_test.add_argument("--json", action="store_true")

    a2a = commands.add_parser("a2a", help="manage pinned A2A 1.0 remote agents")
    a2a_commands = a2a.add_subparsers(dest="a2a_command", required=True)
    inspect_card = a2a_commands.add_parser(
        "inspect-card", help="inspect a bounded Agent Card"
    )
    inspect_card.add_argument("url")
    inspect_card.add_argument("--allow-insecure-localhost", action="store_true")
    inspect_card.add_argument("--db", default="seed-society.db")
    inspect_card.add_argument("--json", action="store_true")
    register = a2a_commands.add_parser(
        "register", help="register an operator-pinned remote agent"
    )
    register.add_argument("agent_id")
    register.add_argument("card_url")
    register.add_argument("--sha256", required=True)
    register.add_argument("--interface", required=True)
    register.add_argument("--skill", action="append", default=[])
    register.add_argument("--auth-env", default="")
    register.add_argument(
        "--allow-context",
        action="append",
        default=[],
        choices=(
            "goal",
            "task_context",
            "dependency_artifacts",
            "review_feedback",
            "knowledge",
        ),
    )
    register.add_argument("--allow-insecure-localhost", action="store_true")
    register.add_argument("--db", default="seed-society.db")
    register.add_argument("--json", action="store_true")
    remote_agents = a2a_commands.add_parser("agents", help="list remote trust records")
    remote_agents.add_argument("--db", default="seed-society.db")
    remote_agents.add_argument("--json", action="store_true")
    delegations = a2a_commands.add_parser(
        "delegations", help="inspect durable remote delegations"
    )
    delegations.add_argument("delegation_id", nargs="?")
    delegations.add_argument("--db", default="seed-society.db")
    delegations.add_argument("--json", action="store_true")
    cancel = a2a_commands.add_parser("cancel", help="cancel a known remote task")
    cancel.add_argument("delegation_id")
    cancel.add_argument("--by", required=True)
    cancel.add_argument("--db", default="seed-society.db")
    cancel.add_argument("--json", action="store_true")

    policy = a2a_commands.add_parser(
        "policy", help="validate and activate remote delegation policy"
    )
    policy_commands = policy.add_subparsers(dest="policy_command", required=True)
    for name in ("validate", "import"):
        policy_file = policy_commands.add_parser(name, help=f"{name} a policy file")
        policy_file.add_argument("path")
        policy_file.add_argument("--db", default="seed-society.db")
        policy_file.add_argument("--json", action="store_true")
    policy_list = policy_commands.add_parser("list", help="list policies and activations")
    policy_list.add_argument("--db", default="seed-society.db")
    policy_list.add_argument("--json", action="store_true")
    policy_activate = policy_commands.add_parser(
        "activate", help="activate one exact policy digest for a task type"
    )
    policy_activate.add_argument("task_type")
    policy_activate.add_argument("policy_digest")
    policy_activate.add_argument("--by", required=True)
    policy_activate.add_argument("--db", default="seed-society.db")
    policy_activate.add_argument("--json", action="store_true")
    policy_simulate = policy_commands.add_parser(
        "simulate", help="simulate current policy without persisting a decision"
    )
    policy_simulate.add_argument("agent_id")
    policy_simulate.add_argument("task_type")
    policy_simulate.add_argument("--db", default="seed-society.db")
    policy_simulate.add_argument("--json", action="store_true")

    attestation = a2a_commands.add_parser(
        "attestation", help="import official A2A TCK compatibility evidence"
    )
    attestation_commands = attestation.add_subparsers(
        dest="attestation_command", required=True
    )
    attestation_import = attestation_commands.add_parser(
        "import", help="import a bounded compatibility.json report"
    )
    attestation_import.add_argument("agent_id")
    attestation_import.add_argument("path")
    attestation_import.add_argument("--source-revision", required=True)
    attestation_import.add_argument("--tool-version", required=True)
    attestation_import.add_argument("--db", default="seed-society.db")
    attestation_import.add_argument("--json", action="store_true")
    attestation_list = attestation_commands.add_parser(
        "list", help="list imported conformance attestations"
    )
    attestation_list.add_argument("agent_id", nargs="?")
    attestation_list.add_argument("--db", default="seed-society.db")
    attestation_list.add_argument("--json", action="store_true")

    doctor = a2a_commands.add_parser(
        "doctor", help="check remote deployment readiness without sending a task"
    )
    doctor.add_argument("agent_id")
    doctor.add_argument("task_type")
    doctor.add_argument("--allow-insecure-localhost", action="store_true")
    doctor.add_argument("--db", default="seed-society.db")
    doctor.add_argument("--json", action="store_true")
    self_test = a2a_commands.add_parser(
        "self-test", help="run the local deterministic A2A reliability campaign"
    )
    self_test.add_argument("--db", default="seed-society.db")
    self_test.add_argument("--json", action="store_true")
    decisions = a2a_commands.add_parser(
        "decisions", help="inspect durable remote policy decisions"
    )
    decisions.add_argument("goal_id", nargs="?")
    decisions.add_argument("--db", default="seed-society.db")
    decisions.add_argument("--json", action="store_true")

    maintain = commands.add_parser(
        "maintain", help="inspect a GitHub issue and produce a reviewed proposal"
    )
    maintain.add_argument("repository")
    maintain.add_argument("issue", type=int)
    maintain.add_argument("--workspace", required=True)
    maintain.add_argument("--goal-id")
    maintain.add_argument("--db", default="seed-society.db")
    maintain.add_argument(
        "--apply",
        action="store_true",
        help="enable approved local UTF-8 writes and named verification checks",
    )
    maintain.add_argument(
        "--publish", action="store_true",
        help="enable approved commit, push, and pull-request publication",
    )
    maintain.add_argument("--base", default="main", help="pull-request base branch")
    maintain.add_argument("--remote", default="origin", help="Git remote to push")
    maintain.add_argument(
        "--branch-prefix", default="seed-society/",
        help="required prefix for the current publication branch",
    )
    maintain.add_argument(
        "--check",
        dest="checks",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help="operator-configured verification command; repeat for multiple checks",
    )
    maintain.add_argument("--json", action="store_true")

    traces = commands.add_parser("traces", help="inspect linked execution spans")
    traces.add_argument("goal_id")
    traces.add_argument("--db", default="seed-society.db")
    _add_postgres_options(traces)
    traces.add_argument("--json", action="store_true")

    approvals = commands.add_parser("approvals", help="inspect durable approvals")
    approvals.add_argument("goal_id")
    approvals.add_argument("--db", default="seed-society.db")
    _add_postgres_options(approvals)
    approvals.add_argument("--json", action="store_true")

    for name in ("approve", "reject"):
        decision = commands.add_parser(name, help=f"{name} a pending tool call")
        decision.add_argument("approval_id")
        decision.add_argument("--by", required=True)
        decision.add_argument("--db", default="seed-society.db")
        _add_postgres_options(decision)
        decision.add_argument("--json", action="store_true")

    knowledge = commands.add_parser("knowledge", help="manage long-term knowledge")
    knowledge_commands = knowledge.add_subparsers(dest="knowledge_command", required=True)
    add = knowledge_commands.add_parser("add", help="add a knowledge item")
    add.add_argument("title")
    add.add_argument("content")
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--db", default="seed-society.db")
    add.add_argument("--json", action="store_true")
    search = knowledge_commands.add_parser("search", help="search knowledge")
    search.add_argument("query")
    search.add_argument("--tag", action="append", default=[])
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--db", default="seed-society.db")
    search.add_argument("--json", action="store_true")

    revision = commands.add_parser(
        "revision", help="inspect seed revision history and roll changes back"
    )
    revision_commands = revision.add_subparsers(
        dest="revision_command", required=True
    )
    revision_list = revision_commands.add_parser(
        "list", help="list recorded seed revisions (oldest first)"
    )
    revision_list.add_argument(
        "--kind", choices=[kind.value for kind in SeedKind]
    )
    revision_list.add_argument("--seed-id")
    revision_list.add_argument("--limit", type=int, default=50)
    revision_list.add_argument("--db", default="seed-society.db")
    revision_list.add_argument("--json", action="store_true")
    revision_show = revision_commands.add_parser(
        "show", help="inspect one revision including both payload sides"
    )
    revision_show.add_argument("revision_id")
    revision_show.add_argument("--db", default="seed-society.db")
    revision_show.add_argument("--json", action="store_true")
    revision_rollback = revision_commands.add_parser(
        "rollback", help="undo a revision by writing its inverse back"
    )
    revision_rollback.add_argument("revision_id")
    revision_rollback.add_argument(
        "--by", required=True, help="operator identity (never inferred)"
    )
    revision_rollback.add_argument("--reason", default="")
    revision_rollback.add_argument("--db", default="seed-society.db")
    revision_rollback.add_argument("--json", action="store_true")

    genome = commands.add_parser("genome", help="manage auditable agent seed genomes")
    genome_commands = genome.add_subparsers(dest="genome_command", required=True)
    genome_set = genome_commands.add_parser("set", help="save an agent genome file")
    genome_set.add_argument("agent_id")
    genome_set.add_argument("path")
    genome_set.add_argument("--db", default="seed-society.db")
    _add_postgres_options(genome_set)
    genome_set.add_argument("--json", action="store_true")
    genome_show = genome_commands.add_parser("show", help="inspect an agent genome")
    genome_show.add_argument("agent_id")
    genome_show.add_argument("--db", default="seed-society.db")
    _add_postgres_options(genome_show)
    genome_show.add_argument("--json", action="store_true")
    genome_recombine = genome_commands.add_parser(
        "recombine", help="create an auditable child genome candidate"
    )
    genome_recombine.add_argument("child_id")
    genome_recombine.add_argument("--parents", nargs="+", required=True)
    genome_recombine.add_argument("--task-type", required=True)
    genome_recombine.add_argument("--db", default="seed-society.db")
    _add_postgres_options(genome_recombine)
    genome_recombine.add_argument("--json", action="store_true")

    experience = commands.add_parser(
        "experience", help="distill and inspect reviewed task lessons"
    )
    experience_commands = experience.add_subparsers(
        dest="experience_command", required=True
    )
    experience_distill = experience_commands.add_parser(
        "distill", help="distill reviewed attempts for one goal"
    )
    experience_distill.add_argument("goal_id")
    experience_distill.add_argument("--db", default="seed-society.db")
    _add_postgres_options(experience_distill)
    experience_distill.add_argument("--json", action="store_true")
    experience_list = experience_commands.add_parser(
        "list", help="list distilled experience records"
    )
    experience_list.add_argument("--agent-id")
    experience_list.add_argument("--task-type")
    experience_list.add_argument("--goal-id")
    experience_list.add_argument("--limit", type=int, default=100)
    experience_list.add_argument("--db", default="seed-society.db")
    _add_postgres_options(experience_list)
    experience_list.add_argument("--json", action="store_true")

    consolidate = commands.add_parser(
        "consolidate",
        help="sleep-replay consolidation: salience, decay, semantic promotion",
    )
    consolidate.add_argument("goal_id")
    consolidate.add_argument(
        "--apply",
        action="store_true",
        help="persist decay and promotion mutations (default is dry-run)",
    )
    consolidate.add_argument(
        "--mneme-dir",
        help="after consolidation, sync seeds into the dsh-mneme store "
        "(same dry/apply mode); decays then lower mneme importance so "
        "forgotten seeds stop being injected",
    )
    consolidate.add_argument("--db", default="seed-society.db")
    _add_postgres_options(consolidate)
    consolidate.add_argument("--json", action="store_true")

    mneme = commands.add_parser(
        "mneme", help="bridge the dsh-mneme cross-session memory store"
    )
    mneme_commands = mneme.add_subparsers(dest="mneme_command", required=True)
    mneme_sync = mneme_commands.add_parser(
        "sync", help="push promoted society seeds into dsh-mneme"
    )
    mneme_sync.add_argument("--db", default="seed-society.db")
    mneme_sync.add_argument("--mneme-dir", default="~/.dsh/memory")
    mneme_sync.add_argument(
        "--type",
        dest="memory_type",
        default="project",
        choices=["preference", "project", "decision", "history", "summary"],
    )
    mneme_sync.add_argument("--limit", type=int, default=10)
    mneme_sync.add_argument(
        "--include-experience",
        action="store_true",
        help="also push high-strength PASS experience lessons",
    )
    mneme_sync.add_argument(
        "--push", action="store_true", help="write rows (default is dry-run)"
    )
    mneme_sync.add_argument("--json", action="store_true")
    mneme_import = mneme_commands.add_parser(
        "import", help="import non-society mneme entries into knowledge seeds"
    )
    mneme_import.add_argument("--db", default="seed-society.db")
    mneme_import.add_argument("--mneme-dir", default="~/.dsh/memory")
    mneme_import.add_argument(
        "--type",
        dest="memory_type",
        choices=["preference", "project", "decision", "history", "summary"],
    )
    mneme_import.add_argument("--limit", type=int, default=5)
    mneme_import.add_argument(
        "--apply", action="store_true", help="write knowledge items (default is dry-run)"
    )
    mneme_import.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = None
    try:
        if args.command == "product":
            from . import __version__

            if args.product_command == "self-test":
                value = run_product_readiness_self_test(__version__)
                _emit(
                    value,
                    args.json,
                    (
                        "Product readiness: "
                        f"{'pass' if value['passed'] else 'fail'} "
                        f"({len(value['checks'])} checks)"
                    ),
                )
                return 0 if value["passed"] else 1

        if args.command == "model":
            runtime = load_model_runtime(args.config, os.environ)
            value = doctor_model_runtime(runtime)
            _emit(
                value,
                args.json,
                (
                    f"Model compatibility: {'pass' if value['passed'] else 'fail'} "
                    f"({len(value['providers'])} providers)"
                ),
            )
            return 0 if value["passed"] else 1

        if args.command == "plugins":
            from .plugins import describe_society, list_plugins

            if getattr(args, "describe", False):
                value = describe_society()
                _emit(
                    value,
                    args.json,
                    (
                        "Agent society doctrine: "
                        f"{len(value['consciousnesses'])} consciousness groups, "
                        f"{sum(len(group['plugins']) for group in value['consciousnesses'])} plugins"
                    ),
                )
            else:
                value = list_plugins(
                    consciousness=getattr(args, "consciousness", None)
                )
                _emit(value, args.json, f"{len(value)} plugins")
            return 0

        repository = _open_repository(args)
        if args.command == "health":
            value = collect_health(
                repository,
                worker_id=args.worker_id or "",
            )
            _emit(
                value,
                args.json,
                f"Runtime health: {value['status']} (ready={value['ready']})",
            )
            return 0 if value["ready"] else 1
        if args.command == "metrics":
            value = collect_metrics(repository)
            _emit(value, args.json, f"{len(value)} operational metrics")
            return 0

        if args.command == "outbox":
            if args.outbox_command == "list":
                value = repository.list_outbox(
                    status=(
                        OutboxStatus(args.status)
                        if args.status is not None
                        else None
                    ),
                    limit=args.limit,
                )
                _emit(value, args.json, f"{len(value)} outbox messages")
                return 0
            if args.outbox_command == "purge":
                value = {
                    "purged": repository.purge_outbox(
                        before=parse_utc(args.before).isoformat(),
                        limit=args.limit,
                    )
                }
                _emit(value, args.json, f"Purged {value['purged']} outbox messages")
                return 0
            token = ""
            if args.token_env:
                token = os.environ.get(args.token_env, "")
                if not token:
                    raise ValueError(
                        f"webhook token environment is missing: {args.token_env}"
                    )
            handler = WebhookOutboxHandler(
                args.webhook_url,
                bearer_token=token,
                allow_insecure_localhost=args.allow_insecure_localhost,
                timeout_seconds=args.timeout,
            )
            dispatcher = OutboxDispatcher(
                repository,
                args.worker_id,
                topic=args.topic,
                lease_seconds=args.lease,
                retry_seconds=args.retry_seconds,
                repository_factory=lambda: _open_repository(args),
            )
            if args.watch:
                stop_event = ThreadEvent()
                previous_handlers = {}

                def request_dispatch_stop(signum, frame):
                    stop_event.set()

                try:
                    for signum in (signal.SIGINT, signal.SIGTERM):
                        previous_handlers[signum] = signal.getsignal(signum)
                        signal.signal(signum, request_dispatch_stop)
                    value = dispatcher.run(
                        handler,
                        stop_event=stop_event,
                        poll_interval_seconds=args.poll_interval,
                    )
                finally:
                    for signum, previous in previous_handlers.items():
                        signal.signal(signum, previous)
            else:
                value = dispatcher.run_once(handler)
            _emit(value, args.json, f"Outbox dispatch: {value.status.value}")
            return (
                0
                if value.status
                in {
                    OutboxDispatchStatus.IDLE,
                    OutboxDispatchStatus.DELIVERED,
                    OutboxDispatchStatus.STOPPED,
                }
                else 1
            )

        if args.command == "demo":
            engine = build_demo_engine(repository)
            goal = repository.get_goal(args.goal_id)
            if goal is None:
                goal = engine.create_goal(
                    "Quantum Coffee Mug Launch",
                    "Create market, visual, copy, and integrated launch materials",
                    goal_id=args.goal_id,
                )
            report = engine.resume(goal.goal_id)
            _emit(
                report,
                args.json,
                f"Goal {report.goal_id}: {report.status.value} "
                f"({report.tasks_succeeded}/{report.tasks_total} tasks, {report.retries} retries)",
            )
            return 0 if report.status.value == "succeeded" else 1

        if args.command in {"run", "enqueue"}:
            spec = _load_spec(args.spec)
            remote_limits = None
            if args.command == "run":
                remote_limits = A2ALimits(
                    request_timeout=min(10.0, args.remote_timeout),
                    total_timeout=args.remote_timeout,
                    max_polls=args.remote_max_polls,
                    poll_interval=args.remote_poll_interval,
                )
            engine = _build_spec_engine(
                repository,
                spec,
                allow_remote=args.allow_remote if args.command == "run" else False,
                remote_limits=remote_limits,
            )
            goal_id = str(spec.get("goal_id") or "") or None
            if goal_id and repository.get_goal(goal_id) is not None:
                raise ValueError(f"goal already exists: {goal_id}")
            goal = engine.create_goal(
                str(spec["title"]), str(spec["description"]), goal_id=goal_id
            )
            report = (
                engine.run(goal.goal_id)
                if args.command == "run"
                else engine.plan(goal.goal_id)
            )
            _emit(report, args.json, f"Goal {report.goal_id}: {report.status.value}")
            if args.command == "enqueue":
                return 0 if report.status.value == "running" else 1
            return 0 if report.status.value == "succeeded" else 1

        if args.command == "worker":
            if args.model_config:
                runtime = load_model_runtime(
                    args.model_config,
                    os.environ,
                    tracer=TraceRecorder(repository),
                )
                existing_profiles = {
                    profile.agent_id: profile for profile in repository.list_agents()
                }
                for profile in runtime.profiles:
                    existing = existing_profiles.get(profile.agent_id)
                    if existing is not None and existing != profile:
                        raise ValueError(
                            f"configured model agent identity changed: {profile.agent_id}"
                        )
                    repository.save_agent(profile)
                assignments = runtime.assignments
                workers = runtime.workers
                reviewer = runtime.reviewer
            else:
                profiles = {
                    profile.agent_id: profile for profile in repository.list_agents()
                }
                assignments = {}
                workers = {}
                for agent_id in args.agent_ids:
                    profile = profiles.get(agent_id)
                    if profile is None:
                        raise ValueError(f"agent not found: {agent_id}")
                    if profile.role != "worker" or profile.execution_kind != "local":
                        raise ValueError(f"agent is not a local worker: {agent_id}")
                    if "*" in profile.task_types:
                        raise ValueError(
                            f"worker CLI requires explicit task types: {agent_id}"
                        )
                    for task_type in profile.task_types:
                        owner = assignments.get(task_type)
                        if owner is not None and owner != agent_id:
                            raise ValueError(
                                f"task type {task_type} is assigned to multiple agents"
                            )
                        assignments[task_type] = agent_id
                    workers[agent_id] = SpecWorker(agent_id)
                reviewer = CriteriaReviewer()
            config = WorkerServiceConfig(
                worker_id=args.worker_id,
                session_id=args.session_id or f"session-{uuid4().hex[:16]}",
                heartbeat_ttl_seconds=args.heartbeat_ttl,
                lease_seconds=args.lease,
                renew_interval_seconds=args.renew_interval,
                poll_interval_seconds=args.poll_interval,
            )
            service = WorkerService(
                repository=repository,
                repository_factory=lambda: _open_repository(args),
                workers=workers,
                assignments=assignments,
                reviewer=reviewer,
                config=config,
            )
            if args.once:
                value = service.run_once()
            else:
                previous_handlers = {}

                def request_stop(signum, frame):
                    service.stop()

                try:
                    for signum in (signal.SIGINT, signal.SIGTERM):
                        previous_handlers[signum] = signal.getsignal(signum)
                        signal.signal(signum, request_stop)
                    value = service.run(max_tasks=args.max_tasks)
                finally:
                    for signum, handler in previous_handlers.items():
                        signal.signal(signum, handler)
            _emit(
                value,
                args.json,
                f"Worker {args.worker_id}: {value.status.value}"
                + (f" {value.goal_id}/{value.task_id}" if value.task_id else ""),
            )
            return 3 if value.status == WorkerRunStatus.LOST else 0

        if args.command == "scheduler":
            if args.scheduler_command == "self-test":
                value = run_scheduler_self_test()
                _emit(
                    value,
                    args.json,
                    f"Scheduler safety: {'pass' if value['passed'] else 'fail'}",
                )
                return 0 if value["passed"] else 1
            inspected_at = args.at or utc_now()
            parse_utc(inspected_at)
            if args.scheduler_command == "workers":
                value = [
                    {**_jsonable(worker), "expired": worker.is_expired(inspected_at)}
                    for worker in repository.list_workers()
                ]
                _emit(value, args.json, f"{len(value)} worker sessions")
                return 0
            if args.scheduler_command == "claims":
                value = [
                    {**_jsonable(claim), "lease_expired": claim.is_expired(inspected_at)}
                    for claim in repository.list_claims(args.goal_id)
                ]
                _emit(value, args.json, f"{len(value)} task claims")
                return 0
            value = repository.reap_expired_claims(now=inspected_at)
            _emit(value, args.json, f"Recovered {len(value)} expired claims")
            return 0

        if args.command == "a2a":
            if args.a2a_command == "inspect-card":
                client = A2AHTTPClient(
                    allow_insecure_localhost=args.allow_insecure_localhost
                )
                value = client.inspect_card(args.url)
                _emit(value, args.json, f"Agent Card SHA-256: {value.sha256}")
                return 0
            if args.a2a_command == "register":
                skills = _parse_skill_declarations(args.skill)
                token = _registration_token(args.auth_env)
                client = A2AHTTPClient(
                    auth_token=token,
                    allow_insecure_localhost=args.allow_insecure_localhost,
                )
                contexts = tuple(args.allow_context) or ("review_feedback",)
                value = register_remote_agent(
                    repository,
                    client,
                    args.agent_id,
                    args.card_url,
                    args.sha256,
                    args.interface,
                    skills,
                    auth_env=args.auth_env,
                    allowed_context_sections=contexts,
                    allow_insecure_localhost=args.allow_insecure_localhost,
                )
                _emit(value, args.json, f"Registered remote agent {value.agent_id}")
                return 0
            if args.a2a_command == "agents":
                value = repository.list_remote_agents()
                _emit(value, args.json, f"{len(value)} remote agents")
                return 0
            if args.a2a_command == "delegations":
                if args.delegation_id:
                    value = repository.get_delegation(args.delegation_id)
                    if value is None:
                        raise KeyError(
                            f"delegation not found: {args.delegation_id}"
                        )
                    human = f"Delegation {value.delegation_id}: {value.status.value}"
                else:
                    value = repository.list_delegations()
                    human = f"{len(value)} delegations"
                _emit(value, args.json, human)
                return 0
            if args.a2a_command == "policy":
                if args.policy_command in {"validate", "import"}:
                    value = parse_policy_document(
                        _read_bounded(args.path, 1_048_576, "delegation policy")
                    )
                    if args.policy_command == "import":
                        repository.save_policy(value)
                    action = "Validated" if args.policy_command == "validate" else "Imported"
                    _emit(
                        value,
                        args.json,
                        f"{action} policy {value.policy_id}@{value.version} "
                        f"({value.policy_digest})",
                    )
                    return 0
                if args.policy_command == "list":
                    value = {
                        "policies": repository.list_policies(),
                        "activations": repository.list_policy_activations(),
                    }
                    _emit(
                        value,
                        args.json,
                        f"{len(value['policies'])} policies, "
                        f"{len(value['activations'])} activations",
                    )
                    return 0
                if args.policy_command == "activate":
                    policy_value = repository.get_policy(args.policy_digest)
                    if policy_value is None:
                        raise KeyError(f"policy not found: {args.policy_digest}")
                    value = PolicyActivation.create(
                        args.task_type, policy_value, args.by
                    )
                    repository.activate_policy(value)
                    _emit(
                        value,
                        args.json,
                        f"Activated {value.policy_digest} for {value.task_type}",
                    )
                    return 0
                registration = repository.get_remote_agent(args.agent_id)
                if registration is None:
                    raise KeyError(f"remote agent not found: {args.agent_id}")
                value = DelegationPolicyEvaluator(repository).decide(
                    Task.create(
                        "policy-simulation",
                        f"simulate:{args.task_type}:{args.agent_id}",
                        args.task_type,
                        "Read-only delegation policy simulation",
                        max_attempts=1,
                    ),
                    registration,
                    1,
                    A2ALimits(),
                    persist=False,
                    reuse_existing=False,
                )
                _emit(
                    value,
                    args.json,
                    f"Policy simulation: {value.verdict.value}",
                )
                return 0 if value.verdict == PolicyVerdict.ALLOW else 1
            if args.a2a_command == "attestation":
                if args.attestation_command == "list":
                    value = repository.list_attestations(args.agent_id)
                    _emit(value, args.json, f"{len(value)} attestations")
                    return 0
                registration = repository.get_remote_agent(args.agent_id)
                if registration is None:
                    raise KeyError(f"remote agent not found: {args.agent_id}")
                value = parse_a2a_tck_report(
                    _read_bounded(args.path, 4_194_304, "A2A TCK report"),
                    registration,
                    source_revision=args.source_revision,
                    tool_version=args.tool_version,
                )
                repository.save_attestation(value)
                _emit(
                    value,
                    args.json,
                    f"Imported {'passing' if value.passed else 'failing'} "
                    f"attestation {value.attestation_id}",
                )
                return 0 if value.passed else 1
            if args.a2a_command == "doctor":
                registration = repository.get_remote_agent(args.agent_id)
                if registration is None:
                    raise KeyError(f"remote agent not found: {args.agent_id}")
                token = _registration_token(registration.auth_env)
                limits = A2ALimits()
                inspection = A2AHTTPClient(
                    auth_token=token,
                    limits=limits,
                    allow_insecure_localhost=(
                        registration.allow_insecure_localhost
                        or args.allow_insecure_localhost
                    ),
                ).inspect_card(registration.card_url)
                value = build_doctor_report(
                    repository,
                    registration,
                    inspection,
                    args.task_type,
                    limits,
                )
                _emit(
                    value.to_dict(),
                    args.json,
                    f"A2A readiness: {'ready' if value.ready else 'not ready'}",
                )
                return 0 if value.ready else 1
            if args.a2a_command == "self-test":
                value = run_reliability_campaign()
                _emit(
                    value.to_dict(),
                    args.json,
                    f"A2A reliability: {'pass' if value.passed else 'fail'}",
                )
                return 0 if value.passed else 1
            if args.a2a_command == "decisions":
                value = repository.list_policy_decisions(args.goal_id)
                _emit(value, args.json, f"{len(value)} policy decisions")
                return 0
            delegation = repository.get_delegation(args.delegation_id)
            if delegation is None:
                raise KeyError(f"delegation not found: {args.delegation_id}")
            registration = repository.get_remote_agent(delegation.agent_id)
            if registration is None or registration.model_id != delegation.model_id:
                raise ValueError("delegation remote identity is unavailable")
            token = _registration_token(registration.auth_env)
            limits = A2ALimits()
            executor = A2ARemoteExecutor(
                repository,
                registration,
                A2AHTTPClient(
                    auth_token=token,
                    limits=limits,
                    allow_insecure_localhost=registration.allow_insecure_localhost,
                ),
                tracer=TraceRecorder(repository),
                limits=limits,
            )
            value = executor.cancel(delegation.delegation_id, args.by)
            _emit(value, args.json, f"Delegation {value.delegation_id}: {value.status.value}")
            return 0

        if args.command == "status":
            value = _status(repository, args.goal_id)
            _emit(value, args.json, f"Goal {args.goal_id}: {value['goal'].status.value}")
            return 0

        if args.command == "events":
            if repository.get_goal(args.goal_id) is None:
                raise KeyError(f"goal not found: {args.goal_id}")
            value = repository.list_events(args.goal_id)
            human = "\n".join(
                f"{event.sequence:04d} {event.event_type}" for event in value
            )
            _emit(value, args.json, human)
            return 0

        if args.command == "agents":
            performance = repository.list_performance()
            value = [
                {
                    **_jsonable(agent),
                    "performance": [
                        _jsonable(record)
                        for record in performance
                        if record.agent_id == agent.agent_id
                    ],
                }
                for agent in repository.list_agents()
            ]
            _emit(value, args.json, f"{len(value)} registered agents")
            return 0

        if args.command == "evaluate":
            benchmark, champion, challenger, results = _load_evaluation_spec(args.spec)
            _ensure_evaluation_agent(repository, champion, benchmark[0].task_type)
            _ensure_evaluation_agent(repository, challenger, benchmark[0].task_type)
            value = BenchmarkEvaluator(
                repository,
                _ObservedBenchmarkRunner(results),
                _score_observed_result,
                PromotionPolicy(),
            ).evaluate(benchmark, champion, challenger)
            _emit(
                value,
                args.json,
                f"Evaluation {value.run_id}: "
                f"{'recommended' if value.recommended else 'rejected'}",
            )
            return 0 if value.recommended else 1

        if args.command == "evaluations":
            if args.run_id:
                run = repository.get_evaluation_run(args.run_id)
                if run is None:
                    raise KeyError(f"evaluation run not found: {args.run_id}")
                value = {
                    "run": run,
                    "outcomes": repository.list_evaluation_outcomes(args.run_id),
                }
                human = f"Evaluation {run.run_id}: {run.status.value}"
            else:
                value = repository.list_evaluation_runs()
                human = f"{len(value)} evaluation runs"
            _emit(value, args.json, human)
            return 0

        if args.command == "promote":
            value = repository.promote_evaluation(args.run_id, args.by)
            _emit(
                value,
                args.json,
                f"Deployed {value.champion_agent_id} for {value.task_type}",
            )
            return 0

        if args.command == "deployments":
            value = repository.list_deployments()
            _emit(value, args.json, f"{len(value)} active deployments")
            return 0

        if args.command == "maintain":
            provider = _provider_from_environment()
            workspace = Path(args.workspace).resolve()
            checks = _parse_check_declarations(args.checks)
            if args.apply and not checks:
                raise ValueError("--apply requires at least one --check NAME=COMMAND")
            if checks and not args.apply:
                raise ValueError("--check requires --apply")
            if args.publish and not args.apply:
                raise ValueError("--publish requires --apply")
            protected_database = _database_protected_paths(args.db, workspace)
            if args.publish and protected_database:
                raise ValueError("publication requires --db outside the workspace")
            pull_request_client = None
            if args.publish:
                pull_request_client = GitHubPullRequestClient(
                    os.environ.get("GITHUB_TOKEN", "")
                )
            engine = build_maintenance_engine(
                repository,
                provider,
                workspace,
                apply=args.apply,
                checks=checks,
                protected_paths=protected_database,
                publish=args.publish,
                pull_request_client=pull_request_client,
                github_repository=args.repository,
                base_branch=args.base,
                remote=args.remote,
                branch_prefix=args.branch_prefix,
            )
            goal_id = args.goal_id or _maintenance_goal_id(
                args.repository, args.issue
            )
            goal = repository.get_goal(goal_id)
            if goal is None:
                issue = GitHubIssueClient(
                    token=os.environ.get("GITHUB_TOKEN", "")
                ).get_issue(args.repository, args.issue)
                goal = create_maintenance_goal(
                    engine,
                    issue,
                    goal_id=goal_id,
                    apply=args.apply,
                    workspace=workspace if args.apply else None,
                    check_names=tuple(checks),
                    publish=args.publish,
                    base_branch=args.base,
                    remote=args.remote,
                    branch_prefix=args.branch_prefix,
                )
            else:
                stored_apply, stored_checks = maintenance_goal_configuration(goal)
                if stored_apply != args.apply or stored_checks != tuple(sorted(checks)):
                    raise ValueError(
                        "maintenance resume must use the original apply mode and check names"
                    )
                stored_publication = maintenance_publication_configuration(goal)
                requested_publication = {
                    "enabled": args.publish,
                    "base_branch": args.base,
                    "remote": args.remote,
                    "branch_prefix": args.branch_prefix,
                }
                if stored_publication != requested_publication:
                    raise ValueError(
                        "maintenance resume must use the original publication configuration"
                    )
            report = engine.resume(goal.goal_id)
            _emit(
                report,
                args.json,
                f"Goal {report.goal_id}: {report.status.value}",
            )
            if report.status.value == "succeeded":
                return 0
            if report.status.value == "paused":
                return 3
            return 1

        if args.command == "traces":
            if repository.get_goal(args.goal_id) is None:
                raise KeyError(f"goal not found: {args.goal_id}")
            value = repository.list_spans(args.goal_id)
            _emit(value, args.json, f"{len(value)} trace spans")
            return 0

        if args.command == "approvals":
            if repository.get_goal(args.goal_id) is None:
                raise KeyError(f"goal not found: {args.goal_id}")
            value = repository.list_approvals(args.goal_id)
            _emit(value, args.json, f"{len(value)} approval requests")
            return 0

        if args.command in {"approve", "reject"}:
            value = resolve_approval(
                repository,
                args.approval_id,
                approved=args.command == "approve",
                decided_by=args.by,
            )
            _emit(value, args.json, f"Approval {value.approval_id}: {value.status.value}")
            return 0

        if args.command == "genome":
            if args.genome_command == "set":
                value = _load_genome(args.agent_id, args.path)
                repository.save_agent_genome(value)
                _emit(value, args.json, f"Saved genome for {value.agent_id}")
                return 0
            if args.genome_command == "recombine":
                value = GenomeRecombiner(repository).recombine(
                    args.child_id,
                    args.parents,
                    task_type=args.task_type,
                )
                _emit(
                    value,
                    args.json,
                    f"Created child genome candidate {value.child.agent_id}",
                )
                return 0
            value = repository.get_agent_genome(args.agent_id)
            if value is None:
                raise KeyError(f"agent genome not found: {args.agent_id}")
            _emit(value, args.json, f"Genome {value.agent_id}: {value.role_seed}")
            return 0

        if args.command == "consolidate":
            if repository.get_goal(args.goal_id) is None:
                raise KeyError(f"goal not found: {args.goal_id}")
            value = ConsolidationEngine(repository).consolidate(
                args.goal_id, apply=args.apply
            )
            if getattr(args, "mneme_dir", ""):
                if not hasattr(repository, "append_event"):
                    raise RuntimeError("mneme linkage requires the SQLite path")
                from .mneme_bridge import push_seeds

                mneme = push_seeds(
                    repository,
                    mneme_dir=args.mneme_dir,
                    include_experience=True,
                    apply=args.apply,
                )
                _emit(
                    {"consolidation": value, "mneme": mneme},
                    args.json,
                    (
                        f"Consolidated {value.goal_id}: {value.replayed_attempts} "
                        f"replayed, {value.experiences_new} new experiences, "
                        f"{len(value.promotion_candidates)} promotion candidates"
                        f"{', applied' if value.applied else ' (dry-run)'}; "
                        f"mneme: {mneme.pushed} pushed, {mneme.refreshed} refreshed, "
                        f"{mneme.decay_refreshed} decay-refreshed"
                    ),
                )
            else:
                _emit(
                    value,
                    args.json,
                    (
                        f"Consolidated {value.goal_id}: {value.replayed_attempts} "
                        f"replayed, {value.experiences_new} new experiences, "
                        f"{len(value.promotion_candidates)} promotion candidates"
                        f"{', applied' if value.applied else ' (dry-run)'}"
                    ),
                )
            return 0

        if args.command == "mneme":
            if not hasattr(repository, "append_event"):
                raise RuntimeError("the mneme bridge requires the SQLite path")
            from .mneme_bridge import import_seeds, push_seeds

            if args.mneme_command == "sync":
                value = push_seeds(
                    repository,
                    mneme_dir=args.mneme_dir,
                    memory_type=args.memory_type,
                    limit=args.limit,
                    include_experience=args.include_experience,
                    apply=args.push,
                )
                _emit(
                    value,
                    args.json,
                    (
                        f"Mneme push: {value.planned} planned, {value.pushed} pushed, "
                        f"{value.refreshed} refreshed, "
                        f"{value.decay_refreshed} decay-refreshed"
                        f"{'' if value.applied else ' (dry-run)'}"
                    ),
                )
                return 0
            value = import_seeds(
                repository,
                mneme_dir=args.mneme_dir,
                memory_type=args.memory_type,
                limit=args.limit,
                apply=args.apply,
            )
            _emit(
                value,
                args.json,
                (
                    f"Mneme import: {value.candidates} candidates, "
                    f"{value.imported} imported"
                    f"{'' if value.applied else ' (dry-run)'}"
                ),
            )
            return 0

        if args.command == "experience":
            if args.experience_command == "distill":
                if repository.get_goal(args.goal_id) is None:
                    raise KeyError(f"goal not found: {args.goal_id}")
                value = ExperienceDistiller(repository).distill_goal(args.goal_id)
                _emit(value, args.json, f"Distilled {len(value)} experience records")
                return 0
            value = repository.list_experience(
                agent_id=args.agent_id,
                task_type=args.task_type,
                goal_id=args.goal_id,
                limit=args.limit,
            )
            _emit(value, args.json, f"{len(value)} experience records")
            return 0

        if args.command == "revision":
            if not hasattr(repository, "list_seed_revisions"):
                raise RuntimeError("seed revisions require the SQLite path")
            from .revisions import SeedRollback

            if args.revision_command == "list":
                value = repository.list_seed_revisions(
                    seed_kind=SeedKind(args.kind) if args.kind else None,
                    seed_id=args.seed_id,
                    limit=args.limit,
                )
                _emit(value, args.json, f"{len(value)} seed revisions")
                return 0
            if args.revision_command == "show":
                value = repository.get_seed_revision(args.revision_id)
                if value is None:
                    raise KeyError(f"revision not found: {args.revision_id}")
                _emit(
                    value,
                    args.json,
                    (
                        f"{value.revision_id}: {value.action.value} "
                        f"{value.seed_kind.value}/{value.seed_id} by {value.operator}"
                    ),
                )
                return 0
            value = SeedRollback(repository).rollback(
                args.revision_id,
                operator=args.by,
                reason=args.reason,
            )
            _emit(
                value,
                args.json,
                (
                    f"Rolled back {value.revision_id} ({value.seed_kind.value}/"
                    f"{value.seed_id}): {value.detail}"
                ),
            )
            return 0

        memory = MemoryManager(repository)
        if args.knowledge_command == "add":
            knowledge_id = memory.add_knowledge(args.title, args.content, args.tag)
            _emit(
                {"knowledge_id": knowledge_id},
                args.json,
                f"Added knowledge {knowledge_id}",
            )
            return 0
        results = memory.search_knowledge(args.query, args.tag, limit=args.limit)
        _emit(results, args.json, f"{len(results)} matching knowledge items")
        return 0
    except (KeyError, ValueError, OSError, RuntimeError) as error:
        message = error.args[0] if isinstance(error, KeyError) else str(error)
        print(f"error: {message}", file=sys.stderr)
        return 2
    finally:
        if repository is not None:
            repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
