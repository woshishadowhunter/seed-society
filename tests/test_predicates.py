"""Tests for the pure seed predicates: duplicates, secrets, staleness."""

import unittest

from seed_society.predicates import (
    NEAR_DUPLICATE_SCORE,
    SIMILAR_WARN_SCORE,
    STALE_MIN_AGE_SECONDS,
    is_stale,
    retrieval_count,
    screen_duplicate,
    secret_reason,
    similarity,
    tokens,
)


class SimilarityTests(unittest.TestCase):
    def test_identical_text_scores_one(self):
        self.assertAlmostEqual(similarity("alpha beta", "alpha beta"), 1.0)

    def test_disjoint_text_scores_zero(self):
        self.assertAlmostEqual(similarity("alpha", "beta"), 0.0)

    def test_empty_input_scores_zero(self):
        self.assertAlmostEqual(similarity("", "anything"), 0.0)

    def test_cjk_text_is_not_disjoint(self):
        """Character-level tokens are required or Chinese seeds never match."""
        score = similarity("成功模式：先验证再写入", "成功模式：先验证再落盘")
        self.assertGreater(score, 0.4)

    def test_tokens_split_ascii_and_cjk(self):
        result = tokens("Evidence 证据")
        self.assertIn("evidence", result)
        self.assertIn("证", result)


class DuplicateScreeningTests(unittest.TestCase):
    def test_near_duplicate_is_blocked(self):
        existing = [("Market analysis 成功模式", "Successful pattern: verify sources")]
        verdict = screen_duplicate(
            "Market analysis 成功模式", "Successful pattern: verify sources", existing
        )
        self.assertTrue(verdict.blocked)
        self.assertFalse(verdict.accepted)
        self.assertGreaterEqual(verdict.score, NEAR_DUPLICATE_SCORE)

    def test_similar_but_distinct_is_warned_not_blocked(self):
        existing = [
            (
                "Market analysis 成功模式",
                "Successful pattern: verify sources before writing the summary",
            )
        ]
        verdict = screen_duplicate(
            "Market analysis 成功模式",
            "Verify sources before writing; prefer primary data over summaries",
            existing,
        )
        self.assertFalse(verdict.blocked)
        self.assertTrue(verdict.accepted)
        self.assertGreaterEqual(verdict.score, SIMILAR_WARN_SCORE)
        self.assertEqual(verdict.matched_title, "Market analysis 成功模式")

    def test_unrelated_candidate_is_accepted_quietly(self):
        existing = [("Brand voice", "Use plain language")]
        verdict = screen_duplicate("Integration", "Combine approved materials", existing)
        self.assertTrue(verdict.accepted)
        self.assertFalse(verdict.warned)

    def test_empty_corpus_accepts_everything(self):
        verdict = screen_duplicate("Any", "Thing", [])
        self.assertTrue(verdict.accepted)
        self.assertAlmostEqual(verdict.score, 0.0)


class SecretScreeningTests(unittest.TestCase):
    def test_openai_style_key_is_detected(self):
        self.assertTrue(secret_reason("token sk-abcdefghijklmnopqrstuvwx"))

    def test_github_token_is_detected(self):
        self.assertTrue(secret_reason("ghp_abcdefghijklmnopqrstuvwxyz0123456789"))

    def test_aws_key_is_detected(self):
        self.assertTrue(secret_reason("AKIAIOSFODNN7EXAMPLE"))

    def test_private_key_block_is_detected(self):
        self.assertTrue(secret_reason("-----BEGIN RSA PRIVATE KEY-----"))

    def test_jwt_is_detected(self):
        self.assertTrue(
            secret_reason("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturepart")
        )

    def test_labelled_secret_is_detected(self):
        self.assertTrue(secret_reason("api_key = 8f3ba29c1d"))

    def test_ordinary_prose_is_clean(self):
        self.assertEqual(
            secret_reason("Successful pattern: verify sources before writing"),
            "",
        )

    def test_word_secret_without_a_value_is_clean(self):
        """The word alone is not a leak; a labelled value is required."""
        self.assertEqual(secret_reason("avoid pasting the secret into logs"), "")


class StalenessTests(unittest.TestCase):
    def test_old_and_never_retrieved_is_stale(self):
        self.assertTrue(
            is_stale(age_seconds=STALE_MIN_AGE_SECONDS + 1, retrieval_count=0)
        )

    def test_old_but_retrieved_is_not_stale(self):
        """Use is what earns a seed its place, regardless of age."""
        self.assertFalse(
            is_stale(age_seconds=STALE_MIN_AGE_SECONDS * 10, retrieval_count=1)
        )

    def test_recent_and_unretrieved_is_not_stale(self):
        self.assertFalse(is_stale(age_seconds=60, retrieval_count=0))

    def test_threshold_boundary_is_not_stale(self):
        self.assertFalse(
            is_stale(age_seconds=STALE_MIN_AGE_SECONDS, retrieval_count=0)
        )


class RetrievalCountTests(unittest.TestCase):
    def test_reads_activations(self):
        class Record:
            activations = 4

        self.assertEqual(retrieval_count(Record()), 4)

    def test_missing_attribute_reads_as_zero(self):
        class Record:
            pass

        self.assertEqual(retrieval_count(Record()), 0)

    def test_boolean_is_not_counted_as_a_count(self):
        class Record:
            activations = True

        self.assertEqual(retrieval_count(Record()), 0)


if __name__ == "__main__":
    unittest.main()
