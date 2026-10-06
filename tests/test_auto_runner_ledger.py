import unittest
from pathlib import Path

from ai_lab import auto_runner
from ai_lab import evo_results as ER


class ReplayLedgerTests(unittest.TestCase):
    def setUp(self):
        self.manifest = ER.build_schedule(4, [(2, 7)])
        self.match = self.manifest["matches"][0]

    def _result(self, info):
        return auto_runner.match_result_from_replay(
            info, 4, 2, 7, self.match, "reverse-order.aoe2record", "record-test")

    def test_reverse_replay_order_records_winner_and_scores_by_genome_identity(self):
        info = {"players": [
            {"name": "EvoAI_B", "winner": True, "score": 2100},
            {"name": "EvoAI_A", "winner": False, "score": 1850},
        ]}
        row = self._result(info)

        accepted = ER.reconcile_results([row], self.manifest)

        saved = accepted[self.match["match_id"]]
        self.assertEqual(saved["players"], [
            ["EvoAI_G4P2", False], ["EvoAI_G4P7", True],
        ])
        self.assertEqual(saved["winners"], ["EvoAI_G4P7"])
        self.assertEqual(saved["scores"], {
            "EvoAI_G4P2": 1850, "EvoAI_G4P7": 2100,
        })

    def test_multiple_replay_winners_are_rejected_before_ledger_append(self):
        info = {"players": [
            {"name": "EvoAI_B", "winner": True, "score": 2100},
            {"name": "EvoAI_A", "winner": True, "score": 1850},
        ]}

        with self.assertRaisesRegex(ValueError, "多个胜者"):
            self._result(info)

    def test_non_finite_or_non_numeric_replay_score_is_rejected(self):
        for invalid in (float("nan"), float("inf"), float("-inf"), "high"):
            info = {"players": [
                {"name": "EvoAI_A", "winner": True, "score": 1850},
                {"name": "EvoAI_B", "winner": False, "score": invalid},
            ]}
            with self.subTest(score=invalid):
                with self.assertRaisesRegex(ValueError, "有限数值"):
                    self._result(info)

    def test_ledger_rejects_multiple_winners_and_mismatched_winner_flags(self):
        row = self._result({"players": [
            {"name": "EvoAI_A", "winner": True, "score": 1850},
            {"name": "EvoAI_B", "winner": False, "score": 2100},
        ]})
        row["winners"] = ["EvoAI_G4P2", "EvoAI_G4P7"]

        with self.assertRaisesRegex(ValueError, "胜者无效"):
            ER.reconcile_results([row], self.manifest)

        row["winners"] = ["EvoAI_G4P2"]
        row["players"] = [["EvoAI_G4P2", False], ["EvoAI_G4P7", True]]
        with self.assertRaisesRegex(ValueError, "胜者标记"):
            ER.reconcile_results([row], self.manifest)

    def test_ledger_rejects_non_finite_and_invalid_scores(self):
        row = self._result({"players": [
            {"name": "EvoAI_A", "winner": True, "score": 1850},
            {"name": "EvoAI_B", "winner": False, "score": 2100},
        ]})
        for invalid in (float("nan"), float("inf"), "high", True):
            row["scores"]["EvoAI_G4P7"] = invalid
            with self.subTest(score=invalid):
                with self.assertRaisesRegex(ValueError, "有限数值"):
                    ER.reconcile_results([row], self.manifest)


if __name__ == "__main__":
    unittest.main()
