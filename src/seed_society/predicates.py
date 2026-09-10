"""Pure seed predicates: duplicate screening, secret screening, staleness.

All functions here are deterministic and side-effect free so they can be
unit-tested directly and reused by any writer (consolidation, bridge, CLI)
without pulling in storage.

Two design rules are deliberately non-configurable:

- the secret screen cannot be turned off or narrowed by configuration, because
  a user typo in a pattern list must never be able to disable screening;
- the near-duplicate block threshold is a hard block, not a warning, because a
  near-duplicate seed adds no information and should be an update instead.

Thresholds mirror the reviewed `dsh-continual-evolve` policy (0.8 block /
0.5 warn, Jaccard over token sets) re-specified for this project's seed shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

# Similarity thresholds for one candidate seed against the existing corpus.
NEAR_DUPLICATE_SCORE = 0.8
SIMILAR_WARN_SCORE = 0.5

# A seed that has never been retrieved and is older than this is stale.
STALE_MIN_AGE_SECONDS = 30 * 86_400.0

# Secret patterns are intentionally not configurable.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),                      # OpenAI-style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),               # GitHub tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),             # Slack tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                         # AWS access key
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                    # Google API key
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),           # PEM blocks
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."), # JWT
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*\S{8,}"),
)

_RETRIEVAL_MARKERS = (
    "injected",
    "retrieved",
    "injection_count",
    "activations",
)


@dataclass(frozen=True, slots=True)
class DuplicateVerdict:
    """Outcome of screening one candidate against the existing corpus."""

    score: float
    blocked: bool
    warned: bool
    matched_title: str = ""

    @property
    def accepted(self) -> bool:
        return not self.blocked


def tokens(text: str) -> set[str]:
    """Tokenize for similarity.

    CJK has no word spacing, so a character-level pass is included alongside
    the ASCII word pass; without it Chinese seed text scores as disjoint.
    """
    folded = str(text).casefold()
    words = set(re.findall(r"[a-z0-9_]+", folded))
    cjk = {ch for ch in folded if "\u4e00" <= ch <= "\u9fff"}
    return words | cjk


def similarity(left: str, right: str) -> float:
    """Jaccard similarity over token sets, in [0, 1]."""
    left_tokens = tokens(left)
    right_tokens = tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def screen_duplicate(
    candidate_title: str,
    candidate_content: str,
    existing: Iterable[tuple[str, str]],
) -> DuplicateVerdict:
    """Screen a candidate seed against existing (title, content) pairs.

    Returns the best-scoring match. Callers decide what to do with a warn; a
    block must not be written.
    """
    candidate = f"{candidate_title} {candidate_content}"
    best = DuplicateVerdict(score=0.0, blocked=False, warned=False)
    for title, content in existing:
        score = similarity(candidate, f"{title} {content}")
        if score > best.score:
            best = DuplicateVerdict(
                score=score,
                blocked=score >= NEAR_DUPLICATE_SCORE,
                warned=score >= SIMILAR_WARN_SCORE,
                matched_title=title,
            )
    return best


def secret_reason(text: str) -> str:
    """Return why the text looks like it carries a credential, or "".

    Not configurable by design: a typo in a user pattern list must never be
    able to switch credential screening off.
    """
    candidate = str(text)
    for pattern in _SECRET_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return f"matches credential pattern ({match.group(0)[:8]}…)"
    return ""


def is_stale(
    *,
    age_seconds: float,
    retrieval_count: int,
    stale_min_age_seconds: float = STALE_MIN_AGE_SECONDS,
) -> bool:
    """Two-signal staleness: old AND never retrieved.

    Age alone is not evidence of uselessness — a seed that keeps getting
    injected is earning its place regardless of how old it is. A seed is only
    stale when it is both long-lived and never used.
    """
    if retrieval_count > 0:
        return False
    return age_seconds > stale_min_age_seconds


def retrieval_count(record: object) -> int:
    """Best-effort retrieval count for a seed-like record.

    Experience seeds track `activations`; knowledge seeds currently track
    nothing, so they read as never retrieved and are only eligible for
    staleness once old. Reading is tolerant on purpose: a missing attribute
    means "no evidence of use", not an error.
    """
    for name in _RETRIEVAL_MARKERS:
        value = getattr(record, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def describe_stale(
    *,
    age_seconds: float,
    retrieval_count: int,
    stale_min_age_seconds: float = STALE_MIN_AGE_SECONDS,
) -> str:
    days = age_seconds / 86_400.0
    return (
        f"never retrieved in {days:.1f} days "
        f"(threshold {stale_min_age_seconds / 86_400.0:.0f} days)"
    )


def screen_all(
    candidates: Sequence[tuple[str, str]],
    existing: Sequence[tuple[str, str]],
) -> list[DuplicateVerdict]:
    """Screen several candidates against the corpus and against each other."""
    seen = list(existing)
    verdicts: list[DuplicateVerdict] = []
    for title, content in candidates:
        verdict = screen_duplicate(title, content, seen)
        verdicts.append(verdict)
        seen.append((title, content))
    return verdicts
