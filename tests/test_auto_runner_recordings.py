import unittest
from unittest.mock import patch

from ai_lab import auto_runner


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class RecordingWaitTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.events = []
        self.before = {"old.aoe2record": {"size_bytes": 10, "mtime_ns": 1}}
        self.signature = {"size_bytes": 20, "mtime_ns": 2}

    def _wait(self, snapshots):
        path = object()
        with patch.object(auto_runner, "recording_snapshot", side_effect=snapshots), \
                patch.object(auto_runner, "list_recordings",
                             return_value={"new.aoe2record": path}):
            result = auto_runner.wait_new_recording(
                {}, self.before, 1, diagnostic=self.events.append,
                match_id="g0-m0001", poll_interval_s=5, settle_interval_s=3,
                clock=self.clock.now, sleep=self.clock.sleep)
        return result

    def test_one_stable_new_recording_is_associated_and_logged(self):
        result = self._wait([
            {**self.before, "new.aoe2record": self.signature},
            {**self.before, "new.aoe2record": self.signature},
        ])

        self.assertIsNotNone(result)
        poll = next(event for event in self.events if event["event"] == "recording_poll")
        settled = next(event for event in self.events
                       if event["event"] == "recording_settled_check")
        self.assertEqual(poll["new_keys"], ["new.aoe2record"])
        self.assertEqual(settled["first_stat"], self.signature)
        self.assertEqual(settled["settled_stat"], self.signature)
        self.assertTrue(settled["settled"])
        self.assertEqual(settled["match_id"], "g0-m0001")

    def test_growing_recording_retries_until_first_and_settled_stats_match(self):
        growing = {"size_bytes": 21, "mtime_ns": 3}
        self._wait([
            {**self.before, "new.aoe2record": self.signature},
            {**self.before, "new.aoe2record": growing},
            {**self.before, "new.aoe2record": growing},
            {**self.before, "new.aoe2record": growing},
        ])

        checks = [event for event in self.events
                  if event["event"] == "recording_settled_check"]
        self.assertEqual([event["settled"] for event in checks], [False, True])
        self.assertEqual(checks[0]["first_stat"], self.signature)
        self.assertEqual(checks[0]["settled_stat"], growing)
        self.assertEqual(checks[1]["first_stat"], growing)
        self.assertEqual(checks[1]["settled_stat"], growing)

    def test_multiple_new_paths_reject_and_emit_candidates(self):
        with patch.object(auto_runner, "recording_snapshot", return_value={
                **self.before,
                "new-a.aoe2record": self.signature,
                "new-b.aoe2record": {"size_bytes": 30, "mtime_ns": 3},
        }):
            with self.assertRaisesRegex(ValueError, "不唯一"):
                auto_runner.wait_new_recording(
                    {}, self.before, 1, diagnostic=self.events.append,
                    clock=self.clock.now, sleep=self.clock.sleep)

        rejected = next(event for event in self.events
                        if event["event"] == "recording_association_rejected")
        self.assertEqual(rejected["new_keys"],
                         ["new-a.aoe2record", "new-b.aoe2record"])

    def test_snapshot_exception_is_diagnosed_and_propagated(self):
        with patch.object(auto_runner, "recording_snapshot",
                          side_effect=PermissionError("access denied")):
            with self.assertRaisesRegex(PermissionError, "access denied"):
                auto_runner.wait_new_recording(
                    {}, self.before, 1, diagnostic=self.events.append,
                    clock=self.clock.now, sleep=self.clock.sleep)

        error = next(event for event in self.events
                     if event["event"] == "recording_snapshot_error")
        self.assertEqual(error["phase"], "poll")
        self.assertEqual(error["error_type"], "PermissionError")

    def test_per_file_stat_exception_is_logged_without_inventing_candidate(self):
        class MissingPath:
            def stat(self):
                raise PermissionError("sharing violation")

            def __str__(self):
                return "blocked.aoe2record"

        with patch.object(auto_runner, "list_recordings",
                          return_value={"blocked.aoe2record": MissingPath()}):
            snapshot = auto_runner.recording_snapshot(
                {}, diagnostic=self.events.append,
                diagnostic_context={"iteration": 4, "phase": "poll"})

        self.assertEqual(snapshot, {})
        error = next(event for event in self.events
                     if event["event"] == "recording_stat_error")
        self.assertEqual(error["iteration"], 4)
        self.assertEqual(error["path"], "blocked.aoe2record")
        self.assertEqual(error["error_type"], "PermissionError")


if __name__ == "__main__":
    unittest.main()
