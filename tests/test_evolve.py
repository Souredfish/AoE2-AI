import unittest
import json
import tempfile
from pathlib import Path

from ai_lab.evolve import _load_champion_fitness, _should_update_champion, _tournament
from ai_lab.evo_results import build_schedule, match_id_for_recording, pending_matches, reconcile_results, validate_complete


class SequenceRng:
    def __init__(self, choices):
        self.choices = iter(choices)

    def choice(self, population):
        return next(self.choices)


class TournamentTests(unittest.TestCase):
    def test_highest_fitness_among_sampled_candidates_wins(self):
        fitness = [(9.0, 0), (1.0, 1), (4.0, 2)]
        rng = SequenceRng([(1.0, 1), (9.0, 0), (4.0, 2)])
        self.assertEqual(_tournament(fitness, 3, rng), 0)

    def test_low_index_weak_individual_does_not_win_by_index(self):
        fitness = [(0.0, 0), (2.0, 7), (1.0, 3)]
        rng = SequenceRng([(0.0, 0), (2.0, 7), (1.0, 3)])
        self.assertEqual(_tournament(fitness, 3, rng), 7)


class ChampionTests(unittest.TestCase):
    def test_new_champion_is_saved_when_no_champion_exists(self):
        self.assertTrue(_should_update_champion(-2.0, None, False))

    def test_existing_scored_champion_only_replaced_by_improvement(self):
        self.assertFalse(_should_update_champion(1.5, 1.5, True))
        self.assertFalse(_should_update_champion(1.4, 1.5, True))
        self.assertTrue(_should_update_champion(1.6, 1.5, True))

    def test_legacy_champion_without_score_is_preserved(self):
        self.assertFalse(_should_update_champion(10.0, None, True))

    def test_non_finite_stored_fitness_is_treated_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            score_file = Path(tmp) / "champion_fitness.json"
            for value in (float("nan"), float("inf"), float("-inf")):
                score_file.write_text(json.dumps({"fitness": value}), encoding="utf-8")
                self.assertIsNone(_load_champion_fitness(score_file))

    def test_non_finite_candidate_does_not_replace_existing_champion(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            self.assertFalse(_should_update_champion(value, 1.0, True))

    def test_non_finite_candidate_does_not_create_first_champion(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            self.assertFalse(_should_update_champion(value, None, False))


class ResultLedgerTests(unittest.TestCase):
    def setUp(self):
        self.manifest = build_schedule(2, [(0, 1), (0, 2), (1, 2)])
        self.results = [
            {"match_id": match["match_id"], "players": [[match["players"][0], True],
                       [match["players"][1], False]], "winners": [match["players"][0]],
             "record": "record-%d.aoe2record" % i}
            for i, match in enumerate(self.manifest["matches"])
        ]

    def test_complete_schedule_and_results_validate(self):
        accepted = validate_complete(self.results, self.manifest, 3, 3)
        self.assertEqual(len(accepted), 3)

    def test_missing_match_is_rejected_before_next_generation(self):
        with self.assertRaisesRegex(ValueError, "缺少 1 场"):
            validate_complete(self.results[:-1], self.manifest, 3, 3)

    def test_duplicate_match_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "重复记账"):
            reconcile_results(self.results + [dict(self.results[0])], self.manifest)

    def test_invalid_winner_is_rejected(self):
        invalid = [dict(row) for row in self.results]
        invalid[0] = dict(invalid[0], winners=["EvoAI_G2P9"])
        with self.assertRaisesRegex(ValueError, "胜者无效"):
            reconcile_results(invalid, self.manifest)

    def test_duplicate_recording_is_rejected_even_for_another_match(self):
        duplicate = [dict(row) for row in self.results]
        duplicate[1]["record"] = duplicate[0]["record"]
        with self.assertRaisesRegex(ValueError, "录像重复记账"):
            reconcile_results(duplicate, self.manifest)

    def test_legacy_rows_are_assigned_stable_ids_by_pair(self):
        legacy = [dict(row) for row in self.results]
        for row in legacy:
            row.pop("match_id")
        accepted = reconcile_results(legacy, self.manifest)
        self.assertEqual(set(accepted), {m["match_id"] for m in self.manifest["matches"]})

    def test_duplicate_legacy_pair_cannot_be_mapped_twice(self):
        legacy = [dict(row) for row in self.results]
        legacy[1]["players"] = list(legacy[0]["players"])
        legacy[1]["winners"] = list(legacy[0]["winners"])
        for row in legacy:
            row.pop("match_id")
        with self.assertRaisesRegex(ValueError, "重复记账"):
            reconcile_results(legacy, self.manifest)

    def test_resume_uses_first_unrecorded_schedule_entries_after_truncation(self):
        pending = pending_matches(self.results[:2], self.manifest)
        self.assertEqual([m["match_id"] for m in pending], ["g2-m0003"])

    def test_resume_recovers_a_valid_hole_without_shifting_match_mapping(self):
        pending = pending_matches([self.results[2]], self.manifest)
        self.assertEqual([m["match_id"] for m in pending], ["g2-m0001", "g2-m0002"])

    def test_recording_id_is_stable_across_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "game.aoe2record"
            copied = Path(tmp) / "renamed.aoe2record"
            original.write_bytes(b"sample recording bytes")
            copied.write_bytes(original.read_bytes())
            self.assertEqual(match_id_for_recording(original), match_id_for_recording(copied))


if __name__ == "__main__":
    unittest.main()
