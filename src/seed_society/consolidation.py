"""Sleep-replay consolidation: salience, decay, and semantic promotion.

Neuroscience <-> Yogacara mapping implemented here:

- Hippocampal replay during sleep  == 离线熏习: replay a goal's attempts in
  order and distill them into experience lessons.
- Dopamine reward-prediction error == 现行与种子预期的差异: the salience of
  an attempt grows with |score - expected_score|, so surprising outcomes are
  encoded more strongly.
- Vedana (受心所)                == valence: PASS is pleasant (+), FAIL is
  unpleasant (-), scaled by score.
- Ebbinghaus forgetting curve     == 种子势力衰减: strength decays
  exponentially after its half-life; retrieval re-strengthens it.
- Systems consolidation / vipaka  == 异熟: a lesson is promoted from episodic
  experience into semantic knowledge only after it is corroborated across
  multiple goals and retains high strength. Promotion is bounded and
  advisory unless the operator passes --apply.

Sila: consolidation never rewrites acceptance criteria, never mutates
genomes, never activates deployments, and performs dynamic mutations (decay,
promotion writes) only under apply=True. Replay distillation itself is
idempotent and content-addressed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .domain import (
    Artifact,
    Event,
    ExperienceRecord,
    KnowledgeItem,
    Review,
    Task,
    Verdict,
)
from .experience import lessons_for_review, tags_for_review
from .scheduler import parse_utc
from .storage import SQLiteRepository

EXPERIENCE_DORMANT_THRESHOLD = 0.2


@dataclass(frozen=True, slots=True)
class ConsolidationPolicy:
    """Neuro-inspired thresholds and weights for one consolidation cycle."""

    salience_base_weight: float = 0.4
    salience_surprise_weight: float = 0.4
    salience_defect_weight: float = 0.2
    strength_half_life_seconds: float = 7 * 86_400.0
    decay_horizon_seconds: float = 86_400.0
    promotion_min_strength: float = 0.8
    promotion_min_salience: float = 0.6
    promotion_min_goals: int = 2
    promotion_max_items: int = 10
    promotion_overlap_limit: float = 0.5

    def __post_init__(self) -> None:
        weights = (
            self.salience_base_weight,
            self.salience_surprise_weight,
            self.salience_defect_weight,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
            for value in weights
        ) or not 0 < sum(weights) <= 1:
            raise ValueError("salience weights must be non-negative and sum <= 1")
        if not 0 < self.strength_half_life_seconds:
            raise ValueError("strength half-life must be positive")
        if not 0 <= self.decay_horizon_seconds:
            raise ValueError("decay horizon must not be negative")
        for name, value in (
            ("promotion_min_strength", self.promotion_min_strength),
            ("promotion_min_salience", self.promotion_min_salience),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.promotion_min_goals < 2:
            raise ValueError("promotion requires corroboration across at least 2 goals")
        if not 1 <= self.promotion_max_items <= 100:
            raise ValueError("promotion_max_items must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class ConsolidationReport:
    goal_id: str
    replayed_attempts: int
    experiences_considered: int
    experiences_new: int
    decayed_records: int
    dormant_records: int
    promotion_candidates: tuple[dict[str, Any], ...]
    promotions_applied: int
    self_model_suggestions: tuple[dict[str, Any], ...]
    applied: bool


def decay_factor(elapsed_seconds: float, half_life_seconds: float) -> float:
    """Ebbinghaus-style exponential decay factor for an elapsed duration."""
    if elapsed_seconds <= 0:
        return 1.0
    return 0.5 ** (elapsed_seconds / half_life_seconds)


def _tokens(text: str) -> set[str]:
    import re

    return set(re.findall(r"[\w-]+", str(text).casefold()))


def _overlap(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


class ConsolidationEngine:
    """Replay, re-weight, and promote the society's episodic seeds."""

    def __init__(
        self,
        repository: SQLiteRepository,
        policy: ConsolidationPolicy | None = None,
    ):
        self.repository = repository
        self.policy = policy or ConsolidationPolicy()

    def _now(self) -> str:
        scheduler_now = getattr(self.repository, "scheduler_now", None)
        if callable(scheduler_now):
            return str(scheduler_now())
        from .domain import utc_now

        return utc_now()

    def consolidate(
        self, goal_id: str, *, apply: bool = False
    ) -> ConsolidationReport:
        policy = self.policy
        now = self._now()
        tasks = {
            task.task_id: task
            for task in sorted(
                self.repository.list_tasks(goal_id), key=lambda item: item.position
            )
        }
        if not tasks:
            raise ValueError(f"goal has no task graph: {goal_id}")

        existing_ids = {
            record.experience_id
            for record in self.repository.list_experience(goal_id=goal_id)
        }

        replayed = 0
        considered = 0
        new_records = 0
        expected_by_agent: dict[str, float] = {}
        replayed_records: list[ExperienceRecord] = []
        for task_id, task in tasks.items():
            artifacts = self.repository.list_artifacts(goal_id, task_id)
            attempts = {
                attempt.attempt_no: attempt
                for attempt in self.repository.list_attempts(goal_id, task_id)
            }
            for review in self.repository.list_reviews(goal_id, task_id):
                artifact = self._artifact_for_attempt(artifacts, attempts, review)
                agent_id = self._agent_for_attempt(artifact, attempts, review)
                lessons = lessons_for_review(review)
                tags = tags_for_review(review)
                if not lessons or not agent_id:
                    continue
                replayed += 1
                expected = expected_by_agent.get(agent_id, 50.0)
                surprise = abs(review.score - expected) / 100.0
                defect_ratio = min(1.0, len(review.defects) / 3.0)
                salience = min(
                    1.0,
                    policy.salience_base_weight
                    + policy.salience_surprise_weight * surprise
                    + policy.salience_defect_weight * defect_ratio,
                )
                valence = (
                    review.score / 100.0
                    if review.verdict == Verdict.PASS
                    else -review.score / 100.0
                )
                record = ExperienceRecord.create(
                    task,
                    review,
                    artifact,
                    agent_id,
                    lessons=lessons,
                    tags=tags,
                    salience=salience,
                    valence=valence,
                )
                considered += 1
                replayed_records.append(record)
                if record.experience_id not in existing_ids:
                    self.repository.save_experience(record)
                    new_records += 1
                previous = expected_by_agent.get(agent_id)
                expected_by_agent[agent_id] = (
                    review.score
                    if previous is None
                    else previous + (review.score - previous) / 2.0
                )

        self_model_suggestions = self._self_model_suggestions(replayed_records)

        promotions_applied = 0
        decayed = 0
        dormant = 0
        if apply:
            # Reconsolidation: refresh dynamics of replayed records first, so
            # the store reflects the consolidated salience/strength values.
            for record in replayed_records:
                self.repository.save_experience(
                    record, overwrite=True, operator="consolidate", reason=goal_id
                )
            promotion_candidates = self._promotion_candidates(replayed_records)
            promotions_applied = self._apply_promotions(promotion_candidates)
            decayed, dormant = self._apply_decay(now)
            self.repository.append_event(
                Event.create(
                    goal_id,
                    "memory.consolidated",
                    {
                        "replayed_attempts": replayed,
                        "experiences_new": new_records,
                        "decayed_records": decayed,
                        "dormant_records": dormant,
                        "promotions_applied": promotions_applied,
                    },
                )
            )
        else:
            promotion_candidates = self._promotion_candidates(replayed_records)
            dormant = self._count_dormant()

        return ConsolidationReport(
            goal_id=goal_id,
            replayed_attempts=replayed,
            experiences_considered=considered,
            experiences_new=new_records,
            decayed_records=decayed,
            dormant_records=dormant,
            promotion_candidates=tuple(promotion_candidates),
            promotions_applied=promotions_applied,
            self_model_suggestions=tuple(self_model_suggestions),
            applied=apply,
        )

    def _promotion_candidates(
        self,
        replayed: list[ExperienceRecord],
    ) -> list[dict[str, Any]]:
        """Lessons corroborated across >= min_goals and still strong may
        promote from episodic experience into semantic knowledge (异熟).

        Corroboration is evaluated against the whole experience store, not
        only the replayed goal: systems consolidation needs repeated episodes
        from distinct goals before a lesson becomes semantic knowledge.
        """
        policy = self.policy
        replayed_by_id = {record.experience_id: record for record in replayed}
        keys_of_interest = {
            (record.task_type, record.lessons[0][:120])
            for record in replayed
            if record.verdict.casefold() == Verdict.PASS.value.casefold()
            and record.strength >= policy.promotion_min_strength
            and record.salience >= policy.promotion_min_salience
        }
        if not keys_of_interest:
            return []

        groups: dict[tuple[str, str], dict[str, Any]] = {}
        seen: set[str] = set()
        sources = list(self.repository.list_experience()) + list(replayed)
        for record in sources:
            if record.experience_id in seen:
                continue
            seen.add(record.experience_id)
            if record.verdict.casefold() != Verdict.PASS.value.casefold():
                continue
            key = (record.task_type, record.lessons[0][:120])
            if key not in keys_of_interest:
                continue
            strength = replayed_by_id.get(record.experience_id, record).strength
            group = groups.setdefault(
                key,
                {
                    "goals": set(),
                    "agents": set(),
                    "max_strength": 0.0,
                    "lesson": record.lessons[0],
                    "task_type": record.task_type,
                },
            )
            group["goals"].add(record.goal_id)
            group["agents"].add(record.agent_id)
            group["max_strength"] = max(group["max_strength"], strength)

        existing_knowledge = self.repository.list_knowledge()
        candidates: list[dict[str, Any]] = []
        for key in sorted(groups):
            group = groups[key]
            if len(group["goals"]) < policy.promotion_min_goals:
                continue
            if group["max_strength"] < policy.promotion_min_strength:
                continue
            title = f"{group['task_type']} 成功模式"
            content = str(group["lesson"])
            if any(
                _overlap(f"{item.title} {item.content}", f"{title} {content}")
                >= policy.promotion_overlap_limit
                for item in existing_knowledge
            ):
                continue
            candidates.append(
                {
                    "title": title,
                    "content": content,
                    "tags": sorted(
                        {group["task_type"], "pattern:success", "source:consolidation"}
                    ),
                    "corroborated_goals": sorted(group["goals"]),
                    "agents": sorted(group["agents"]),
                    "max_strength": round(group["max_strength"], 6),
                }
            )
            if len(candidates) >= policy.promotion_max_items:
                break
        return candidates

    def _apply_promotions(self, candidates: list[dict[str, Any]]) -> int:
        applied = 0
        for candidate in candidates:
            self.repository.save_knowledge(
                KnowledgeItem.create(
                    candidate["title"],
                    candidate["content"],
                    candidate["tags"],
                ),
                operator="promotion",
                reason=",".join(candidate["corroborated_goals"]),
            )
            applied += 1
        return applied

    def _apply_decay(self, now: str) -> tuple[int, int]:
        """Decay every long-dormant seed; return (decayed, dormant)."""
        policy = self.policy
        decayed = 0
        dormant = 0
        for record in self.repository.list_experience():
            anchor = record.last_activated_at or record.created_at
            try:
                elapsed = (parse_utc(now) - parse_utc(anchor)).total_seconds()
            except ValueError:
                continue
            if elapsed <= policy.decay_horizon_seconds:
                continue
            updated = record.decay(
                factor=decay_factor(elapsed, policy.strength_half_life_seconds)
            )
            if updated.strength < EXPERIENCE_DORMANT_THRESHOLD:
                dormant += 1
            if updated.strength != record.strength:
                self.repository.save_experience(
                    updated, overwrite=True, operator="decay"
                )
                decayed += 1
        return decayed, dormant

    def _count_dormant(self) -> int:
        return sum(
            1
            for record in self.repository.list_experience()
            if record.strength < EXPERIENCE_DORMANT_THRESHOLD
        )

    def _self_model_suggestions(
        self, replayed: list[ExperienceRecord]
    ) -> list[dict[str, Any]]:
        """Advisory aggregates for the agent genome self-model (never written)."""
        passes = sorted(
            (record for record in replayed if record.verdict.casefold() == "pass"),
            key=lambda record: (-record.strength, record.experience_id),
        )
        failures = sorted(
            (record for record in replayed if record.verdict.casefold() == "fail"),
            key=lambda record: (-record.strength, record.experience_id),
        )
        return [
            {
                "kind": "success_signal",
                "agent_id": record.agent_id,
                "task_type": record.task_type,
                "lesson": record.lessons[0],
                "strength": round(record.strength, 6),
            }
            for record in passes[:3]
        ] + [
            {
                "kind": "failure_mode",
                "agent_id": record.agent_id,
                "task_type": record.task_type,
                "lesson": record.lessons[0],
                "strength": round(record.strength, 6),
            }
            for record in failures[:3]
        ]

    @staticmethod
    def _artifact_for_attempt(
        artifacts: list[Artifact],
        attempts: Mapping[int, Any],
        review: Review,
    ) -> Artifact | None:
        attempt = attempts.get(review.attempt_no)
        artifact_id = getattr(attempt, "artifact_id", None)
        if artifact_id:
            for artifact in artifacts:
                if artifact.artifact_id == artifact_id:
                    return artifact
        return artifacts[-1] if artifacts else None

    @staticmethod
    def _agent_for_attempt(
        artifact: Artifact | None,
        attempts: Mapping[int, Any],
        review: Review,
    ) -> str:
        attempt = attempts.get(review.attempt_no)
        agent_id = str(getattr(attempt, "agent_id", "") or "").strip()
        if agent_id:
            return agent_id
        return artifact.agent_id if artifact is not None else ""
