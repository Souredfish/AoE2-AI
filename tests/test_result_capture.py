import json
import tempfile
import unittest
from pathlib import Path

from ai_lab.result_capture import (CaptureRejected, append_capture, context_from_runner_evidence,
                                   validate_capture)


def observation():
    return {
        "schema_version": 1,
        "capture_sequence": 1,
        "game_time_seconds": {"ok": True, "value_type": "number", "value": 385.0},
        "game_speed_set": {"call_ok": True, "return_value": {"ok": True, "value_type": "nil"}},
        "game_speed_readback": {"ok": True, "value_type": "number", "value": 2.0},
        "victory_player": {
            "ok": True, "value_type": "Player", "player_id": 2, "player_name": "EvoAI_A"
        },
        "players": [
            {"slot": 2, "name": "EvoAI_A",
             "has_won": {"ok": True, "value_type": "boolean", "value": True},
             "current_score": {"ok": True, "value_type": "number", "value": 1250}},
            {"slot": 3, "name": "EvoAI_B",
             "has_won": {"ok": True, "value_type": "boolean", "value": False},
             "current_score": {"ok": True, "value_type": "number", "value": 1110}},
        ],
    }


def context():
    return {
        "match_id": "g0-m0001",
        "game_version": "101.103.54800.0",
        "control_version": "1.1.0",
        "modules_see_everything_confirmed": True,
        "slots_by_alias": {"A": 2, "B": 3},
        "installed": {
            "A": {"genome": "EvoAI_G0P1", "per_sha256": "a" * 64},
            "B": {"genome": "EvoAI_G0P0", "per_sha256": "b" * 64},
        },
        "recording": {"path": "match.aoe2record", "sha256": "c" * 64},
        "captured_at_utc": "2026-10-06T13:00:00Z",
    }


class ResultCaptureTests(unittest.TestCase):
    def test_valid_observation_is_retained_as_diagnostic_only_record(self):
        event = validate_capture(observation(), context())
        self.assertEqual(event["match_id"], "g0-m0001")
        self.assertEqual(event["winner_slot"], 2)
        self.assertEqual(event["players"][0]["current_score"], 1250)
        self.assertEqual(event["recording"]["sha256"], "c" * 64)

    def test_missing_final_score_is_rejected(self):
        raw = observation()
        raw["players"][1]["current_score"] = {"ok": True, "value_type": "nil"}
        with self.assertRaisesRegex(CaptureRejected, "CURRENT_SCORE"):
            validate_capture(raw, context())

    def test_unconfirmed_omniscient_permission_is_rejected(self):
        ctx = context()
        ctx["modules_see_everything_confirmed"] = False
        with self.assertRaisesRegex(CaptureRejected, "Modules See Everything"):
            validate_capture(observation(), ctx)

    def test_conflicting_victory_api_and_player_flags_are_rejected(self):
        raw = observation()
        raw["players"][1]["has_won"]["value"] = True
        with self.assertRaisesRegex(CaptureRejected, "矛盾"):
            validate_capture(raw, context())

    def test_missing_victory_player_is_rejected(self):
        raw = observation()
        raw["victory_player"] = {"ok": True, "value_type": "nil"}
        with self.assertRaisesRegex(CaptureRejected, "GetVictoryPlayer"):
            validate_capture(raw, context())

    def test_duplicate_match_capture_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "captures.jsonl"
            event = validate_capture(observation(), context())
            append_capture(path, event)
            with self.assertRaisesRegex(CaptureRejected, "重复"):
                append_capture(path, event)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_speed_readback_must_match_requested_value(self):
        raw = observation()
        raw["game_speed_readback"]["value"] = 1.7
        with self.assertRaisesRegex(CaptureRejected, "GetGameSpeed"):
            validate_capture(raw, context())

    def test_repeat_invocation_sequence_is_rejected(self):
        raw = observation()
        raw["capture_sequence"] = 2
        with self.assertRaisesRegex(CaptureRejected, "首次调用"):
            validate_capture(raw, context())

    def test_failed_speed_set_call_is_rejected(self):
        raw = observation()
        raw["game_speed_set"]["call_ok"] = False
        with self.assertRaisesRegex(CaptureRejected, "SetGameSpeed"):
            validate_capture(raw, context())

    def test_repeated_capture_sentinel_is_rejected(self):
        from ai_lab.result_capture import parse_observation
        line = "CONTROL: EVOLAB_RESULT_CAPTURE_V1:" + json.dumps(observation())
        with self.assertRaisesRegex(CaptureRejected, "只能包含一次"):
            parse_observation(line + "\n" + line)

    def test_runner_association_must_be_unique_and_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replay = root / "match.aoe2record"
            replay.write_bytes(b"replay")
            stat = replay.stat()
            prepared = {"event": "match_prepared", "match_id": "g0-m0001",
                        "slots_by_alias": {"A": 2, "B": 3}, "installed": context()["installed"]}
            association = {
                "event": "recording_associated", "match_id": "g0-m0001",
                "association": {
                    "match_id": "g0-m0001",
                    "slots_by_alias": {"A": 2, "B": 3},
                    "installed": context()["installed"],
                    "recording": {"path": str(replay.resolve()), "record_id": "rec-1",
                                  "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                  "new_since_snapshot": True,
                                  "new_candidates": [str(replay.resolve())]},
                },
            }
            evidence = root / "gen_0.jsonl"
            evidence.write_text(json.dumps(prepared) + "\n" + json.dumps(association) + "\n",
                                 encoding="utf-8")
            ctx = context_from_runner_evidence(evidence, replay, "game-v", "control-v", True)
            self.assertEqual(len(ctx["recording"]["sha256"]), 64)
            evidence.write_text(evidence.read_text(encoding="utf-8") * 2, encoding="utf-8")
            with self.assertRaisesRegex(CaptureRejected, "唯一关联"):
                context_from_runner_evidence(evidence, replay, "game-v", "control-v", True)

    def test_runner_association_rejects_replaced_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replay = root / "match.aoe2record"
            replay.write_bytes(b"original")
            stat = replay.stat()
            evidence = root / "gen_0.jsonl"
            prepared = {"event": "match_prepared", "match_id": "g0-m0001",
                        "slots_by_alias": {"A": 2, "B": 3}, "installed": context()["installed"]}
            event = {"event": "recording_associated", "match_id": "g0-m0001", "association": {
                "match_id": "g0-m0001", "slots_by_alias": {"A": 2, "B": 3},
                "installed": context()["installed"],
                "recording": {"path": str(replay.resolve()), "record_id": "rec-1",
                              "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                              "new_since_snapshot": True,
                              "new_candidates": [str(replay.resolve())]}}}
            evidence.write_text(json.dumps(prepared) + "\n" + json.dumps(event) + "\n",
                                encoding="utf-8")
            replay.write_bytes(b"replacement")
            with self.assertRaisesRegex(CaptureRejected, "大小/mtime"):
                context_from_runner_evidence(evidence, replay, "game-v", "control-v", True)


if __name__ == "__main__":
    unittest.main()
