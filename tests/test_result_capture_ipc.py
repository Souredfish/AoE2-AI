import json
import tempfile
import unittest
from pathlib import Path

from ai_lab.result_capture_ipc import (CaptureIPCRejected, append_capture_association,
                                       append_raw_capture,
                                       load_raw_capture, parse_ipc_envelope)


def envelope(payload=None, source=None):
    return {
        "type": "module_message",
        "pipeName": "EvoLabResultCaptureV1",
        "source": source or {
            "instanceId": 1,
            "assignedPlayerId": 1,
            "moduleName": "evolab_driver",
            "settingsGroup": "evolab_driver [P1]",
        },
        "payload": payload or ('EVOLAB_RESULT_CAPTURE_V1:{"schema_version":1,'
                                '"capture_sequence":1,"match_id":"g0-m0001"}'),
    }


class ResultCaptureIPCTests(unittest.TestCase):
    def association(self):
        return {
            "match_id": "g0-m0001",
            "slots_by_alias": {"A": 2, "B": 3},
            "installed": {"A": {"per_sha256": "a" * 64},
                          "B": {"per_sha256": "b" * 64}},
            "recording": {"path": "match.aoe2record", "record_id": "rec-1",
                          "sha256": "c" * 64},
        }

    def test_raw_payload_is_preserved_verbatim(self):
        raw = ('EVOLAB_RESULT_CAPTURE_V1:{ "schema_version" : 1, '
               '"capture_sequence": 1, "match_id":"g0-m0001" }')
        message = parse_ipc_envelope(json.dumps(envelope(raw)))
        self.assertEqual(message["raw_sentinel"], raw)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.jsonl"
            append_raw_capture(path, message, "g0-m0001")
            association = self.association()
            append_capture_association(path, association)
            row = load_raw_capture(path, "g0-m0001", association)
            self.assertEqual(row["raw_sentinel"], raw)
            self.assertEqual(row["source"]["moduleName"], "evolab_driver")
            self.assertEqual(row["runner_association"]["recording"]["sha256"], "c" * 64)

    def test_malformed_envelope_or_untrusted_source_is_rejected(self):
        with self.assertRaisesRegex(CaptureIPCRejected, "envelope"):
            parse_ipc_envelope("not json")
        bad = envelope(source={"moduleName": "other", "assignedPlayerId": 1})
        with self.assertRaisesRegex(CaptureIPCRejected, "来源"):
            parse_ipc_envelope(json.dumps(bad))

    def test_missing_or_malformed_raw_payload_is_rejected(self):
        with self.assertRaisesRegex(CaptureIPCRejected, "sentinel"):
            parse_ipc_envelope(json.dumps(envelope("other")))

    def test_wrong_match_id_or_duplicate_rows_are_rejected(self):
        message = parse_ipc_envelope(json.dumps(envelope()))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.jsonl"
            append_raw_capture(path, message, "g0-m0001")
            append_capture_association(path, self.association())
            with self.assertRaisesRegex(CaptureIPCRejected, "match_id"):
                load_raw_capture(path, "g0-m0002")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(path.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(CaptureIPCRejected, "必须且只能"):
                load_raw_capture(path, "g0-m0001")

    def test_association_mismatch_or_missing_association_is_rejected(self):
        message = parse_ipc_envelope(json.dumps(envelope()))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.jsonl"
            append_raw_capture(path, message, "g0-m0001")
            with self.assertRaisesRegex(CaptureIPCRejected, "必须且只能"):
                load_raw_capture(path, "g0-m0001")
            association = self.association()
            association["recording"]["sha256"] = "d" * 64
            append_capture_association(path, association)
            with self.assertRaisesRegex(CaptureIPCRejected, "不一致"):
                load_raw_capture(path, "g0-m0001", self.association())

    def test_capture_path_is_create_once_and_not_overwritten(self):
        message = parse_ipc_envelope(json.dumps(envelope()))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.jsonl"
            append_raw_capture(path, message, "g0-m0001")
            before = path.read_bytes()
            with self.assertRaisesRegex(CaptureIPCRejected, "已存在"):
                append_raw_capture(path, message, "g0-m0001")
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
