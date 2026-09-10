"""SQLite persistence for goals, memories, artifacts, and audit events."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .domain import (
    AgentGenome,
    AgentProfile,
    AgentSelfModel,
    ApprovalRequest,
    ApprovalStatus,
    Artifact,
    Attempt,
    ConformanceAttestation,
    Defect,
    DelegationPolicy,
    DelegationRecord,
    DelegationRule,
    DelegationStatus,
    DeploymentRecord,
    Event,
    EvaluationOutcome,
    EvaluationRun,
    EvaluationStatus,
    ExperienceRecord,
    Goal,
    GoalStatus,
    KnowledgeItem,
    OutboxMessage,
    OutboxStatus,
    PerformanceRecord,
    PolicyActivation,
    PolicyDecision,
    PolicyVerdict,
    PublicationRecord,
    PublicationStatus,
    RemoteAgentRegistration,
    Review,
    RevisionAction,
    SeedKind,
    SeedRevision,
    Task,
    TaskStatus,
    SpanStatus,
    TraceSpan,
    VerificationResult,
    Verdict,
    WorkspaceSnapshot,
    transition_goal,
)
from .scheduler import (
    ClaimedTask,
    ClaimStatus,
    StaleClaim,
    TaskClaim,
    WorkerSession,
    WorkerSessionRejected,
    parse_utc,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default, sort_keys=True)


def _load(value: str) -> dict[str, Any]:
    return json.loads(value)


class SQLiteRepository:
    """A small transactional repository with a stable, inspectable schema."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self.connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT NOT NULL,
                goal_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(goal_id, task_id),
                FOREIGN KEY(goal_id) REFERENCES goals(goal_id)
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(goal_id, task_id, attempt_no)
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                goal_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS performance (
                agent_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(agent_id, task_type)
            );
            CREATE TABLE IF NOT EXISTS agent_genomes (
                agent_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experience_records (
                experience_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                goal_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS experience_records_agent_idx
                ON experience_records(agent_id, task_type);
            CREATE INDEX IF NOT EXISTS experience_records_goal_idx
                ON experience_records(goal_id);
            CREATE TABLE IF NOT EXISTS knowledge (
                knowledge_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            -- Append-only seed history: one row per knowledge/experience
            -- mutation, carrying both sides of the change. A rollback writes
            -- the inverse into the live table and records its own revision;
            -- history rows are never updated or deleted, so an undo is always
            -- itself auditable and can be undone again.
            CREATE TABLE IF NOT EXISTS seed_revisions (
                revision_id TEXT PRIMARY KEY,
                seed_kind TEXT NOT NULL,
                seed_id TEXT NOT NULL,
                action TEXT NOT NULL,
                before_payload TEXT,
                after_payload TEXT,
                operator TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                rolled_back_revision_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS seed_revisions_seed_idx
                ON seed_revisions(seed_kind, seed_id, created_at);
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                fingerprint TEXT UNIQUE NOT NULL,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS approvals_goal_idx
                ON approvals(goal_id, task_id);
            CREATE INDEX IF NOT EXISTS approvals_status_idx
                ON approvals(json_extract(payload, '$.status'));
            CREATE TABLE IF NOT EXISTS trace_spans (
                span_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                goal_id TEXT NOT NULL,
                task_id TEXT,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS trace_spans_goal_idx
                ON trace_spans(goal_id, task_id);
            CREATE TABLE IF NOT EXISTS workspace_snapshots (
                goal_id TEXT NOT NULL,
                path TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(goal_id, path)
            );
            CREATE TABLE IF NOT EXISTS verification_results (
                result_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                check_name TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS verification_results_goal_idx
                ON verification_results(goal_id, task_id, check_name);
            CREATE TABLE IF NOT EXISTS publication_records (
                goal_id TEXT PRIMARY KEY,
                publication_id TEXT UNIQUE NOT NULL,
                payload_digest TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evaluation_runs (
                run_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS evaluation_runs_task_type_idx
                ON evaluation_runs(task_type);
            CREATE TABLE IF NOT EXISTS evaluation_outcomes (
                outcome_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                candidate_agent_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(run_id, case_id, candidate_agent_id)
            );
            CREATE INDEX IF NOT EXISTS evaluation_outcomes_run_idx
                ON evaluation_outcomes(run_id, case_id);
            CREATE TABLE IF NOT EXISTS deployments (
                task_type TEXT PRIMARY KEY,
                source_run_id TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS remote_agents (
                agent_id TEXT PRIMARY KEY,
                card_sha256 TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS delegations (
                delegation_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                agent_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(goal_id, task_id, attempt_no, agent_id)
            );
            CREATE INDEX IF NOT EXISTS delegations_goal_idx
                ON delegations(goal_id, task_id, attempt_no);
            CREATE TABLE IF NOT EXISTS delegation_policies (
                policy_digest TEXT PRIMARY KEY,
                policy_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS delegation_policies_id_idx
                ON delegation_policies(policy_id);
            CREATE TABLE IF NOT EXISTS policy_activations (
                task_type TEXT PRIMARY KEY,
                policy_digest TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conformance_attestations (
                attestation_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                card_sha256 TEXT NOT NULL,
                report_sha256 TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS conformance_attestations_agent_idx
                ON conformance_attestations(agent_id, card_sha256);
            CREATE TABLE IF NOT EXISTS policy_decisions (
                decision_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                agent_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(goal_id, task_id, attempt_no, agent_id)
            );
            CREATE INDEX IF NOT EXISTS policy_decisions_goal_idx
                ON policy_decisions(goal_id, task_id, attempt_no);
            CREATE TABLE IF NOT EXISTS scheduler_workers (
                worker_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_claim_fences (
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                last_token INTEGER NOT NULL,
                PRIMARY KEY(goal_id, task_id)
            );
            CREATE TABLE IF NOT EXISTS task_claims (
                claim_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                status TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(goal_id, task_id, fencing_token)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS task_claims_one_active_idx
                ON task_claims(goal_id, task_id) WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS task_claims_goal_idx
                ON task_claims(goal_id, task_id, fencing_token);
            CREATE TABLE IF NOT EXISTS outbox_messages (
                message_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                status TEXT NOT NULL,
                available_at TEXT NOT NULL,
                claim_expires_at TEXT NOT NULL,
                delivery_token INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(topic, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS outbox_delivery_idx
                ON outbox_messages(status, available_at, claim_expires_at, created_at);
            """
        )
        self.connection.commit()

    @contextmanager
    def _immediate_transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def scheduler_now(self) -> str:
        row = self.connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now') AS now"
        ).fetchone()
        return str(row["now"])

    def operational_counts(self, now: str) -> dict[str, int]:
        workers = self.connection.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN expires_at<=? THEN 1 ELSE 0 END), 0) AS expired "
            "FROM scheduler_workers",
            (now,),
        ).fetchone()
        claims = self.connection.execute(
            "SELECT COUNT(*) AS active, "
            "COALESCE(SUM(CASE WHEN expires_at<=? THEN 1 ELSE 0 END), 0) "
            "AS expired_active FROM task_claims WHERE status='active'",
            (now,),
        ).fetchone()
        approvals = self.connection.execute(
            "SELECT COUNT(*) AS pending FROM approvals "
            "WHERE json_extract(payload, '$.status')='pending'"
        ).fetchone()
        outbox = {
            row["status"]: int(row["count"])
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM outbox_messages GROUP BY status"
            ).fetchall()
        }
        return {
            "workers_total": int(workers["total"]),
            "workers_expired": int(workers["expired"]),
            "claims_active": int(claims["active"]),
            "claims_expired_active": int(claims["expired_active"]),
            "approvals_pending": int(approvals["pending"]),
            "outbox_pending": outbox.get("pending", 0),
            "outbox_delivering": outbox.get("delivering", 0),
            "outbox_delivered": outbox.get("delivered", 0),
            "outbox_failed": outbox.get("failed", 0),
        }

    @staticmethod
    def _outbox_from_payload(payload: str) -> OutboxMessage:
        data = _load(payload)
        data["status"] = OutboxStatus(data["status"])
        return OutboxMessage(**data)

    def _enqueue_outbox_locked(self, message: OutboxMessage) -> OutboxMessage:
        row = self.connection.execute(
            "SELECT payload FROM outbox_messages "
            "WHERE topic=? AND idempotency_key=?",
            (message.topic, message.idempotency_key),
        ).fetchone()
        if row is not None:
            existing = self._outbox_from_payload(row["payload"])
            if (
                existing.payload_digest != message.payload_digest
                or existing.max_attempts != message.max_attempts
            ):
                raise ValueError(
                    "outbox idempotency payload does not match existing message"
                )
            return existing
        self.connection.execute(
            "INSERT INTO outbox_messages("
            "message_id, topic, idempotency_key, status, available_at, "
            "claim_expires_at, delivery_token, created_at, payload"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.message_id,
                message.topic,
                message.idempotency_key,
                message.status.value,
                message.available_at,
                message.claim_expires_at,
                message.delivery_token,
                message.created_at,
                _dump(asdict(message)),
            ),
        )
        return message

    def enqueue_outbox(self, message: OutboxMessage) -> OutboxMessage:
        with self._immediate_transaction():
            return self._enqueue_outbox_locked(message)

    def claim_outbox(
        self,
        worker_id: str,
        *,
        topic: str | None = None,
        now: str,
        lease_seconds: int,
    ) -> OutboxMessage | None:
        if topic is not None and not str(topic).strip():
            raise ValueError("outbox topic must not be empty")
        topic_filter = "topic=? AND " if topic is not None else ""
        parameters = (
            (str(topic).strip(), now, now)
            if topic is not None
            else (now, now)
        )
        with self._immediate_transaction():
            while True:
                row = self.connection.execute(
                    "SELECT payload FROM outbox_messages "
                    f"WHERE {topic_filter}((status='pending' AND available_at<=?) "
                    "OR (status='delivering' AND claim_expires_at<=?)) "
                    "ORDER BY created_at, message_id LIMIT 1",
                    parameters,
                ).fetchone()
                if row is None:
                    return None
                current = self._outbox_from_payload(row["payload"])
                if (
                    current.status == OutboxStatus.DELIVERING
                    and current.attempt_count >= current.max_attempts
                ):
                    exhausted = current.expire(now=now)
                    self.connection.execute(
                        "UPDATE outbox_messages SET status=?, "
                        "claim_expires_at=?, payload=? "
                        "WHERE message_id=? AND status='delivering' "
                        "AND delivery_token=?",
                        (
                            exhausted.status.value,
                            exhausted.claim_expires_at,
                            _dump(asdict(exhausted)),
                            exhausted.message_id,
                            current.delivery_token,
                        ),
                    )
                    continue
                break
            claimed = current.claim(
                worker_id, now=now, lease_seconds=lease_seconds
            )
            cursor = self.connection.execute(
                "UPDATE outbox_messages SET status=?, available_at=?, "
                "claim_expires_at=?, delivery_token=?, payload=? "
                "WHERE message_id=? AND status=? AND delivery_token=?",
                (
                    claimed.status.value,
                    claimed.available_at,
                    claimed.claim_expires_at,
                    claimed.delivery_token,
                    _dump(asdict(claimed)),
                    claimed.message_id,
                    current.status.value,
                    current.delivery_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("outbox delivery ownership is stale")
        return claimed

    def renew_outbox(
        self,
        message_id: str,
        worker_id: str,
        delivery_token: int,
        *,
        now: str,
        lease_seconds: int,
    ) -> OutboxMessage:
        with self._immediate_transaction():
            row = self.connection.execute(
                "SELECT payload FROM outbox_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise ValueError("outbox message not found")
            current = self._outbox_from_payload(row["payload"])
            renewed = current.renew(
                worker_id,
                delivery_token,
                now=now,
                lease_seconds=lease_seconds,
            )
            cursor = self.connection.execute(
                "UPDATE outbox_messages SET claim_expires_at=?, payload=? "
                "WHERE message_id=? AND status='delivering' "
                "AND delivery_token=? "
                "AND json_extract(payload, '$.claimed_by')=?",
                (
                    renewed.claim_expires_at,
                    _dump(asdict(renewed)),
                    message_id,
                    delivery_token,
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("outbox delivery ownership is stale")
        return renewed

    def complete_outbox(
        self,
        message_id: str,
        worker_id: str,
        delivery_token: int,
        *,
        now: str,
        error: str = "",
        retry_seconds: int = 0,
    ) -> OutboxMessage:
        with self._immediate_transaction():
            row = self.connection.execute(
                "SELECT payload FROM outbox_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise ValueError("outbox message not found")
            current = self._outbox_from_payload(row["payload"])
            updated = (
                current.fail(
                    worker_id,
                    delivery_token,
                    now=now,
                    error=error,
                    retry_seconds=retry_seconds,
                )
                if error
                else current.deliver(worker_id, delivery_token, now=now)
            )
            cursor = self.connection.execute(
                "UPDATE outbox_messages SET status=?, available_at=?, "
                "claim_expires_at=?, delivery_token=?, payload=? "
                "WHERE message_id=? AND status='delivering' "
                "AND delivery_token=?",
                (
                    updated.status.value,
                    updated.available_at,
                    updated.claim_expires_at,
                    updated.delivery_token,
                    _dump(asdict(updated)),
                    message_id,
                    delivery_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("outbox delivery ownership is stale")
        return updated

    def list_outbox(
        self,
        *,
        status: OutboxStatus | None = None,
        limit: int | None = None,
    ) -> list[OutboxMessage]:
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("outbox list limit must be between 1 and 10000")
        query = "SELECT payload FROM outbox_messages"
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE status=?"
            parameters.append(OutboxStatus(status).value)
        query += " ORDER BY created_at, message_id"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        rows = self.connection.execute(query, parameters).fetchall()
        return [self._outbox_from_payload(row["payload"]) for row in rows]

    def purge_outbox(self, *, before: str, limit: int) -> int:
        cutoff = parse_utc(before).isoformat()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("outbox purge limit must be between 1 and 10000")
        with self._immediate_transaction():
            cursor = self.connection.execute(
                """DELETE FROM outbox_messages WHERE message_id IN (
                       SELECT message_id FROM outbox_messages
                       WHERE status IN ('delivered', 'failed')
                         AND json_extract(payload, '$.updated_at')<?
                       ORDER BY created_at, message_id LIMIT ?
                   )""",
                (cutoff, limit),
            )
        return cursor.rowcount

    def register_worker(self, session: WorkerSession) -> WorkerSession:
        with self._immediate_transaction():
            self.connection.execute(
                "INSERT INTO scheduler_workers(worker_id, session_id, expires_at, payload) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(worker_id) DO UPDATE SET "
                "session_id=excluded.session_id, expires_at=excluded.expires_at, "
                "payload=excluded.payload",
                (
                    session.worker_id,
                    session.session_id,
                    session.expires_at,
                    _dump(asdict(session)),
                ),
            )
        return session

    def heartbeat_worker(
        self,
        worker_id: str,
        session_id: str,
        *,
        now: str,
        ttl_seconds: int,
    ) -> WorkerSession:
        with self._immediate_transaction():
            row = self.connection.execute(
                "SELECT payload FROM scheduler_workers WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if row is None:
                raise WorkerSessionRejected("worker session is not registered")
            current = self._worker_from_payload(row["payload"])
            if current.session_id != session_id:
                raise WorkerSessionRejected("worker session has been superseded")
            updated = current.heartbeat(now=now, ttl_seconds=ttl_seconds)
            cursor = self.connection.execute(
                "UPDATE scheduler_workers SET expires_at=?, payload=? "
                "WHERE worker_id=? AND session_id=?",
                (updated.expires_at, _dump(asdict(updated)), worker_id, session_id),
            )
            if cursor.rowcount != 1:
                raise WorkerSessionRejected("worker session has been superseded")
        return updated

    @staticmethod
    def _worker_from_payload(payload: str) -> WorkerSession:
        data = _load(payload)
        data["capabilities"] = tuple(data["capabilities"])
        return WorkerSession(**data)

    def get_worker(self, worker_id: str) -> WorkerSession | None:
        row = self.connection.execute(
            "SELECT payload FROM scheduler_workers WHERE worker_id=?", (worker_id,)
        ).fetchone()
        return None if row is None else self._worker_from_payload(row["payload"])

    def list_workers(self) -> list[WorkerSession]:
        rows = self.connection.execute(
            "SELECT payload FROM scheduler_workers ORDER BY worker_id"
        ).fetchall()
        return [self._worker_from_payload(row["payload"]) for row in rows]

    def _require_worker_session_locked(
        self, worker_id: str, session_id: str, now: str
    ) -> WorkerSession:
        row = self.connection.execute(
            "SELECT payload FROM scheduler_workers WHERE worker_id=?", (worker_id,)
        ).fetchone()
        if row is None:
            raise WorkerSessionRejected("worker session is not registered")
        session = self._worker_from_payload(row["payload"])
        if session.session_id != session_id:
            raise WorkerSessionRejected("worker session has been superseded")
        if session.is_expired(now):
            raise WorkerSessionRejected("worker session has expired")
        return session

    @staticmethod
    def _task_from_payload(payload: str) -> Task:
        data = _load(payload)
        data["dependencies"] = tuple(data["dependencies"])
        data["status"] = TaskStatus(data["status"])
        return Task(**data)

    @staticmethod
    def _claim_from_payload(payload: str) -> TaskClaim:
        data = _load(payload)
        data["status"] = ClaimStatus(data["status"])
        return TaskClaim(**data)

    def claim_task(
        self,
        goal_id: str,
        task_id: str,
        worker_id: str,
        session_id: str,
        agent_id: str,
        *,
        now: str,
        lease_seconds: int,
    ) -> TaskClaim | None:
        with self._immediate_transaction():
            session = self._require_worker_session_locked(worker_id, session_id, now)
            goal_row = self.connection.execute(
                "SELECT payload FROM goals WHERE goal_id=?", (goal_id,)
            ).fetchone()
            if goal_row is None or GoalStatus(_load(goal_row["payload"])["status"]) != GoalStatus.RUNNING:
                return None

            task_row = self.connection.execute(
                "SELECT payload FROM tasks WHERE goal_id=? AND task_id=?",
                (goal_id, task_id),
            ).fetchone()
            if task_row is None:
                return None
            task = self._task_from_payload(task_row["payload"])
            if task.status != TaskStatus.PENDING:
                return None

            for dependency_id in task.dependencies:
                dependency_row = self.connection.execute(
                    "SELECT payload FROM tasks WHERE goal_id=? AND task_id=?",
                    (goal_id, dependency_id),
                ).fetchone()
                if dependency_row is None:
                    return None
                dependency = self._task_from_payload(dependency_row["payload"])
                if dependency.status != TaskStatus.SUCCEEDED:
                    return None

            active = self.connection.execute(
                "SELECT payload FROM task_claims "
                "WHERE goal_id=? AND task_id=? AND status='active'",
                (goal_id, task_id),
            ).fetchone()
            if active is not None:
                return None

            claim, _ = self._create_claim_locked(
                session,
                task,
                agent_id,
                now=now,
                lease_seconds=lease_seconds,
            )
        return claim

    def claim_next_task(
        self,
        worker_id: str,
        session_id: str,
        assignments: Mapping[str, str],
        *,
        now: str,
        lease_seconds: int,
    ) -> ClaimedTask | None:
        normalized = {str(key).strip(): str(value).strip() for key, value in assignments.items()}
        if not normalized or any(not key or not value for key, value in normalized.items()):
            raise ValueError("assignments must map task types to agent identifiers")
        with self._immediate_transaction():
            session = self._require_worker_session_locked(worker_id, session_id, now)
            if not set(normalized).issubset(session.capabilities):
                raise ValueError("assignments must be within worker session capabilities")

            running_goals = {
                row["goal_id"]
                for row in self.connection.execute(
                    "SELECT goal_id, payload FROM goals ORDER BY goal_id"
                ).fetchall()
                if GoalStatus(_load(row["payload"])["status"]) == GoalStatus.RUNNING
            }
            rows = self.connection.execute(
                "SELECT goal_id, task_id, payload FROM tasks ORDER BY goal_id, position, task_id"
            ).fetchall()
            tasks = [self._task_from_payload(row["payload"]) for row in rows]
            statuses = {
                (task.goal_id, task.task_id): task.status for task in tasks
            }
            for task in tasks:
                if (
                    task.goal_id not in running_goals
                    or task.status != TaskStatus.PENDING
                    or task.task_type not in normalized
                    or any(
                        statuses.get((task.goal_id, dependency_id))
                        != TaskStatus.SUCCEEDED
                        for dependency_id in task.dependencies
                    )
                ):
                    continue
                active = self.connection.execute(
                    "SELECT 1 FROM task_claims "
                    "WHERE goal_id=? AND task_id=? AND status='active'",
                    (task.goal_id, task.task_id),
                ).fetchone()
                if active is not None:
                    continue
                claim, running = self._create_claim_locked(
                    session,
                    task,
                    normalized[task.task_type],
                    now=now,
                    lease_seconds=lease_seconds,
                )
                return ClaimedTask(claim, running)
        return None

    def _create_claim_locked(
        self,
        session: WorkerSession,
        task: Task,
        agent_id: str,
        *,
        now: str,
        lease_seconds: int,
    ) -> tuple[TaskClaim, Task]:
        fence_row = self.connection.execute(
            "SELECT last_token FROM task_claim_fences WHERE goal_id=? AND task_id=?",
            (task.goal_id, task.task_id),
        ).fetchone()
        fencing_token = 1 if fence_row is None else int(fence_row["last_token"]) + 1
        self.connection.execute(
            "INSERT INTO task_claim_fences(goal_id, task_id, last_token) VALUES (?, ?, ?) "
            "ON CONFLICT(goal_id, task_id) DO UPDATE SET last_token=excluded.last_token",
            (task.goal_id, task.task_id, fencing_token),
        )
        claim = TaskClaim.create(
            task.goal_id,
            task.task_id,
            session,
            agent_id,
            fencing_token,
            now=now,
            lease_seconds=lease_seconds,
        )
        running = replace(
            task, status=TaskStatus.RUNNING, assigned_agent_id=agent_id
        )
        self.connection.execute(
            "UPDATE tasks SET payload=? WHERE goal_id=? AND task_id=?",
            (_dump(asdict(running)), task.goal_id, task.task_id),
        )
        self.connection.execute(
            "INSERT INTO task_claims(claim_id, goal_id, task_id, worker_id, "
            "session_id, fencing_token, status, expires_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim.claim_id,
                claim.goal_id,
                claim.task_id,
                claim.worker_id,
                claim.session_id,
                claim.fencing_token,
                claim.status.value,
                claim.expires_at,
                _dump(asdict(claim)),
            ),
        )
        return claim, running

    def _require_claim_locked(
        self,
        claim_id: str,
        worker_id: str,
        session_id: str,
        fencing_token: int,
        now: str,
    ) -> TaskClaim:
        row = self.connection.execute(
            "SELECT payload FROM task_claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if row is None:
            raise StaleClaim("claim identity no longer owns task")
        claim = self._claim_from_payload(row["payload"])
        if (
            claim.status != ClaimStatus.ACTIVE
            or claim.worker_id != worker_id
            or claim.session_id != session_id
            or claim.fencing_token != fencing_token
        ):
            raise StaleClaim("claim identity no longer owns task")
        if claim.is_expired(now):
            raise StaleClaim("claim lease has expired")
        return claim

    def renew_claim(
        self,
        claim_id: str,
        worker_id: str,
        session_id: str,
        fencing_token: int,
        *,
        now: str,
        lease_seconds: int,
    ) -> TaskClaim:
        with self._immediate_transaction():
            self._require_worker_session_locked(worker_id, session_id, now)
            current = self._require_claim_locked(
                claim_id, worker_id, session_id, fencing_token, now
            )
            renewed = current.renew(now=now, lease_seconds=lease_seconds)
            cursor = self.connection.execute(
                "UPDATE task_claims SET expires_at=?, payload=? "
                "WHERE claim_id=? AND status='active' AND fencing_token=?",
                (
                    renewed.expires_at,
                    _dump(asdict(renewed)),
                    claim_id,
                    fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleClaim("claim identity no longer owns task")
        return renewed

    def release_claim(
        self,
        claim_id: str,
        worker_id: str,
        session_id: str,
        fencing_token: int,
        *,
        now: str,
        reason: str = "",
    ) -> TaskClaim:
        with self._immediate_transaction():
            self._require_worker_session_locked(worker_id, session_id, now)
            current = self._require_claim_locked(
                claim_id, worker_id, session_id, fencing_token, now
            )
            released = current.finish(ClaimStatus.RELEASED, now=now, reason=reason)
            self.connection.execute(
                "UPDATE task_claims SET status=?, expires_at=?, payload=? WHERE claim_id=?",
                (
                    released.status.value,
                    released.expires_at,
                    _dump(asdict(released)),
                    claim_id,
                ),
            )
            task_row = self.connection.execute(
                "SELECT payload FROM tasks WHERE goal_id=? AND task_id=?",
                (released.goal_id, released.task_id),
            ).fetchone()
            if task_row is not None:
                task = self._task_from_payload(task_row["payload"])
                if task.status == TaskStatus.RUNNING:
                    pending = replace(
                        task, status=TaskStatus.PENDING, assigned_agent_id=None
                    )
                    self.connection.execute(
                        "UPDATE tasks SET payload=? WHERE goal_id=? AND task_id=?",
                        (_dump(asdict(pending)), task.goal_id, task.task_id),
                    )
        return released

    def pause_claim_for_approval(
        self,
        claim: TaskClaim,
        approval: ApprovalRequest,
        *,
        now: str,
    ) -> TaskClaim:
        """Atomically persist an approval and surrender the claimed task."""
        with self._immediate_transaction():
            self._require_worker_session_locked(claim.worker_id, claim.session_id, now)
            current = self._require_claim_locked(
                claim.claim_id,
                claim.worker_id,
                claim.session_id,
                claim.fencing_token,
                now,
            )
            if (
                approval.goal_id != current.goal_id
                or approval.task_id != current.task_id
                or approval.status != ApprovalStatus.PENDING
            ):
                raise ValueError("approval identity does not match active claim")

            approval_row = self.connection.execute(
                "SELECT payload FROM approvals WHERE approval_id=?",
                (approval.approval_id,),
            ).fetchone()
            if approval_row is not None:
                data = _load(approval_row["payload"])
                data["status"] = ApprovalStatus(data["status"])
                if ApprovalRequest(**data) != approval:
                    raise ValueError("approval identity cannot change")
            else:
                self.connection.execute(
                    "INSERT INTO approvals(approval_id, fingerprint, goal_id, task_id, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        approval.approval_id,
                        approval.fingerprint,
                        approval.goal_id,
                        approval.task_id,
                        _dump(asdict(approval)),
                    ),
                )

            task_row = self.connection.execute(
                "SELECT payload FROM tasks WHERE goal_id=? AND task_id=?",
                (current.goal_id, current.task_id),
            ).fetchone()
            goal_row = self.connection.execute(
                "SELECT payload FROM goals WHERE goal_id=?", (current.goal_id,)
            ).fetchone()
            if task_row is None or goal_row is None:
                raise StaleClaim("claimed task or goal no longer exists")
            task = self._task_from_payload(task_row["payload"])
            goal_data = _load(goal_row["payload"])
            goal_data["status"] = GoalStatus(goal_data["status"])
            goal = Goal(**goal_data)
            if (
                task.status != TaskStatus.RUNNING
                or task.assigned_agent_id != current.agent_id
                or goal.status != GoalStatus.RUNNING
            ):
                raise StaleClaim("claim identity no longer owns running work")

            reason = f"approval required for tool {approval.tool_name}"
            released = current.finish(ClaimStatus.RELEASED, now=now, reason=reason)
            pending = replace(task, status=TaskStatus.PENDING, assigned_agent_id=None)
            paused = transition_goal(goal, GoalStatus.PAUSED, reason)
            event = Event.create(
                current.goal_id,
                "approval.requested",
                {
                    "approval_id": approval.approval_id,
                    "task_id": current.task_id,
                    "tool_name": approval.tool_name,
                    "claim_id": current.claim_id,
                    "fencing_token": current.fencing_token,
                },
            )
            self.connection.execute(
                "UPDATE task_claims SET status=?, expires_at=?, payload=? WHERE claim_id=?",
                (
                    released.status.value,
                    released.expires_at,
                    _dump(asdict(released)),
                    released.claim_id,
                ),
            )
            self.connection.execute(
                "UPDATE tasks SET payload=? WHERE goal_id=? AND task_id=?",
                (_dump(asdict(pending)), pending.goal_id, pending.task_id),
            )
            self.connection.execute(
                "UPDATE goals SET payload=? WHERE goal_id=?",
                (_dump(asdict(paused)), paused.goal_id),
            )
            self.connection.execute(
                "INSERT INTO events(event_id, goal_id, payload) VALUES (?, ?, ?)",
                (event.event_id, event.goal_id, _dump(asdict(event))),
            )
        return released

    def get_claim(self, claim_id: str) -> TaskClaim | None:
        row = self.connection.execute(
            "SELECT payload FROM task_claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        return None if row is None else self._claim_from_payload(row["payload"])

    def list_claims(self, goal_id: str | None = None) -> list[TaskClaim]:
        query = "SELECT payload FROM task_claims"
        parameters: tuple[str, ...] = ()
        if goal_id is not None:
            query += " WHERE goal_id=?"
            parameters = (goal_id,)
        query += " ORDER BY goal_id, task_id, fencing_token"
        rows = self.connection.execute(query, parameters).fetchall()
        return [self._claim_from_payload(row["payload"]) for row in rows]

    def reap_expired_claims(self, *, now: str) -> list[TaskClaim]:
        expired_claims: list[TaskClaim] = []
        unsafe_remote_states = {
            DelegationStatus.SUBMITTING,
            DelegationStatus.UNKNOWN,
            DelegationStatus.INTERRUPTED,
        }
        with self._immediate_transaction():
            rows = self.connection.execute(
                "SELECT payload FROM task_claims WHERE status='active' "
                "ORDER BY goal_id, task_id, fencing_token"
            ).fetchall()
            for row in rows:
                claim = self._claim_from_payload(row["payload"])
                if not claim.is_expired(now):
                    continue

                delegation_row = self.connection.execute(
                    "SELECT payload FROM delegations WHERE goal_id=? AND task_id=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (claim.goal_id, claim.task_id),
                ).fetchone()
                delegation = (
                    _delegation_from_payload(delegation_row["payload"])
                    if delegation_row is not None
                    else None
                )
                unsafe = (
                    delegation is not None
                    and delegation.status in unsafe_remote_states
                )
                reason = (
                    f"unsafe remote delegation state: {delegation.status.value}"
                    if unsafe and delegation is not None
                    else "lease expired"
                )
                expired = claim.finish(
                    ClaimStatus.EXPIRED, now=now, reason=reason
                )
                self.connection.execute(
                    "UPDATE task_claims SET status=?, payload=? "
                    "WHERE claim_id=? AND status='active'",
                    (expired.status.value, _dump(asdict(expired)), claim.claim_id),
                )

                task_row = self.connection.execute(
                    "SELECT payload FROM tasks WHERE goal_id=? AND task_id=?",
                    (claim.goal_id, claim.task_id),
                ).fetchone()
                if task_row is not None:
                    task = self._task_from_payload(task_row["payload"])
                    if task.status == TaskStatus.RUNNING:
                        recovered = replace(
                            task,
                            status=(
                                TaskStatus.BLOCKED if unsafe else TaskStatus.PENDING
                            ),
                            assigned_agent_id=None,
                        )
                        self.connection.execute(
                            "UPDATE tasks SET payload=? WHERE goal_id=? AND task_id=?",
                            (_dump(asdict(recovered)), task.goal_id, task.task_id),
                        )

                event = Event.create(
                    claim.goal_id,
                    "task.claim_expired",
                    {
                        "claim_id": claim.claim_id,
                        "task_id": claim.task_id,
                        "worker_id": claim.worker_id,
                        "fencing_token": claim.fencing_token,
                        "recovery": "blocked" if unsafe else "pending",
                        "reason": reason,
                    },
                )
                self.connection.execute(
                    "INSERT INTO events(event_id, goal_id, payload) VALUES (?, ?, ?)",
                    (event.event_id, event.goal_id, _dump(asdict(event))),
                )
                expired_claims.append(expired)
        return expired_claims

    def commit_claim_outcome(
        self,
        claim: TaskClaim,
        task: Task,
        artifact: Artifact | None,
        attempt: Attempt,
        review: Review,
        performance: PerformanceRecord,
        events: Sequence[Event],
        outbox_messages: Sequence[OutboxMessage] = (),
        *,
        now: str,
    ) -> TaskClaim:
        with self._immediate_transaction():
            self._require_worker_session_locked(
                claim.worker_id, claim.session_id, now
            )
            current = self._require_claim_locked(
                claim.claim_id,
                claim.worker_id,
                claim.session_id,
                claim.fencing_token,
                now,
            )
            if (
                task.goal_id != current.goal_id
                or task.task_id != current.task_id
                or task.status
                not in {
                    TaskStatus.PENDING,
                    TaskStatus.SUCCEEDED,
                    TaskStatus.FAILED,
                    TaskStatus.BLOCKED,
                }
                or attempt.goal_id != current.goal_id
                or attempt.task_id != current.task_id
                or attempt.agent_id != current.agent_id
                or review.goal_id != current.goal_id
                or review.task_id != current.task_id
                or review.review_id != attempt.review_id
                or review.attempt_no != attempt.attempt_no
                or performance.agent_id != current.agent_id
                or performance.task_type != task.task_type
            ):
                raise ValueError("outcome identity does not match active claim")
            if task.status == TaskStatus.SUCCEEDED and (
                artifact is None or review.verdict != Verdict.PASS
            ):
                raise ValueError(
                    "succeeded task requires a passing review and artifact"
                )
            if artifact is None:
                if attempt.artifact_id is not None or task.artifact_id is not None:
                    raise ValueError("outcome artifact identity is inconsistent")
            elif (
                artifact.goal_id != current.goal_id
                or artifact.task_id != current.task_id
                or artifact.agent_id != current.agent_id
                or attempt.artifact_id != artifact.artifact_id
                or (
                    task.status == TaskStatus.SUCCEEDED
                    and task.artifact_id != artifact.artifact_id
                )
                or (
                    task.status != TaskStatus.SUCCEEDED
                    and task.artifact_id not in {None, artifact.artifact_id}
                )
            ):
                raise ValueError("outcome artifact identity does not match active claim")
            if any(event.goal_id != current.goal_id for event in events):
                raise ValueError("outcome event belongs to another goal")

            task_row = self.connection.execute(
                "SELECT payload FROM tasks WHERE goal_id=? AND task_id=?",
                (current.goal_id, current.task_id),
            ).fetchone()
            if task_row is None:
                raise StaleClaim("claimed task no longer exists")
            durable_task = self._task_from_payload(task_row["payload"])
            if (
                durable_task.status != TaskStatus.RUNNING
                or durable_task.assigned_agent_id != current.agent_id
            ):
                raise StaleClaim("claim identity no longer owns task")

            if artifact is not None:
                self.connection.execute(
                    "INSERT INTO artifacts(artifact_id, goal_id, task_id, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        artifact.artifact_id,
                        artifact.goal_id,
                        artifact.task_id,
                        _dump(asdict(artifact)),
                    ),
                )
            self.connection.execute(
                "INSERT INTO attempts(attempt_id, goal_id, task_id, attempt_no, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    attempt.attempt_id,
                    attempt.goal_id,
                    attempt.task_id,
                    attempt.attempt_no,
                    _dump(asdict(attempt)),
                ),
            )
            self.connection.execute(
                "INSERT INTO reviews(review_id, goal_id, task_id, attempt_no, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    review.review_id,
                    review.goal_id,
                    review.task_id,
                    review.attempt_no,
                    _dump(asdict(review)),
                ),
            )
            self.connection.execute(
                "INSERT INTO performance(agent_id, task_type, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(agent_id, task_type) DO UPDATE SET payload=excluded.payload",
                (
                    performance.agent_id,
                    performance.task_type,
                    _dump(asdict(performance)),
                ),
            )
            for event in events:
                self.connection.execute(
                    "INSERT INTO events(event_id, goal_id, payload) VALUES (?, ?, ?)",
                    (event.event_id, event.goal_id, _dump(asdict(event))),
                )
            for message in outbox_messages:
                self._enqueue_outbox_locked(message)
            self.connection.execute(
                "UPDATE tasks SET payload=? WHERE goal_id=? AND task_id=?",
                (_dump(asdict(task)), task.goal_id, task.task_id),
            )
            self._reconcile_goal_locked(task)
            committed = current.finish(ClaimStatus.COMMITTED, now=now)
            self.connection.execute(
                "UPDATE task_claims SET status=?, payload=? "
                "WHERE claim_id=? AND status='active' AND fencing_token=?",
                (
                    committed.status.value,
                    _dump(asdict(committed)),
                    committed.claim_id,
                    committed.fencing_token,
                ),
            )
        return committed

    def _reconcile_goal_locked(self, committed_task: Task) -> None:
        goal_row = self.connection.execute(
            "SELECT payload FROM goals WHERE goal_id=?", (committed_task.goal_id,)
        ).fetchone()
        if goal_row is None:
            raise StaleClaim("claimed goal no longer exists")
        goal_data = _load(goal_row["payload"])
        goal_data["status"] = GoalStatus(goal_data["status"])
        goal = Goal(**goal_data)
        if goal.status != GoalStatus.RUNNING:
            raise StaleClaim("claimed goal is no longer running")

        target: GoalStatus | None = None
        reason = ""
        if committed_task.status == TaskStatus.FAILED:
            target = GoalStatus.FAILED
            reason = f"task {committed_task.task_id} failed"
        elif committed_task.status == TaskStatus.BLOCKED:
            target = GoalStatus.BLOCKED
            reason = f"task {committed_task.task_id} blocked"
        elif committed_task.status == TaskStatus.SUCCEEDED:
            task_rows = self.connection.execute(
                "SELECT payload FROM tasks WHERE goal_id=?", (committed_task.goal_id,)
            ).fetchall()
            tasks = [self._task_from_payload(row["payload"]) for row in task_rows]
            if tasks and all(task.status == TaskStatus.SUCCEEDED for task in tasks):
                target = GoalStatus.SUCCEEDED

        if target is None:
            return
        updated = transition_goal(goal, target, reason)
        self.connection.execute(
            "UPDATE goals SET payload=? WHERE goal_id=?",
            (_dump(asdict(updated)), updated.goal_id),
        )
        event = Event.create(
            updated.goal_id,
            f"goal.{target.value}",
            {
                "task_id": committed_task.task_id,
                **({"reason": reason} if reason else {}),
            },
        )
        self.connection.execute(
            "INSERT INTO events(event_id, goal_id, payload) VALUES (?, ?, ?)",
            (event.event_id, event.goal_id, _dump(asdict(event))),
        )

    def save_goal(self, goal: Goal) -> None:
        self.connection.execute(
            "INSERT INTO goals(goal_id, payload) VALUES (?, ?) "
            "ON CONFLICT(goal_id) DO UPDATE SET payload=excluded.payload",
            (goal.goal_id, _dump(asdict(goal))),
        )
        self.connection.commit()

    def get_goal(self, goal_id: str) -> Goal | None:
        row = self.connection.execute(
            "SELECT payload FROM goals WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["status"] = GoalStatus(data["status"])
        return Goal(**data)

    def _reject_legacy_claimed_write_locked(
        self, goal_id: str, task_id: str
    ) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM task_claims "
            "WHERE goal_id=? AND task_id=? AND status='active'",
            (goal_id, task_id),
        ).fetchone()
        if row is not None:
            raise StaleClaim(
                "active scheduler claim requires a fenced outcome mutation"
            )

    def save_tasks(self, tasks: Iterable[Task]) -> None:
        values = list(tasks)
        with self._immediate_transaction():
            for task in values:
                self._reject_legacy_claimed_write_locked(
                    task.goal_id, task.task_id
                )
                self.connection.execute(
                    "INSERT INTO tasks(task_id, goal_id, position, payload) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(goal_id, task_id) DO UPDATE SET "
                    "position=excluded.position, payload=excluded.payload",
                    (task.task_id, task.goal_id, task.position, _dump(asdict(task))),
                )

    def save_task(self, task: Task) -> None:
        self.save_tasks([task])

    def list_tasks(self, goal_id: str) -> list[Task]:
        rows = self.connection.execute(
            "SELECT payload FROM tasks WHERE goal_id=? ORDER BY position, task_id",
            (goal_id,),
        ).fetchall()
        tasks = []
        for row in rows:
            data = _load(row["payload"])
            data["dependencies"] = tuple(data["dependencies"])
            data["status"] = TaskStatus(data["status"])
            tasks.append(Task(**data))
        return tasks

    def save_artifact(self, artifact: Artifact) -> None:
        with self._immediate_transaction():
            self._reject_legacy_claimed_write_locked(
                artifact.goal_id, artifact.task_id
            )
            self.connection.execute(
                "INSERT INTO artifacts(artifact_id, goal_id, task_id, payload) "
                "VALUES (?, ?, ?, ?)",
                (
                    artifact.artifact_id,
                    artifact.goal_id,
                    artifact.task_id,
                    _dump(asdict(artifact)),
                ),
            )

    def list_artifacts(self, goal_id: str, task_id: str | None = None) -> list[Artifact]:
        if task_id is None:
            rows = self.connection.execute(
                "SELECT payload FROM artifacts WHERE goal_id=? ORDER BY rowid", (goal_id,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM artifacts WHERE goal_id=? AND task_id=? ORDER BY rowid",
                (goal_id, task_id),
            ).fetchall()
        return [Artifact(**_load(row["payload"])) for row in rows]

    def save_review(self, review: Review) -> None:
        with self._immediate_transaction():
            self._reject_legacy_claimed_write_locked(
                review.goal_id, review.task_id
            )
            self.connection.execute(
                "INSERT INTO reviews(review_id, goal_id, task_id, attempt_no, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    review.review_id,
                    review.goal_id,
                    review.task_id,
                    review.attempt_no,
                    _dump(asdict(review)),
                ),
            )

    def list_reviews(self, goal_id: str, task_id: str | None = None) -> list[Review]:
        query = "SELECT payload FROM reviews WHERE goal_id=?"
        parameters: list[Any] = [goal_id]
        if task_id is not None:
            query += " AND task_id=?"
            parameters.append(task_id)
        query += " ORDER BY attempt_no, rowid"
        rows = self.connection.execute(query, parameters).fetchall()
        reviews = []
        for row in rows:
            data = _load(row["payload"])
            data["verdict"] = Verdict(data["verdict"])
            data["defects"] = tuple(Defect(**defect) for defect in data["defects"])
            reviews.append(Review(**data))
        return reviews

    def list_attempts(self, goal_id: str, task_id: str | None = None) -> list[Attempt]:
        query = "SELECT payload FROM attempts WHERE goal_id=?"
        parameters: list[Any] = [goal_id]
        if task_id is not None:
            query += " AND task_id=?"
            parameters.append(task_id)
        query += " ORDER BY attempt_no, rowid"
        rows = self.connection.execute(query, parameters).fetchall()
        return [Attempt(**_load(row["payload"])) for row in rows]

    def save_attempt_outcome(
        self,
        attempt: Attempt,
        review: Review,
        performance: PerformanceRecord,
        event: Event,
    ) -> None:
        """Commit the reviewed outcome and its audit evidence as one unit."""
        with self._immediate_transaction():
            self._reject_legacy_claimed_write_locked(
                attempt.goal_id, attempt.task_id
            )
            self.connection.execute(
                "INSERT INTO attempts(attempt_id, goal_id, task_id, attempt_no, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    attempt.attempt_id,
                    attempt.goal_id,
                    attempt.task_id,
                    attempt.attempt_no,
                    _dump(asdict(attempt)),
                ),
            )
            self.connection.execute(
                "INSERT INTO reviews(review_id, goal_id, task_id, attempt_no, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    review.review_id,
                    review.goal_id,
                    review.task_id,
                    review.attempt_no,
                    _dump(asdict(review)),
                ),
            )
            self.connection.execute(
                "INSERT INTO performance(agent_id, task_type, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(agent_id, task_type) DO UPDATE SET payload=excluded.payload",
                (
                    performance.agent_id,
                    performance.task_type,
                    _dump(asdict(performance)),
                ),
            )
            self.connection.execute(
                "INSERT INTO events(event_id, goal_id, payload) VALUES (?, ?, ?)",
                (event.event_id, event.goal_id, _dump(asdict(event))),
            )

    def append_event(self, event: Event) -> Event:
        cursor = self.connection.execute(
            "INSERT INTO events(event_id, goal_id, payload) VALUES (?, ?, ?)",
            (event.event_id, event.goal_id, _dump(asdict(event))),
        )
        self.connection.commit()
        return replace(event, sequence=int(cursor.lastrowid))

    def list_events(self, goal_id: str) -> list[Event]:
        rows = self.connection.execute(
            "SELECT sequence, payload FROM events WHERE goal_id=? ORDER BY sequence",
            (goal_id,),
        ).fetchall()
        return [
            Event(**(_load(row["payload"]) | {"sequence": row["sequence"]}))
            for row in rows
        ]

    def save_agent(self, agent: AgentProfile) -> None:
        self.connection.execute(
            "INSERT INTO agents(agent_id, payload) VALUES (?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET payload=excluded.payload",
            (agent.agent_id, _dump(asdict(agent))),
        )
        self.connection.commit()

    def list_agents(self) -> list[AgentProfile]:
        rows = self.connection.execute(
            "SELECT payload FROM agents ORDER BY agent_id"
        ).fetchall()
        result = []
        for row in rows:
            data = _load(row["payload"])
            data["task_types"] = tuple(data["task_types"])
            data.setdefault("execution_kind", "local")
            result.append(AgentProfile(**data))
        return result

    def get_agent(self, agent_id: str) -> AgentProfile | None:
        row = self.connection.execute(
            "SELECT payload FROM agents WHERE agent_id=?", (agent_id,)
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["task_types"] = tuple(data["task_types"])
        data.setdefault("execution_kind", "local")
        return AgentProfile(**data)

    def save_performance(self, performance: PerformanceRecord) -> None:
        self.connection.execute(
            "INSERT INTO performance(agent_id, task_type, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(agent_id, task_type) DO UPDATE SET payload=excluded.payload",
            (performance.agent_id, performance.task_type, _dump(asdict(performance))),
        )
        self.connection.commit()

    def get_performance(self, agent_id: str, task_type: str) -> PerformanceRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM performance WHERE agent_id=? AND task_type=?",
            (agent_id, task_type),
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["recent_results"] = tuple(data["recent_results"])
        return PerformanceRecord(**data)

    def list_performance(self) -> list[PerformanceRecord]:
        rows = self.connection.execute(
            "SELECT payload FROM performance ORDER BY agent_id, task_type"
        ).fetchall()
        records = []
        for row in rows:
            data = _load(row["payload"])
            data["recent_results"] = tuple(data["recent_results"])
            records.append(PerformanceRecord(**data))
        return records

    @staticmethod
    def _genome_from_payload(payload: str) -> AgentGenome:
        data = _load(payload)
        self_model = data["self_model"]
        if isinstance(self_model, dict):
            self_model["success_signals"] = tuple(self_model.get("success_signals", ()))
            self_model["failure_modes"] = tuple(self_model.get("failure_modes", ()))
            data["self_model"] = AgentSelfModel(**self_model)
        data["traits"] = tuple(data.get("traits", ()))
        data["tool_profile"] = tuple(data.get("tool_profile", ()))
        data["parents"] = tuple(data.get("parents", ()))
        return AgentGenome(**data)

    def save_agent_genome(self, genome: AgentGenome) -> None:
        self.connection.execute(
            "INSERT INTO agent_genomes(agent_id, payload) VALUES (?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET payload=excluded.payload",
            (genome.agent_id, _dump(asdict(genome))),
        )
        self.connection.commit()

    def get_agent_genome(self, agent_id: str) -> AgentGenome | None:
        row = self.connection.execute(
            "SELECT payload FROM agent_genomes WHERE agent_id=?", (agent_id,)
        ).fetchone()
        return None if row is None else self._genome_from_payload(row["payload"])

    def list_agent_genomes(self) -> list[AgentGenome]:
        rows = self.connection.execute(
            "SELECT payload FROM agent_genomes ORDER BY agent_id"
        ).fetchall()
        return [self._genome_from_payload(row["payload"]) for row in rows]

    @staticmethod
    def _experience_from_payload(payload: str) -> ExperienceRecord:
        data = _load(payload)
        data["lessons"] = tuple(data.get("lessons", ()))
        data["tags"] = tuple(data.get("tags", ()))
        return ExperienceRecord(**data)

    def save_experience(
        self,
        experience: ExperienceRecord,
        *,
        overwrite: bool = False,
        operator: str = "local",
        reason: str = "",
    ) -> None:
        # Read the prior state BEFORE writing so the revision records what
        # actually changed. A non-overwrite insert that conflicts changes
        # nothing, so it must not append a revision claiming a change.
        previous = self.get_experience(experience.experience_id)
        if overwrite:
            self.connection.execute(
                "INSERT INTO experience_records("
                "experience_id, agent_id, task_type, goal_id, payload"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(experience_id) DO UPDATE SET "
                "agent_id=excluded.agent_id, task_type=excluded.task_type, "
                "goal_id=excluded.goal_id, payload=excluded.payload",
                (
                    experience.experience_id,
                    experience.agent_id,
                    experience.task_type,
                    experience.goal_id,
                    _dump(asdict(experience)),
                ),
            )
            mutated = True
        else:
            cursor = self.connection.execute(
                "INSERT INTO experience_records("
                "experience_id, agent_id, task_type, goal_id, payload"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(experience_id) DO NOTHING",
                (
                    experience.experience_id,
                    experience.agent_id,
                    experience.task_type,
                    experience.goal_id,
                    _dump(asdict(experience)),
                ),
            )
            mutated = cursor.rowcount > 0
        if mutated:
            self._append_seed_revision(
                SeedKind.EXPERIENCE,
                experience.experience_id,
                RevisionAction.UPDATE if previous is not None else RevisionAction.CREATE,
                operator=operator,
                before_payload=asdict(previous) if previous is not None else None,
                after_payload=asdict(experience),
                reason=reason,
            )
        self.connection.commit()

    def get_experience(self, experience_id: str) -> ExperienceRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM experience_records WHERE experience_id=?",
            (experience_id,),
        ).fetchone()
        return None if row is None else self._experience_from_payload(row["payload"])

    def delete_experience(self, experience_id: str) -> None:
        self.connection.execute(
            "DELETE FROM experience_records WHERE experience_id=?", (experience_id,)
        )
        self.connection.commit()

    def list_experience(
        self,
        *,
        agent_id: str | None = None,
        task_type: str | None = None,
        goal_id: str | None = None,
        limit: int | None = None,
        min_strength: float | None = None,
    ) -> list[ExperienceRecord]:
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("experience list limit must be between 1 and 10000")
        if min_strength is not None and not 0 <= min_strength <= 1:
            raise ValueError("experience min_strength must be between 0 and 1")
        query = "SELECT payload FROM experience_records"
        clauses = []
        parameters: list[Any] = []
        for field, value in (
            ("agent_id", agent_id),
            ("task_type", task_type),
            ("goal_id", goal_id),
        ):
            if value is not None:
                clauses.append(f"{field}=?")
                parameters.append(str(value))
        if min_strength is not None:
            clauses.append(
                "COALESCE(json_extract(payload, '$.strength'), 1.0) >= ?"
            )
            parameters.append(min_strength)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY experience_id"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        rows = self.connection.execute(query, parameters).fetchall()
        return [self._experience_from_payload(row["payload"]) for row in rows]

    def save_knowledge(
        self,
        item: KnowledgeItem,
        *,
        operator: str = "local",
        reason: str = "",
    ) -> None:
        """Write a knowledge seed and append its revision atomically."""
        previous = self.get_knowledge(item.knowledge_id)
        with self._immediate_transaction():
            self.connection.execute(
                "INSERT INTO knowledge(knowledge_id, payload) VALUES (?, ?) "
                "ON CONFLICT(knowledge_id) DO UPDATE SET payload=excluded.payload",
                (item.knowledge_id, _dump(asdict(item))),
            )
            self._append_seed_revision(
                SeedKind.KNOWLEDGE,
                item.knowledge_id,
                RevisionAction.UPDATE if previous is not None else RevisionAction.CREATE,
                operator=operator,
                before_payload=asdict(previous) if previous is not None else None,
                after_payload=asdict(item),
                reason=reason,
            )

    def get_knowledge(self, knowledge_id: str) -> KnowledgeItem | None:
        row = self.connection.execute(
            "SELECT payload FROM knowledge WHERE knowledge_id=?", (knowledge_id,)
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["tags"] = tuple(data["tags"])
        return KnowledgeItem(**data)

    def list_knowledge(self) -> list[KnowledgeItem]:
        rows = self.connection.execute(
            "SELECT payload FROM knowledge ORDER BY rowid"
        ).fetchall()
        result = []
        for row in rows:
            data = _load(row["payload"])
            data["tags"] = tuple(data["tags"])
            result.append(KnowledgeItem(**data))
        return result

    def delete_knowledge(self, knowledge_id: str) -> None:
        self.connection.execute(
            "DELETE FROM knowledge WHERE knowledge_id=?", (knowledge_id,)
        )
        self.connection.commit()

    def _append_seed_revision(
        self,
        seed_kind: SeedKind,
        seed_id: str,
        action: RevisionAction,
        *,
        operator: str,
        before_payload: dict[str, Any] | None,
        after_payload: dict[str, Any] | None,
        reason: str = "",
        rolled_back_revision_id: str = "",
    ) -> SeedRevision:
        revision = SeedRevision.create(
            seed_kind,
            seed_id,
            action,
            operator=operator,
            before_payload=before_payload,
            after_payload=after_payload,
            reason=reason,
            rolled_back_revision_id=rolled_back_revision_id,
        )
        self.connection.execute(
            "INSERT INTO seed_revisions("
            "revision_id, seed_kind, seed_id, action, before_payload, "
            "after_payload, operator, reason, rolled_back_revision_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision.revision_id,
                revision.seed_kind.value,
                revision.seed_id,
                revision.action.value,
                _dump(revision.before_payload) if revision.before_payload else None,
                _dump(revision.after_payload) if revision.after_payload else None,
                revision.operator,
                revision.reason,
                revision.rolled_back_revision_id,
                revision.created_at,
            ),
        )
        return revision

    def record_seed_revision(
        self, revision: SeedRevision
    ) -> SeedRevision:
        """Append an already-built revision (used by rollback)."""
        with self._immediate_transaction():
            self.connection.execute(
                "INSERT INTO seed_revisions("
                "revision_id, seed_kind, seed_id, action, before_payload, "
                "after_payload, operator, reason, rolled_back_revision_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision.revision_id,
                    revision.seed_kind.value,
                    revision.seed_id,
                    revision.action.value,
                    _dump(revision.before_payload) if revision.before_payload else None,
                    _dump(revision.after_payload) if revision.after_payload else None,
                    revision.operator,
                    revision.reason,
                    revision.rolled_back_revision_id,
                    revision.created_at,
                ),
            )
        return revision

    @staticmethod
    def _revision_from_row(row: Any) -> SeedRevision:
        return SeedRevision(
            revision_id=row["revision_id"],
            seed_kind=SeedKind(row["seed_kind"]),
            seed_id=row["seed_id"],
            action=RevisionAction(row["action"]),
            operator=row["operator"],
            before_payload=_load(row["before_payload"]) if row["before_payload"] else None,
            after_payload=_load(row["after_payload"]) if row["after_payload"] else None,
            reason=row["reason"] or "",
            rolled_back_revision_id=row["rolled_back_revision_id"] or "",
            created_at=row["created_at"],
        )

    def list_seed_revisions(
        self,
        *,
        seed_kind: SeedKind | None = None,
        seed_id: str | None = None,
        limit: int | None = None,
    ) -> list[SeedRevision]:
        query = "SELECT * FROM seed_revisions"
        clauses: list[str] = []
        parameters: list[Any] = []
        if seed_kind is not None:
            clauses.append("seed_kind=?")
            parameters.append(SeedKind(seed_kind).value)
        if seed_id is not None:
            clauses.append("seed_id=?")
            parameters.append(str(seed_id))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, rowid"
        if limit is not None:
            if not 1 <= limit <= 10_000:
                raise ValueError("revision list limit must be between 1 and 10000")
            query += " LIMIT ?"
            parameters.append(limit)
        rows = self.connection.execute(query, parameters).fetchall()
        return [self._revision_from_row(row) for row in rows]

    def get_seed_revision(self, revision_id: str) -> SeedRevision | None:
        row = self.connection.execute(
            "SELECT * FROM seed_revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        return None if row is None else self._revision_from_row(row)

    def save_approval(self, approval: ApprovalRequest) -> None:
        with self._immediate_transaction():
            row = self.connection.execute(
                "SELECT payload FROM approvals WHERE approval_id=?",
                (approval.approval_id,),
            ).fetchone()
            if row is not None:
                data = _load(row["payload"])
                data["status"] = ApprovalStatus(data["status"])
                current = ApprovalRequest(**data)
                if current == approval:
                    return
                if (
                    current.status != ApprovalStatus.PENDING
                    or approval.status
                    not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
                    or replace(
                        approval,
                        status=current.status,
                        decided_at=current.decided_at,
                        decided_by=current.decided_by,
                    )
                    != current
                ):
                    raise ValueError("approval identity cannot change")
                self.connection.execute(
                    "UPDATE approvals SET payload=? WHERE approval_id=?",
                    (_dump(asdict(approval)), approval.approval_id),
                )
                return
            self.connection.execute(
                "INSERT INTO approvals(approval_id, fingerprint, goal_id, task_id, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    approval.approval_id,
                    approval.fingerprint,
                    approval.goal_id,
                    approval.task_id,
                    _dump(asdict(approval)),
                ),
            )

    def save_approval_resolution(
        self,
        approval: ApprovalRequest,
        events: Iterable[Event],
        goal: Goal | None = None,
    ) -> None:
        """Commit an approval decision, lifecycle state, and audit events atomically."""
        with self._immediate_transaction():
            approval_row = self.connection.execute(
                "SELECT payload FROM approvals WHERE approval_id=?",
                (approval.approval_id,),
            ).fetchone()
            if approval_row is None:
                raise KeyError(f"approval not found: {approval.approval_id}")
            data = _load(approval_row["payload"])
            data["status"] = ApprovalStatus(data["status"])
            current = ApprovalRequest(**data)
            expected_pending = replace(
                approval,
                status=ApprovalStatus.PENDING,
                decided_at="",
                decided_by="",
            )
            if current.status != ApprovalStatus.PENDING:
                raise ValueError("approval request is already resolved")
            if current != expected_pending:
                raise ValueError("approval identity cannot change")
            if goal is not None:
                goal_row = self.connection.execute(
                    "SELECT payload FROM goals WHERE goal_id=?", (goal.goal_id,)
                ).fetchone()
                if goal_row is None:
                    raise KeyError(f"goal not found: {goal.goal_id}")
                goal_data = _load(goal_row["payload"])
                goal_data["status"] = GoalStatus(goal_data["status"])
                current_goal = Goal(**goal_data)
                if (
                    current_goal.goal_id != approval.goal_id
                    or current_goal.status != GoalStatus.PAUSED
                    or goal.status not in {GoalStatus.RUNNING, GoalStatus.FAILED}
                ):
                    raise ValueError("approval goal is no longer paused")
            self.connection.execute(
                "UPDATE approvals SET payload=? WHERE approval_id=?",
                (_dump(asdict(approval)), approval.approval_id),
            )
            if goal is not None:
                self.connection.execute(
                    "UPDATE goals SET payload=? WHERE goal_id=?",
                    (_dump(asdict(goal)), goal.goal_id),
                )
            for event in events:
                self.connection.execute(
                    "INSERT INTO events(event_id, goal_id, payload) VALUES (?, ?, ?)",
                    (event.event_id, event.goal_id, _dump(asdict(event))),
                )

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        row = self.connection.execute(
            "SELECT payload FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["status"] = ApprovalStatus(data["status"])
        return ApprovalRequest(**data)

    def get_approval_by_fingerprint(self, fingerprint: str) -> ApprovalRequest | None:
        row = self.connection.execute(
            "SELECT payload FROM approvals WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["status"] = ApprovalStatus(data["status"])
        return ApprovalRequest(**data)

    def list_approvals(self, goal_id: str | None = None) -> list[ApprovalRequest]:
        if goal_id is None:
            rows = self.connection.execute(
                "SELECT payload FROM approvals ORDER BY rowid"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM approvals WHERE goal_id=? ORDER BY rowid",
                (goal_id,),
            ).fetchall()
        result = []
        for row in rows:
            data = _load(row["payload"])
            data["status"] = ApprovalStatus(data["status"])
            result.append(ApprovalRequest(**data))
        return result

    def save_span(self, span: TraceSpan) -> None:
        self.connection.execute(
            "INSERT INTO trace_spans(span_id, trace_id, goal_id, task_id, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (span.span_id, span.trace_id, span.goal_id, span.task_id, _dump(asdict(span))),
        )
        self.connection.commit()

    def list_spans(self, goal_id: str) -> list[TraceSpan]:
        rows = self.connection.execute(
            "SELECT payload FROM trace_spans WHERE goal_id=? ORDER BY rowid",
            (goal_id,),
        ).fetchall()
        result = []
        for row in rows:
            data = _load(row["payload"])
            data["status"] = SpanStatus(data["status"])
            result.append(TraceSpan(**data))
        return result

    def save_workspace_snapshot(self, snapshot: WorkspaceSnapshot) -> None:
        existing = self.get_workspace_snapshot(snapshot.goal_id, snapshot.path)
        if existing is not None and (
            existing.original_exists != snapshot.original_exists
            or existing.original_content != snapshot.original_content
            or existing.original_sha256 != snapshot.original_sha256
            or existing.created_at != snapshot.created_at
        ):
            raise ValueError("workspace snapshot original recovery point cannot change")
        self.connection.execute(
            "INSERT INTO workspace_snapshots(goal_id, path, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(goal_id, path) DO UPDATE SET payload=excluded.payload",
            (snapshot.goal_id, snapshot.path, _dump(asdict(snapshot))),
        )
        self.connection.commit()

    def get_workspace_snapshot(
        self, goal_id: str, path: str
    ) -> WorkspaceSnapshot | None:
        row = self.connection.execute(
            "SELECT payload FROM workspace_snapshots WHERE goal_id=? AND path=?",
            (goal_id, path),
        ).fetchone()
        return WorkspaceSnapshot(**_load(row["payload"])) if row is not None else None

    def list_workspace_snapshots(self, goal_id: str) -> list[WorkspaceSnapshot]:
        rows = self.connection.execute(
            "SELECT payload FROM workspace_snapshots WHERE goal_id=? ORDER BY path",
            (goal_id,),
        ).fetchall()
        return [WorkspaceSnapshot(**_load(row["payload"])) for row in rows]

    def save_verification_result(self, result: VerificationResult) -> None:
        self.connection.execute(
            "INSERT INTO verification_results(result_id, goal_id, task_id, check_name, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                result.result_id,
                result.goal_id,
                result.task_id,
                result.check_name,
                _dump(asdict(result)),
            ),
        )
        self.connection.commit()

    def list_verification_results(self, goal_id: str) -> list[VerificationResult]:
        rows = self.connection.execute(
            "SELECT payload FROM verification_results WHERE goal_id=? ORDER BY rowid",
            (goal_id,),
        ).fetchall()
        results = []
        for row in rows:
            data = _load(row["payload"])
            data["command"] = tuple(data["command"])
            results.append(VerificationResult(**data))
        return results

    def save_publication(self, publication: PublicationRecord) -> None:
        existing = self.get_publication(publication.goal_id)
        if existing is not None and existing.payload_digest != publication.payload_digest:
            raise ValueError("publication payload cannot change for a goal")
        if existing is not None and list(PublicationStatus).index(publication.status) < list(PublicationStatus).index(existing.status):
            raise ValueError("publication status cannot move backwards")
        self.connection.execute(
            "INSERT INTO publication_records(goal_id, publication_id, payload_digest, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(goal_id) DO UPDATE SET payload=excluded.payload",
            (publication.goal_id, publication.publication_id, publication.payload_digest, _dump(asdict(publication))),
        )
        self.connection.commit()

    def get_publication(self, goal_id: str) -> PublicationRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM publication_records WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["status"] = PublicationStatus(data["status"])
        data["changed_paths"] = tuple(data["changed_paths"])
        data["check_names"] = tuple(data["check_names"])
        return PublicationRecord(**data)

    def save_evaluation_outcome(self, outcome: EvaluationOutcome) -> None:
        self.connection.execute(
            "INSERT INTO evaluation_outcomes("
            "outcome_id, run_id, case_id, candidate_agent_id, payload"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                outcome.outcome_id,
                outcome.run_id,
                outcome.case_id,
                outcome.candidate_agent_id,
                _dump(asdict(outcome)),
            ),
        )
        self.connection.commit()

    def list_evaluation_outcomes(self, run_id: str) -> list[EvaluationOutcome]:
        rows = self.connection.execute(
            "SELECT payload FROM evaluation_outcomes "
            "WHERE run_id=? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        outcomes = []
        for row in rows:
            data = _load(row["payload"])
            data.setdefault("evidence", {})
            outcomes.append(EvaluationOutcome(**data))
        return outcomes

    def save_evaluation_run(self, run: EvaluationRun) -> None:
        existing = self.get_evaluation_run(run.run_id)
        if existing is not None:
            immutable = (
                "task_type",
                "benchmark_digest",
                "champion_agent_id",
                "champion_model_id",
                "challenger_agent_id",
                "challenger_model_id",
                "case_count",
                "metrics",
                "recommended",
                "failed_gates",
                "created_at",
            )
            if any(getattr(existing, name) != getattr(run, name) for name in immutable):
                raise ValueError("evaluation identity and results cannot change")
            if (
                existing.status == EvaluationStatus.PROMOTED
                and run.status != EvaluationStatus.PROMOTED
            ):
                raise ValueError("evaluation status cannot move backwards")
        self.connection.execute(
            "INSERT INTO evaluation_runs(run_id, task_type, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload",
            (run.run_id, run.task_type, _dump(asdict(run))),
        )
        self.connection.commit()

    def get_evaluation_run(self, run_id: str) -> EvaluationRun | None:
        row = self.connection.execute(
            "SELECT payload FROM evaluation_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return _evaluation_run_from_payload(row["payload"])

    def list_evaluation_runs(self, task_type: str | None = None) -> list[EvaluationRun]:
        if task_type is None:
            rows = self.connection.execute(
                "SELECT payload FROM evaluation_runs ORDER BY rowid"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM evaluation_runs WHERE task_type=? ORDER BY rowid",
                (task_type,),
            ).fetchall()
        return [_evaluation_run_from_payload(row["payload"]) for row in rows]

    def get_deployment(self, task_type: str) -> DeploymentRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM deployments WHERE task_type=?", (task_type,)
        ).fetchone()
        return DeploymentRecord(**_load(row["payload"])) if row is not None else None

    def list_deployments(self) -> list[DeploymentRecord]:
        rows = self.connection.execute(
            "SELECT payload FROM deployments ORDER BY task_type"
        ).fetchall()
        return [DeploymentRecord(**_load(row["payload"])) for row in rows]

    def promote_evaluation(self, run_id: str, promoted_by: str) -> DeploymentRecord:
        run = self.get_evaluation_run(run_id)
        if run is None:
            raise KeyError(f"evaluation run not found: {run_id}")
        promoted = run.promote(promoted_by)
        candidate = self.get_agent(run.challenger_agent_id)
        if (
            candidate is None
            or not candidate.enabled
            or candidate.model_id != run.challenger_model_id
            or candidate.role != "worker"
            or not ("*" in candidate.task_types or run.task_type in candidate.task_types)
        ):
            raise ValueError("challenger identity no longer matches an eligible agent")
        active = self.get_deployment(run.task_type)
        if active is not None and (
            active.champion_agent_id != run.champion_agent_id
            or active.champion_model_id != run.champion_model_id
        ):
            raise ValueError("evaluation has a stale champion identity")
        deployment = DeploymentRecord(
            task_type=run.task_type,
            champion_agent_id=run.challenger_agent_id,
            champion_model_id=run.challenger_model_id,
            source_run_id=run.run_id,
            promoted_by=promoted_by.strip(),
        )
        with self.connection:
            self.connection.execute(
                "UPDATE evaluation_runs SET payload=? WHERE run_id=?",
                (_dump(asdict(promoted)), run.run_id),
            )
            self.connection.execute(
                "INSERT INTO deployments(task_type, source_run_id, payload) "
                "VALUES (?, ?, ?) ON CONFLICT(task_type) DO UPDATE SET "
                "source_run_id=excluded.source_run_id, payload=excluded.payload",
                (
                    deployment.task_type,
                    deployment.source_run_id,
                    _dump(asdict(deployment)),
                ),
            )
        return deployment

    def save_remote_agent(self, registration: RemoteAgentRegistration) -> None:
        existing = self.get_remote_agent(registration.agent_id)
        if existing is not None and existing != registration:
            raise ValueError("remote agent identity cannot change")
        self.connection.execute(
            "INSERT INTO remote_agents(agent_id, card_sha256, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET payload=excluded.payload",
            (
                registration.agent_id,
                registration.card_sha256,
                _dump(asdict(registration)),
            ),
        )
        self.connection.commit()

    def save_remote_agent_profile(
        self,
        registration: RemoteAgentRegistration,
        profile: AgentProfile,
    ) -> None:
        if (
            registration.agent_id != profile.agent_id
            or registration.model_id != profile.model_id
            or profile.execution_kind != "a2a"
        ):
            raise ValueError("remote registration and agent profile do not match")
        existing_registration = self.get_remote_agent(registration.agent_id)
        if existing_registration is not None and existing_registration != registration:
            raise ValueError("remote agent identity cannot change")
        existing_profile = self.get_agent(profile.agent_id)
        if existing_profile is not None and existing_profile != profile:
            raise ValueError("remote agent profile cannot change")
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO remote_agents(agent_id, card_sha256, payload) "
                    "VALUES (?, ?, ?) ON CONFLICT(agent_id) DO UPDATE SET "
                    "payload=excluded.payload",
                    (
                        registration.agent_id,
                        registration.card_sha256,
                        _dump(asdict(registration)),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO agents(agent_id, payload) VALUES (?, ?) "
                    "ON CONFLICT(agent_id) DO UPDATE SET payload=excluded.payload",
                    (profile.agent_id, _dump(asdict(profile))),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("remote agent card identity is already registered") from error

    def get_remote_agent(self, agent_id: str) -> RemoteAgentRegistration | None:
        row = self.connection.execute(
            "SELECT payload FROM remote_agents WHERE agent_id=?", (agent_id,)
        ).fetchone()
        return _remote_agent_from_payload(row["payload"]) if row is not None else None

    def list_remote_agents(self) -> list[RemoteAgentRegistration]:
        rows = self.connection.execute(
            "SELECT payload FROM remote_agents ORDER BY agent_id"
        ).fetchall()
        return [_remote_agent_from_payload(row["payload"]) for row in rows]

    def save_policy(self, policy: DelegationPolicy) -> None:
        existing = self.get_policy(policy.policy_digest)
        if existing is not None and replace(
            existing, created_at=policy.created_at
        ) != policy:
            raise ValueError("delegation policy identity cannot change")
        self.connection.execute(
            "INSERT INTO delegation_policies(policy_digest, policy_id, payload) "
            "VALUES (?, ?, ?) ON CONFLICT(policy_digest) DO NOTHING",
            (policy.policy_digest, policy.policy_id, _dump(asdict(policy))),
        )
        self.connection.commit()

    def get_policy(self, policy_digest: str) -> DelegationPolicy | None:
        row = self.connection.execute(
            "SELECT payload FROM delegation_policies WHERE policy_digest=?",
            (policy_digest,),
        ).fetchone()
        return _policy_from_payload(row["payload"]) if row is not None else None

    def list_policies(self) -> list[DelegationPolicy]:
        rows = self.connection.execute(
            "SELECT payload FROM delegation_policies ORDER BY policy_id, rowid"
        ).fetchall()
        return [_policy_from_payload(row["payload"]) for row in rows]

    def activate_policy(self, activation: PolicyActivation) -> None:
        policy = self.get_policy(activation.policy_digest)
        if policy is None or (
            policy.policy_id != activation.policy_id
            or policy.version != activation.policy_version
        ):
            raise ValueError("policy activation references an unknown policy")
        self.connection.execute(
            "INSERT INTO policy_activations(task_type, policy_digest, payload) "
            "VALUES (?, ?, ?) ON CONFLICT(task_type) DO UPDATE SET "
            "policy_digest=excluded.policy_digest, payload=excluded.payload",
            (
                activation.task_type,
                activation.policy_digest,
                _dump(asdict(activation)),
            ),
        )
        self.connection.commit()

    def get_policy_activation(self, task_type: str) -> PolicyActivation | None:
        row = self.connection.execute(
            "SELECT payload FROM policy_activations WHERE task_type=?", (task_type,)
        ).fetchone()
        return _policy_activation_from_payload(row["payload"]) if row else None

    def list_policy_activations(self) -> list[PolicyActivation]:
        rows = self.connection.execute(
            "SELECT payload FROM policy_activations ORDER BY task_type"
        ).fetchall()
        return [_policy_activation_from_payload(row["payload"]) for row in rows]

    def save_attestation(self, attestation: ConformanceAttestation) -> None:
        existing = self.get_attestation(attestation.attestation_id)
        if existing is not None and replace(
            existing, created_at=attestation.created_at
        ) != attestation:
            raise ValueError("conformance attestation identity cannot change")
        try:
            self.connection.execute(
                "INSERT INTO conformance_attestations("
                "attestation_id, agent_id, card_sha256, report_sha256, payload"
                ") VALUES (?, ?, ?, ?, ?) ON CONFLICT(attestation_id) DO NOTHING",
                (
                    attestation.attestation_id,
                    attestation.agent_id,
                    attestation.card_sha256,
                    attestation.report_sha256,
                    _dump(asdict(attestation)),
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as error:
            raise ValueError("attestation report identity is already bound") from error

    def get_attestation(
        self, attestation_id: str
    ) -> ConformanceAttestation | None:
        row = self.connection.execute(
            "SELECT payload FROM conformance_attestations WHERE attestation_id=?",
            (attestation_id,),
        ).fetchone()
        return _attestation_from_payload(row["payload"]) if row else None

    def list_attestations(
        self, agent_id: str | None = None, card_sha256: str | None = None
    ) -> list[ConformanceAttestation]:
        clauses: list[str] = []
        arguments: list[str] = []
        if agent_id is not None:
            clauses.append("agent_id=?")
            arguments.append(agent_id)
        if card_sha256 is not None:
            clauses.append("card_sha256=?")
            arguments.append(card_sha256)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT payload FROM conformance_attestations{where} ORDER BY rowid",
            arguments,
        ).fetchall()
        return [_attestation_from_payload(row["payload"]) for row in rows]

    def save_policy_decision(self, decision: PolicyDecision) -> None:
        existing = self.get_policy_decision(decision.decision_id)
        if existing is not None and existing != decision:
            raise ValueError("policy decision identity cannot change")
        try:
            self.connection.execute(
                "INSERT INTO policy_decisions("
                "decision_id, goal_id, task_id, attempt_no, agent_id, payload"
                ") VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(decision_id) DO NOTHING",
                (
                    decision.decision_id,
                    decision.goal_id,
                    decision.task_id,
                    decision.attempt_no,
                    decision.agent_id,
                    _dump(asdict(decision)),
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as error:
            raise ValueError("policy decision attempt identity already exists") from error

    def get_policy_decision(self, decision_id: str) -> PolicyDecision | None:
        row = self.connection.execute(
            "SELECT payload FROM policy_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        return _policy_decision_from_payload(row["payload"]) if row else None

    def get_attempt_policy_decision(
        self, goal_id: str, task_id: str, attempt_no: int, agent_id: str
    ) -> PolicyDecision | None:
        row = self.connection.execute(
            "SELECT payload FROM policy_decisions "
            "WHERE goal_id=? AND task_id=? AND attempt_no=? AND agent_id=?",
            (goal_id, task_id, attempt_no, agent_id),
        ).fetchone()
        return _policy_decision_from_payload(row["payload"]) if row else None

    def list_policy_decisions(
        self, goal_id: str | None = None
    ) -> list[PolicyDecision]:
        if goal_id is None:
            rows = self.connection.execute(
                "SELECT payload FROM policy_decisions ORDER BY rowid"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM policy_decisions WHERE goal_id=? ORDER BY rowid",
                (goal_id,),
            ).fetchall()
        return [_policy_decision_from_payload(row["payload"]) for row in rows]

    def save_delegation(self, delegation: DelegationRecord) -> None:
        existing = self.get_delegation(delegation.delegation_id)
        immutable = (
            "goal_id",
            "task_id",
            "attempt_no",
            "agent_id",
            "model_id",
            "card_sha256",
            "message_id",
            "payload_sha256",
            "policy_decision_id",
            "policy_digest",
            "policy_rule_id",
            "created_at",
        )
        if existing is not None and any(
            getattr(existing, name) != getattr(delegation, name) for name in immutable
        ):
            raise ValueError("delegation identity cannot change")
        try:
            self.connection.execute(
                "INSERT INTO delegations("
                "delegation_id, goal_id, task_id, attempt_no, agent_id, payload"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(delegation_id) DO UPDATE SET payload=excluded.payload",
                (
                    delegation.delegation_id,
                    delegation.goal_id,
                    delegation.task_id,
                    delegation.attempt_no,
                    delegation.agent_id,
                    _dump(asdict(delegation)),
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as error:
            raise ValueError("delegation attempt identity already exists") from error

    def get_delegation(self, delegation_id: str) -> DelegationRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM delegations WHERE delegation_id=?", (delegation_id,)
        ).fetchone()
        return _delegation_from_payload(row["payload"]) if row is not None else None

    def get_attempt_delegation(
        self, goal_id: str, task_id: str, attempt_no: int, agent_id: str
    ) -> DelegationRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM delegations "
            "WHERE goal_id=? AND task_id=? AND attempt_no=? AND agent_id=?",
            (goal_id, task_id, attempt_no, agent_id),
        ).fetchone()
        return _delegation_from_payload(row["payload"]) if row is not None else None

    def list_delegations(self, goal_id: str | None = None) -> list[DelegationRecord]:
        if goal_id is None:
            rows = self.connection.execute(
                "SELECT payload FROM delegations ORDER BY rowid"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM delegations WHERE goal_id=? ORDER BY rowid",
                (goal_id,),
            ).fetchall()
        return [_delegation_from_payload(row["payload"]) for row in rows]


def _evaluation_run_from_payload(payload: str) -> EvaluationRun:
    data = _load(payload)
    data["status"] = EvaluationStatus(data["status"])
    data["failed_gates"] = tuple(data["failed_gates"])
    return EvaluationRun(**data)


def _remote_agent_from_payload(payload: str) -> RemoteAgentRegistration:
    data = _load(payload)
    data.setdefault("tenant", "")
    data["allowed_context_sections"] = tuple(data["allowed_context_sections"])
    return RemoteAgentRegistration(**data)


def _rule_from_data(data: dict[str, Any]) -> DelegationRule:
    values = dict(data)
    for name in (
        "task_types",
        "agent_ids",
        "card_sha256s",
        "allowed_context_sections",
        "required_attestation_kinds",
    ):
        values[name] = tuple(values[name])
    return DelegationRule(**values)


def _policy_from_payload(payload: str) -> DelegationPolicy:
    data = _load(payload)
    data["rules"] = tuple(_rule_from_data(item) for item in data["rules"])
    return DelegationPolicy(**data)


def _policy_activation_from_payload(payload: str) -> PolicyActivation:
    return PolicyActivation(**_load(payload))


def _attestation_from_payload(payload: str) -> ConformanceAttestation:
    return ConformanceAttestation(**_load(payload))


def _policy_decision_from_payload(payload: str) -> PolicyDecision:
    data = _load(payload)
    data["verdict"] = PolicyVerdict(data["verdict"])
    data["reason_codes"] = tuple(data["reason_codes"])
    data["allowed_context_sections"] = tuple(data["allowed_context_sections"])
    data["attestation_ids"] = tuple(data["attestation_ids"])
    return PolicyDecision(**data)


def _delegation_from_payload(payload: str) -> DelegationRecord:
    data = _load(payload)
    data["status"] = DelegationStatus(data["status"])
    data.setdefault("policy_decision_id", "")
    data.setdefault("policy_digest", "")
    data.setdefault("policy_rule_id", "")
    return DelegationRecord(**data)
