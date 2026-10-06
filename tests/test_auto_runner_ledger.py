import unittest
import tempfile
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

    def _slot_evidence(self, record, *, slots=None, installed=None, candidates=None):
        path = Path(record).resolve()
        record_id = ER.match_id_for_recording(path)
        return {
            "schema_version": 1,
            "match_id": self.match["match_id"],
            "slots_by_alias": slots or {"A": 2, "B": 3},
            "installed": installed or {
                "A": {"genome": "EvoAI_G4P2", "per_sha256": "a" * 64, "per_path": "/ai/EvoAI_A.per"},
                "B": {"genome": "EvoAI_G4P7", "per_sha256": "b" * 64, "per_path": "/ai/EvoAI_B.per"},
            },
            "baseline_captured_at_ns": 1_700_000_000_000_000_000,
            "recording_baseline": {"/old/replay.aoe2record": {"size_bytes": 10, "mtime_ns": 1}},
            "recording": {
                "path": str(path), "record_id": record_id, "size_bytes": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns, "new_since_snapshot": True,
                "new_candidates": candidates or [str(path)],
            },
        }

    def _empty_name_replay(self):
        return {"players": [
            {"slot": 3, "name": "", "user_id": 4294967295, "winner": True, "score": 2100},
            {"slot": 1, "name": "Observer", "user_id": 42, "score": 0},
            {"slot": 2, "name": "", "user_id": 4294967295, "winner": False, "score": 1850},
        ]}

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

    def test_empty_ai_names_map_by_explicit_slots_and_preserve_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "real.aoe2record"
            record.write_bytes(b"record bytes")
            evidence = self._slot_evidence(record)
            row = auto_runner.match_result_from_replay(
                self._empty_name_replay(), 4, 2, 7, self.match, record,
                evidence["recording"]["record_id"], evidence=evidence)

        accepted = ER.reconcile_results([row], self.manifest)
        saved = accepted[self.match["match_id"]]
        self.assertEqual(saved["winners"], ["EvoAI_G4P7"])
        self.assertEqual(saved["scores"], {"EvoAI_G4P2": 1850, "EvoAI_G4P7": 2100})
        self.assertEqual(saved["association"]["slots_by_alias"], {"A": 2, "B": 3})

    def test_empty_ai_names_reject_misaligned_slot_schedule_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "real.aoe2record"
            record.write_bytes(b"record bytes")
            evidence = self._slot_evidence(record, slots={"A": 3, "B": 2})
            with self.assertRaisesRegex(ValueError, "槽位映射"):
                auto_runner.match_result_from_replay(
                    self._empty_name_replay(), 4, 2, 7, self.match, record,
                    evidence["recording"]["record_id"], evidence=evidence)

    def test_slot_mapping_rejects_ai_marker_or_name_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "real.aoe2record"
            record.write_bytes(b"record bytes")
            evidence = self._slot_evidence(record)
            for update in ({"user_id": 1234, "name": ""},
                           {"user_id": 4294967295, "name": "EvoAI_B"}):
                replay = self._empty_name_replay()
                player = next(p for p in replay["players"] if p["slot"] == 2)
                player.update(update)
                with self.subTest(update=update):
                    with self.assertRaisesRegex(ValueError, "AI 玩家|名字与槽位映射冲突"):
                        auto_runner.match_result_from_replay(
                            replay, 4, 2, 7, self.match, record,
                            evidence["recording"]["record_id"], evidence=evidence)

    def test_empty_ai_names_reject_missing_or_wrong_recording_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "real.aoe2record"
            record.write_bytes(b"record bytes")
            evidence = self._slot_evidence(record)
            for mutation in (
                    lambda value: value.pop("recording"),
                    lambda value: value["recording"].update(new_candidates=[str(record), str(record) + ".copy"]),
                    lambda value: value["recording"].update(new_since_snapshot=False)):
                altered = {**evidence, "recording": dict(evidence["recording"])}
                mutation(altered)
                with self.subTest(evidence=altered):
                    with self.assertRaisesRegex(ValueError, "证据|录像"):
                        auto_runner.match_result_from_replay(
                            self._empty_name_replay(), 4, 2, 7, self.match, record,
                            evidence["recording"]["record_id"], evidence=altered)

    def test_empty_ai_names_without_schedule_evidence_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "唯一映射"):
            self._result(self._empty_name_replay())

    def test_empty_ai_names_reject_ambiguous_or_incomplete_player_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "real.aoe2record"
            record.write_bytes(b"record bytes")
            evidence = self._slot_evidence(record)
            ambiguous = self._empty_name_replay()
            ambiguous["players"].append(dict(ambiguous["players"][0]))
            with self.assertRaisesRegex(ValueError, "槽位" ):
                auto_runner.match_result_from_replay(
                    ambiguous, 4, 2, 7, self.match, record,
                    evidence["recording"]["record_id"], evidence=evidence)

    def test_recording_association_rejects_multiple_new_candidates(self):
        before = {"old": {"size_bytes": 1, "mtime_ns": 1}}
        after = {
            "old": {"size_bytes": 1, "mtime_ns": 1},
            "new-a": {"size_bytes": 2, "mtime_ns": 2},
            "new-b": {"size_bytes": 3, "mtime_ns": 3},
        }
        with self.assertRaisesRegex(ValueError, "不唯一"):
            auto_runner.unique_new_recording_paths(before, after)

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
