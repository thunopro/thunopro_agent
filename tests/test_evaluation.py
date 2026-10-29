import math
import unittest

from thunopro_agent.evaluation import DIMENSIONS, WEIGHTS, evaluate, weighted_score


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.scores = dict(zip(DIMENSIONS, (5, 4, 3, 2, 1, 0)))

    def test_required_dimensions_and_weights(self):
        self.assertEqual(DIMENSIONS, (
            "requirement_coverage",
            "correctness_tests",
            "independent_review",
            "handoff",
            "efficiency",
            "deduplication",
        ))
        self.assertEqual(list(WEIGHTS.values()), [25, 25, 20, 15, 10, 5])
        self.assertEqual(sum(WEIGHTS.values()), 100)

    def test_weighted_score(self):
        self.assertAlmostEqual(weighted_score(self.scores), 3.25)
        self.assertEqual(weighted_score(dict.fromkeys(DIMENSIONS, 5)), 5.0)
        self.assertEqual(weighted_score(dict.fromkeys(DIMENSIONS, 0)), 0.0)
        self.assertIsInstance(weighted_score(self.scores), float)

    def test_every_dimension_is_required(self):
        for dimension in DIMENSIONS:
            with self.subTest(dimension=dimension):
                incomplete = dict(self.scores)
                del incomplete[dimension]
                with self.assertRaisesRegex(ValueError, dimension):
                    weighted_score(incomplete)

    def test_unknown_dimensions_are_rejected(self):
        scores = dict(self.scores, typo=5)
        with self.assertRaisesRegex(ValueError, "Unknown dimensions: typo"):
            weighted_score(scores)

    def test_scores_must_be_within_range(self):
        for value in (-0.01, 5.01, -1, 6):
            with self.subTest(value=value):
                scores = dict(self.scores, handoff=value)
                with self.assertRaisesRegex(ValueError, "between 0 and 5"):
                    weighted_score(scores)

    def test_scores_must_be_finite(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                scores = dict(self.scores, efficiency=value)
                with self.assertRaisesRegex(ValueError, "finite"):
                    weighted_score(scores)

    def test_scores_must_be_numeric(self):
        for value in (None, "high", True):
            with self.subTest(value=value):
                scores = dict(self.scores, deduplication=value)
                with self.assertRaisesRegex(TypeError, "real number"):
                    weighted_score(scores)

    def test_scores_must_be_a_mapping(self):
        with self.assertRaisesRegex(TypeError, "mapping"):
            weighted_score([5, 4, 3, 2, 1, 0])

    def test_evaluate_returns_complete_result(self):
        result = evaluate(self.scores)
        self.assertEqual(result["dimensions"], {
            "requirement_coverage": 5.0,
            "correctness_tests": 4.0,
            "independent_review": 3.0,
            "handoff": 2.0,
            "efficiency": 1.0,
            "deduplication": 0.0,
        })
        self.assertEqual(result["weights"], WEIGHTS)
        self.assertAlmostEqual(result["score"], 3.25)
        self.assertEqual(result["grade"], "acceptable")
        self.assertEqual(result["missing_evidence"], list(DIMENSIONS))

    def test_grade_boundaries(self):
        expected = {
            5: "excellent",
            4.5: "excellent",
            4.499: "good",
            4: "good",
            3.999: "acceptable",
            3: "acceptable",
            2.999: "poor",
            2: "poor",
            1.999: "critical",
            0: "critical",
        }
        for value, grade in expected.items():
            with self.subTest(value=value):
                self.assertEqual(evaluate(dict.fromkeys(DIMENSIONS, value))["grade"], grade)

    def test_missing_and_blank_evidence_are_listed_in_dimension_order(self):
        evidence = {
            "requirement_coverage": "Acceptance criteria checked",
            "independent_review": "Reviewer approved",
            "handoff": "   ",
            "efficiency": "",
        }
        result = evaluate(self.scores, evidence)
        self.assertEqual(result["missing_evidence"], [
            "correctness_tests",
            "handoff",
            "efficiency",
            "deduplication",
        ])

    def test_evidence_does_not_change_score_or_grade(self):
        without_evidence = evaluate(self.scores)
        with_evidence = evaluate(self.scores, {
            dimension: "Evidence recorded" for dimension in DIMENSIONS
        })
        self.assertEqual(with_evidence["score"], without_evidence["score"])
        self.assertEqual(with_evidence["grade"], without_evidence["grade"])
        self.assertEqual(with_evidence["missing_evidence"], [])


if __name__ == "__main__":
    unittest.main()
