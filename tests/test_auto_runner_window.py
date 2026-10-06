import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_lab import auto_runner


class WindowReadinessTests(unittest.TestCase):
    GAME_EXE = r"C:\Steam\steamapps\common\AoE2DE\AoE2DE_s.exe"

    def _window(self, hwnd=1, pid=20, visible=True, minimized=False,
                width=1382, height=807):
        return {
            "hwnd": hwnd,
            "pid": pid,
            "title": "Age of Empires II: Definitive Edition",
            "class": auto_runner.GAME_MAIN_WINDOW_CLASS,
            "visible": visible, "minimized": minimized,
            "width": width, "height": height,
        }

    def _steam_processes(self):
        return [
            {"Name": "steam.exe", "ProcessId": 10, "ParentProcessId": 1,
             "ExecutablePath": r"C:\Steam\steam.exe"},
            {"Name": "AoE2DE_s.exe", "ProcessId": 20, "ParentProcessId": 10,
             "ExecutablePath": self.GAME_EXE},
        ]

    def _cfg(self, launcher):
        return {"control": {"launcher": str(launcher)}, "game": {
            "install_dir": r"C:\Steam\steamapps\common\AoE2DE",
            "exe": "AoE2DE_s.exe",
        }}

    def test_game_window_gate_ignores_directshow_helper_windows(self):
        main = self._window()
        helpers = [
            {"hwnd": 2, "title": "EVR Fullscreen Window", "class": "EVRFullscreenVideo",
             "visible": False, "minimized": False, "width": 400, "height": 300},
            {"hwnd": 3, "title": "ActiveMovie Window", "class": "FilterGraphWindow",
             "visible": False, "minimized": False, "width": 320, "height": 240},
        ]
        ready, reason = auto_runner.evaluate_game_windows([*helpers, main])
        self.assertTrue(ready)
        self.assertIn("hwnd=0x1", reason)

    def test_game_window_gate_rejects_unready_or_ambiguous_main_window(self):
        cases = [
            ([self._window(visible=False)], "同类游戏窗口均隐藏"),
            ([self._window(minimized=True, width=160, height=28)], "最小化"),
            ([self._window(width=500, height=300)], "尺寸未达到"),
            ([self._window(), {**self._window(hwnd=2), "title": "Second game window"}],
             "真正就绪的游戏主窗口不唯一"),
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

    def test_startup_splash_alone_cannot_pass_ready_gate(self):
        ready, reason = auto_runner.evaluate_game_windows([
            self._window(hwnd=2, width=640, height=480)])
        self.assertFalse(ready)
        self.assertIn("仅检测到启动 splash", reason)

    def test_main_window_with_splash_and_hidden_window_passes(self):
        main = self._window(hwnd=1, pid=20, width=1920, height=1080)
        splash = self._window(hwnd=2, pid=20, width=640, height=480)
        hidden = self._window(hwnd=3, pid=20, visible=False, width=1382, height=807)
        ready, reason = auto_runner.evaluate_game_windows([splash, hidden, main])
        self.assertTrue(ready)
        self.assertIn("pid=20 hwnd=0x1", reason)
        self.assertIn("忽略 2 个非就绪同类窗", reason)

    def test_hidden_and_minimized_windows_do_not_create_ambiguity(self):
        windows = [self._window(hwnd=1),
                   self._window(hwnd=2, visible=False),
                   self._window(hwnd=3, minimized=True)]
        ready, _ = auto_runner.evaluate_game_windows(windows)
        self.assertTrue(ready)

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
            cfg = self._cfg(launcher)
            probe = iter([(False, "最小化"), (True, "ready")])
            runner = unittest.mock.Mock(return_value=SimpleNamespace(returncode=0, stdout="Ready"))
            sleeps = []
            result = auto_runner.ensure_control(
                cfg, max_wait_s=10, window_probe=lambda pids: next(probe),
                control_runner=runner, monotonic=lambda: 1,
                sleep=sleeps.append, process_probe=self._steam_processes)
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
                cfg = self._cfg(launcher)
                runner = unittest.mock.Mock(
                    return_value=SimpleNamespace(returncode=0, stdout=stdout))
                result = auto_runner.ensure_control(
                    cfg, max_wait_s=10, window_probe=lambda pids: (True, "ready"),
                    control_runner=runner, monotonic=lambda: 1,
                    sleep=lambda _: None, process_probe=self._steam_processes)
                self.assertFalse(result)
                self.assertEqual(runner.call_count, 1)

    def test_headless_exit_seven_never_counts_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            launcher = Path(temp) / "launcher.exe"
            launcher.touch()
            cfg = self._cfg(launcher)
            runner = unittest.mock.Mock(return_value=SimpleNamespace(returncode=7, stdout="not ready"))
            ticks = iter([0, 0, 1, 1, 3, 3, 5, 5])
            result = auto_runner.ensure_control(
                cfg, max_wait_s=4, window_probe=lambda pids: (True, "ready"),
                control_runner=runner, monotonic=lambda: next(ticks), sleep=lambda _: None,
                process_probe=self._steam_processes)
            self.assertFalse(result)
            self.assertGreater(runner.call_count, 0)

    def test_only_steam_started_process_at_configured_path_is_accepted(self):
        processes = self._steam_processes() + [
            {"Name": "AoE2DE_s.exe", "ProcessId": 30, "ParentProcessId": 999,
             "ExecutablePath": self.GAME_EXE},
            {"Name": "AoE2DE_s.exe", "ProcessId": 40, "ParentProcessId": 10,
             "ExecutablePath": r"C:\Other\AoE2DE_s.exe"},
        ]
        self.assertEqual(auto_runner.steam_launched_game_pids(
            processes, self.GAME_EXE), {20})

    def test_runner_ignores_direct_transient_and_waits_for_steam_game_pid(self):
        with tempfile.TemporaryDirectory() as temp:
            launcher = Path(temp) / "launcher.exe"
            launcher.touch()
            cfg = self._cfg(launcher)
            direct = [{"Name": "AoE2DE_s.exe", "ProcessId": 30, "ParentProcessId": 999,
                       "ExecutablePath": self.GAME_EXE}]
            steam_game = self._steam_processes()
            snapshots = iter([direct, direct + steam_game, direct + steam_game])
            seen_pids = []
            runner = unittest.mock.Mock(
                return_value=SimpleNamespace(returncode=0, stdout="Ready"))
            launch = unittest.mock.Mock()
            ready = auto_runner.ensure_control(
                cfg, max_wait_s=10,
                window_probe=lambda pids: (seen_pids.append(set(pids)) or (True, "ready")),
                control_runner=runner, monotonic=lambda: 1, sleep=lambda _: None,
                process_probe=lambda: next(snapshots), game_launcher=launch)
            self.assertTrue(ready)
            launch.assert_called_once_with(cfg)
            self.assertEqual(seen_pids, [{20}])

    def test_steam_launcher_uses_app_id_without_skipintro(self):
        cfg = {"game": {"install_dir": r"C:\Steam\steamapps\common\AoE2DE",
                        "exe": "AoE2DE_s.exe"}}
        steam_exe = Path("C:/Steam/steam.exe")
        with patch.object(auto_runner, "resolve_steam_exe", return_value=steam_exe), \
                patch.object(auto_runner.subprocess, "Popen") as popen:
            auto_runner.start_game(cfg)
        popen.assert_called_once_with(
            [str(steam_exe), "-applaunch", "813780"], cwd=str(steam_exe.parent))


if __name__ == "__main__":
    unittest.main()
