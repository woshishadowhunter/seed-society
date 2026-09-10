"""Tests for auditable seed revisions and deterministic rollback."""

import unittest
from dataclasses import replace

from seed_society.domain import (
    ExperienceRecord,
    KnowledgeItem,
    RevisionAction,
    SeedKind,
    SeedRevision,
    utc_now,
)
from seed_society.revisions import RollbackRejected, SeedRollback
from seed_society.storage import SQLiteRepository


class RevisionRecordingTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")

    def tearDown(self):
        self.repository.close()

    def _knowledge(self, title="Seed", content="Body", item_id=None):
        item = KnowledgeItem.create(title, content)
        if item_id is not None:
            item = KnowledgeItem(
                item_id, item.title, item.content, item.tags, item.created_at
            )
        return item

    def test_create_records_a_create_revision(self):
        self.repository.save_knowledge(self._knowledge(), operator="operator")
        revisions = self.repository.list_seed_revisions()
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].action, RevisionAction.CREATE)
        self.assertIsNone(revisions[0].before_payload)
        self.assertIsNotNone(revisions[0].after_payload)
        self.assertEqual(revisions[0].operator, "operator")

    def test_update_records_before_and_after(self):
        item = self._knowledge()
        self.repository.save_knowledge(item, operator="first")
        updated = KnowledgeItem(
            item.knowledge_id, "Seed v2", "Body v2", item.tags, item.created_at
        )
        self.repository.save_knowledge(updated, operator="second")
        revisions = self.repository.list_seed_revisions()
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[1].action, RevisionAction.UPDATE)
        self.assertEqual(revisions[1].before_payload["title"], "Seed")
        self.assertEqual(revisions[1].after_payload["title"], "Seed v2")

    def test_history_is_append_only_across_rollback(self):
        item = self._knowledge()
        self.repository.save_knowledge(item, operator="creator")
        updated = KnowledgeItem(
            item.knowledge_id, "Changed", "Body", item.tags, item.created_at
        )
        self.repository.save_knowledge(updated, operator="editor")
        target = self.repository.list_seed_revisions()[1]
        SeedRollback(self.repository).rollback(
            target.revision_id, operator="reviewer"
        )
        revisions = self.repository.list_seed_revisions()
        self.assertEqual(len(revisions), 3)
        self.assertEqual(revisions[2].action, RevisionAction.ROLLBACK)
        self.assertEqual(revisions[2].rolled_back_revision_id, target.revision_id)
        # The undone revision is still present, unmodified.
        self.assertEqual(
            self.repository.get_seed_revision(target.revision_id).after_payload[
                "title"
            ],
            "Changed",
        )

    def test_experience_overwrite_records_update(self):
        record = self._experience()
        self.repository.save_experience(record, operator="distill")
        changed = replace(record, strength=0.4)
        self.repository.save_experience(
            changed, overwrite=True, operator="decay"
        )
        revisions = self.repository.list_seed_revisions(
            seed_kind=SeedKind.EXPERIENCE
        )
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[1].operator, "decay")
        self.assertAlmostEqual(revisions[1].before_payload["strength"], 1.0)
        self.assertAlmostEqual(revisions[1].after_payload["strength"], 0.4)

    def test_plain_reinsert_does_not_record_a_phantom_revision(self):
        record = self._experience()
        self.repository.save_experience(record, operator="distill")
        # A non-overwrite re-insert is a no-op; it must not claim a change.
        self.repository.save_experience(record, operator="replay")
        revisions = self.repository.list_seed_revisions(
            seed_kind=SeedKind.EXPERIENCE
        )
        self.assertEqual(len(revisions), 1)

    def _experience(self, strength=1.0):
        return ExperienceRecord(
            experience_id="experience-1",
            goal_id="goal-1",
            task_id="task-1",
            task_type="analysis",
            agent_id="agent-a",
            attempt_no=1,
            verdict="PASS",
            score=90.0,
            lessons=("lesson",),
            tags=("pattern:success",),
            created_at=utc_now(),
            strength=strength,
        )


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.rollback = SeedRollback(self.repository)

    def tearDown(self):
        self.repository.close()

    def test_rollback_of_update_restores_previous_payload(self):
        item = KnowledgeItem.create("Original", "Body")
        self.repository.save_knowledge(item, operator="creator")
        changed = KnowledgeItem(
            item.knowledge_id, "Modified", "Changed body", item.tags, item.created_at
        )
        self.repository.save_knowledge(changed, operator="editor")
        target = self.repository.list_seed_revisions()[1]

        report = self.rollback.rollback(target.revision_id, operator="reviewer")

        self.assertTrue(report.restored)
        live = self.repository.get_knowledge(item.knowledge_id)
        self.assertEqual(live.title, "Original")
        self.assertEqual(live.content, "Body")

    def test_rollback_of_create_removes_the_seed(self):
        item = KnowledgeItem.create("Temporary", "Body")
        self.repository.save_knowledge(item, operator="creator")
        target = self.repository.list_seed_revisions()[0]

        report = self.rollback.rollback(target.revision_id, operator="reviewer")

        self.assertTrue(report.restored)
        self.assertIsNone(self.repository.get_knowledge(item.knowledge_id))

    def test_rollback_of_experience_restores_strength(self):
        record = ExperienceRecord(
            experience_id="experience-1",
            goal_id="goal-1",
            task_id="task-1",
            task_type="analysis",
            agent_id="agent-a",
            attempt_no=1,
            verdict="PASS",
            score=90.0,
            lessons=("lesson",),
            tags=("pattern:success",),
            created_at=utc_now(),
            strength=0.9,
        )
        self.repository.save_experience(record, operator="distill")
        decayed = replace(record, strength=0.2)
        self.repository.save_experience(decayed, overwrite=True, operator="decay")
        target = self.repository.list_seed_revisions(
            seed_kind=SeedKind.EXPERIENCE
        )[1]

        self.rollback.rollback(target.revision_id, operator="reviewer")

        restored = self.repository.get_experience("experience-1")
        self.assertAlmostEqual(restored.strength, 0.9)

    def test_rollback_is_itself_reversible(self):
        item = KnowledgeItem.create("Original", "Body")
        self.repository.save_knowledge(item, operator="creator")
        changed = KnowledgeItem(
            item.knowledge_id, "Modified", "Body", item.tags, item.created_at
        )
        self.repository.save_knowledge(changed, operator="editor")
        target = self.repository.list_seed_revisions()[1]

        self.rollback.rollback(target.revision_id, operator="reviewer")
        # Re-applying the original revision restores the newer state.
        self.rollback.rollback(target.revision_id, operator="reviewer")

        self.assertEqual(
            self.repository.get_knowledge(item.knowledge_id).title, "Modified"
        )


class RollbackRejectionTests(unittest.TestCase):
    """Negative cases that MUST fail, not silently succeed."""

    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.rollback = SeedRollback(self.repository)

    def tearDown(self):
        self.repository.close()

    def test_unknown_revision_is_rejected(self):
        with self.assertRaises(KeyError):
            self.rollback.rollback("revision-does-not-exist", operator="reviewer")

    def test_rollback_of_a_rollback_is_rejected(self):
        item = KnowledgeItem.create("Seed", "Body")
        self.repository.save_knowledge(item, operator="creator")
        changed = KnowledgeItem(
            item.knowledge_id, "Changed", "Body", item.tags, item.created_at
        )
        self.repository.save_knowledge(changed, operator="editor")
        target = self.repository.list_seed_revisions()[1]
        report = self.rollback.rollback(target.revision_id, operator="reviewer")
        with self.assertRaises(RollbackRejected):
            self.rollback.rollback(
                report.rollback_revision_id, operator="reviewer"
            )

    def test_blank_operator_is_rejected(self):
        item = KnowledgeItem.create("Seed", "Body")
        self.repository.save_knowledge(item, operator="creator")
        target = self.repository.list_seed_revisions()[0]
        with self.assertRaises(RollbackRejected):
            self.rollback.rollback(target.revision_id, operator="   ")

    def test_create_revision_cannot_carry_a_before_payload(self):
        with self.assertRaises(ValueError):
            SeedRevision.create(
                SeedKind.KNOWLEDGE,
                "seed-1",
                RevisionAction.CREATE,
                operator="op",
                before_payload={"title": "x"},
            )

    def test_update_revision_requires_a_before_payload(self):
        with self.assertRaises(ValueError):
            SeedRevision.create(
                SeedKind.KNOWLEDGE,
                "seed-1",
                RevisionAction.UPDATE,
                operator="op",
            )

    def test_rollback_revision_requires_a_target(self):
        with self.assertRaises(ValueError):
            SeedRevision.create(
                SeedKind.KNOWLEDGE,
                "seed-1",
                RevisionAction.ROLLBACK,
                operator="op",
            )

    def test_revision_requires_an_operator(self):
        with self.assertRaises(ValueError):
            SeedRevision.create(
                SeedKind.KNOWLEDGE,
                "seed-1",
                RevisionAction.CREATE,
                operator="  ",
            )


if __name__ == "__main__":
    unittest.main()
