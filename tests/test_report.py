import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_lab import report
from ai_lab.recordings import latest_recording, list_recordings


class RecordingPathTests(unittest.TestCase):
    def test_discovers_latest_replay_when_configured_path_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            multi = profile / "Games" / "Age of Empires 2 DE" / "profile-1" / "savegame" / "multi"
            savegame = multi.parent
            multi.mkdir(parents=True)
            stale = profile / "old-profile" / "multi"
            old = multi / "older.aoe2record"
            latest = savegame / "newer.aoe2record"
            old.write_bytes(b"old")
            latest.write_bytes(b"new")
            cfg = {"game": {"recordings_dir": str(stale)}}

            self.assertEqual(latest_recording(cfg, {"USERPROFILE": str(profile)}), latest)
            self.assertEqual(len(list_recordings(cfg, {"USERPROFILE": str(profile)})), 2)


class MgzPlayerParsingTests(unittest.TestCase):
    def _patch_summary(self, summary):
        mgz = types.ModuleType("mgz")
        summary_module = types.ModuleType("mgz.summary")
        summary_module.Summary = lambda stream: summary
        mgz.summary = summary_module
        return patch.dict(sys.modules, {"mgz": mgz, "mgz.summary": summary_module})

    def test_player_parser_failure_is_not_silently_reported_as_an_empty_match(self):
        class BrokenSummary:
            def get_players(self):
                raise ValueError("expected 8 to 8, found 1")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "match.aoe2record"
            path.write_bytes(b"minimal-placeholder")
            with self._patch_summary(BrokenSummary()):
                with self.assertRaisesRegex(ValueError, "expected 8 to 8, found 1"):
                    report.parse_record(path)

    def test_summary_players_and_scores_remain_available(self):
        class Summary:
            def get_map(self):
                return {"name": "Arabia"}

            def get_duration(self):
                return 600

            def get_players(self):
                return [{"name": "Observer"}, {"name": "EvoAI_A"}, {"name": "EvoAI_B"}]

            def get_achievements(self):
                return [types.SimpleNamespace(score=n, military_score=0, razed_score=0, total_xp=0)
                        for n in (0, 1400, 1200)]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "match.aoe2record"
            path.write_bytes(b"minimal-placeholder")
            with self._patch_summary(Summary()):
                info = report.parse_record(path)

        self.assertEqual([player["name"] for player in info["players"]], ["Observer", "EvoAI_A", "EvoAI_B"])
        self.assertEqual(info["players"][1]["score"], 1400)
        self.assertEqual(info["winners"], [])


if __name__ == "__main__":
    unittest.main()
