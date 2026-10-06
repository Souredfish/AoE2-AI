import unittest

from ai_lab.evolve import _should_update_champion, _tournament


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


if __name__ == "__main__":
    unittest.main()
