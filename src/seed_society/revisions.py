"""Deterministic seed rollback: undo a knowledge or experience revision.

Design (adapted from the reviewed `dsh-continual-evolve` rollback discipline,
re-specified for this project's seed semantics):

- History is append-only (`seed_revisions`). A rollback never deletes or edits
  a revision; it writes the inverse payload back into the live table and
  appends its own revision naming the one it undid.
- Rollback is pure data transformation over recorded payloads. No model is
  asked to guess a previous state, and nothing is inferred.
- A rollback is itself reversible: re-applying the named revision restores the
  newer state.

Scope is deliberately limited to the two seed collections this project owns and
mutates. Goals, tasks, artifacts, reviews, attempts and events are audit
records; rewriting them would break the invariant that the audit trail only
ever grows.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from .domain import (
    ExperienceRecord,
    KnowledgeItem,
    RevisionAction,
    SeedKind,
    SeedRevision,
)
from .storage import SQLiteRepository, _dump


class RollbackRejected(RuntimeError):
    """The requested revision cannot be undone, with the reason why."""


@dataclass(frozen=True, slots=True)
class RollbackReport:
    revision_id: str
    seed_kind: SeedKind
    seed_id: str
    action: RevisionAction
    restored: bool
    """True when the live seed was changed back; False for a recorded no-op."""
    detail: str
    rollback_revision_id: str


class SeedRollback:
    """Undo a seed revision by writing its inverse into the live store."""

    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    def rollback(
        self,
        revision_id: str,
        *,
        operator: str,
        reason: str = "",
    ) -> RollbackReport:
        revision = self.repository.get_seed_revision(revision_id)
        if revision is None:
            raise KeyError(f"revision not found: {revision_id}")
        if not revision.is_reversible():
            raise RollbackRejected(
                f"revision {revision_id} is a {revision.action.value} and cannot "
                "be rolled back; re-apply the revision it reversed instead"
            )
        if not str(operator).strip():
            raise RollbackRejected("rollback requires an operator identity")

        if revision.seed_kind is SeedKind.KNOWLEDGE:
            restored = self._rollback_knowledge(revision)
        elif revision.seed_kind is SeedKind.EXPERIENCE:
            restored = self._rollback_experience(revision)
        else:  # pragma: no cover - SeedKind is closed
            raise RollbackRejected(f"unsupported seed kind: {revision.seed_kind}")

        record = SeedRevision.create(
            revision.seed_kind,
            revision.seed_id,
            RevisionAction.ROLLBACK,
            operator=operator,
            before_payload=self._live_payload(revision.seed_kind, revision.seed_id),
            after_payload=revision.inverse_payload(),
            reason=reason or f"rollback of {revision_id}",
            rolled_back_revision_id=revision_id,
        )
        self.repository.record_seed_revision(record)
        return RollbackReport(
            revision_id=revision_id,
            seed_kind=revision.seed_kind,
            seed_id=revision.seed_id,
            action=revision.action,
            restored=restored,
            detail=(
                "live seed restored to its pre-revision payload"
                if restored
                else "no live seed to restore (already absent)"
            ),
            rollback_revision_id=record.revision_id,
        )

    def _live_payload(self, kind: SeedKind, seed_id: str) -> dict[str, Any] | None:
        if kind is SeedKind.KNOWLEDGE:
            item = self.repository.get_knowledge(seed_id)
            return None if item is None else _knowledge_payload(item)
        record = self.repository.get_experience(seed_id)
        return None if record is None else _experience_payload(record)

    def _rollback_knowledge(self, revision: SeedRevision) -> bool:
        target = self._target_payload(revision)
        if target is None:
            # Undoing a create removes the seed.
            if self.repository.get_knowledge(revision.seed_id) is None:
                return False
            self.repository.delete_knowledge(revision.seed_id)
            return True
        self._write_knowledge(_knowledge_from_payload(revision.seed_id, target))
        return True

    def _target_payload(self, revision: SeedRevision) -> dict[str, Any] | None:
        """Which payload a rollback should install.

        Normally the state *before* the revision. If the live seed already
        equals that (the revision was already undone), the rollback toggles
        forward to the state the revision originally produced, so an undo of an
        undo restores the newer state instead of repeating itself.
        """
        before = revision.before_payload
        after = revision.after_payload
        live = self._live_payload(revision.seed_kind, revision.seed_id)
        if revision.action is RevisionAction.CREATE:
            # Undoing a create means removal; only reach here if already absent.
            return after if live is None else None
        if live is not None and before is not None and _same_payload(live, before):
            return after
        return before

    def _write_knowledge(self, item: KnowledgeItem) -> None:
        self.repository.connection.execute(
            "INSERT INTO knowledge(knowledge_id, payload) VALUES (?, ?) "
            "ON CONFLICT(knowledge_id) DO UPDATE SET payload=excluded.payload",
            (item.knowledge_id, _dump_payload(_knowledge_payload(item))),
        )
        self.repository.connection.commit()

    def _rollback_experience(self, revision: SeedRevision) -> bool:
        target = self._target_payload(revision)
        if target is None:
            if self.repository.get_experience(revision.seed_id) is None:
                return False
            self.repository.delete_experience(revision.seed_id)
            return True
        self._write_experience(_experience_from_payload(revision.seed_id, target))
        return True

    def _write_experience(self, restored: ExperienceRecord) -> None:
        self.repository.connection.execute(
            "INSERT INTO experience_records("
            "experience_id, agent_id, task_type, goal_id, payload"
            ") VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(experience_id) DO UPDATE SET "
            "agent_id=excluded.agent_id, task_type=excluded.task_type, "
            "goal_id=excluded.goal_id, payload=excluded.payload",
            (
                restored.experience_id,
                restored.agent_id,
                restored.task_type,
                restored.goal_id,
                _dump_payload(_experience_payload(restored)),
            ),
        )
        self.repository.connection.commit()


def _dump_payload(payload: dict[str, Any]) -> str:
    return _dump(payload)


def _same_payload(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare two payloads by their canonical serialization.

    A live payload carries tuples where a JSON-loaded one carries lists, so a
    plain dict comparison would report a difference that does not exist.
    """
    return _dump_payload(left) == _dump_payload(right)


def _knowledge_payload(item: KnowledgeItem) -> dict[str, Any]:
    return asdict(item)


def _knowledge_from_payload(
    knowledge_id: str, payload: dict[str, Any]
) -> KnowledgeItem:
    return KnowledgeItem(
        knowledge_id=knowledge_id,
        title=str(payload["title"]),
        content=str(payload["content"]),
        tags=tuple(payload.get("tags") or ()),
        created_at=str(payload["created_at"]),
    )


def _experience_payload(record: ExperienceRecord) -> dict[str, Any]:
    return asdict(record)


def _experience_from_payload(
    experience_id: str, payload: dict[str, Any]
) -> ExperienceRecord:
    return ExperienceRecord(
        experience_id=experience_id,
        goal_id=str(payload["goal_id"]),
        task_id=str(payload["task_id"]),
        task_type=str(payload["task_type"]),
        agent_id=str(payload["agent_id"]),
        attempt_no=int(payload["attempt_no"]),
        verdict=str(payload["verdict"]),
        score=float(payload["score"]),
        lessons=tuple(payload.get("lessons") or ()),
        tags=tuple(payload.get("tags") or ()),
        artifact_excerpt=str(payload.get("artifact_excerpt") or ""),
        created_at=str(payload["created_at"]),
        valence=float(payload.get("valence", 0.0)),
        salience=float(payload.get("salience", 0.5)),
        strength=float(payload.get("strength", 1.0)),
        activations=int(payload.get("activations", 0)),
        last_activated_at=str(payload.get("last_activated_at") or ""),
    )
