# Two DSH plugin audits — source-level review

Method: both repos shallow-cloned and read directly (no install, no build, no modification).

| | Plugin A | Plugin B |
|---|---|---|
| Package | `dsh-akn-plugin` (`@aen/dsh-plugin` 0.0.1, **private: true**) | `dsh-continual-evolve` 0.6.0 |
| Commit | `1fef6c849b89d9c4e499055cb4c26713c956fe76` (2026-08-22) | `d38dea080b54fa53cdc6c136df74791394394466` (2026-09-05) |
| License | Apache-2.0 | MIT |
| Size | ~100 source files, 17-package pnpm monorepo | 46 source files, single package |

Line references are `path:line` against the cloned trees. Anything I could not
confirm is marked **unverified**.

---

# Plugin A — `dsh-akn-plugin` (AEN)

## A1. Where does experience come from?

**No model call anywhere in production code.** This is the single most important
finding. A repo-wide grep for `dsh-llm|ctx.llm|openai|anthropic|apiKey` over
`packages/` returns exactly **one** hit, and it is a test:

```
packages/dsh-plugin/test/dsh-real-composition.test.ts:11: import { CallId } from '@deepseek-ai/dsh-llm'
```

No shipped package depends on an LLM package (see A8). The evidence chain is:

1. **Capture is event-driven from DSH's own live registry**, not from an agent.
   `packages/dsh-plugin/src/coordinator.ts` (404 lines) and
   `packages/dsh-plugin/src/snapshot.ts` (373 lines) observe Model × Harness ×
   Environment surface changes and write snapshots. The README states the
   capture boundary: first `request/header`, subsequent effective-header
   changes, and skill/tool/system-prompt/model-route/policy registry changes
   (`packages/dsh-plugin/README.md:119`).
2. **Analysis is pure deterministic code over normalized events.**
   `packages/adapter-dsh/src/analyze.ts:1-60` imports only `canonicalJson`,
   `sha256`, and protocol types; it emits fingerprints, artifact descriptors,
   `skillCatalogDigest`, `systemPromptDigest`, and a `coverage` object. No model.
3. **Distillation is human/CLI authoring, not generation.** The
   `ExperienceRevision` is a hand-authored protocol object
   (`conformance/valid/experience-revision.json`, and the 3299-line
   `schemas/aexp/0.1/experience_revision.schema.json`).
4. **`experience_search` / `experience_feedback`** are the only model-facing
   tools, and they only *read* and record low-trust feedback
   (`packages/dsh-plugin/src/consumer.ts:32-70`). Critically, `experience_feedback`
   "只记录 viewed/rejected/rolled_back 等本地低信任反馈，不改变 H-level"
   (`packages/dsh-plugin/README.md:126`) — feedback cannot raise an evidence grade.

**Conclusion: 100% deterministic code + human authoring. Zero model invocations.**

## A2. Storage format and location

**SQLite** via Node 22's built-in `node:sqlite` — no native addon:

- `packages/local-store/src/index.ts:3: import { DatabaseSync } from 'node:sqlite'`
- Tables created at `packages/local-store/src/index.ts:283-375`: `objects`,
  `object_links`, `sessions`, `session_objects`, `experience_reviews`,
  `experience_review_events`, `local_deletion_tombstones`.
- Default path: `.aen/evidence.sqlite`, **relative to the project cwd**, not the
  DSH home (`packages/dsh-plugin/src/provider-plugin.ts:24`).

**Human-readable? Partly.** It is a normal SQLite DB, so it is inspectable and
editable with any SQLite client and the rows hold canonical JSON. But:

- Object identity is **content-addressed**: canonicalization is RFC 8785 JCS and
  digest is SHA-256 (`docs/capabilities/aen-mvp-0.1.json:78-85`). Editing a row
  changes its digest and breaks referential integrity.
- Public promotion carries an **in-toto Statement v1 + DSSE Ed25519** attestation
  (`docs/capabilities/aen-mvp-0.1.json:81-84`), verified in
  `packages/protocol/src/attestation.ts` (214 lines). Hand-editing signed
  content fails verification — the `conformance/invalid/` directory contains 18
  fixtures that MUST fail, including `attestation-signature-tampered.json` and
  `*-digest-mismatch.json`.

So: hand-editable in the mechanical sense, but hand edits are **rejected at
every layer** — and this is enforced on *read* as well as write, which my first
draft wrongly left open:

- `packages/protocol/src/validation.ts:251-262` pushes a `digest.mismatch` issue
  when `computeObjectDigest(value) !== value.digest`. The digest covers canonical
  JSON excluding `digest`/`attestation`/`attestations`/`signatures`
  (`protocol/src/digest.ts:7-12,45-47`).
- Enforced on **write**: `local-store/src/index.ts:436-443` calls
  `assertProtocolObject` (throws `cannot store invalid AEXP object`) and further
  throws `digest collision or non-canonical object mismatch` if the stored bytes
  differ from canonical form.
- 20 `conformance/invalid/*-digest-mismatch.json` fixtures exercise this path.

Hand-editing a body is therefore rejected at import, and a byte-level edit to
`canonical_json` inside the DB fires `digest.mismatch` on the next validation.

Also: bodies are **opaque canonical-JSON blobs in `objects.canonical_json`**
(`local-store/src/index.ts:283-291`) — the SQL columns are only an index over
them, so an experience's real schema is the JSON schema under
`schemas/aexp/0.1/`, not a SQL table. `SCHEMA_VERSION = 3` (`:13`);
`journal_mode=WAL`, `secure_delete=ON`, `foreign_keys=ON` on open (`:251-259`);
FTS5 search index (`:346-354`).

**External service? No for the local path.** `@aen/local-store` depends only on
`@aen/protocol` — no `pg`, no HTTP. The hub (Postgres + a filesystem registry,
`packages/hub/src/postgres.ts` 762 lines, `packages/hub/src/git.ts`) is an
optional separate app, gated off by default: `allowHubSearch: false` and
`hubUrl` unset means fully offline (`packages/dsh-plugin/README.md:121`).

## A3. The promotion gate — this is real and the strongest part of the plugin

`packages/promotion/src/promote.ts:500-536` — `promoteExperience()` throws
unless **all** of these hold:

```
:506  source object must be an experience_revision
:510  source.governance.visibility must be 'private'
:511  store.getExperienceReview(source.digest)?.state must be 'public_requested'
:512    → 'Promotion requires an explicit public_requested review decision'
:514  options.license must be non-empty
:515  options.consentRef must be non-empty
:516  options.policyDecisionRef must be non-empty
:535  transcriptRefs must be promoted explicitly first
```

The `public_requested` state is set by a **human CLI review step**, not by code
inference: `apps/cli/src/main.ts:402` defines a `review` command with
`--reviewer <actor-id>` (default `urn:aen:actor:local-reviewer`), `--decision`,
and `--note <text>` (an audit note). `:453` calls `reviewExperience(...)`.

The promotion path also requires **closure**: `:528-552` recursively resolves
`evaluated_on` → evaluation aggregates → trials → benchmark tasks → grader
definitions → observations → trace evidence → task episodes → evidence gap
reports. Missing links throw. This is genuine content-addressed graph closure,
not a naming gesture.

**H0–H4 are defined in the spec** (`spec/AEXP-0.1.md:225-229`):

| Level | Minimum meaning |
|---|---|
| H0 | unverified hypothesis or authored guidance |
| H1 | one or more trace-linked observations without sufficient configuration proof |
| H2 | observations bound to an adequate Model/Harness/Environment snapshot |
| H3 | preregistered comparable baseline/treatment evidence with uncertainty and failure classification |
| H4 | independent replication across operators or compatible implementations |

`spec/AEXP-0.1.md:231`: "Synthetic fixtures, mock models, signatures, downloads,
likes, and author reputation MUST NOT independently raise an evidence level."

An H3-eligible evaluation must freeze task/fixture/model/config/environment/
grader/metric/repeats/stopping-rule/budget before execution, include a
no-Experience baseline, and record every trial status including aborted ones
(`spec/AEXP-0.1.md:253-261`). `--min-evidence H0|H1|H2|H3|H4` is a real CLI
filter (`apps/cli/src/main.ts:728,755-758`).

**But only four levels are actually implemented — H4 is hard-rejected.**

- `packages/hub/src/ingest.ts:175-177`: `if (claim.evidenceLevel === 'H4') throw
  new Error('public H4 claims require the post-MVP independent-replication policy')`
- `:172-173`: H3 requires `claim.mode === 'causal'`, else throws.
- `:182-186`: reaching H3 requires an `evaluated_on` relation pointing at an
  `evaluation_aggregate`, else throws.
- Only **two code sites assign a grade**: `workbench/src/distill.ts:56-62`
  (H0; H2 iff a complete live manifest, else H1) and
  `evaluation/src/evidence.ts:4-30` (H2 or H3, with `reasonCodes`).
- Corroborated by `"H4 replication network"` sitting under `unsupported` in
  `docs/capabilities/aen-mvp-0.1.json:95`.

**The "statistical gate" is CI-excludes-zero, not a p-value.** There is no
alpha, no p-value, no effect size, and no power calculation anywhere in the
repo. Significance is `evaluation/src/aggregate.ts:262-267`: `lower > 0` →
`improved`, `upper < 0` → `harmed`, else `no_significant_difference`;
`inconclusive` when ineligible or the interval is undefined. Intervals are
Wilson / Newcombe-difference / Welch-style (`statistics.ts:42-59,104-127`).
Eligibility (`aggregate.ts:153-167,195,261`) requires the baseline cell to
actually be baseline, `validTrials >= minimum` on both cells, no blocking
confounders, exactly one benchmark digest, benchmark
`validity.status === 'validated'`, and not `synthetic_test`. **No minimum-uplift
threshold exists.** Pilot gates are hard-coded to 2 models × 2 harness configs ×
2 task families = 8 cells with ≥3 participants
(`evaluation/src/pilot.ts:191,222,235-236,262,271`), and there are **no
production defaults** for `repetitions`/`confidenceLevel`/`minValidTrialsPerCell`
— the repo values (20/0.95/20) are test fixtures
(`evaluation/test/evaluation.test.ts:344-347`).

One more machine-checked gate I initially missed:
`packages/hub/src/ingest.ts:355` rejects any public Experience whose
`governance.redactionReport.humanReviewed !== true` — "public Experience was not
human-reviewed". Note the `humanReviewed: true` flags at
`promote.ts:366-370,447,466-469` are **written by the promotion code itself**, so
they are assertions the ingress checks for presence, not independent proof a
human looked.

**However — and this matters — the project's own capability file says the
product-level evidence does not exist yet:**

- `docs/capabilities/aen-mvp-0.1.json:41`: `"realLive2x2x2Result": "not-run"`
- `:40`: `"officialDshHeadlessMechanismRun": "implemented-with-mock-model"`
- `:86`: `"supportedRequiredCapabilities": []`
- `:43-49`: open pilot gates list "real 2 Model x 2 stable Harness configuration x
  2 task-family evaluation", "publicly reachable Reference Hub", "three
  independent developers and cross-user adoption transcript", and
  "independent security review" — all open.
- `packages/dsh-plugin/README.md:150`: the acceptance test "只证明执行、fixture
  隔离、Manifest 关联、grading 和证据机制，不证明真实 DeepSeek 模型能力、H3 uplift
  或 2×2×2 Pilot."
- `apps/cli/src/main.ts:1020`: "Synthetic test mode cannot satisfy M3 real-pilot
  DoD or support H3."

The machinery for H3 is built; **no H3 evidence has been produced**.

## A4. Rollback / versioning — **versioning yes, rollback NO**

**Corrected after a deeper verification pass. My first draft credited A with
rollback; it does not have one.**

- **Versioning is real but forward-only**: a monotonic integer (`revision + 1`)
  on a re-signed JSON clone (`packages/promotion/src/promote.ts:400-406`,
  `packages/workbench/src/review.ts:160,181`). Not a file copy, not a git
  commit, not a snapshot. "Current" = max revision
  (`packages/local-store/src/index.ts:674`, hub variant
  `packages/hub/src/postgres.ts:181-187`).
- **Content addressing** over every object: RFC 8785 JCS + SHA-256, with
  `supersedes` links (`packages/promotion/src/promote.ts:518-526`).
- **No git.** `packages/hub/src/git.ts` imports only `node:fs/promises` and
  `node:path` — zero subprocess calls. A repo-wide grep for git commands found
  none; every `rollback` hit outside docs is SQL `ROLLBACK` (transaction abort,
  e.g. `local-store/src/index.ts:268`). ADR-0016's Limits section states the
  position: "Git history and independent clones are copyable... cannot recall
  prior copies."
- **Revocation** is a first-class protocol object with its own schema and
  conformance fixtures (`packages/promotion/src/revoke.ts:32-48`), but it only
  purges the Hub's *derived* projection (`packages/hub/src/postgres.ts:293`
  `DELETE FROM hub_objects`) plus a tombstone/index row. **The local SQLite
  store is not touched by revocation at all.**
- `spec/AEXP-0.1.md:217` is candid: "Git history, independent clones, backups,
  and already injected context cannot be globally recalled."

**Consequence for comparison: on rollback, Plugin B is the stronger of the two,
not A.**

## A5. Deduplication / conflict handling — real and explicit

- A dedicated `contention` protocol object (437-line schema) plus a valid and an
  invalid conformance fixture.
- `packages/protocol/src/mvp-ranking.ts` (144 lines) implements ranking.
- Store-level dedup by cell: the README (`:119`) describes dedup "按
  `Model × Harness configuration × Environment` cell 去重写库", with
  `HarnessManifest.configurationDigest` covering only the Harness surface so the
  Model/Environment axes stay independent.
- `uniqueByDigest()` at `packages/promotion/src/promote.ts:496-498` dedupes
  closure sources.
- ADR-0005 `docs/adr/0005-resolvable-claim-and-contention-references.md` covers
  resolvable claim/contention references.

## A6. `package.json` `dsh` field, deps, external services

`packages/dsh-plugin/package.json:63-67` — declares **`dsh.bundle` only**:

```json
"dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
```

**No `dsh.client`.** Consistent with there being no UI.

Runtime dependencies (`:75-82`): `@deepseek-ai/schemastery`, `@sinclair/typebox`,
`ajv`, `ajv-formats`, `canonicalize`, `fflate`. Note: `@deepseek-ai/schemastery`
is a real runtime dependency, while `@deepseek-ai/cordis` and
`@deepseek-ai/dsh-tools` are **optional peerDependencies** (`:83-94`).

External service: none for local use. Optional Postgres + git hub for the
networked path (`packages/hub/src/postgres.ts`).

**Note: `"private": true` at `packages/dsh-plugin/package.json:4`** — the package
as cloned cannot be published to npm. Install is via git
(`dsh plugin --profile web add github:symmetryseeker/dsh-akn-plugin`,
`packages/dsh-plugin/README.md:14`).

## A7. Config flags and defaults

Three roles, each with its own schema.

**`aen-policy`** (`packages/dsh-plugin/src/policy-plugin.ts:28-32`):

| Flag | Default |
|---|---|
| `captureSkillContent` | `true` |
| `captureSkillResources` | **`false`** |
| `allowHubSearch` | **`false`** |
| `publicPublishing` | hard-coded `'disabled'` (`:18,39`) |

**`aen` / provider** (`packages/dsh-plugin/src/provider-plugin.ts:22-27`):

| Flag | Default |
|---|---|
| `enabled` | `true` |
| `storePath` | `.aen/evidence.sqlite` |
| `harnessVersion` | `'unknown'` |
| `snapshotDelayMs` | `25` |

**`aen-tools`**: `disabled: true` in the shipped patch
(`cordis.patch.yml:17-20`).

**Writes by default: yes** — the provider is enabled and `captureSkillContent`
defaults true, so manifests and skill content are written to
`.aen/evidence.sqlite` on a fresh install. `captureSkillResources` (full skill
directory traversal) is explicitly opt-in. **LLM calls by default: none —
there is no LLM code path at all.**

Note the shipped patch pins `harnessVersion: 0.1.0-rc.7` and
`captureSkillResources: false` (`cordis.patch.yml:5-15`) — while
`captureSkillContent: true`. And it registers a tools row with
`disabled: true` (`cordis.patch.yml:17-20`), so the two model tools are off out
of the box.

## A8. Official `@deepseek-ai/*` declarations

Runtime deps: only `@deepseek-ai/schemastery` (`^3.18.1`).
Optional peerDependencies: `@deepseek-ai/cordis` (`^4.0.1`) and
`@deepseek-ai/dsh-tools` (`0.1.0-rc.7`).
devDependencies include `dsh-agent`, `dsh-llm`, `dsh-session`, `dsh-skill`,
`dsh-system-prompt`, `dsh-tools` — all at `0.1.0-rc.7`
(`packages/dsh-plugin/package.json:95-114`).

**`dsh-llm` appears only in devDependencies + one test import. The plugin cannot
call a model.**

## A9. Verdict — substantial engineering or naming?

**Substantial, and unusually honest about its own limits.** This is a real
protocol implementation, not a wrapper with good vocabulary. Concretely
substantial: 21 JSON Schemas totalling well over 10,000 lines; RFC 8785
canonicalization + SHA-256 content addressing verified on both read and write;
DSSE/Ed25519 attestation with 18 negative conformance fixtures **and real
signature verification at hub ingress** (`packages/hub/src/ingest.ts:116-136`
`verifySignedObject` → `verifyAttestation`, throwing
`no authorized signature verified` when nothing verifies; called for the target
`:360-366`, every observation `:367-379`, revocations `:388-394`, standalone
observations `:409`); a `node:sqlite` store; a promotion gate with 7 hard
preconditions plus full closure resolution; benchmark slicing, statistical
aggregation, and a preregistration validator. ADR-0015 is the kind of
self-restricting decision a naming exercise does not produce.

Two things keep this honest rather than impressive: its own capability file marks
real-world H3 evidence **not-run** (`aen-mvp-0.1.json:41`), and there is **no
rollback mechanism at all** — versioning is a forward-only integer over
re-signed clones. The "H0–H4" scale is really H0–H3 with H4 refused in code.

The genuine cost is weight: 17 packages, Postgres/hub infrastructure, and a
large protocol surface for a system whose validated result is currently
"mechanism works with a mock model."

**Reusable by an LLM-free project with its own consolidation engine: a lot, and
this is the better of the two for that purpose.** Because it *never* calls a
model, there is nothing to strip out. Directly liftable:
(a) the content-addressing recipe — RFC 8785 JCS + SHA-256 + `digest`/
`supersedes` links — plus the 18 invalid-fixture conformance suite;
(b) the promotion precondition ladder at `promote.ts:506-516`;
(c) the evidence-grade table and its anti-gaming rule
(`spec/AEXP-0.1.md:225-231`) — "signatures, downloads, likes, and author
reputation MUST NOT independently raise an evidence level" is a rule worth
copying verbatim;
(d) the ingress signature-verification helper `ingest.ts:116-136`, which is a
clean, reusable pattern (actor-scoped key allowlist + validity window +
throw-on-zero-verified);
(e) the closure-resolution walk at `promote.ts:528-552`;
(f) the CI-excludes-zero eligibility rule (`aggregate.ts:153-167,262-267`) if a
statistical gate is ever wanted. The protocol surface is far larger than a small
project needs, so I would take the invariants and the fixtures, not the 17
packages.

---

# Plugin B — `dsh-continual-evolve`

## B1. Where do lessons come from?

Mixed, and the division of labour is explicit and consistently implemented.
**Four LLM call sites exist in the entire source tree**, all funnelling through
one helper:

`src/llm-text.ts:32-68` — `streamText()` wraps `ctx.llm.stream(...)` with
`reasoningEffort: off`, a default 8000-token budget, and hard failures on
error/abort/max-tokens/empty output. It has **no deterministic fallback** — it
throws.

The four callers (grep for `streamText(`):

| Site | Purpose | Trigger |
|---|---|---|
| `src/planner.ts:142` | `/evolve plan` — propose edits | explicit command |
| `src/review.ts:145` | auto review gate | turn interval / compaction |
| `src/wrapup.ts:677` | session wrap-up assessor | `/evolve wrapup` or gate |
| `src/fate.ts` (`:447` builds the message) | local-fate assessment | gate cadence |

Everything else is deterministic. `src/consolidate.ts:14` is explicit: "no LLM
call anywhere: code proposes, the human disposes." `src/rollback.ts:3-4`:
"Deterministic rollback... no LLM is asked to 'guess' the previous state."
`src/score.ts:3-5`: "The model only produces per-cell raw scores; every average
and every accept/reject call happens here, in deterministic code."

The architecture is stated in `src/wrapup.ts:16-19`: "the mechanical audit
proposes, the LLM classifies, the user approves, the code applies
deterministically."

**Is there a background subagent?** No. The gate runs in-process on DSH event
hooks, not as a separate agent:
- `src/auto.ts:143` — `ctx.on("agent/turn-stopping", ...)` counts turns
- `src/auto.ts:153-176` — `ctx.on("agent/status", ...)` fires the gate on idle
  once `state.turns - state.lastReviewAt >= config.intervalTurns`
- `src/auto.ts:199-216` — `ctx.on("session/event", ...)` fires on
  `compaction/start`, unconditionally ("Compaction is unconditional: persist what
  is about to be summarized away", `:205`)

Hooks are only registered when `autoReview: true` (`src/index.ts:179`).

**Per-turn LLM cost? No — but per-*interval* yes.** The default interval is 6
turns (`src/index.ts:40`). With `autoReview` **off by default**
(`src/index.ts:38`, and `cordis.patch.yml:5-6`), a default install makes **zero
LLM calls**. Turning it on buys one gate call every 6 turns plus local-fate
assessments on their own cadence.

**Pure-code extraction** includes: `src/consolidate.ts` (conflict/staleness
scanning), `src/failures.ts` (`classifyFailure` at `:40`, regex-based),
`src/usage.ts` (injection counting), `src/score.ts` (aggregation/decision),
`src/search.ts` (BM25), `src/rollback.ts`, `src/validate.ts`, `src/apply.ts`.

## B2. Storage format and location

**Plain JSON + JSONL, fully human-readable.** Layout is documented in code at
`src/store.ts:5-9` and computed at `src/store.ts:31-39`:

```
<baseDir>/evolve/global/harness_state.json     cross-session store
<baseDir>/evolve/global/refinements.jsonl      applied results (rollback source)
<baseDir>/evolve/local/<sessionId>/harness_state.json
<baseDir>/evolve/local/<sessionId>/refinements.jsonl
<baseDir>/evolve/local/<sessionId>/snapshots/<refinementId>.json
<baseDir>/evolve/reviews.jsonl                 audit trail (src/auto.ts:129)
<baseDir>/evolve/plugin.log                    JSONL log (src/logfile.ts:23,35)
<baseDir>/evolve/rubric.key                    encryption key file
```

- Root: `resolveDshHome(config.baseDir)` at `src/index.ts:115`, from
  `@deepseek-ai/dsh-home-paths:13`. Overridable via `baseDir`.
- State written with `JSON.stringify(state, null, 2)` +
  trailing newline (`src/state.ts:135`) — pretty-printed, diffable.
- Skills materialize to `<dshHome>/skills/<id>/SKILL.md`
  (`src/index.ts:116`, `src/skill.ts:19,37`).

**Hand-edit safe by design.** `src/state.ts:1-13` states the safety properties
explicitly, and crucially there is **no digest that rejects hand edits** — the
opposite: `loadHarnessState` *normalizes* every field
(`src/state.ts:41-101`), defaulting missing/wrong-typed fields rather than
throwing. `:49-51` returns empty state on unparseable JSON. `:92-98` shape-checks
refinement members so a hand-edited file cannot crash the renderer. `:10-12`:
"every entry read from disk is shape-normalized... so a hand-edited file cannot
smuggle garbage into the system prompt renderer."

Writes are atomic: temp file + `renameSync`, preserving mode, default `0o600`
(`src/state.ts:129-143`). Snapshots also `0o600` (`src/store.ts:50`).

This is a materially friendlier storage design than Plugin A's for hand editing
— you can edit `harness_state.json` in a text editor and the system will accept
and normalize it.

## B3. The promotion gate — real, multi-layered, with human approval

Ordered checks before something becomes active:

**Layer 1 — validation (`src/validate.ts:44-137`)**, per edit: action/kind enums
(`:45-50`), base system prompt immutable (`:51-53`), id required for non-create
(`:54-56`), blast-radius/scope coherence (`:59-62`, via
`validateBlastRadiusScope:23-34`), create needs title+content (`:74`), update
must carry a change (`:77-87`), skill contract rules (`:88-135`).

**Layer 2 — promotion policy (`src/promotion.ts`)**, applied to global writes:
- `secretLeakReason()` `:100-108` — 11 secret regexes (`:61-80`), deliberately
  **not configurable**: "a user pattern typo must never be able to disable
  secret screening" (`:54-60`).
- `projectScopedReason()` `:141-148` — content matching POSIX paths, `session-`
  ids, or `~/.dsh` stays local (`DEFAULT_BLOCK_PATTERNS:22-26`).
- `promotionMinChars` — thin promotions stay local (default 100).
- `mostSimilarGlobalEntry()` `:221-229` — near-duplicate detection.

**Layer 3 — human approval.** Two separate gates:
- `src/approval.ts:31-57` — `requireGlobalApproval()`. Every global write asks
  the user via `userQuestions.ask()` with options `批准` / `拒绝` (`:44-47`); a
  missing service **throws** (`:38-40`). "Forward edits to the shared global
  store require an explicit human 批准... rollbacks do not" (`:2-5`).
- `src/fate.ts:153-191` — `consultLocalFates()`. One dialog covering all
  governed actions ("the gate never spams questions", `:146-148`), with a
  10-turn cooldown after a decline (`FATE_CONSULT_COOLDOWN_TURNS:55`, checked
  `:165`). Conservative on every edge: no service → not approved; error → not
  approved (`:169-170,188-189`).

**Layer 4 — benchmark acceptance (optional).** `src/score.ts:21-25`:

```
DEFAULT_AGGREGATE = { passThreshold: 60, regressionTolerance: 0, maxFailedCells: 0 }
```

`maxFailedCells: 0` means any failed cell rejects the round outright, and failed
cells are never averaged in as zeros (`:38-42`). Rubrics are AES-256-GCM
encrypted at rest (`src/rubric.ts`).

**No evidence-grade / H0-H4 system.** The closest analogue is the numeric
`passThreshold` and `conflictHint` scores. Do not expect AEN-style grading here.

**Answer: yes to human approval (real and load-bearing), yes to scoring
thresholds, no to evidence grades.**

## B4. Rollback / versioning — real, and notably honest

- **Snapshots, enforced in code**: `src/store.ts:41-51` —
  `snapshotBefore()` copies `harness_state.json` to
  `snapshots/<refinementId>.json` before any mutation. `src/store.ts:11-13`:
  "The model has no way to skip it — it runs inside the service, not in a
  prompt."
- **Version counter** per entry: `version: before ? before.version + 1 : 1`
  (`src/apply.ts:157`).
- **Inverse-edit rollback**: `src/rollback.ts:9-24` rebuilds the inverse edit
  list in reverse order. Three cases at `:26-73`: update→restore `before`,
  delete→re-create from snapshot, create→delete. `rollback.ts:3-4`: "pure data
  transformation — no LLM is asked to 'guess' the previous state."
- **Full history artifact**: every edit records `before`/`after` snapshots
  (`src/apply.ts:161-167`), appended to `refinements.jsonl` (`src/store.ts:54-57`).
- **Audit trail**: `reviews.jsonl` records every gate outcome including failures
  and a boot "armed" marker (`src/auto.ts:131-138,181-197`).
- **Auto-rollback**: `autoRollbackOnReject` defaults `true`
  (`src/index.ts:60`).
- **Archives are reversible**: archive stamps `metadata.archivedAt` rather than
  deleting, and gets its own before/after + version bump + rollback inverse
  (`src/apply.ts:94-119`).

**Not git.** Snapshot files + a version counter + JSONL. Real, but
file-copy-based, not a VCS.

## B5. Deduplication / conflict handling — real, with calibrated thresholds

Three tiers, all deterministic:

**Write-time guard** (`src/promotion.ts:43-52`):
- `CONFLICT_BLOCK_SCORE = 0.8` — a global create reaching this similarity is
  **rejected outright** ("a near-duplicate adds zero information and the model
  should evolve_update the existing entry instead").
- `CONFLICT_WARN_SCORE = 0.5` — stamps `conflictHint` metadata but lets the
  write proceed.

Similarity is Jaccard over token sets (`contentOverlap:164-177`), using a
tokenizer that handles CJK via character bigrams (`:150-161`). The comment at
`:154-157` is candid that "the calibrated 0.6/0.8 thresholds are judgment values,
not data-fitted."

**Consolidation** (`src/consolidate.ts`, `/evolve consolidate`):
- `findConflictPairs():76-95` reads `conflictHint` stamps whose target still
  exists and is active; the newer entry is the archive candidate.
- `findStaleEntries():102+` finds zero-injection entries older than
  `STALE_MIN_AGE_MS = 30 days` (`:21`).
- **Merge tier**: `mergeInto` (`:68`) folds near-duplicate content into the
  survivor before archiving, so nothing readable is lost (`:62-69`).
- Re-scans fresh state before applying; "prefer under-archiving over
  mis-archiving" (`:8-10`).

**Scope conflicts** (`src/state.ts:108-123`): local wins over same-id global and
the colliding local id is prefixed `local:` so both stay addressable.

## B6. `package.json` `dsh` field, deps, external services

`package.json:35-39` — **`dsh.bundle` only**, no `dsh.client`:

```json
"dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
```

Runtime dependencies: **none** — there is no `dependencies` key at all
(`package.json:58-78` jumps from `peerDependencies` to `devDependencies`). This
is unusually clean and means the plugin adds no third-party runtime supply chain.

peerDependencies (`:58-63`): `@deepseek-ai/cordis ^4.0.1`,
`@deepseek-ai/dsh-home-paths`, `@deepseek-ai/dsh-llm`, `@deepseek-ai/dsh-tools`
(all `^0.1.0-rc.6`).

External service: **none**. Everything is local files. No daemon, no DB, no
network. Engines: node `^22.19.0 || >=24.0.0` (`:40-42`).

## B7. Config flags and defaults

`src/index.ts:32-101`. **LLM-triggering and write-by-default flags marked.**

| Flag | Default | Note |
|---|---|---|
| `baseDir` | resolved DSH home | store root |
| `sectionOrder` | `118` | prompt section order |
| `autoReview` | **`false`** | **⚡ LLM gate — OFF by default** |
| `reviewIntervalTurns` | `6` | gate cadence |
| `maxReviewInputChars` | `40000` | trajectory slice |
| `reviewBudgetTokens` | `4096` | gate output budget |
| `notifyOnAutoReview` | `true` | visible follow-up notice |
| `requireGlobalApproval` | **`true`** | **🔒 human approval for global writes** |
| `skillsDir` | `<dshHome>/skills` | skill materialization |
| `rubricKey` | key file / `DSH_EVOLVE_RUBRIC_KEY` | AES-256-GCM passphrase |
| `logToFile` | **`true`** | **✍️ writes `evolve/plugin.log`** |
| `logLevel` | `1` | 0=error 1=info 2=warn 3=debug |
| `logMaxBytes` | `5 MiB` | rotation |
| `autoRollbackOnReject` | **`true`** | deterministic rollback |
| `autoCase` | **`true`** | **✍️ writes draft regression cases** |
| `reviewModel` | agent's own | optional cheaper gate model |
| `localFate` | `true` | gate audits local entries (only if `autoReview`) |
| `fateIntervalTurns` | follows `reviewIntervalTurns` | |
| `goalBlockedWrapupTurns` | `3` | 0 disables |
| `promotionBlockPatterns` | POSIX paths, session ids, `~/.dsh` | |
| `promotionMinChars` | `100` | |
| `injectionDirectoryLines` | `15` | |

**Write-by-default: `logToFile: true` and `autoCase: true` both write on a
default install**, and the boot header appends an "armed" marker to
`reviews.jsonl` only when `autoReview` is on (`src/auto.ts:181`). Note
`localFate` defaults `true` but is inert without `autoReview`.

**LLM-by-default: nothing.** The shutoff is single and clean: `autoReview:
false`. Additionally `cordis.patch.yml:5-6` states the shipped bundle's intent —
"Conservative defaults: the auto-review gate is OFF (opt in via your profile)".

Also note `src/index.ts:30` declares `inject` including `"llm"` — the plugin
hard-requires the LLM service to load, even when `autoReview` is false.
**Unverified** whether DSH makes the `llm` service mandatory for every plugin
context; if not, a deployment without it would fail to mount.

## B8. Official `@deepseek-ai/*` declarations

**peerDependencies** (the important part, `package.json:58-63`):
`@deepseek-ai/cordis`, `@deepseek-ai/dsh-home-paths`, `@deepseek-ai/dsh-llm`,
`@deepseek-ai/dsh-tools`.

**No runtime `dependencies` at all.** devDependencies add `dsh-agent`,
`dsh-commands`, `dsh-system-prompt`, `schemastery` at `0.1.1-rc.2`.

So: declares official packages as **peerDependencies** (correct — the host
provides them), not as bundled dependencies.

## B9. Verdict — substantial engineering or naming?

**Substantial engineering, and the discipline is real rather than rhetorical.**
"573 tests" across 36 files is plausible given the surface, and the code reads
like it was written against its own failure record: comments cite specific audit
findings (`src/apply.ts:49-51` "Review audit 2026-08-28 S7", `src/store.ts:49`
"S5", `src/state.ts:93` "S6"), and `src/rollback.ts:29-31,47-49` documents a
bug where a deleted guidance skill was unrecoverable via rollback until
`skill_kind` was re-carried. The genuinely strong parts: code-enforced
snapshotting the model cannot bypass (`store.ts:11-13`); deterministic
inverse-edit rollback with no LLM; the non-configurable secret screen with the
rationale that a user typo must not disable it; failure cells never averaged as
zeros; the `approval.ts` fail-closed policy. The storage design — pretty-printed
JSON, hand-edit normalization, atomic writes — is careful and humane.

Where it is thinner than the marketing implies: versioning is file copies plus a
counter, not a VCS — you cannot branch, diff, or merge history, and snapshots
accumulate unbounded with no pruning I could find (**unverified** whether any
GC exists). Skill "hot-reload" is not something I confirmed: `src/skill.ts:37`
`syncSkillsFromResult` writes SKILL.md files, and `src/mount.ts:165-231`
mounts/unmounts plugin sources with `restoreMounted` on boot — **unverified**
whether a *changed* skill is picked up live without restart. The overlap
thresholds are self-described as "judgment values, not data-fitted". And the
benchmark loop, while well-built, only measures against cases the operator
writes.

**Reusable by an LLM-free project with its own consolidation engine: the
storage, safety and rollback layers specifically — but not the lesson extraction.**
Its core value proposition (trajectory → LLM classification → proposal) is
exactly the part such a project replaces. What transfers cleanly:

(a) **the whole snapshot/rollback discipline** — `store.ts:41-51`,
`rollback.ts` inverse-edit construction, `apply.ts` before/after accounting, and
the `version` counter. This is engine-agnostic and has no LLM dependency
whatsoever.
(b) **`state.ts` atomic-write + normalize-on-read pattern** (155 lines) — a
genuinely good, dependency-free template for hand-editable JSON state that
cannot crash a renderer.
(c) **the promotion policy predicates** (`promotion.ts`) — `secretLeakReason`,
`projectScopedReason`, Jaccard `contentOverlap`, and the 0.8/0.5 block/warn
tiering with `conflictHint` stamps. Pure functions, no I/O, trivially liftable.
(d) **`consolidate.ts` two-signal staleness** (conflictHint + zero-injection
age) and the merge-into-survivor tier.
(e) **the failure-cell protocol** (`score.ts:38-42`) and deterministic
aggregation/decision split.
(f) **the approval/cooldown pattern** (`approval.ts`, `fate.ts:153-191`) —
fail-closed human gating with decline cooldown.
(g) **the non-configurable security invariant** — `promotion.ts:54-60` is a
design principle worth adopting wholesale.

What does not transfer: `llm-text.ts`, `planner.ts`, `review.ts`, `wrapup.ts`
assessment, `fate.ts` assessment, and the gate wiring in `auto.ts` — roughly the
top half of the pipeline, since those exist to get a model to propose edits.

---

# Head-to-head on the questions that matter most

| Question | Plugin A (AEN) | Plugin B (continual-evolve) |
|---|---|---|
| Calls an LLM? | **Never** (prod) | 4 sites, all opt-in |
| LLM calls on default install | none | **none** (`autoReview: false`) |
| Writes on default install | yes (SQLite) | yes (log + JSON) |
| Storage | SQLite, content-addressed | JSON/JSONL, pretty-printed |
| Hand-editable | yes, but signed objects reject edits | **yes, normalized on read** |
| Human approval | CLI `review` → `public_requested` | in-session dialog (批准/拒绝) |
| Scoring threshold | H0–H3 implemented (H4 hard-rejected); CI-excludes-zero | `passThreshold: 60`, 0.8/0.5 similarity |
| Rollback | **none** — forward-only revision int + revocation | snapshots + inverse edits + version |
| Dedup | contention object + cell dedup | 0.8 block / 0.5 warn + merge tier |
| Evidence produced? | **H3 not-run; mock model only** | benchmark loop tested |
| Runtime deps | 6 (`ajv`, `canonicalize`, …) | **0** |
| `dsh.bundle` | yes | yes |
| `dsh.client` | no | no |
| Officially published | **no — `private: true`** | yes (npm, MIT) |

**For a project with its own deterministic consolidation engine that wants to
stay LLM-free by default: Plugin A is the better architectural match — it is
already LLM-free, so nothing needs removing — but Plugin B contains more
directly liftable, dependency-free mechanics (state.ts, rollback.ts, promotion.ts,
consolidate.ts) because A's equivalents are welded to a 21-schema protocol and a
content-addressing regime that is heavier than a small project needs. The
practical recommendation is to take B's storage/rollback/predicate layer and A's
evidence-grade discipline and conformance-fixture methodology.**

One correction to that recommendation's framing: **on rollback specifically, B
wins outright.** A has no rollback at all — only forward-only revision integers
and a revocation path that purges the hub's derived projection while leaving the
local store untouched. If reversible state changes matter, take B's
`store.ts:41-51` + `rollback.ts` wholesale rather than looking to A.

## Caveats on this report

- Both trees are single shallow-clone snapshots at one commit each; I did not
  review history, issues, or CI runs.
- **This report was revised once.** A second verification pass corrected four
  things in the first draft: (1) Plugin A has **no rollback** (I had credited it
  with content-addressed supersede as if that were rollback); (2) digest
  verification **is** enforced on read, not unverified; (3) H4 is **hard-rejected**
  in code, so the scale is H0–H3; (4) the statistical gate is CI-excludes-zero
  with no p-value/effect-size/uplift threshold. The Q4 rollback comparison
  between the two plugins **inverts** as a result — B is the stronger of the two
  on rollback.
- **Resolved open question:** hub ingress **does** verify Ed25519/DSSE
  signatures (`ingest.ts:116-136`, four call sites), plus a
  `humanReviewed !== true` rejection at `:355`. A prior note flagged this as the
  most consequential unknown; it is now confirmed as implemented.
- Still **unverified**: whether `refreshLatest`
  (`packages/hub/src/postgres.ts:178+`) drops `hub_experiences` rows on
  revocation (only the `hub_objects` delete at `:293` was confirmed); whether CI
  enforces ADR-0016 decision 4; Plugin B's snapshot GC/pruning and live skill
  reload.
- I did not execute any code, install any package, or run either test suite, so
  all claims about runtime behaviour are read from source, not observed.
