import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai_lab"))
import auto_runner


class WinnerIdentityTests(unittest.TestCase):
    def test_reverse_replay_order_maps_winner_to_installed_genome(self):
        info = {"players": [
            {"name": "Observer"},
            {"name": "EvoAI_B", "winner": True},
            {"name": "EvoAI_A", "winner": False},
        ]}

        self.assertEqual(auto_runner.resolve_winner(info, 4, 2, 7), "EvoAI_G4P7")

    def test_reverse_replay_order_maps_scores_to_ai_identities(self):
        info = {"players": [
            {"name": "EvoAI_B", "score": 900},
            {"name": "EvoAI_A", "score": 1200},
        ]}

        self.assertEqual(auto_runner.resolve_winner(info, 4, 2, 7), "EvoAI_G4P2")

    def test_ambiguous_or_missing_ai_identity_has_no_winner(self):
        info = {"players": [
            {"name": "EvoAI_A", "winner": True},
            {"name": "EvoAI_A", "winner": False},
        ]}

        self.assertIsNone(auto_runner.resolve_winner(info, 4, 2, 7))


if __name__ == "__main__":
    unittest.main()
