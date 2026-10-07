import json
import queue
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from ai_lab.result_capture_ipc import (CaptureIPCRejected, append_capture_association,
                                       append_raw_capture,
                                       load_raw_capture, parse_ipc_envelope,
                                       WindowsPipeCaptureReceiver, _binding_request,
                                       _capture_hello_request, _is_capture_ready,
                                       run_bounded_capture_io,
                                       _send_capture_hellos, CAPTURE_MODULE_BUILD)


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
    def test_capture_file_io_timeout_is_bounded_and_diagnostic(self):
        release = threading.Event()
        completed = threading.Event()
        events = []
        started = time.monotonic()

        def blocked_operation():
            release.wait()
            completed.set()

        try:
            with self.assertRaisesRegex(TimeoutError, "association append.*超时"):
                run_bounded_capture_io(
                    "association append", blocked_operation, timeout_s=0.02,
                    diagnostic=events.append)
        finally:
            release.set()

        self.assertTrue(completed.wait(1))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual([event["stage"] for event in events],
                         ["association append_started", "association append_timeout"])
        self.assertTrue(all("elapsed_s" in event for event in events))

    def test_bounded_capture_io_returns_result_and_keeps_capture_only_read_only(self):
        events = []
        self.assertEqual(run_bounded_capture_io(
            "raw capture validation", lambda: "verified", timeout_s=1,
            diagnostic=events.append), "verified")
        self.assertEqual([event["stage"] for event in events],
                         ["raw capture validation_started", "raw capture validation_finished"])
        from ai_lab import auto_runner
        self.assertFalse(auto_runner.result_commits_enabled(True))

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
            append_diagnostics = []
            append_capture_association(path, association, diagnostic=append_diagnostics.append)
            read_diagnostics = []
            row = load_raw_capture(path, "g0-m0001", association,
                                   diagnostic=read_diagnostics.append)
            self.assertEqual(row["raw_sentinel"], raw)
            self.assertEqual(row["source"]["moduleName"], "evolab_driver")
            self.assertEqual(row["runner_association"]["recording"]["sha256"], "c" * 64)
            append_stages = [event["stage"] for event in append_diagnostics]
            self.assertIn("association_os_open_append_started", append_stages)
            self.assertIn("association_append_write_finished", append_stages)
            self.assertIn("association_append_fsync_finished", append_stages)
            self.assertEqual(read_diagnostics[0]["stage"],
                             "raw_capture_validation_read_started")
            self.assertEqual(read_diagnostics[-1]["stage"],
                             "raw_capture_validation_read_finished")

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

    def test_binding_request_targets_the_assigned_module_and_match(self):
        self.assertEqual(_binding_request("g0-m0001"), {
            "target": {"assignedPlayerId": 1, "moduleName": "evolab_driver"},
            "payload": {"action": "bind_match", "match_id": "g0-m0001"},
        })
        with self.assertRaisesRegex(CaptureIPCRejected, "match_id"):
            _binding_request("")

    def test_server_ready_requires_supported_handshake_version(self):
        ready = envelope({"action": "capture_ready", "protocol_version": 1,
                          "module_build": CAPTURE_MODULE_BUILD})
        self.assertTrue(_is_capture_ready(ready))
        ready["payload"]["protocol_version"] = 2
        self.assertFalse(_is_capture_ready(ready))
        ready["payload"]["protocol_version"] = 1
        ready["payload"]["module_build"] = "stale-loaded-module"
        self.assertFalse(_is_capture_ready(ready))

    def test_client_repeats_hello_until_server_ready(self):
        ready = threading.Event()
        stop = threading.Event()
        sent = []

        def write(payload):
            sent.append(json.loads(payload))
            if len(sent) == 3:
                ready.set()

        attempts = _send_capture_hellos(write, ready, stop, interval_s=0)
        self.assertEqual(attempts, 3)
        self.assertEqual([item["payload"]["action"] for item in sent],
                         ["capture_hello"] * 3)
        self.assertEqual(sent[0], _capture_hello_request())
        self.assertEqual(sent[0]["target"], {
            "assignedPlayerId": 1, "moduleName": "evolab_driver"})
        self.assertEqual(sent[0]["payload"]["protocol_version"], 1)

    def test_binding_wait_times_out_without_confirmation(self):
        for phase in ("connecting", "waiting_for_control_ready", "waiting_for_match_bound"):
            receiver = WindowsPipeCaptureReceiver("unused.jsonl", "g0-m0001")
            receiver._handshake_phase = phase
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(CaptureIPCRejected,
                                            "握手阶段=" + phase):
                    receiver.wait_until_bound(0.001)

    def test_lua_logs_startserver_and_each_handshake_stage(self):
        source = (Path(__file__).parents[1] / "ai_lab/control/evolab_driver/"
                  "evolab_driver.main.lua").read_text(encoding="utf-8")
        for marker in (
                "lifecycle Load", "lifecycle Init", "IPC StartServer result",
                "Update polling started", "capture_ready queued after client hello",
                "module_build", "capture_update_heartbeat", "IPC.GetStats()",
                "IPC.HasMessages()", "IPC.WaitForMessage(1)",
                "IPC.WaitForMessage failed", "binding acknowledgement failed",
                "runner match_id bound", 'start_capture_ipc_server("Load")'):
            with self.subTest(marker=marker):
                self.assertIn(marker, source)
        receive_body = source.split("local function receive_capture_ipc_messages()", 1)[1].split(
            "\nlocal function start_capture_ipc_server", 1)[0]
        self.assertIn("pcall(function() return IPC.HasMessages() end)", receive_body)
        self.assertIn("pcall(function() return IPC.WaitForMessage(1) end)", receive_body)
        self.assertIn('type(raw) ~= "string"', receive_body)
        self.assertIn("received < 32", receive_body)
        self.assertIn("return nil, \"IPC.WaitForMessage failed:", receive_body)
        update_body = source.split("function Update()", 1)[1].split("\nfunction End(", 1)[0]
        self.assertNotIn("IPC.GetMessages()", source)
        self.assertIn("capture_ready", update_body)
        self.assertIn("receive_capture_ipc_messages()", update_body)
        self.assertIn("messages = {}", update_body)
        self.assertLess(update_body.index("if receive_error ~= nil"),
                        update_body.index('parsed.action == "capture_hello"'))
        init_body = source.split("function Init()", 1)[1].split("\nfunction Update(", 1)[0]
        self.assertNotIn("IPC.GetMessages()", init_body)

    def test_pipe_handshake_connect_ready_bind_ack_sequence(self):
        responses = queue.Queue()
        responses.put(json.dumps(envelope({
            "action": "capture_update_heartbeat",
            "module_build": CAPTURE_MODULE_BUILD,
            "update_count": 1,
            "hello_count": 0,
            "ipc_stats": {"receiveDroppedInvalidRouting": 0},
        })).encode("utf-8"))
        written = []
        events = []
        receiver = WindowsPipeCaptureReceiver(
            "unused.jsonl", "g0-m0001", diagnostic=events.append)

        class FakePipeError(Exception):
            def __init__(self, winerror):
                super().__init__(winerror)
                self.winerror = winerror

        def write_file(_handle, raw):
            message = json.loads(raw.decode("utf-8"))
            written.append(message)
            action = message["payload"]["action"]
            if action == "capture_hello":
                payload = {"action": "capture_ready", "protocol_version": 1,
                           "module_build": CAPTURE_MODULE_BUILD}
            else:
                payload = {"action": "match_bound", "match_id": "g0-m0001"}
            responses.put(json.dumps({
                "type": "module_message", "pipeName": "EvoLabResultCaptureV1",
                "source": {"moduleName": "evolab_driver", "assignedPlayerId": 1},
                "payload": payload,
            }).encode("utf-8"))

        def read_file(_handle, _size):
            raw = responses.get(timeout=1)
            envelope = json.loads(raw.decode("utf-8"))
            if envelope["payload"]["action"] == "match_bound":
                receiver._stop.set()
            return 0, raw

        fake_file = SimpleNamespace(
            GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3,
            CreateFile=lambda *_args: "fake-pipe", WriteFile=write_file,
            ReadFile=read_file, CloseHandle=lambda _handle: None,
        )
        fake_pipe = SimpleNamespace(
            PIPE_READMODE_MESSAGE=1,
            SetNamedPipeHandleState=lambda *_args: None,
        )
        fake_winerror = SimpleNamespace(error=FakePipeError)
        with patch.dict(sys.modules, {
                "pywintypes": fake_winerror,
                "win32file": fake_file,
                "win32pipe": fake_pipe,
        }):
            receiver._receive()
            receiver.stop()

        self.assertEqual(receiver.handshake_phase, "bound")
        self.assertEqual([m["payload"]["action"] for m in written],
                         ["capture_hello", "bind_match"])
        stages = [event["stage"] for event in events]
        for stage in ("pipe_connected", "capture_hello_sent", "capture_ready_received",
                      "control_update_heartbeat", "bind_match_sent",
                      "match_bound_received"):
            self.assertIn(stage, stages)
        self.assertLess(stages.index("pipe_connected"), stages.index("capture_ready_received"))
        self.assertLess(stages.index("capture_ready_received"), stages.index("bind_match_sent"))
        self.assertLess(stages.index("bind_match_sent"), stages.index("match_bound_received"))
        self.assertLess(stages.index("control_update_heartbeat"),
                        stages.index("capture_ready_received"))

    def test_stale_module_ready_is_rejected_before_binding(self):
        events = []
        receiver = WindowsPipeCaptureReceiver(
            "unused.jsonl", "g0-m0001", diagnostic=events.append)
        stale_ready = json.dumps(envelope({
            "action": "capture_ready", "protocol_version": 1,
            "module_build": "stale-loaded-module",
        })).encode("utf-8")

        class FakePipeError(Exception):
            def __init__(self, winerror):
                super().__init__(winerror)
                self.winerror = winerror

        fake_file = SimpleNamespace(
            GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3,
            CreateFile=lambda *_args: "fake-pipe",
            WriteFile=lambda *_args: (0, len(_args[-1])),
            ReadFile=lambda *_args: (0, stale_ready),
            CloseHandle=lambda _handle: None,
        )
        fake_pipe = SimpleNamespace(
            PIPE_READMODE_MESSAGE=1,
            SetNamedPipeHandleState=lambda *_args: None,
        )
        fake_winerror = SimpleNamespace(error=FakePipeError)
        with patch.dict(sys.modules, {
                "pywintypes": fake_winerror,
                "win32file": fake_file,
                "win32pipe": fake_pipe,
        }):
            receiver._receive()
            receiver.stop()

        self.assertIsInstance(receiver._error, CaptureIPCRejected)
        self.assertIn("模块构建标识不匹配", str(receiver._error))
        self.assertFalse(receiver._binding_sent)
        self.assertIn("capture_ready_rejected", [event["stage"] for event in events])

    def test_missing_server_reports_connect_timeout(self):
        events = []

        class FakePipeError(Exception):
            winerror = 2

        def missing_pipe(*_args):
            raise FakePipeError("pipe not found")

        fake_file = SimpleNamespace(
            GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3,
            CreateFile=missing_pipe,
        )
        fake_pipe = SimpleNamespace(PIPE_READMODE_MESSAGE=1)
        fake_winerror = SimpleNamespace(error=FakePipeError)
        receiver = WindowsPipeCaptureReceiver(
            "unused.jsonl", "g0-m0001", connect_timeout_s=0, diagnostic=events.append)
        with patch.dict(sys.modules, {
                "pywintypes": fake_winerror,
                "win32file": fake_file,
                "win32pipe": fake_pipe,
        }):
            receiver._receive()

        self.assertEqual(receiver.handshake_phase, "failed")
        self.assertTrue(any(e["stage"] == "connect_timeout" and e["winerror"] == 2
                            for e in events))
        self.assertTrue(any(e["stage"] == "handshake_failed"
                            and "TimeoutError" in e["error"] for e in events))


if __name__ == "__main__":
    unittest.main()
