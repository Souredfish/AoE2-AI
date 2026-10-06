import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_lab import auto_runner


class WindowReadinessTests(unittest.TestCase):
    def _window(self, hwnd=1, visible=True, minimized=False, width=1382, height=807):
        return {
            "hwnd": hwnd, "title": "Age of Empires II: Definitive Edition",
            "visible": visible, "minimized": minimized,
            "width": width, "height": height,
        }

    def test_game_window_gate_rejects_unready_or_ambiguous_main_window(self):
        cases = [
            ([self._window(visible=False)], "不可见"),
            ([self._window(minimized=True, width=160, height=28)], "最小化"),
            ([self._window(width=500, height=300)], "尺寸小于"),
            ([self._window(visible=False), self._window(hwnd=2)], "识别不唯一"),
            ([self._window(minimized=True, width=160, height=28),
              self._window(hwnd=2)], "识别不唯一"),
        ]
        for windows, expected_reason in cases:
            with self.subTest(windows=windows):
                ready, reason = auto_runner.evaluate_game_windows(windows)
                self.assertFalse(ready)
                self.assertIn(expected_reason, reason)

    def test_game_window_gate_accepts_only_one_ready_main_window(self):
        ready, reason = auto_runner.evaluate_game_windows([self._window()])
        self.assertTrue(ready)
        self.assertIn("hwnd=0x1", reason)

    def _wait(self, states, timeout=4):
        now = [0.0]

        def probe():
            return states.pop(0) if len(states) > 1 else states[0]

        def sleep(seconds):
            now[0] += seconds

        return auto_runner.wait_for_game_window(
            timeout, window_probe=probe, poll_interval_s=1,
            monotonic=lambda: now[0], sleep=sleep)

    def test_missing_minimized_and_small_windows_are_not_ready(self):
        for reason in ("窗口不存在或不可见", "游戏窗口已最小化", "尺寸小于"):
            with self.subTest(reason=reason):
                self.assertIn(reason, self._wait([(False, reason)], timeout=0)[1])

    def test_waits_until_window_recovers(self):
        states = [(False, "最小化"), (False, "尺寸过小"), (True, "ready")]
        self.assertEqual(self._wait(states), (True, "ready"))

    def test_timeout_reports_last_window_reason(self):
        ready, reason = self._wait([(False, "窗口不可见")], timeout=2)
        self.assertFalse(ready)
        self.assertIn("超时", reason)
        self.assertIn("窗口不可见", reason)

    def test_control_runs_only_after_ready_and_exit_zero_is_required(self):
        with tempfile.TemporaryDirectory() as temp:
            launcher = Path(temp) / "launcher.exe"
            launcher.touch()
            cfg = {"control": {"launcher": str(launcher)}}
            probe = iter([(False, "最小化"), (True, "ready")])
            runner = unittest.mock.Mock(return_value=SimpleNamespace(returncode=0, stdout="Ready"))
            sleeps = []
            with patch.object(auto_runner, "game_running", return_value=True):
                result = auto_runner.ensure_control(
                    cfg, max_wait_s=10, window_probe=lambda: next(probe),
                    control_runner=runner, monotonic=lambda: 1,
                    sleep=sleeps.append)
            self.assertTrue(result)
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(sleeps, [2])

    def test_documented_headless_success_statuses_are_recognized(self):
        for status in auto_runner.CONTROL_SUCCESS_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(
                    auto_runner.control_terminal_status("v1.1.0\nScanning...\n" + status),
                    status,
                )

    def test_unknown_or_missing_headless_status_is_not_recognized(self):
        for stdout in ("v1.1.0\nScanning...\nUnexpected", "", "\n  "):
            with self.subTest(stdout=stdout):
                self.assertIsNone(auto_runner.control_terminal_status(stdout))

    def test_exit_zero_without_documented_success_status_is_rejected(self):
        for stdout in ("v1.1.0\nScanning...\nUnexpected", ""):
            with self.subTest(stdout=stdout), tempfile.TemporaryDirectory() as temp:
                launcher = Path(temp) / "launcher.exe"
                launcher.touch()
                cfg = {"control": {"launcher": str(launcher)}}
                runner = unittest.mock.Mock(
                    return_value=SimpleNamespace(returncode=0, stdout=stdout))
                with patch.object(auto_runner, "game_running", return_value=True):
                    result = auto_runner.ensure_control(
                        cfg, max_wait_s=10, window_probe=lambda: (True, "ready"),
                        control_runner=runner, monotonic=lambda: 1,
                        sleep=lambda _: None)
                self.assertFalse(result)
                self.assertEqual(runner.call_count, 1)

    def test_headless_exit_seven_never_counts_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            launcher = Path(temp) / "launcher.exe"
            launcher.touch()
            cfg = {"control": {"launcher": str(launcher)}}
            runner = unittest.mock.Mock(return_value=SimpleNamespace(returncode=7, stdout="not ready"))
            ticks = iter([0, 0, 1, 1, 3, 3, 5, 5])
            with patch.object(auto_runner, "game_running", return_value=True):
                result = auto_runner.ensure_control(
                    cfg, max_wait_s=4, window_probe=lambda: (True, "ready"),
                    control_runner=runner, monotonic=lambda: next(ticks), sleep=lambda _: None)
            self.assertFalse(result)
            self.assertGreater(runner.call_count, 0)


if __name__ == "__main__":
    unittest.main()
