import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from ai_lab import auto_runner
recordings = auto_runner.REC


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class RecordingWaitTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.events = []
        self.before = {"old.aoe2record": {"size_bytes": 10, "mtime_ns": 1}}
        self.signature = {"size_bytes": 20, "mtime_ns": 2}

    def _wait(self, snapshots, *, timeout_min=1, settle_interval_s=3,
              lookup=None):
        path = object()
        with patch.object(auto_runner, "recording_snapshot", side_effect=snapshots), \
                patch.object(auto_runner, "list_recordings",
                             side_effect=lookup or
                             (lambda _cfg: {"new.aoe2record": path})):
            result = auto_runner.wait_new_recording(
                {}, self.before, timeout_min, diagnostic=self.events.append,
                match_id="g0-m0001", poll_interval_s=5,
                settle_interval_s=settle_interval_s,
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

    def test_incomplete_pre_match_baseline_fails_closed(self):
        class UnreadablePath:
            def stat(self):
                raise PermissionError("sharing violation")

            def __str__(self):
                return "old.aoe2record"

        with patch.object(auto_runner, "list_recordings",
                          return_value={"old.aoe2record": UnreadablePath()}):
            with self.assertRaisesRegex(OSError, "基线|快照"):
                auto_runner.recording_snapshot(
                    {}, diagnostic=self.events.append,
                    diagnostic_context={"phase": "baseline"}, strict=True)

        self.assertEqual(self.events[0]["event"], "recording_stat_error")
        self.assertEqual(self.events[0]["phase"], "baseline")

    def test_locked_candidate_cannot_switch_after_disappearing(self):
        path_a = "new-a.aoe2record"
        path_b = "new-b.aoe2record"
        with patch.object(auto_runner, "recording_snapshot", side_effect=[
                {**self.before, path_a: self.signature},
                dict(self.before),
                {**self.before, path_b: self.signature},
        ]):
            with patch.object(auto_runner, "list_recordings", return_value={}):
                with self.assertRaisesRegex(ValueError, "拒绝切换"):
                    auto_runner.wait_new_recording(
                        {}, self.before, 1, diagnostic=self.events.append,
                        clock=self.clock.now, sleep=self.clock.sleep,
                        poll_interval_s=1, settle_interval_s=1)

        locked = next(event for event in self.events
                      if event["event"] == "recording_candidate_locked")
        rejected = next(event for event in self.events
                        if event["event"] == "recording_association_rejected")
        self.assertEqual(locked["candidate"], path_a)
        self.assertEqual(rejected["candidate"], path_a)
        self.assertEqual(rejected["new_keys"], [path_b])

    def test_settle_finishing_at_deadline_does_not_return_candidate(self):
        path = object()
        with patch.object(auto_runner, "recording_snapshot", return_value={
                **self.before, "new.aoe2record": self.signature,
        }) as snapshot, patch.object(auto_runner, "list_recordings",
                                    return_value={"new.aoe2record": path}) as lookup:
            result = auto_runner.wait_new_recording(
                {}, self.before, 0.1, diagnostic=self.events.append,
                clock=self.clock.now, sleep=self.clock.sleep,
                poll_interval_s=1, settle_interval_s=6)

        self.assertIsNone(result)
        self.assertEqual(snapshot.call_count, 1)
        lookup.assert_not_called()
        deadline = next(event for event in self.events
                        if event["event"] == "recording_deadline_expired")
        self.assertEqual(deadline["phase"], "after_settle_wait")

    def test_settle_sleep_is_clamped_to_two_seconds_remaining(self):
        path = object()

        def discover_candidate(_cfg, **_kwargs):
            # timeout_min=0.1 gives a six-second deadline; discovery itself
            # consumes four seconds, leaving exactly two before settle.
            self.clock.value = 4
            return {**self.before, "new.aoe2record": self.signature}

        with patch.object(auto_runner, "recording_snapshot",
                          side_effect=discover_candidate) as snapshot, \
                patch.object(auto_runner, "list_recordings",
                             return_value={"new.aoe2record": path}) as lookup:
            result = auto_runner.wait_new_recording(
                {}, self.before, 0.1, diagnostic=self.events.append,
                clock=self.clock.now, sleep=self.clock.sleep,
                poll_interval_s=5, settle_interval_s=6)

        self.assertIsNone(result)
        self.assertEqual(self.clock.sleeps, [2])
        self.assertEqual(self.clock.now(), 6)
        self.assertEqual(snapshot.call_count, 1)
        lookup.assert_not_called()
        deadline = next(event for event in self.events
                        if event["event"] == "recording_deadline_expired")
        self.assertEqual(deadline["phase"], "after_settle_wait")

    def test_deadline_is_rechecked_after_candidate_lookup_before_return(self):
        path = object()

        def lookup(_cfg):
            self.clock.value = 60
            return {"new.aoe2record": path}

        result = self._wait([
            {**self.before, "new.aoe2record": self.signature},
            {**self.before, "new.aoe2record": self.signature},
        ], lookup=lookup)

        self.assertIsNone(result)
        deadline = next(event for event in self.events
                        if event["event"] == "recording_deadline_expired")
        self.assertEqual(deadline["phase"], "before_return")

    def test_deadline_is_checked_immediately_before_return(self):
        path = object()

        def diagnostic(event):
            self.events.append(event)
            if event["event"] == "recording_associated_candidate":
                self.clock.value = 60

        with patch.object(auto_runner, "recording_snapshot", side_effect=[
                {**self.before, "new.aoe2record": self.signature},
                {**self.before, "new.aoe2record": self.signature},
        ]), patch.object(auto_runner, "list_recordings",
                         return_value={"new.aoe2record": path}):
            result = auto_runner.wait_new_recording(
                {}, self.before, 1, diagnostic=diagnostic,
                clock=self.clock.now, sleep=self.clock.sleep,
                poll_interval_s=1, settle_interval_s=1)

        self.assertIsNone(result)
        deadline = next(event for event in self.events
                        if event["event"] == "recording_deadline_expired")
        self.assertEqual(deadline["phase"], "immediately_before_return")

    def test_poll_sleep_is_clamped_to_remaining_deadline(self):
        with patch.object(auto_runner, "recording_snapshot",
                          return_value=dict(self.before)):
            result = auto_runner.wait_new_recording(
                {}, self.before, 0.1, diagnostic=self.events.append,
                clock=self.clock.now, sleep=self.clock.sleep,
                poll_interval_s=5)

        self.assertIsNone(result)
        self.assertEqual(self.clock.sleeps, [5, 1])
        self.assertEqual(self.clock.now(), 6)

    def test_missing_expected_directory_is_not_an_enumeration_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "not-created"
            found = recordings.list_recordings(
                {"game": {"recordings_dir": str(missing)}},
                environ={}, strict=True, diagnostic=self.events.append)

        self.assertEqual(found, {})
        self.assertEqual(self.events, [])

    def test_configured_directory_enumeration_failure_rejects_strict_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = Path(tmp) / "recordings"
            expected.mkdir()
            with patch.object(recordings.os, "scandir",
                              side_effect=PermissionError("access denied")):
                with self.assertRaisesRegex(
                        recordings.RecordingDirectoryError, "enumerate"):
                    auto_runner.recording_snapshot(
                        {"game": {"recordings_dir": str(expected)}},
                        strict=True, diagnostic=self.events.append,
                        diagnostic_context={"phase": "baseline"})

        event = next(event for event in self.events
                     if event["event"] == "recording_directory_error")
        self.assertEqual(event["phase"], "baseline")
        self.assertEqual(event["operation"], "enumerate")
        self.assertEqual(event["error_type"], "PermissionError")

    def test_existing_profile_root_enumeration_failure_rejects_strict_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            expected_root = profile / "Games" / "Age of Empires 2 DE"
            expected_root.mkdir(parents=True)

            def fail_expected_root(path):
                if Path(path) == expected_root:
                    raise PermissionError("profile root cannot be enumerated")
                return iter(())

            with patch.object(recordings.os, "scandir", side_effect=fail_expected_root):
                with self.assertRaisesRegex(
                        recordings.RecordingDirectoryError, "enumerate"):
                    recordings.list_recordings(
                        {}, environ={"USERPROFILE": str(profile)}, strict=True,
                        diagnostic=self.events.append,
                        diagnostic_context={"phase": "baseline"})

        event = next(event for event in self.events
                     if event["event"] == "recording_directory_error")
        self.assertEqual(Path(event["path"]), expected_root)
        self.assertEqual(event["error_type"], "PermissionError")


if __name__ == "__main__":
    unittest.main()
