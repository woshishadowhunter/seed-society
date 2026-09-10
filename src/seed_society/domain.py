"""Typed domain contracts for the Agent Society runtime."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Sequence
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_sensitive(value: Any, key: str = "") -> Any:
    sensitive = ("key", "token", "secret", "authorization", "password")
    if key and any(part in key.casefold() for part in sensitive):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            name: _redact_sensitive(item, str(name)) for name, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    return value


class GoalStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class ToolRisk(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SpanStatus(str, Enum):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


class PublicationStatus(str, Enum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    PUSHED = "pushed"
    PULL_REQUEST_CREATED = "pull_request_created"


class EvaluationStatus(str, Enum):
    EVALUATED = "evaluated"
    PROMOTED = "promoted"


class DelegationStatus(str, Enum):
    PREPARED = "prepared"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class PolicyVerdict(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class OutboxStatus(str, Enum):
    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Goal:
    goal_id: str
    title: str
    description: str
    status: GoalStatus = GoalStatus.CREATED
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    failure_reason: str = ""

    @classmethod
    def create(cls, title: str, description: str, goal_id: str | None = None) -> Goal:
        if not title.strip():
            raise ValueError("goal title must not be empty")
        if not description.strip():
            raise ValueError("goal description must not be empty")
        return cls(goal_id or f"goal-{uuid4().hex[:12]}", title.strip(), description.strip())


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    goal_id: str
    task_type: str
    description: str
    assigned_role: str = "worker"
    acceptance_criteria: dict[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    max_attempts: int = 3
    position: int = 0
    assigned_agent_id: str | None = None
    artifact_id: str | None = None

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        task_type: str,
        description: str,
        *,
        assigned_role: str = "worker",
        acceptance_criteria: dict[str, Any] | None = None,
        dependencies: Sequence[str] = (),
        context: dict[str, Any] | None = None,
        max_attempts: int = 3,
        position: int = 0,
    ) -> Task:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        for name, value in (
            ("goal_id", goal_id),
            ("task_id", task_id),
            ("task_type", task_type),
            ("description", description),
            ("assigned_role", assigned_role),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        return cls(
            task_id=task_id,
            goal_id=goal_id,
            task_type=task_type,
            description=description.strip(),
            assigned_role=assigned_role,
            acceptance_criteria=dict(acceptance_criteria or {}),
            dependencies=tuple(dependencies),
            context=dict(context or {}),
            max_attempts=max_attempts,
            position=position,
        )


@dataclass(frozen=True, slots=True)
class Defect:
    location: str
    issue: str
    suggestion: str


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    goal_id: str
    task_id: str
    agent_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        agent_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        if not content.strip():
            raise ValueError("artifact content must not be empty")
        return cls(
            f"artifact-{uuid4().hex[:12]}",
            goal_id,
            task_id,
            agent_id,
            content,
            dict(metadata or {}),
        )


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    goal_id: str
    task_id: str
    agent_id: str
    attempt_no: int
    duration_ms: float
    artifact_id: str | None
    review_id: str
    error: str = ""
    completed_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        agent_id: str,
        attempt_no: int,
        duration_ms: float,
        artifact_id: str | None,
        review_id: str,
        error: str = "",
    ) -> Attempt:
        if attempt_no < 1:
            raise ValueError("attempt_no must be positive")
        if duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        return cls(
            f"attempt-{uuid4().hex[:12]}",
            goal_id,
            task_id,
            agent_id,
            attempt_no,
            float(duration_ms),
            artifact_id,
            review_id,
            error,
        )


@dataclass(frozen=True, slots=True)
class Review:
    review_id: str
    goal_id: str
    task_id: str
    attempt_no: int
    verdict: Verdict
    score: float
    defects: tuple[Defect, ...]
    summary: str
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        attempt_no: int,
        verdict: Verdict,
        score: float,
        defects: Iterable[Defect],
        summary: str,
    ) -> Review:
        if not 0 <= score <= 100:
            raise ValueError("review score must be between 0 and 100")
        if attempt_no < 1:
            raise ValueError("attempt_no must be positive")
        return cls(
            review_id=f"review-{uuid4().hex[:12]}",
            goal_id=goal_id,
            task_id=task_id,
            attempt_no=attempt_no,
            verdict=verdict,
            score=float(score),
            defects=tuple(defects),
            summary=summary,
        )


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    goal_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str = field(default_factory=utc_now)
    sequence: int = 0

    @classmethod
    def create(cls, goal_id: str, event_type: str, payload: dict[str, Any]) -> Event:
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        return cls(f"event-{uuid4().hex[:12]}", goal_id, event_type, dict(payload))


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_id: str
    fingerprint: str
    goal_id: str
    task_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: str = field(default_factory=utc_now)
    decided_at: str = ""
    decided_by: str = ""

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str,
    ) -> ApprovalRequest:
        for name, value in (
            ("goal_id", goal_id),
            ("task_id", task_id),
            ("tool_name", tool_name),
            ("reason", reason),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        canonical = json.dumps(
            {
                "goal_id": goal_id,
                "task_id": task_id,
                "tool_name": tool_name,
                "arguments": arguments,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(
            approval_id=f"approval-{fingerprint[:16]}",
            fingerprint=fingerprint,
            goal_id=goal_id,
            task_id=task_id,
            tool_name=tool_name,
            arguments=_redact_sensitive(arguments),
            reason=reason.strip(),
        )

    def resolve(self, status: ApprovalStatus, decided_by: str) -> ApprovalRequest:
        if self.status != ApprovalStatus.PENDING:
            raise ValueError("approval request is already resolved")
        if status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("approval resolution must be approved or rejected")
        if not decided_by.strip():
            raise ValueError("decided_by must not be empty")
        return replace(
            self,
            status=status,
            decided_at=utc_now(),
            decided_by=decided_by.strip(),
        )


@dataclass(frozen=True, slots=True)
class TraceSpan:
    span_id: str
    trace_id: str
    goal_id: str
    task_id: str | None
    agent_id: str | None
    parent_span_id: str | None
    kind: str
    name: str
    status: SpanStatus
    started_at: str
    ended_at: str = ""
    duration_ms: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    error_category: str = ""

    @classmethod
    def start(
        cls,
        goal_id: str,
        task_id: str | None,
        agent_id: str | None,
        kind: str,
        name: str,
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> TraceSpan:
        if not goal_id.strip() or not kind.strip() or not name.strip():
            raise ValueError("goal_id, kind, and name must not be empty")
        return cls(
            span_id=f"span-{uuid4().hex[:16]}",
            trace_id=trace_id or goal_id,
            goal_id=goal_id,
            task_id=task_id,
            agent_id=agent_id,
            parent_span_id=parent_span_id,
            kind=kind.strip(),
            name=name.strip(),
            status=SpanStatus.RUNNING,
            started_at=utc_now(),
            attributes=dict(attributes or {}),
        )

    def finish(
        self,
        status: SpanStatus,
        *,
        error_category: str = "",
        attributes: dict[str, Any] | None = None,
    ) -> TraceSpan:
        if self.status != SpanStatus.RUNNING:
            raise ValueError("trace span is already finished")
        if status == SpanStatus.RUNNING:
            raise ValueError("finished trace span cannot remain running")
        ended_at = utc_now()
        started = datetime.fromisoformat(self.started_at)
        ended = datetime.fromisoformat(ended_at)
        merged = dict(self.attributes)
        merged.update(attributes or {})
        return replace(
            self,
            status=status,
            ended_at=ended_at,
            duration_ms=max(0.0, (ended - started).total_seconds() * 1000.0),
            attributes=merged,
            error_category=error_category,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    goal_id: str
    path: str
    original_exists: bool
    original_content: str
    original_sha256: str
    previous_sha256: str
    latest_sha256: str
    restored: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        path: str,
        original_exists: bool,
        original_content: str,
        original_sha256: str,
        latest_sha256: str,
    ) -> WorkspaceSnapshot:
        for name, value in (
            ("goal_id", goal_id),
            ("path", path),
            ("original_sha256", original_sha256),
            ("latest_sha256", latest_sha256),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not original_exists and original_content:
            raise ValueError("missing original file cannot have content")
        return cls(
            goal_id.strip(),
            path.strip(),
            original_exists,
            original_content,
            original_sha256.strip(),
            original_sha256.strip(),
            latest_sha256.strip(),
        )

    def advance(self, latest_sha256: str, *, restored: bool = False) -> WorkspaceSnapshot:
        if not latest_sha256.strip():
            raise ValueError("latest_sha256 must not be empty")
        return replace(
            self,
            previous_sha256=self.latest_sha256,
            latest_sha256=latest_sha256.strip(),
            restored=restored,
            updated_at=utc_now(),
        )


@dataclass(frozen=True, slots=True)
class VerificationResult:
    result_id: str
    goal_id: str
    task_id: str
    check_name: str
    command: tuple[str, ...]
    passed: bool
    exit_code: int
    duration_ms: float
    stdout: str
    stderr: str
    workspace_digest: str
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        check_name: str,
        command: Sequence[str],
        passed: bool,
        exit_code: int,
        duration_ms: float,
        stdout: str,
        stderr: str,
        workspace_digest: str,
    ) -> VerificationResult:
        if not goal_id.strip() or not task_id.strip() or not check_name.strip():
            raise ValueError("goal_id, task_id, and check_name must not be empty")
        normalized_command = tuple(str(part) for part in command)
        if not normalized_command or any(not part for part in normalized_command):
            raise ValueError("verification command must not be empty")
        if duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        if not workspace_digest.strip():
            raise ValueError("workspace_digest must not be empty")
        return cls(
            f"verification-{uuid4().hex[:16]}",
            goal_id.strip(),
            task_id.strip(),
            check_name.strip(),
            normalized_command,
            bool(passed),
            int(exit_code),
            float(duration_ms),
            stdout,
            stderr,
            workspace_digest.strip(),
        )


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    publication_id: str
    goal_id: str
    payload_digest: str
    repository: str
    remote: str
    branch: str
    base_branch: str
    title: str
    body: str
    base_head_sha: str
    changed_paths: tuple[str, ...]
    workspace_digest: str
    check_names: tuple[str, ...]
    status: PublicationStatus = PublicationStatus.PREPARED
    commit_sha: str = ""
    pull_request_number: int = 0
    pull_request_url: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(cls, goal_id: str, payload: dict[str, Any]) -> PublicationRecord:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        required = ("repository", "remote", "branch", "base_branch", "title", "body", "base_head_sha", "workspace_digest")
        if not goal_id.strip() or any(not str(payload.get(name, "")).strip() for name in required):
            raise ValueError("publication identity fields must not be empty")
        paths = tuple(sorted(str(item) for item in payload.get("changed_paths", ())))
        checks = tuple(sorted(str(item) for item in payload.get("check_names", ())))
        if not paths or not checks:
            raise ValueError("publication requires changed paths and checks")
        return cls(
            f"publication-{digest[:16]}", goal_id, digest,
            str(payload["repository"]), str(payload["remote"]), str(payload["branch"]),
            str(payload["base_branch"]), str(payload["title"]), str(payload["body"]),
            str(payload["base_head_sha"]), paths, str(payload["workspace_digest"]), checks,
        )

    def advance(
        self,
        status: PublicationStatus,
        *,
        commit_sha: str = "",
        pull_request_number: int = 0,
        pull_request_url: str = "",
    ) -> PublicationRecord:
        order = list(PublicationStatus)
        if order.index(status) < order.index(self.status):
            raise ValueError("publication status cannot move backwards")
        if order.index(status) > order.index(self.status) + 1:
            raise ValueError("publication status cannot skip a state")
        return replace(
            self,
            status=status,
            commit_sha=commit_sha or self.commit_sha,
            pull_request_number=pull_request_number or self.pull_request_number,
            pull_request_url=pull_request_url or self.pull_request_url,
            updated_at=utc_now(),
        )


@dataclass(frozen=True, slots=True)
class AgentProfile:
    agent_id: str
    role: str
    model_id: str
    task_types: tuple[str, ...] = ("*",)
    enabled: bool = True
    execution_kind: str = "local"

    def __post_init__(self) -> None:
        if self.execution_kind not in {"local", "a2a"}:
            raise ValueError("agent execution_kind must be local or a2a")


@dataclass(frozen=True, slots=True)
class PerformanceRecord:
    agent_id: str
    task_type: str
    attempts: int = 0
    passes: int = 0
    avg_score: float = 0.0
    avg_duration_ms: float = 0.0
    recent_results: tuple[dict[str, Any], ...] = ()

    @property
    def success_rate(self) -> float:
        return self.passes / self.attempts if self.attempts else 0.5


_GENOME_RISK_POLICIES = frozenset(
    {"read_only", "approval_required", "sandboxed", "operator_managed"}
)


def _sorted_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))


@dataclass(frozen=True, slots=True)
class AgentSelfModel:
    mission: str
    success_signals: tuple[str, ...] = ()
    failure_modes: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        mission: str,
        success_signals: Sequence[str] = (),
        failure_modes: Sequence[str] = (),
    ) -> AgentSelfModel:
        if not str(mission).strip():
            raise ValueError("self model mission must not be empty")
        return cls(
            str(mission).strip(),
            _sorted_unique(success_signals),
            _sorted_unique(failure_modes),
        )


@dataclass(frozen=True, slots=True)
class AgentGenome:
    agent_id: str
    base_model: str
    role_seed: str
    self_model: AgentSelfModel
    traits: tuple[str, ...] = ()
    tool_profile: tuple[str, ...] = ()
    memory_profile: dict[str, Any] = field(default_factory=dict)
    risk_policy: str = "read_only"
    parents: tuple[str, ...] = ()
    generation: int = 1
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        agent_id: str,
        *,
        base_model: str,
        role_seed: str,
        self_model: AgentSelfModel | None = None,
        traits: Sequence[str] = (),
        tool_profile: Sequence[str] = (),
        memory_profile: dict[str, Any] | None = None,
        risk_policy: str = "read_only",
        parents: Sequence[str] = (),
        generation: int = 1,
    ) -> AgentGenome:
        normalized_agent_id = str(agent_id).strip()
        if not normalized_agent_id:
            raise ValueError("agent ID must not be empty")
        if not str(base_model).strip() or not str(role_seed).strip():
            raise ValueError("base model and role seed must not be empty")
        normalized_policy = str(risk_policy).strip()
        if normalized_policy not in _GENOME_RISK_POLICIES:
            raise ValueError("risk policy is not supported")
        if isinstance(generation, bool) or int(generation) < 1:
            raise ValueError("generation must be a positive integer")
        return cls(
            normalized_agent_id,
            str(base_model).strip(),
            str(role_seed).strip(),
            self_model
            or AgentSelfModel.create(mission=f"Serve as {normalized_agent_id}"),
            _sorted_unique(traits),
            _sorted_unique(tool_profile),
            dict(memory_profile or {}),
            normalized_policy,
            _sorted_unique(parents),
            int(generation),
        )


@dataclass(frozen=True, slots=True)
class ExperienceRecord:
    experience_id: str
    goal_id: str
    task_id: str
    task_type: str
    agent_id: str
    attempt_no: int
    verdict: str
    score: float
    lessons: tuple[str, ...]
    tags: tuple[str, ...] = ()
    artifact_excerpt: str = ""
    created_at: str = field(default_factory=utc_now)
    valence: float = 0.0
    salience: float = 0.5
    strength: float = 1.0
    activations: int = 0
    last_activated_at: str = ""

    def __post_init__(self) -> None:
        if not -1 <= float(self.valence) <= 1:
            raise ValueError("experience valence must be between -1 and 1")
        for name, value in (("salience", self.salience), ("strength", self.strength)):
            if not 0 <= float(value) <= 1:
                raise ValueError(f"experience {name} must be between 0 and 1")
        if self.activations < 0:
            raise ValueError("experience activations must not be negative")

    @classmethod
    def create(
        cls,
        task: Task,
        review: Review,
        artifact: Artifact | None,
        agent_id: str,
        *,
        lessons: Sequence[str],
        tags: Sequence[str] = (),
        valence: float | None = None,
        salience: float | None = None,
        strength: float | None = None,
    ) -> ExperienceRecord:
        normalized_lessons = tuple(
            str(lesson).strip()[:240]
            for lesson in lessons
            if str(lesson).strip()
        )
        if not normalized_lessons:
            raise ValueError("experience lessons must not be empty")
        normalized_agent_id = str(agent_id).strip()
        if not normalized_agent_id:
            raise ValueError("experience agent ID must not be empty")
        payload = {
            "goal_id": task.goal_id,
            "task_id": task.task_id,
            "task_type": task.task_type,
            "agent_id": normalized_agent_id,
            "attempt_no": review.attempt_no,
            "verdict": review.verdict.value,
            "score": float(review.score),
            "lessons": normalized_lessons,
            "tags": _sorted_unique(tags),
            "defects": [
                {
                    "location": defect.location,
                    "issue": defect.issue,
                    "suggestion": defect.suggestion,
                }
                for defect in review.defects
            ],
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        excerpt = (artifact.content if artifact is not None else "")[:500]
        normalized_valence = (
            float(valence)
            if valence is not None
            else (
                float(review.score) / 100.0
                if review.verdict == Verdict.PASS
                else -float(review.score) / 100.0
            )
        )
        normalized_salience = 0.5 if salience is None else float(salience)
        normalized_strength = (
            0.5 + 0.5 * normalized_salience if strength is None else float(strength)
        )
        return cls(
            f"experience-{digest[:16]}",
            task.goal_id,
            task.task_id,
            task.task_type,
            normalized_agent_id,
            int(review.attempt_no),
            review.verdict.value,
            float(review.score),
            normalized_lessons,
            _sorted_unique(tags),
            excerpt,
            utc_now(),
            min(1.0, max(-1.0, normalized_valence)),
            min(1.0, max(0.0, normalized_salience)),
            min(1.0, max(0.0, normalized_strength)),
            0,
            "",
        )

    def reactivate(self, *, now: str, gain: float = 0.5) -> ExperienceRecord:
        """Retrieval re-strengthens the trace (reconsolidation / 现行熏种子)."""
        if not 0 < gain <= 1:
            raise ValueError("experience reactivation gain must be in (0, 1]")
        return replace(
            self,
            strength=min(1.0, self.strength + (1.0 - self.strength) * gain),
            activations=self.activations + 1,
            last_activated_at=now,
        )

    def decay(self, *, factor: float) -> ExperienceRecord:
        """Multiply seed strength by an Ebbinghaus-style decay factor."""
        if not 0 <= factor <= 1:
            raise ValueError("experience decay factor must be between 0 and 1")
        return replace(self, strength=max(0.0, self.strength * factor))


@dataclass(frozen=True, slots=True)
class GenomeRecombinationReport:
    child: AgentGenome
    task_type: str
    parents: tuple[str, ...]
    supporting_experience: tuple[str, ...] = ()
    inherited_traits: tuple[str, ...] = ()
    safety_notes: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    task_type: str
    input: dict[str, Any]
    acceptance_criteria: dict[str, Any]
    context: dict[str, Any] = field(default_factory=dict)
    critical: bool = False

    @classmethod
    def create(
        cls,
        case_id: str,
        task_type: str,
        input: dict[str, Any],
        acceptance_criteria: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        critical: bool = False,
    ) -> BenchmarkCase:
        if not case_id.strip() or not task_type.strip():
            raise ValueError("benchmark case ID and task type must not be empty")
        if not acceptance_criteria:
            raise ValueError("benchmark acceptance criteria must not be empty")
        return cls(
            case_id.strip(),
            task_type.strip(),
            dict(input),
            dict(acceptance_criteria),
            dict(context or {}),
            bool(critical),
        )


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    agent_id: str
    model_id: str

    def __post_init__(self) -> None:
        if not self.agent_id.strip() or not self.model_id.strip():
            raise ValueError("candidate agent and model IDs must not be empty")


@dataclass(frozen=True, slots=True)
class CandidateExecution:
    output: Any
    duration_ms: float
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("candidate duration must not be negative")
        object.__setattr__(self, "evidence", dict(self.evidence))


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    passed: bool
    score: float

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 100:
            raise ValueError("case score must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    outcome_id: str
    run_id: str
    case_id: str
    candidate_agent_id: str
    candidate_model_id: str
    passed: bool
    score: float
    duration_ms: float
    critical: bool
    error: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


_REMOTE_CONTEXT_SECTIONS = frozenset(
    {
        "goal",
        "task_context",
        "dependency_artifacts",
        "review_feedback",
        "knowledge",
    }
)

_ATTESTATION_KINDS = frozenset({"a2a-tck"})
_SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}")
_SAFE_REASON_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


def _safe_id(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not _SAFE_ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} is invalid")
    return normalized


def _record_identity(value: str, label: str) -> str:
    normalized = str(value).strip()
    if (
        not normalized
        or len(normalized) > 256
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(f"{label} is invalid")
    return normalized


def _normalized_values(values: Sequence[str], label: str) -> tuple[str, ...]:
    normalized = tuple(sorted({_safe_id(value, label) for value in values}))
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def _sha256_digest(value: str, label: str) -> str:
    normalized = str(value).strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError(f"{label} SHA-256 must be 64 hexadecimal characters")
    return normalized


def _positive_integer(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _aware_timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise ValueError(f"{label} timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} timestamp must include a timezone")
    return parsed.isoformat()


@dataclass(frozen=True, slots=True)
class DelegationRule:
    rule_id: str
    task_types: tuple[str, ...]
    agent_ids: tuple[str, ...]
    card_sha256s: tuple[str, ...]
    allowed_context_sections: tuple[str, ...]
    max_request_bytes: int
    max_result_bytes: int
    max_polls: int
    total_timeout_seconds: int
    required_attestation_kinds: tuple[str, ...]
    max_attestation_age_hours: int

    @classmethod
    def create(
        cls,
        rule_id: str,
        task_types: Sequence[str],
        agent_ids: Sequence[str],
        card_sha256s: Sequence[str],
        *,
        allowed_context_sections: Sequence[str],
        max_request_bytes: int,
        max_result_bytes: int,
        max_polls: int,
        total_timeout_seconds: int,
        required_attestation_kinds: Sequence[str],
        max_attestation_age_hours: int,
    ) -> DelegationRule:
        contexts = tuple(
            sorted({str(item).strip() for item in allowed_context_sections})
        )
        if any(item not in _REMOTE_CONTEXT_SECTIONS for item in contexts):
            raise ValueError("remote context section is not allowed")
        attestation_kinds = tuple(
            sorted({str(item).strip() for item in required_attestation_kinds})
        )
        if not attestation_kinds or any(
            item not in _ATTESTATION_KINDS for item in attestation_kinds
        ):
            raise ValueError("required attestation kind is not allowed")
        digests = tuple(
            sorted({_sha256_digest(value, "card") for value in card_sha256s})
        )
        if not digests:
            raise ValueError("card SHA-256 list must not be empty")
        return cls(
            rule_id=_safe_id(rule_id, "rule ID"),
            task_types=_normalized_values(task_types, "task type"),
            agent_ids=_normalized_values(agent_ids, "agent ID"),
            card_sha256s=digests,
            allowed_context_sections=contexts,
            max_request_bytes=_positive_integer(max_request_bytes, "max request bytes"),
            max_result_bytes=_positive_integer(max_result_bytes, "max result bytes"),
            max_polls=_positive_integer(max_polls, "max polls"),
            total_timeout_seconds=_positive_integer(
                total_timeout_seconds, "total timeout seconds"
            ),
            required_attestation_kinds=attestation_kinds,
            max_attestation_age_hours=_positive_integer(
                max_attestation_age_hours, "max attestation age hours"
            ),
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "task_types": list(self.task_types),
            "agent_ids": list(self.agent_ids),
            "card_sha256s": list(self.card_sha256s),
            "allowed_context_sections": list(self.allowed_context_sections),
            "max_request_bytes": self.max_request_bytes,
            "max_result_bytes": self.max_result_bytes,
            "max_polls": self.max_polls,
            "total_timeout_seconds": self.total_timeout_seconds,
            "required_attestation_kinds": list(self.required_attestation_kinds),
            "max_attestation_age_hours": self.max_attestation_age_hours,
        }


@dataclass(frozen=True, slots=True)
class DelegationPolicy:
    policy_id: str
    version: int
    default: str
    rules: tuple[DelegationRule, ...]
    policy_digest: str
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        policy_id: str,
        version: int,
        rules: Sequence[DelegationRule],
        *,
        default: str = "deny",
    ) -> DelegationPolicy:
        if default != "deny":
            raise ValueError("delegation policy default must be deny")
        normalized_rules = tuple(sorted(rules, key=lambda item: item.rule_id))
        if not normalized_rules:
            raise ValueError("delegation policy rules must not be empty")
        if len({rule.rule_id for rule in normalized_rules}) != len(normalized_rules):
            raise ValueError("delegation policy rule IDs must be unique")
        for index, left in enumerate(normalized_rules):
            for right in normalized_rules[index + 1 :]:
                if (
                    set(left.task_types) & set(right.task_types)
                    and set(left.agent_ids) & set(right.agent_ids)
                    and set(left.card_sha256s) & set(right.card_sha256s)
                ):
                    raise ValueError("delegation policy rule domains overlap")
        normalized_id = _safe_id(policy_id, "policy ID")
        normalized_version = _positive_integer(version, "policy version")
        payload = {
            "policy_id": normalized_id,
            "version": normalized_version,
            "default": default,
            "rules": [rule.canonical_payload() for rule in normalized_rules],
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return cls(
            policy_id=normalized_id,
            version=normalized_version,
            default=default,
            rules=normalized_rules,
            policy_digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class PolicyActivation:
    task_type: str
    policy_digest: str
    policy_id: str
    policy_version: int
    activated_by: str
    activated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls, task_type: str, policy: DelegationPolicy, activated_by: str
    ) -> PolicyActivation:
        normalized_task_type = _safe_id(task_type, "task type")
        if not any(normalized_task_type in rule.task_types for rule in policy.rules):
            raise ValueError("policy does not govern the activated task type")
        return cls(
            task_type=normalized_task_type,
            policy_digest=policy.policy_digest,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            activated_by=_safe_id(activated_by, "activating operator"),
        )


@dataclass(frozen=True, slots=True)
class ConformanceAttestation:
    attestation_id: str
    agent_id: str
    card_sha256: str
    kind: str
    report_sha256: str
    source_revision: str
    tool_version: str
    spec_version: str
    observed_at: str
    passed: bool
    metrics: dict[str, float | int]
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        *,
        agent_id: str,
        card_sha256: str,
        kind: str,
        report_sha256: str,
        source_revision: str,
        tool_version: str,
        spec_version: str,
        observed_at: str,
        passed: bool,
        metrics: dict[str, float | int],
    ) -> ConformanceAttestation:
        normalized_kind = str(kind).strip()
        if normalized_kind not in _ATTESTATION_KINDS:
            raise ValueError("attestation kind is not allowed")
        revision = str(source_revision).strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("attestation source revision must be a 40-character SHA")
        version = str(tool_version).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}", version):
            raise ValueError("attestation tool version is invalid")
        normalized_metrics: dict[str, float | int] = {}
        for name, value in metrics.items():
            metric_name = _safe_id(name, "attestation metric")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("attestation metric values must be numeric")
            if not math.isfinite(float(value)):
                raise ValueError("attestation metric values must be finite")
            if "compatibility" in metric_name and not 0 <= float(value) <= 100:
                raise ValueError("attestation percentage must be between 0 and 100")
            normalized_metrics[metric_name] = value
        report_digest = _sha256_digest(report_sha256, "report")
        return cls(
            attestation_id=f"attestation-{report_digest[:16]}",
            agent_id=_safe_id(agent_id, "agent ID"),
            card_sha256=_sha256_digest(card_sha256, "card"),
            kind=normalized_kind,
            report_sha256=report_digest,
            source_revision=revision,
            tool_version=version,
            spec_version=str(spec_version).strip(),
            observed_at=_aware_timestamp(observed_at, "attestation observation"),
            passed=bool(passed),
            metrics=normalized_metrics,
        )


@dataclass(frozen=True, slots=True)
class RemoteAgentRegistration:
    agent_id: str
    card_url: str
    card_sha256: str
    interface_url: str
    skill_by_task_type: dict[str, str]
    tenant: str = ""
    auth_env: str = ""
    allowed_context_sections: tuple[str, ...] = ("review_feedback",)
    allow_insecure_localhost: bool = False
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        agent_id: str,
        card_url: str,
        card_sha256: str,
        interface_url: str,
        skill_by_task_type: dict[str, str],
        *,
        tenant: str = "",
        auth_env: str = "",
        allowed_context_sections: Sequence[str] = ("review_feedback",),
        allow_insecure_localhost: bool = False,
    ) -> RemoteAgentRegistration:
        if not agent_id.strip() or not card_url.strip() or not interface_url.strip():
            raise ValueError("remote agent identity and URLs must not be empty")
        digest = card_sha256.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("card SHA-256 must be 64 hexadecimal characters")
        normalized_skills = {
            str(task_type).strip(): str(skill_id).strip()
            for task_type, skill_id in skill_by_task_type.items()
        }
        if not normalized_skills or any(
            not task_type or not skill_id
            for task_type, skill_id in normalized_skills.items()
        ):
            raise ValueError("remote agent skill mapping must not be empty")
        if auth_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", auth_env):
            raise ValueError("authentication environment variable name is invalid")
        sections = tuple(sorted({str(item).strip() for item in allowed_context_sections}))
        if any(item not in _REMOTE_CONTEXT_SECTIONS for item in sections):
            raise ValueError("remote context section is not allowed")
        return cls(
            agent_id=agent_id.strip(),
            card_url=card_url.strip(),
            card_sha256=digest,
            interface_url=interface_url.strip().rstrip("/"),
            skill_by_task_type=normalized_skills,
            tenant=tenant.strip(),
            auth_env=auth_env,
            allowed_context_sections=sections,
            allow_insecure_localhost=bool(allow_insecure_localhost),
        )

    @property
    def model_id(self) -> str:
        return f"a2a:{self.card_sha256}"

    @property
    def task_types(self) -> tuple[str, ...]:
        return tuple(sorted(self.skill_by_task_type))


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision_id: str
    goal_id: str
    task_id: str
    attempt_no: int
    agent_id: str
    model_id: str
    card_sha256: str
    verdict: PolicyVerdict
    policy_digest: str
    policy_rule_id: str
    reason_codes: tuple[str, ...]
    allowed_context_sections: tuple[str, ...]
    max_request_bytes: int
    max_result_bytes: int
    max_polls: int
    total_timeout_seconds: int
    attestation_ids: tuple[str, ...]
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        attempt_no: int,
        registration: RemoteAgentRegistration,
        verdict: PolicyVerdict,
        *,
        policy_digest: str = "",
        policy_rule_id: str = "",
        reason_codes: Sequence[str],
        allowed_context_sections: Sequence[str] = (),
        max_request_bytes: int = 0,
        max_result_bytes: int = 0,
        max_polls: int = 0,
        total_timeout_seconds: int = 0,
        attestation_ids: Sequence[str] = (),
    ) -> PolicyDecision:
        if attempt_no < 1:
            raise ValueError("policy decision attempt number must be positive")
        reasons = tuple(sorted({str(item).strip() for item in reason_codes}))
        if not reasons or any(not _SAFE_REASON_PATTERN.fullmatch(item) for item in reasons):
            raise ValueError("policy decision reason code is invalid")
        contexts = tuple(
            sorted({str(item).strip() for item in allowed_context_sections})
        )
        if any(item not in _REMOTE_CONTEXT_SECTIONS for item in contexts):
            raise ValueError("policy decision context section is not allowed")
        normalized_verdict = PolicyVerdict(verdict)
        digest = ""
        if policy_digest:
            digest = _sha256_digest(policy_digest, "policy")
        rule_id = ""
        if policy_rule_id:
            rule_id = _safe_id(policy_rule_id, "policy rule ID")
        limits = (
            max_request_bytes,
            max_result_bytes,
            max_polls,
            total_timeout_seconds,
        )
        if normalized_verdict == PolicyVerdict.ALLOW:
            if not digest or not rule_id:
                raise ValueError("allowed policy decision requires policy and rule identity")
            if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in limits):
                raise ValueError("allowed policy decision limits must be positive")
        elif any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in limits):
            raise ValueError("denied policy decision limits must not be negative")
        normalized_attestations = tuple(
            sorted({_safe_id(item, "attestation ID") for item in attestation_ids})
        )
        return cls(
            decision_id=f"decision-{uuid4().hex[:16]}",
            goal_id=_record_identity(goal_id, "goal ID"),
            task_id=_record_identity(task_id, "task ID"),
            attempt_no=attempt_no,
            agent_id=registration.agent_id,
            model_id=registration.model_id,
            card_sha256=registration.card_sha256,
            verdict=normalized_verdict,
            policy_digest=digest,
            policy_rule_id=rule_id,
            reason_codes=reasons,
            allowed_context_sections=contexts,
            max_request_bytes=max_request_bytes,
            max_result_bytes=max_result_bytes,
            max_polls=max_polls,
            total_timeout_seconds=total_timeout_seconds,
            attestation_ids=normalized_attestations,
        )


_DELEGATION_TRANSITIONS: dict[DelegationStatus, frozenset[DelegationStatus]] = {
    DelegationStatus.PREPARED: frozenset({DelegationStatus.SUBMITTING}),
    DelegationStatus.SUBMITTING: frozenset(
        {
            DelegationStatus.ACCEPTED,
            DelegationStatus.COMPLETED,
            DelegationStatus.FAILED,
            DelegationStatus.REJECTED,
            DelegationStatus.INTERRUPTED,
            DelegationStatus.UNKNOWN,
        }
    ),
    DelegationStatus.ACCEPTED: frozenset(
        {
            DelegationStatus.ACCEPTED,
            DelegationStatus.COMPLETED,
            DelegationStatus.FAILED,
            DelegationStatus.CANCELED,
            DelegationStatus.REJECTED,
            DelegationStatus.INTERRUPTED,
        }
    ),
    DelegationStatus.INTERRUPTED: frozenset({DelegationStatus.CANCELED}),
    DelegationStatus.COMPLETED: frozenset(),
    DelegationStatus.FAILED: frozenset(),
    DelegationStatus.CANCELED: frozenset(),
    DelegationStatus.REJECTED: frozenset(),
    DelegationStatus.UNKNOWN: frozenset(),
}


@dataclass(frozen=True, slots=True)
class DelegationRecord:
    delegation_id: str
    goal_id: str
    task_id: str
    attempt_no: int
    agent_id: str
    model_id: str
    card_sha256: str
    message_id: str
    payload_sha256: str
    status: DelegationStatus = DelegationStatus.PREPARED
    remote_task_id: str = ""
    remote_task_state: str = ""
    poll_count: int = 0
    result_content: str = ""
    result_sha256: str = ""
    error_category: str = ""
    canceled_by: str = ""
    policy_decision_id: str = ""
    policy_digest: str = ""
    policy_rule_id: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        attempt_no: int,
        registration: RemoteAgentRegistration,
        message_id: str,
        payload_sha256: str,
        *,
        policy_decision: PolicyDecision | None = None,
    ) -> DelegationRecord:
        values = (goal_id, task_id, message_id, payload_sha256)
        if any(not value.strip() for value in values):
            raise ValueError("delegation identity fields must not be empty")
        if attempt_no < 1:
            raise ValueError("delegation attempt number must be positive")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", payload_sha256):
            raise ValueError("delegation payload SHA-256 is invalid")
        if policy_decision is not None and (
            policy_decision.verdict != PolicyVerdict.ALLOW
            or policy_decision.goal_id != goal_id.strip()
            or policy_decision.task_id != task_id.strip()
            or policy_decision.attempt_no != attempt_no
            or policy_decision.agent_id != registration.agent_id
            or policy_decision.model_id != registration.model_id
            or policy_decision.card_sha256 != registration.card_sha256
        ):
            raise ValueError("delegation policy decision does not match delegation")
        return cls(
            delegation_id=f"delegation-{uuid4().hex[:16]}",
            goal_id=goal_id.strip(),
            task_id=task_id.strip(),
            attempt_no=attempt_no,
            agent_id=registration.agent_id,
            model_id=registration.model_id,
            card_sha256=registration.card_sha256,
            message_id=message_id.strip(),
            payload_sha256=payload_sha256.casefold(),
            policy_decision_id=(
                policy_decision.decision_id if policy_decision is not None else ""
            ),
            policy_digest=(
                policy_decision.policy_digest if policy_decision is not None else ""
            ),
            policy_rule_id=(
                policy_decision.policy_rule_id if policy_decision is not None else ""
            ),
        )

    def advance(
        self,
        status: DelegationStatus,
        *,
        remote_task_id: str = "",
        remote_task_state: str = "",
        increment_poll: bool = False,
        result_content: str = "",
        error_category: str = "",
        canceled_by: str = "",
    ) -> DelegationRecord:
        if not _DELEGATION_TRANSITIONS[self.status]:
            raise ValueError(f"delegation is terminal in state {self.status.value}")
        if status not in _DELEGATION_TRANSITIONS[self.status]:
            raise ValueError(
                f"invalid delegation transition: {self.status.value} -> {status.value}"
            )
        next_task_id = remote_task_id.strip() or self.remote_task_id
        if self.remote_task_id and remote_task_id and remote_task_id != self.remote_task_id:
            raise ValueError("remote task identity cannot change")
        if status == DelegationStatus.ACCEPTED and not next_task_id:
            raise ValueError("accepted delegation requires a remote task ID")
        if status == DelegationStatus.COMPLETED and not result_content.strip():
            raise ValueError("completed delegation requires result content")
        if status == DelegationStatus.UNKNOWN and not error_category.strip():
            raise ValueError("unknown delegation requires an error category")
        if error_category and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", error_category):
            raise ValueError("delegation error category must be a safe short value")
        if increment_poll and status != DelegationStatus.ACCEPTED:
            raise ValueError("only accepted delegation polling increments the count")
        normalized_result = result_content if status == DelegationStatus.COMPLETED else ""
        result_digest = (
            hashlib.sha256(normalized_result.encode("utf-8")).hexdigest()
            if normalized_result
            else ""
        )
        return replace(
            self,
            status=status,
            remote_task_id=next_task_id,
            remote_task_state=remote_task_state.strip() or self.remote_task_state,
            poll_count=self.poll_count + int(increment_poll),
            result_content=normalized_result or self.result_content,
            result_sha256=result_digest or self.result_sha256,
            error_category=error_category.strip() or self.error_category,
            canceled_by=canceled_by.strip() or self.canceled_by,
            updated_at=utc_now(),
        )


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    run_id: str
    task_type: str
    benchmark_digest: str
    champion_agent_id: str
    champion_model_id: str
    challenger_agent_id: str
    challenger_model_id: str
    case_count: int
    metrics: dict[str, Any]
    recommended: bool
    failed_gates: tuple[str, ...]
    status: EvaluationStatus = EvaluationStatus.EVALUATED
    promoted_by: str = ""
    promoted_at: str = ""
    created_at: str = field(default_factory=utc_now)

    def promote(self, promoted_by: str) -> EvaluationRun:
        if not self.recommended:
            raise ValueError("evaluation is not recommended for promotion")
        if self.status != EvaluationStatus.EVALUATED:
            raise ValueError("evaluation is already promoted")
        if not promoted_by.strip():
            raise ValueError("promoted_by must not be empty")
        return replace(
            self,
            status=EvaluationStatus.PROMOTED,
            promoted_by=promoted_by.strip(),
            promoted_at=utc_now(),
        )


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    task_type: str
    champion_agent_id: str
    champion_model_id: str
    source_run_id: str
    promoted_by: str
    created_at: str = field(default_factory=utc_now)


class SeedKind(str, Enum):
    """Seed collections that carry revision history and can be rolled back."""

    KNOWLEDGE = "knowledge"
    EXPERIENCE = "experience"


class RevisionAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    ROLLBACK = "rollback"


@dataclass(frozen=True, slots=True)
class SeedRevision:
    """One auditable knowledge/experience mutation, carrying both sides.

    History is append-only: a rollback writes the inverse into the live table
    and appends its own revision, so an undo never destroys evidence and is
    itself reversible.
    """

    revision_id: str
    seed_kind: SeedKind
    seed_id: str
    action: RevisionAction
    operator: str
    before_payload: dict[str, Any] | None = None
    after_payload: dict[str, Any] | None = None
    reason: str = ""
    rolled_back_revision_id: str = ""
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        seed_kind: SeedKind,
        seed_id: str,
        action: RevisionAction,
        *,
        operator: str,
        before_payload: dict[str, Any] | None = None,
        after_payload: dict[str, Any] | None = None,
        reason: str = "",
        rolled_back_revision_id: str = "",
    ) -> SeedRevision:
        normalized_operator = str(operator).strip()
        if not normalized_operator:
            raise ValueError("revision operator must not be empty")
        normalized_id = str(seed_id).strip()
        if not normalized_id:
            raise ValueError("revision seed id must not be empty")
        kind = SeedKind(seed_kind)
        act = RevisionAction(action)
        if act == RevisionAction.CREATE and before_payload is not None:
            raise ValueError("a create revision cannot carry a previous payload")
        if act == RevisionAction.UPDATE and before_payload is None:
            raise ValueError("an update revision requires the previous payload")
        if act == RevisionAction.ROLLBACK and not rolled_back_revision_id.strip():
            raise ValueError("a rollback revision must name the revision it undoes")
        if before_payload is not None and not isinstance(before_payload, dict):
            raise ValueError("revision payloads must be objects")
        if after_payload is not None and not isinstance(after_payload, dict):
            raise ValueError("revision payloads must be objects")
        return cls(
            revision_id=f"revision-{uuid4().hex[:16]}",
            seed_kind=kind,
            seed_id=normalized_id,
            action=act,
            operator=normalized_operator,
            before_payload=dict(before_payload) if before_payload else None,
            after_payload=dict(after_payload) if after_payload else None,
            reason=" ".join(str(reason).split()).strip()[:256],
            rolled_back_revision_id=str(rolled_back_revision_id).strip(),
        )

    def inverse_payload(self) -> dict[str, Any] | None:
        """The payload that restores the state before this revision ran."""
        return self.before_payload

    def is_reversible(self) -> bool:
        """A rollback revision is not itself re-rolled; undo it by re-applying
        the revision it reversed."""
        return self.action in {RevisionAction.CREATE, RevisionAction.UPDATE}


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    agent_id: str
    total_score: float
    components: dict[str, float]
    considered: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    knowledge_id: str
    title: str
    content: str
    tags: tuple[str, ...]
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls, title: str, content: str, tags: Sequence[str] = ()
    ) -> KnowledgeItem:
        if not title.strip() or not content.strip():
            raise ValueError("knowledge title and content must not be empty")
        return cls(
            f"knowledge-{uuid4().hex[:12]}",
            title.strip(),
            content.strip(),
            tuple(tag.strip() for tag in tags if tag.strip()),
        )


@dataclass(frozen=True, slots=True)
class RunBudget:
    max_actions: int = 100
    min_passing_score: float = 70.0

    def __post_init__(self) -> None:
        if self.max_actions < 1:
            raise ValueError("max_actions must be positive")
        if not 0 <= self.min_passing_score <= 100:
            raise ValueError("min_passing_score must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class RunReport:
    goal_id: str
    status: GoalStatus
    tasks_total: int
    tasks_succeeded: int
    actions: int
    attempts: int
    retries: int
    artifacts: int
    reason: str = ""


_OUTBOX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_OUTBOX_PAYLOAD_LIMIT = 65_536


def _outbox_time(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("outbox timestamp must be a UTC ISO-8601 string")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("outbox timestamp must be a UTC ISO-8601 string") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("outbox timestamp must include the UTC offset")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    message_id: str
    topic: str
    idempotency_key: str
    payload: dict[str, Any]
    status: OutboxStatus = OutboxStatus.PENDING
    attempt_count: int = 0
    max_attempts: int = 5
    delivery_token: int = 0
    available_at: str = field(default_factory=utc_now)
    claimed_by: str = ""
    claimed_at: str = ""
    claim_expires_at: str = ""
    delivered_at: str = ""
    last_error: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        topic: str,
        idempotency_key: str,
        payload: dict[str, Any],
        *,
        now: str | None = None,
        max_attempts: int = 5,
    ) -> OutboxMessage:
        topic = str(topic).strip()
        key = str(idempotency_key).strip()
        if not _OUTBOX_NAME.fullmatch(topic):
            raise ValueError("outbox topic must be a safe identifier")
        if not key or len(key) > 256:
            raise ValueError("outbox idempotency key must contain 1-256 characters")
        if not isinstance(payload, dict):
            raise ValueError("outbox payload must be an object")
        try:
            encoded = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("outbox payload must be JSON serializable") from error
        if len(encoded) > _OUTBOX_PAYLOAD_LIMIT:
            raise ValueError(
                f"outbox payload exceeds {_OUTBOX_PAYLOAD_LIMIT} bytes"
            )
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 100
        ):
            raise ValueError("outbox max_attempts must be between 1 and 100")
        created = now or utc_now()
        _outbox_time(created)
        return cls(
            message_id=f"outbox-{uuid4().hex}",
            topic=topic,
            idempotency_key=key,
            payload=dict(payload),
            max_attempts=max_attempts,
            available_at=created,
            created_at=created,
            updated_at=created,
        )

    @property
    def payload_digest(self) -> str:
        encoded = json.dumps(
            self.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def claim(
        self,
        worker_id: str,
        *,
        now: str,
        lease_seconds: int,
    ) -> OutboxMessage:
        worker_id = str(worker_id).strip()
        if not _OUTBOX_NAME.fullmatch(worker_id):
            raise ValueError("outbox worker ID must be a safe identifier")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 86_400
        ):
            raise ValueError("outbox lease_seconds must be between 1 and 86400")
        current = _outbox_time(now)
        pending_ready = (
            self.status == OutboxStatus.PENDING
            and current >= _outbox_time(self.available_at)
        )
        expired_delivery = (
            self.status == OutboxStatus.DELIVERING
            and bool(self.claim_expires_at)
            and current >= _outbox_time(self.claim_expires_at)
        )
        if not pending_ready and not expired_delivery:
            raise ValueError("outbox message is not available for delivery")
        expires = (current + timedelta(seconds=lease_seconds)).isoformat()
        return replace(
            self,
            status=OutboxStatus.DELIVERING,
            attempt_count=self.attempt_count + 1,
            delivery_token=self.delivery_token + 1,
            claimed_by=worker_id,
            claimed_at=current.isoformat(),
            claim_expires_at=expires,
            updated_at=current.isoformat(),
        )

    def _require_delivery(self, worker_id: str, token: int, now: str) -> str:
        current = _outbox_time(now)
        if (
            self.status != OutboxStatus.DELIVERING
            or self.claimed_by != worker_id
            or self.delivery_token != token
            or not self.claim_expires_at
            or current >= _outbox_time(self.claim_expires_at)
        ):
            raise ValueError("outbox delivery ownership is stale")
        return current.isoformat()

    def deliver(self, worker_id: str, token: int, *, now: str) -> OutboxMessage:
        completed_at = self._require_delivery(worker_id, token, now)
        return replace(
            self,
            status=OutboxStatus.DELIVERED,
            delivered_at=completed_at,
            claim_expires_at="",
            updated_at=completed_at,
        )

    def renew(
        self,
        worker_id: str,
        token: int,
        *,
        now: str,
        lease_seconds: int,
    ) -> OutboxMessage:
        renewed_at = self._require_delivery(worker_id, token, now)
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 86_400
        ):
            raise ValueError("outbox lease_seconds must be between 1 and 86400")
        expires = (
            _outbox_time(renewed_at) + timedelta(seconds=lease_seconds)
        ).isoformat()
        return replace(
            self,
            claim_expires_at=expires,
            updated_at=renewed_at,
        )

    def expire(self, *, now: str) -> OutboxMessage:
        expired_at = _outbox_time(now)
        if (
            self.status != OutboxStatus.DELIVERING
            or not self.claim_expires_at
            or expired_at < _outbox_time(self.claim_expires_at)
            or self.attempt_count < self.max_attempts
        ):
            raise ValueError("outbox delivery is not terminally expired")
        return replace(
            self,
            status=OutboxStatus.FAILED,
            claim_expires_at="",
            last_error="delivery lease expired at attempt limit",
            updated_at=expired_at.isoformat(),
        )

    def fail(
        self,
        worker_id: str,
        token: int,
        *,
        now: str,
        error: str,
        retry_seconds: int = 0,
    ) -> OutboxMessage:
        failed_at = self._require_delivery(worker_id, token, now)
        if (
            isinstance(retry_seconds, bool)
            or not isinstance(retry_seconds, int)
            or not 0 <= retry_seconds <= 86_400
        ):
            raise ValueError("outbox retry_seconds must be between 0 and 86400")
        reason = " ".join(str(error).split()).strip()[:512]
        if not reason:
            raise ValueError("outbox failure reason must not be empty")
        terminal = self.attempt_count >= self.max_attempts
        available = (
            _outbox_time(failed_at) + timedelta(seconds=retry_seconds)
        ).isoformat()
        return replace(
            self,
            status=OutboxStatus.FAILED if terminal else OutboxStatus.PENDING,
            available_at=available,
            claimed_by="",
            claimed_at="",
            claim_expires_at="",
            last_error=reason,
            updated_at=failed_at,
        )


_GOAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.CREATED: frozenset({GoalStatus.PLANNING}),
    GoalStatus.PLANNING: frozenset({GoalStatus.RUNNING, GoalStatus.FAILED}),
    GoalStatus.RUNNING: frozenset(
        {GoalStatus.PAUSED, GoalStatus.SUCCEEDED, GoalStatus.FAILED, GoalStatus.BLOCKED}
    ),
    GoalStatus.PAUSED: frozenset({GoalStatus.RUNNING, GoalStatus.FAILED}),
    GoalStatus.SUCCEEDED: frozenset(),
    GoalStatus.FAILED: frozenset(),
    GoalStatus.BLOCKED: frozenset(),
}


def transition_goal(goal: Goal, target: GoalStatus, reason: str = "") -> Goal:
    if goal.status in {GoalStatus.SUCCEEDED, GoalStatus.FAILED, GoalStatus.BLOCKED}:
        raise ValueError(f"goal is terminal in state {goal.status.value}")
    if target not in _GOAL_TRANSITIONS[goal.status]:
        raise ValueError(f"invalid goal transition: {goal.status.value} -> {target.value}")
    return replace(goal, status=target, updated_at=utc_now(), failure_reason=reason)


def validate_task_graph(tasks: Sequence[Task]) -> Sequence[Task]:
    identifiers = [task.task_id for task in tasks]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("task graph contains duplicate task IDs")

    known = set(identifiers)
    for task in tasks:
        for dependency in task.dependencies:
            if dependency not in known:
                raise ValueError(
                    f"task {task.task_id} has missing dependency {dependency}"
                )

    visiting: set[str] = set()
    visited: set[str] = set()
    dependencies = {task.task_id: task.dependencies for task in tasks}

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("task graph contains a dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for identifier in identifiers:
        visit(identifier)
    return tasks
