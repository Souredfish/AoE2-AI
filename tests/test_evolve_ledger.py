import unittest

from ai_lab import evo_results as ER
from ai_lab import evolve


class ReportLedgerTests(unittest.TestCase):
    def setUp(self):
        self.manifest = ER.build_schedule(4, [(2, 7)])

    def _record(self, info):
        row = evolve.result_row_from_report(
            info, 4, 8, self.manifest, {}, "reverse-order.aoe2record", "record-test")
        ER.reconcile_results([row], self.manifest)
        return row

    def test_reversed_report_order_maps_winner_to_correct_scheduled_genome(self):
        row = self._record({"players": [
            {"name": "EvoAI_G4P7", "winner": True, "score": 2100},
            {"name": "EvoAI_G4P2", "winner": False, "score": 1850},
        ], "winners": ["EvoAI_G4P7"]})

        self.assertEqual(row["players"], [
            ["EvoAI_G4P2", False], ["EvoAI_G4P7", True],
        ])
        self.assertEqual(row["winners"], ["EvoAI_G4P7"])
        self.assertEqual(row["scores"], {
            "EvoAI_G4P2": 1850, "EvoAI_G4P7": 2100,
        })

    def test_report_with_multiple_winners_is_rejected_before_record_creation(self):
        info = {"players": [
            {"name": "EvoAI_G4P7", "winner": True, "score": 2100},
            {"name": "EvoAI_G4P2", "winner": True, "score": 1850},
        ], "winners": ["EvoAI_G4P7", "EvoAI_G4P2"]}

        with self.assertRaisesRegex(ValueError, "只能有一个参赛胜者"):
            evolve.result_row_from_report(info, 4, 8, self.manifest, {})

    def test_conflicting_player_winner_flag_is_rejected(self):
        info = {"players": [
            {"name": "EvoAI_G4P7", "winner": False, "score": 2100},
            {"name": "EvoAI_G4P2", "winner": True, "score": 1850},
        ], "winners": ["EvoAI_G4P7"]}

        with self.assertRaisesRegex(ValueError, "玩家胜者标记与战报 winners 不一致"):
            evolve.result_row_from_report(info, 4, 8, self.manifest, {})

    def test_all_false_player_winner_flags_conflict_with_report_winner(self):
        info = {"players": [
            {"name": "EvoAI_G4P7", "winner": False, "score": 2100},
            {"name": "EvoAI_G4P2", "winner": False, "score": 1850},
        ], "winners": ["EvoAI_G4P7"]}

        with self.assertRaisesRegex(ValueError, "玩家胜者标记与战报 winners 不一致"):
            evolve.result_row_from_report(info, 4, 8, self.manifest, {})

    def test_non_boolean_player_winner_flag_is_rejected(self):
        info = {"players": [
            {"name": "EvoAI_G4P7", "winner": 1, "score": 2100},
            {"name": "EvoAI_G4P2", "winner": False, "score": 1850},
        ], "winners": ["EvoAI_G4P7"]}

        with self.assertRaisesRegex(ValueError, "胜者标记必须是布尔值"):
            evolve.result_row_from_report(info, 4, 8, self.manifest, {})

    def test_report_with_invalid_score_is_rejected_by_ledger_validation(self):
        info = {"players": [
            {"name": "EvoAI_G4P7", "winner": True, "score": 2100},
            {"name": "EvoAI_G4P2", "winner": False, "score": float("nan")},
        ], "winners": ["EvoAI_G4P7"]}

        with self.assertRaisesRegex(ValueError, "有限数值"):
            self._record(info)


if __name__ == "__main__":
    unittest.main()
