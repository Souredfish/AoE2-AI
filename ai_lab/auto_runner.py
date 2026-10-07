# -*- coding: utf-8 -*-
"""
auto_runner.py — 全自动跑局（AoE2Control 驱动）
================================================
无人值守地跑完一整代（或指定数量）的对局：

  python auto_runner.py              # 跑完当前代的全部赛程
  python auto_runner.py --matches 4  # 只跑 4 场（调试用）

原理：
  1. 游戏内由 AoE2Control 的 evolab_driver 模块驱动（挂在玩家1）：
     自动建房 → P1 自毁成观察者 → 结束后自动开下一局，循环不息。
  2. 对局双方 AI 固定为游戏目录里的 EvoAI_A / EvoAI_B（大厅里选一次即可），
     本脚本在每局间隙改写这两个 .per 文件的内容来切换对战组合——
     槽位名不变，基因内容在变。
  3. 每局结束的 .aoe2record 录像用 mgz 解析出胜者，写进 evolve 的积分账本。

前置条件（一次性，详见 README「全自动模式」）：
  - 大厅建过一次房：P2=EvoAI_A、P3=EvoAI_B，之后全自动沿用该配置
  - CONTROL 菜单里把 evolab_driver 指派给 Player 1（只做一次，settings.ini 会记住）
  - 关闭 Multithreading / Tournament Mode（否则 Dispatch* 被拒绝）
"""

import argparse
import hashlib
import json
import math
import ntpath
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
import genome as G  # noqa: E402
import make_ai as MK  # noqa: E402
import report as RPT  # noqa: E402
import evolve as EV  # noqa: E402
import recordings as REC  # noqa: E402
from result_capture_ipc import (CaptureIPCRejected, WindowsPipeCaptureReceiver,
                                append_capture_association, load_raw_capture,
                                raw_capture_path)  # noqa: E402

LAB = ROOT / "lab_data"
CONTROL_CFG = Path.home().parent.parent / "AppData/Roaming"  # %APPDATA%
APPDATA = Path.home().drive and Path(str(Path.home()).split("Users")[0] + "Users")  # unused fallback
AI_PLAYER_SLOT_BY_ALIAS = {"A": 2, "B": 3}
AI_USER_ID = 0xFFFFFFFF


def appdata():
    import os
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))


def load_config():
    with open(ROOT / "config.json", "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------
# 部署：CONTROL 启动器 + evolab_driver 模块
# ------------------------------------------------------------------
def deploy_module(cfg):
    src = Path(__file__).parent / "control" / "evolab_driver"
    dst = appdata() / "CONTROL" / "AoE2Control" / "modules" / "evolab_driver"
    dst.mkdir(parents=True, exist_ok=True)
    source_file = src / "evolab_driver.main.lua"
    deployed_file = dst / "evolab_driver.main.lua"
    shutil.copy2(source_file, deployed_file)
    source_sha256 = hashlib.sha256(source_file.read_bytes()).hexdigest()
    deployed_sha256 = hashlib.sha256(deployed_file.read_bytes()).hexdigest()
    if source_sha256 != deployed_sha256:
        raise OSError("CONTROL Lua 部署后哈希不一致，拒绝启动 capture PoC")
    print("[部署] evolab_driver.lua sha256=%s verified=%s" % (
        source_sha256, deployed_sha256 == source_sha256))
    return dst


def check_module_assigned():
    """settings.ini 里应能找到模块名（说明已指派给某个玩家槽位）。"""
    ini = appdata() / "CONTROL" / "AoE2Control" / "settings.ini"
    if not ini.exists():
        return False, "settings.ini 还不存在（CONTROL 首次启动后才会生成）"
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except Exception:
        text = ini.read_text(encoding="latin-1", errors="replace")
    if "evolab_driver" in text:
        return True, ""
    return False, "settings.ini 里没找到 evolab_driver（模块还没指派给玩家1）"


# ------------------------------------------------------------------
# 游戏与 CONTROL 生命周期
# ------------------------------------------------------------------
STEAM_APP_ID = "813780"
# Transient argument passed only by this runner invocation. Never write Steam's
# per-user LaunchOptions, which would also affect ordinary library launches.
STEAM_RUNNER_LAUNCH_ARGS = ("SKIPINTRO",)


def resolve_steam_exe(cfg):
    """Find Steam from config, the game library path, or its registry entry."""
    game_cfg = cfg["game"]
    explicit = game_cfg.get("steam_exe")
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_file() else None
    install_dir = Path(game_cfg["install_dir"])
    for parent in (install_dir, *install_dir.parents):
        candidate = parent / "steam.exe"
        if candidate.is_file():
            return candidate
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
            candidate = Path(steam_path) / "steam.exe"
            return candidate if candidate.is_file() else None
        except (OSError, ImportError):
            pass
    return None


def start_game(cfg):
    """Start AoE2 through Steam with this runner's transient intro-skip arg."""
    steam_exe = resolve_steam_exe(cfg)
    if steam_exe is None:
        raise FileNotFoundError("未找到 Steam 客户端；请配置 game.steam_exe")
    app_id = str(cfg["game"].get("steam_app_id", STEAM_APP_ID))
    if app_id != STEAM_APP_ID:
        raise ValueError("AoE2 DE Steam App ID 不匹配: %s" % app_id)
    command = [str(steam_exe), "-applaunch", app_id, *STEAM_RUNNER_LAUNCH_ARGS]
    print("[启动] 通过 Steam -applaunch %s 启动游戏（runner 临时参数：%s）" % (
        app_id, " ".join(STEAM_RUNNER_LAUNCH_ARGS)))
    return subprocess.Popen(command, cwd=str(steam_exe.parent))


def game_process_snapshot():
    """Read process ancestry so a transient direct game launch cannot pass readiness."""
    if sys.platform != "win32":
        raise OSError("Steam 启动进程探测仅支持 Windows")
    command = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -in @('AoE2DE_s.exe','steam.exe') } | "
        "Select-Object Name,ProcessId,ParentProcessId,ExecutablePath | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    if result.returncode != 0:
        raise OSError("无法读取 Steam/游戏进程关系: %s" % (result.stderr or result.stdout))
    try:
        raw = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise OSError("Steam/游戏进程探测返回无效 JSON") from exc
    return raw if isinstance(raw, list) else [raw]


def steam_launched_game_pids(processes, game_exe):
    """Select game processes matching the configured binary with a Steam ancestor."""
    by_pid = {}
    for proc in processes:
        try:
            by_pid[int(proc["ProcessId"])] = proc
        except (KeyError, TypeError, ValueError):
            continue
    expected_path = ntpath.normcase(ntpath.normpath(str(game_exe)))
    steam_pids = {pid for pid, proc in by_pid.items()
                  if str(proc.get("Name", "")).lower() == "steam.exe"}
    result = set()
    for pid, proc in by_pid.items():
        if str(proc.get("Name", "")).lower() != "aoe2de_s.exe":
            continue
        actual_path = ntpath.normcase(ntpath.normpath(str(proc.get("ExecutablePath") or "")))
        if actual_path != expected_path:
            continue
        seen = set()
        parent = proc.get("ParentProcessId")
        while parent is not None:
            try:
                parent = int(parent)
            except (TypeError, ValueError):
                break
            if parent in steam_pids:
                result.add(pid)
                break
            if parent in seen or parent not in by_pid:
                break
            seen.add(parent)
            parent = by_pid[parent].get("ParentProcessId")
    return result


WINDOW_MIN_WIDTH = 641
WINDOW_MIN_HEIGHT = 481
WINDOW_SPLASH_MAX_WIDTH = 640
WINDOW_SPLASH_MAX_HEIGHT = 480
GAME_MAIN_WINDOW_CLASS = "Age of Empires II: Definitive Edition"
CONTROL_SUCCESS_STATUSES = {
    "Ready",
    "Ready - Partially outdated",
    "Ready - Requires update",
    "Already Running!",
}


def evaluate_game_windows(windows):
    """Accept one visible full-size game main, excluding hidden windows and splash."""
    class_windows = [window for window in windows
                     if window.get("class") == GAME_MAIN_WINDOW_CLASS]
    if not class_windows:
        return False, "未找到游戏主窗口类 %r（枚举到 %d 个标题窗口）" % (
            GAME_MAIN_WINDOW_CLASS, len(windows))
    visible = [window for window in class_windows if window.get("visible")]
    restored = [window for window in visible if not window.get("minimized")]
    ready = [window for window in restored
             if window.get("width", 0) >= WINDOW_MIN_WIDTH
             and window.get("height", 0) >= WINDOW_MIN_HEIGHT]

    def details(items):
        return "; ".join(
            "pid=%s hwnd=0x%x visible=%s minimized=%s size=%dx%d title=%r class=%r" %
            (w.get("pid", "?"), w["hwnd"], w.get("visible"), w.get("minimized"),
             w.get("width", 0), w.get("height", 0), w.get("title", ""),
             w.get("class", ""))
            for w in items)

    if len(ready) > 1:
        return False, "真正就绪的游戏主窗口不唯一（%d 个：%s）" % (
            len(ready), details(ready))
    if len(ready) == 1:
        return True, "游戏主窗口就绪（%s；忽略 %d 个非就绪同类窗）" % (
            details(ready), len(class_windows) - 1)
    if not visible:
        return False, "同类游戏窗口均隐藏（%s）" % details(class_windows)
    if not restored:
        return False, "同类游戏窗口均已最小化（%s）" % details(visible)
    splash = [window for window in restored
              if window.get("width", 0) == WINDOW_SPLASH_MAX_WIDTH
              and window.get("height", 0) == WINDOW_SPLASH_MAX_HEIGHT]
    if splash and len(splash) == len(restored):
        return False, "仅检测到启动 splash，游戏主窗口尚未就绪（%s）" % details(splash)
    return False, "游戏主窗口尺寸未达到 %dx%d（%s）" % (
        WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT, details(restored))


def control_terminal_status(stdout):
    """Return a documented Headless terminal status, or None if unknown."""
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    status = lines[-1]
    return status if status in CONTROL_SUCCESS_STATUSES else None


def game_window_status(game_pids=None):
    """Check that one visible, restored AoE2 main window is ready on Windows."""
    if sys.platform != "win32":
        return False, "窗口状态探测仅支持 Windows"
    try:
        import ctypes
        from ctypes import wintypes

        if game_pids is None:
            game_cfg = load_config()["game"]
            game_exe = Path(game_cfg["install_dir"]) / game_cfg["exe"]
            game_pids = steam_launched_game_pids(game_process_snapshot(), game_exe)
        pids = set(game_pids)
        if not pids:
            return False, "未找到 AoE2DE_s.exe 进程"

        user32 = ctypes.windll.user32
        windows = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def visit(hwnd, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and not user32.GetWindow(hwnd, 4):  # GW_OWNER
                rect = wintypes.RECT()
                title = ctypes.create_unicode_buffer(512)
                class_name = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, len(title))
                user32.GetClassNameW(hwnd, class_name, len(class_name))
                if title.value and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    windows.append({
                        "hwnd": int(hwnd), "pid": int(pid.value),
                        "title": title.value, "class": class_name.value,
                        "visible": bool(user32.IsWindowVisible(hwnd)),
                        "minimized": bool(user32.IsIconic(hwnd)),
                        "width": rect.right - rect.left, "height": rect.bottom - rect.top,
                    })
            return True

        user32.EnumWindows(visit, 0)
        return evaluate_game_windows(windows)
    except Exception as exc:
        return False, "窗口探测失败: %s" % exc


def wait_for_game_window(timeout_s, window_probe=game_window_status,
                         poll_interval_s=2, monotonic=time.monotonic, sleep=time.sleep):
    deadline = monotonic() + timeout_s
    last_reason = "尚未探测"
    while True:
        ready, last_reason = window_probe()
        if ready:
            return True, last_reason
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False, "等待游戏窗口就绪超时（%s）" % last_reason
        sleep(min(poll_interval_s, remaining))


def ensure_control(cfg, max_wait_s=600, window_probe=game_window_status,
                   control_runner=subprocess.run, monotonic=time.monotonic, sleep=time.sleep,
                   process_probe=game_process_snapshot, game_launcher=start_game):
    """确保游戏运行且 CONTROL 就绪。返回 True/False。"""
    launcher = Path(cfg["control"]["launcher"])
    if not launcher.is_absolute():
        launcher = ROOT / launcher
    launcher = launcher.resolve()
    if not launcher.exists():
        print("[错误] 找不到 AoE2Control 启动器: %s" % launcher)
        return False

    game_exe = Path(cfg["game"]["install_dir"]) / cfg["game"]["exe"]
    try:
        existing_pids = steam_launched_game_pids(process_probe(), game_exe)
    except (OSError, ValueError) as exc:
        print("[错误] 无法确认 Steam 启动的游戏进程: %s" % exc)
        return False
    if not existing_pids:
        try:
            launch_process = game_launcher(cfg)
        except (OSError, ValueError) as exc:
            print("[错误] Steam 启动失败: %s" % exc)
            return False
        launch_pid = getattr(launch_process, "pid", None)
        print("[等待] Steam launcher_pid=%s；等待 Steam 启动最终游戏进程（有界）..." %
              (launch_pid if launch_pid is not None else "unknown"))

    deadline = monotonic() + max_wait_s
    last_window_reason = "未检测"
    while monotonic() < deadline:
        try:
            game_pids = steam_launched_game_pids(process_probe(), game_exe)
        except (OSError, ValueError) as exc:
            print("[错误] Steam/游戏进程探测失败: %s" % exc)
            return False
        print("[Steam进程探测] 候选最终游戏 PID=%s" % sorted(game_pids))
        if not game_pids:
            sleep(min(2, max(0, deadline - monotonic())))
            continue
        ready, last_window_reason = window_probe(game_pids)
        print("[窗口就绪探测] pids=%s ready=%s reason=%s" % (
            sorted(game_pids), ready, last_window_reason))
        if not ready:
            sleep(min(2, max(0, deadline - monotonic())))
            continue
        proc = control_runner(
            [str(launcher), "--headless", "--timeout-ms", "120000"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180)
        out = proc.stdout or ""
        last = [l for l in out.splitlines() if l.strip()]
        print("[CONTROL] %s (exit=%d)" % (last[-1] if last else "无输出", proc.returncode))
        if proc.returncode == 0:
            status = control_terminal_status(out)
            if status is not None:
                return True
            print("[错误] CONTROL 返回 exit 0，但没有可识别的成功状态（末行：%s）" %
                  (last[-1].strip() if last else "无输出"))
            return False
        if proc.returncode in (6, 7):
            last_window_reason = "Headless exit %d: %s" % (
                proc.returncode, last[-1] if last else "无输出")
            sleep(min(2, max(0, deadline - monotonic())))
            continue
        print("[错误] CONTROL 启动失败，输出：\n%s" % out)
        return False
    print("[错误] 等待游戏窗口/CONTROL 超时（窗口状态：%s）" % last_window_reason)
    return False


# ------------------------------------------------------------------
# 对局结果
# ------------------------------------------------------------------
def list_recordings(cfg, *, strict=False, diagnostic=None,
                    diagnostic_context=None):
    return REC.list_recordings(
        cfg, strict=strict, diagnostic=diagnostic,
        diagnostic_context=diagnostic_context)


def wait_new_recording(cfg, before, timeout_min, *, diagnostic=None,
                       match_id=None, poll_interval_s=5, settle_interval_s=3,
                       clock=time.monotonic, sleep=time.sleep):
    """Wait for exactly one new replay, logging every discovery/settle decision.

    ``diagnostic`` is deliberately a read-only observer. It cannot change the
    association decision, which remains fail-closed when candidates are
    ambiguous or disappear while settling. Clock/sleep injection keeps the
    retry and timeout paths deterministic in tests.
    """
    started = clock()
    deadline = started + timeout_min * 60
    iteration = 0

    def emit(event, **fields):
        if diagnostic is None:
            return
        payload = {"event": event, **fields}
        if match_id is not None:
            payload["match_id"] = match_id
        try:
            diagnostic(payload)
        except Exception:
            # Diagnostics must never make a file association succeed/fail.
            pass

    emit("recording_wait_started", timeout_min=timeout_min,
         baseline_count=len(before), baseline_keys=sorted(before))
    last_new_keys = []
    candidate_key = None
    while clock() < deadline:
        iteration += 1
        try:
            now = recording_snapshot(cfg, diagnostic=diagnostic,
                                     diagnostic_context={"iteration": iteration,
                                                         "phase": "poll"})
        except Exception as exc:
            emit("recording_snapshot_error", iteration=iteration, phase="poll",
                 error_type=type(exc).__name__, error=str(exc))
            raise
        try:
            new_keys = unique_new_recording_paths(before, now)
        except ValueError as exc:
            emit("recording_association_rejected", iteration=iteration,
                 phase="poll", snapshot_count=len(now), new_keys=sorted(set(now) - set(before)),
                 error_type=type(exc).__name__, error=str(exc))
            raise
        last_new_keys = new_keys
        emit("recording_poll", iteration=iteration, elapsed_s=round(clock() - started, 3),
             snapshot_count=len(now), new_keys=new_keys,
             new_stats={key: now[key] for key in new_keys},
             locked_candidate=candidate_key)

        if clock() >= deadline:
            emit("recording_deadline_expired", iteration=iteration,
                 phase="after_poll", remaining_s=0, new_keys=new_keys,
                 locked_candidate=candidate_key)
            break

        if candidate_key is not None and new_keys and new_keys != [candidate_key]:
            error = ValueError("录像候选已锁定为 %s，拒绝切换到 %s" %
                               (candidate_key, ", ".join(new_keys)))
            emit("recording_association_rejected", iteration=iteration,
                 phase="poll", candidate=candidate_key, new_keys=new_keys,
                 error_type=type(error).__name__, error=str(error))
            raise error
        if candidate_key is None and new_keys:
            candidate_key = new_keys[0]
            emit("recording_candidate_locked", iteration=iteration,
                 candidate=candidate_key, first_stat=now[candidate_key])

        if candidate_key is not None and candidate_key in now:
            key = candidate_key
            first_stat = now[key]
            remaining_s = deadline - clock()
            if remaining_s <= 0:
                emit("recording_deadline_expired", iteration=iteration,
                     phase="before_settle_wait", candidate=key,
                     first_stat=first_stat, remaining_s=0)
                break
            sleep(min(settle_interval_s, remaining_s))  # 等游戏完成录像写入
            if clock() >= deadline:
                emit("recording_deadline_expired", iteration=iteration,
                     phase="after_settle_wait", candidate=key,
                     first_stat=first_stat, remaining_s=0)
                break
            try:
                settled = recording_snapshot(
                    cfg, diagnostic=diagnostic,
                    diagnostic_context={"iteration": iteration, "phase": "settle"})
            except Exception as exc:
                emit("recording_snapshot_error", iteration=iteration, phase="settle",
                     candidate=key, first_stat=first_stat,
                     error_type=type(exc).__name__, error=str(exc))
                raise
            try:
                settled_keys = unique_new_recording_paths(before, settled)
            except ValueError as exc:
                emit("recording_association_rejected", iteration=iteration,
                     phase="settle", candidate=key, first_stat=first_stat,
                     settled_new_keys=sorted(set(settled) - set(before)),
                     error_type=type(exc).__name__, error=str(exc))
                raise

            if settled_keys and settled_keys != [key]:
                error = ValueError("录像候选已锁定为 %s，settle 时发现其他新路径：%s" %
                                   (key, ", ".join(settled_keys)))
                emit("recording_association_rejected", iteration=iteration,
                     phase="settle", candidate=key, first_stat=first_stat,
                     settled_new_keys=settled_keys,
                     error_type=type(error).__name__, error=str(error))
                raise error

            settled_stat = settled.get(key)
            is_settled = settled_keys == [key] and settled_stat == first_stat
            emit("recording_settled_check", iteration=iteration, candidate=key,
                 first_stat=first_stat, settled_stat=settled_stat,
                 settled_new_keys=settled_keys, settled=is_settled,
                 reason=(None if is_settled else
                         "candidate_set_changed" if settled_keys != [key] else
                         "stat_changed_or_candidate_missing"))
            if is_settled:
                remaining_s = deadline - clock()
                if remaining_s <= 0:
                    emit("recording_deadline_expired", iteration=iteration,
                         phase="before_candidate_lookup", candidate=key,
                         first_stat=first_stat, settled_stat=settled_stat,
                         remaining_s=0)
                    break
                try:
                    path = list_recordings(cfg)[key]
                except Exception as exc:
                    emit("recording_candidate_lookup_error", iteration=iteration,
                         candidate=key, first_stat=first_stat, settled_stat=settled_stat,
                         error_type=type(exc).__name__, error=str(exc))
                    # The file may have moved/disappeared between snapshot and
                    # lookup. Retry within the original deadline; do not infer.
                else:
                    remaining_s = deadline - clock()
                    if remaining_s <= 0:
                        emit("recording_deadline_expired", iteration=iteration,
                             phase="before_return", candidate=key,
                             first_stat=first_stat, settled_stat=settled_stat,
                             remaining_s=0)
                        break
                    emit("recording_associated_candidate", iteration=iteration,
                         candidate=key, first_stat=first_stat, settled_stat=settled_stat)
                    remaining_s = deadline - clock()
                    if remaining_s <= 0:
                        emit("recording_deadline_expired", iteration=iteration,
                             phase="immediately_before_return", candidate=key,
                             first_stat=first_stat, settled_stat=settled_stat,
                             remaining_s=0)
                        break
                    return path
        remaining_s = deadline - clock()
        if remaining_s <= 0:
            break
        sleep(min(poll_interval_s, remaining_s))

    emit("recording_wait_timeout", iterations=iteration,
         elapsed_s=round(clock() - started, 3), last_new_keys=last_new_keys)
    return None


def recording_snapshot(cfg, *, diagnostic=None, diagnostic_context=None, strict=False):
    """Capture stable file signatures before a match can create its replay."""
    snapshot = {}
    for key, path in list_recordings(
            cfg, strict=strict, diagnostic=diagnostic,
            diagnostic_context=diagnostic_context).items():
        try:
            stat = path.stat()
        except OSError as exc:
            if diagnostic is not None:
                payload = {
                    "event": "recording_stat_error",
                    "path": str(path),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    **(diagnostic_context or {}),
                }
                try:
                    diagnostic(payload)
                except Exception:
                    pass
            if strict:
                raise OSError("无法完整采集录像快照：%s: %s" % (path, exc)) from exc
            continue
        snapshot[key] = {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return snapshot


def unique_new_recording_paths(before, after):
    """Return new recording paths, rejecting an ambiguous match association."""
    new_paths = sorted(set(after) - set(before))
    if len(new_paths) > 1:
        raise ValueError("录像关联不唯一：快照后出现多个新录像")
    return new_paths


def _players_by_identity(info):
    """Index the two replay AIs by their stable installed slot names."""
    players_by_identity = {}
    for player in info.get("players", []):
        name = str(player.get("name", ""))
        identity = next((key for key in ("EvoAI_A", "EvoAI_B") if key in name), None)
        if identity:
            if identity in players_by_identity:
                return None
            players_by_identity[identity] = player
    if set(players_by_identity) != {"EvoAI_A", "EvoAI_B"}:
        return None
    return players_by_identity


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _players_by_scheduled_slot(info, evidence):
    slots_by_alias = evidence.get("slots_by_alias")
    if slots_by_alias != AI_PLAYER_SLOT_BY_ALIAS:
        raise ValueError("录像槽位映射与已配置 A/B 槽位不符")
    players_by_slot = {}
    for player in info.get("players", []):
        if not isinstance(player, dict):
            raise ValueError("录像玩家槽位数据格式无效")
        slot = player.get("slot")
        if not isinstance(slot, int) or isinstance(slot, bool) or slot in players_by_slot:
            raise ValueError("录像玩家槽位缺失或重复，无法唯一映射")
        players_by_slot[slot] = player
    if set(players_by_slot) != {1, 2, 3}:
        raise ValueError("录像必须包含唯一的观察者槽位 1 和 AI 槽位 2/3")
    if players_by_slot[1].get("user_id") == AI_USER_ID:
        raise ValueError("录像槽位 1 未能确认是观察者")

    result = {}
    for alias, slot in slots_by_alias.items():
        player = players_by_slot[slot]
        if player.get("user_id") != AI_USER_ID:
            raise ValueError("录像槽位 %d 未能确认为 AI 玩家" % slot)
        player_name = str(player.get("name", "")).strip()
        expected_name = "EvoAI_" + alias
        if player_name and expected_name not in player_name:
            raise ValueError("录像名字与槽位映射冲突：槽位 %d" % slot)
        result["EvoAI_" + alias] = player
    return result


def validate_match_evidence(info, gen, a, b, match, record, record_id, evidence):
    """Require a unique, auditable schedule/slot/install/replay association."""
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1:
        raise ValueError("缺少本局录像关联证据")
    if evidence.get("match_id") != match.get("match_id"):
        raise ValueError("录像关联证据的 match_id 与赛程不符")
    installed = evidence.get("installed")
    expected = {"A": "EvoAI_G%dP%d" % (gen, a), "B": "EvoAI_G%dP%d" % (gen, b)}
    if not isinstance(installed, dict) or set(installed) != {"A", "B"}:
        raise ValueError("缺少 A/B 已安装基因证据")
    installed_paths = set()
    for alias, genome in expected.items():
        item = installed[alias]
        if not isinstance(item, dict) or item.get("genome") != genome:
            raise ValueError("已安装 %s 基因身份与赛程不符" % alias)
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("per_sha256", ""))):
            raise ValueError("已安装 %s .per SHA-256 缺失或无效" % alias)
        per_path = str(item.get("per_path", ""))
        if Path(per_path).name != "EvoAI_%s.per" % alias:
            raise ValueError("已安装 %s .per 路径缺失" % alias)
        path_key = os.path.normcase(os.path.abspath(per_path))
        if path_key in installed_paths:
            raise ValueError("A/B 已安装 .per 路径不唯一")
        installed_paths.add(path_key)

    baseline = evidence.get("recording_baseline")
    recording = evidence.get("recording")
    captured_at = evidence.get("baseline_captured_at_ns")
    if (not isinstance(baseline, dict) or not isinstance(recording, dict)
            or not isinstance(captured_at, int) or isinstance(captured_at, bool)
            or captured_at <= 0):
        raise ValueError("录像发现前后快照证据不完整")
    record_path = str(Path(record).expanduser().resolve())
    record_key = os.path.normcase(os.path.abspath(record_path))
    new_candidates = recording.get("new_candidates")
    if not isinstance(new_candidates, list):
        raise ValueError("录像新增候选列表缺失")
    candidate_keys = [os.path.normcase(os.path.abspath(str(path)))
                      for path in new_candidates]
    if (record_key in baseline or recording.get("new_since_snapshot") is not True
            or candidate_keys != [record_key]):
        raise ValueError("录像未能证明是本局快照后唯一新增文件")
    stat = Path(record_path).stat()
    if (recording.get("path") != record_path
            or recording.get("record_id") != record_id
            or recording.get("size_bytes") != stat.st_size
            or recording.get("mtime_ns") != stat.st_mtime_ns
            or stat.st_size <= 0
            or record_id != EV.ER.match_id_for_recording(record_path)):
        raise ValueError("录像路径、指纹或文件状态与关联证据不符")
    return _players_by_scheduled_slot(info, evidence)


def _valid_score(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and (isinstance(value, int) or math.isfinite(value)))


def resolve_winner(info, gen, a, b, players_by_identity=None):
    """Map one unambiguous replay winner to its installed genome identity."""
    identity_to_genome = {
        "EvoAI_A": "EvoAI_G%dP%d" % (gen, a),
        "EvoAI_B": "EvoAI_G%dP%d" % (gen, b),
    }
    if players_by_identity is None:
        players_by_identity = _players_by_identity(info)
    if players_by_identity is None:
        return None

    if any(not _valid_score(player.get("score")) for player in players_by_identity.values()):
        return None
    explicit_winners = [identity for identity, player in players_by_identity.items()
                        if player.get("winner") is True]
    if len(explicit_winners) > 1:
        return None
    if explicit_winners:
        return identity_to_genome[explicit_winners[0]]

    # If the replay has no explicit winner marker, compare scores by identity.
    score_a = players_by_identity["EvoAI_A"].get("score")
    score_b = players_by_identity["EvoAI_B"].get("score")
    if (isinstance(score_a, (int, float)) and isinstance(score_b, (int, float))
            and score_a != score_b):
        identity = "EvoAI_A" if score_a > score_b else "EvoAI_B"
        return identity_to_genome[identity]
    return None


def match_result_from_replay(info, gen, a, b, match, record, record_id, evidence=None):
    """Build one ledger row, rejecting ambiguous winners and invalid scores."""
    na, nb = "EvoAI_G%dP%d" % (gen, a), "EvoAI_G%dP%d" % (gen, b)
    if evidence is None:
        players_by_identity = _players_by_identity(info)
    else:
        players_by_identity = validate_match_evidence(
            info, gen, a, b, match, record, record_id, evidence)
    if players_by_identity is None:
        raise ValueError("录像未能唯一映射到 EvoAI_A 与 EvoAI_B")
    if any(not _valid_score(players_by_identity[alias].get("score"))
           for alias in ("EvoAI_A", "EvoAI_B")):
        raise ValueError("录像缺少有效比分；必须包含双方有限数值比分")
    winner_name = resolve_winner(info, gen, a, b, players_by_identity)
    if winner_name not in (na, nb):
        raise ValueError("录像胜者无效、多个胜者或无法唯一判定")
    scores = {
        na: players_by_identity["EvoAI_A"].get("score"),
        nb: players_by_identity["EvoAI_B"].get("score"),
    }
    return {
        "match_id": match["match_id"],
        "players": [[na, winner_name == na], [nb, winner_name == nb]],
        "winners": [winner_name],
        "scores": scores,
        "duration_min": info.get("duration_min"),
        "record": str(record),
        "record_id": record_id,
        **({"association": evidence} if evidence is not None else {}),
    }


def record_validated_result(results, info, gen, a, b, match, record, record_id,
                            manifest, results_path, evidence=None):
    """Validate a replay completely before it can reach ledger or fitness inputs."""
    result = match_result_from_replay(
        info, gen, a, b, match, record, record_id, evidence=evidence)
    candidate_results = [*results, result]
    EV.ER.reconcile_results(candidate_results, manifest)
    EV.ER.write_results(results_path, candidate_results)
    results.append(result)
    return result


def append_runner_evidence(gen, event):
    """Append audit events outside source control, retaining retries by match."""
    path = LAB / "runner_evidence" / ("gen_%d.jsonl" % gen)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def result_commits_enabled(capture_only):
    """Whether this run may advance the result ledger or fitness inputs."""
    return not bool(capture_only)


def main():
    ap = argparse.ArgumentParser(description="EvoLab 全自动跑局")
    ap.add_argument("--matches", type=int, default=None, help="最多跑几场（默认跑完赛程）")
    ap.add_argument("--gen", type=int, default=None, help="指定代数（默认当前最新代）")
    ap.add_argument("--capture-only", action="store_true",
                    help="只跑一局并保留诊断证据；绝不写结果账本/fitness")
    args = ap.parse_args()
    if args.capture_only and args.matches not in (None, 1):
        ap.error("--capture-only 必须单局运行，--matches 只能省略或为 1")

    cfg = load_config()
    deploy_module(cfg)

    # ---- 进化赛程 ----
    gen = args.gen if args.gen is not None else EV.current_gen()
    gen_file = LAB / "generations" / ("gen_%d.json" % gen)
    if not gen_file.exists():
        sys.exit("没有第 %d 代种群，先运行 evolve.py init" % gen)
    gene_pop = json.loads(gen_file.read_text(encoding="utf-8"))
    pop = len(gene_pop)
    ec = EV.evo_cfg(cfg)
    pairs = EV.make_schedule(pop, int(ec.get("matches_per_ai", 3)), random.Random(gen * 7919))
    expected_count = pop * int(ec.get("matches_per_ai", 3)) // 2
    res_dir = LAB / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    res_file = res_dir / ("gen_%d.json" % gen)
    results = json.loads(res_file.read_text(encoding="utf-8")) if res_file.exists() else []
    manifest = EV.ER.load_schedule(LAB, gen, pairs, results, pop, expected_count)
    try:
        EV.ER.validate_schedule(manifest, pop, expected_count)
    except ValueError as e:
        sys.exit("[拒绝] 赛程本身不完整或覆盖不足：%s" % e)

    try:
        accepted = EV.ER.reconcile_results(results, manifest)
    except ValueError as e:
        sys.exit("[拒绝] 现有结果账本无效，未启动对局：%s" % e)
    # Persist assigned IDs for legacy ledger rows before resuming.
    # Capture-only PoC must remain read-only with respect to the result ledger.
    if result_commits_enabled(args.capture_only):
        EV.ER.write_results(res_file, results)
    done = len(accepted)
    schedule = EV.ER.pending_matches(results, manifest)
    if args.capture_only:
        schedule = schedule[:1]
    elif args.matches is not None:
        schedule = schedule[:max(0, args.matches)]
    print("[赛程] 第 %d 代计划 %d 场，已完成 %d 场，本次跑 %d 场"
          % (gen, len(manifest["matches"]), done, len(schedule)))
    if not schedule:
        print("[完成] 本代赛程已全部跑完，直接执行 evolve.py next 即可")
        return

    # ---- 游戏与 CONTROL ----
    if not ensure_control(cfg):
        sys.exit(1)
    ok, why = check_module_assigned()
    if not ok:
        print()
        print("=" * 60)
        print("  还差一步（只做一次）：把 evolab_driver 指派给玩家 1")
        print("=" * 60)
        print("  1. 切到游戏窗口，按 Shift 打开 CONTROL 菜单")
        print("  2. 展开 MODULES → Player 1")
        print("  3. Module 下拉框选 evolab_driver，保持 Enabled 打开")
        print("  4. 确认 Multithreading / Tournament Mode 处于关闭")
        print("  然后重新运行本命令即可。")
        print("  （%s）" % why)
        print("=" * 60)
        sys.exit(2)

    print()
    print("[开始] 全自动跑局。停止方法：CONTROL 菜单关掉 Player1 的模块，或按 Delete 卸载 CONTROL。")
    print("[提示] 想看就切到游戏窗口；不想看就让它跑，战报自动落盘。")
    print()

    timeout_min = int(cfg.get("control", {}).get("auto_match_timeout_min", 150))
    capture_receiver = None

    def log_recording_probe(event):
        print("[录像探测] " + json.dumps(event, ensure_ascii=False, sort_keys=True))

    if args.capture_only:
        raw_path = raw_capture_path(LAB / "runner_evidence", schedule[0]["match_id"])
        def log_capture_ipc(event):
            print("[PoC IPC] " + json.dumps(event, ensure_ascii=False, sort_keys=True))

        capture_receiver = WindowsPipeCaptureReceiver(
            raw_path, schedule[0]["match_id"], diagnostic=log_capture_ipc)
        try:
            capture_receiver.start()
        except OSError as e:
            sys.exit("[PoC 拒绝] 无法启动 AoE2Control IPC 采集器：%s" % e)
        print("[PoC IPC] 等待 CONTROL pipe、capture_ready、match_bound；原始 sentinel 将只追加到 %s" % raw_path)
        try:
            bound = capture_receiver.wait_until_bound(20)
        except CaptureIPCRejected as e:
            capture_receiver.stop()
            sys.exit("[PoC 拒绝] 无法绑定本场 match_id：%s" % e)
        if not bound:
            capture_receiver.stop()
            sys.exit("[PoC 拒绝] CONTROL IPC 握手未完成")
    for idx, match in enumerate(schedule):
        a, b = [EV.ER.individual_from_name(n, gen, pop) for n in match["players"]]
        if a is None or b is None:
            sys.exit("[拒绝] 赛程参赛名无效: %s" % match)
        print("[对局 %d/%d] EvoAI_G%dP%d  vs  EvoAI_G%dP%d"
              % (done + idx + 1, len(manifest["matches"]), gen, a, gen, b))
        # Capture exactly what existed before this match, then record installed
        # identities before waiting for a replay to appear.
        baseline_captured_at_ns = time.time_ns()
        try:
            before = recording_snapshot(
                cfg, diagnostic=log_recording_probe,
                diagnostic_context={"phase": "baseline", "match_id": match["match_id"]},
                strict=True)
        except Exception as exc:
            if capture_receiver is not None:
                capture_receiver.stop()
            sys.exit("[拒绝] 赛前录像基线不完整，未启动本场：%s" % exc)
        log_recording_probe({
            "event": "recording_baseline_complete",
            "match_id": match["match_id"],
            "captured_at_ns": baseline_captured_at_ns,
            "recording_count": len(before),
            "keys": sorted(before),
        })
        MK.install("A", {k: v for k, v in gene_pop[a].items()}, cfg)
        MK.install("B", {k: v for k, v in gene_pop[b].items()}, cfg)
        ai_dir = Path(cfg["game"]["ai_dir"])
        installed = {}
        for alias, individual in (("A", a), ("B", b)):
            per_path = (ai_dir / ("EvoAI_%s.per" % alias)).resolve()
            installed[alias] = {
                "genome": "EvoAI_G%dP%d" % (gen, individual),
                "per_path": str(per_path),
                "per_sha256": _file_sha256(per_path),
            }
        evidence = {
            "schema_version": 1,
            "match_id": match["match_id"],
            "slots_by_alias": dict(AI_PLAYER_SLOT_BY_ALIAS),
            "installed": installed,
            "recording_baseline": before,
            "baseline_captured_at_ns": baseline_captured_at_ns,
        }
        append_runner_evidence(gen, {"event": "match_prepared", **evidence})
        print("[关联准备] match_id=%s A=slot%d/%s sha256=%s B=slot%d/%s sha256=%s" % (
            match["match_id"], AI_PLAYER_SLOT_BY_ALIAS["A"], installed["A"]["genome"],
            installed["A"]["per_sha256"], AI_PLAYER_SLOT_BY_ALIAS["B"],
            installed["B"]["genome"], installed["B"]["per_sha256"]))

        try:
            rec = wait_new_recording(
                cfg, before, timeout_min, diagnostic=log_recording_probe,
                match_id=match["match_id"])
        except ValueError as e:
            append_runner_evidence(gen, {
                "event": "recording_rejected", "match_id": match["match_id"],
                "reason": str(e),
            })
            print("[拒绝] %s；账本不推进。" % e)
            break
        if rec is None:
            append_runner_evidence(gen, {
                "event": "recording_timeout", "match_id": match["match_id"],
            })
            print("[超时] %d 分钟没有等到新录像，可能卡局。已暂停。" % timeout_min)
            print("       排查后重跑本命令会自动续上进度。")
            break

        print("[录像] %s" % rec.name)
        after = recording_snapshot(cfg)
        rec_path = str(rec.resolve())
        rec_key = os.path.normcase(os.path.abspath(rec_path))
        try:
            candidate_keys = unique_new_recording_paths(before, after)
        except ValueError as e:
            append_runner_evidence(gen, {
                "event": "recording_rejected", "match_id": match["match_id"],
                "reason": str(e),
            })
            print("[拒绝] %s；账本不推进。" % e)
            break
        stat = rec.stat()
        record_id = EV.ER.match_id_for_recording(rec)
        recording_evidence = {
            "path": rec_path,
            "record_id": record_id,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "new_since_snapshot": len(candidate_keys) == 1 and candidate_keys[0] == rec_key,
            "new_candidates": candidate_keys,
        }
        if args.capture_only:
            recording_evidence["sha256"] = _file_sha256(rec)
        evidence["recording"] = recording_evidence
        append_runner_evidence(gen, {
            "event": "recording_associated", "match_id": match["match_id"],
            "association": evidence,
        })
        print("[关联录像] match_id=%s record_id=%s path=%s new=%s candidates=%d" % (
            match["match_id"], record_id, rec_path,
            evidence["recording"]["new_since_snapshot"], len(candidate_keys)))
        if not result_commits_enabled(args.capture_only):
            try:
                capture_receiver.wait_for_capture(10)
                capture_receiver.stop()
                capture_association = {
                    "match_id": match["match_id"],
                    "slots_by_alias": evidence["slots_by_alias"],
                    "installed": evidence["installed"],
                    "recording": {
                        "path": rec_path,
                        "record_id": record_id,
                        "sha256": evidence["recording"]["sha256"],
                    },
                }
                append_capture_association(raw_path, capture_association)
                raw_row = load_raw_capture(
                    raw_path, match["match_id"], capture_association)
            except (CaptureIPCRejected, RuntimeError) as e:
                print("[PoC 拒绝] 未能非交互取得唯一原始 IPC sentinel：%s；账本/fitness 未写入。" % e)
                break
            print("[PoC IPC] 已追加并校验原始 sentinel：%s sha256=%s" % (
                raw_path, raw_row["raw_sentinel_sha256"]))
            print("[只读 PoC] 已保留 match_prepared/recording_associated 证据；跳过战报解析、结果账本与 fitness。")
            break
        try:
            info = RPT.parse_record(rec)
        except SystemExit:
            raise
        except Exception as e:
            info = {"players": [], "winners": []}
            print("[警告] 录像解析失败: %s" % e)

        try:
            result = record_validated_result(
                results, info, gen, a, b, match, rec.resolve(), record_id,
                manifest, res_file, evidence=evidence)
        except ValueError as e:
            print("[拒绝] %s；录像保留，账本与 fitness 输入均不推进。修正录像结果后重新运行。" % e)
            break
        winner_name = result["winners"][0]
        print("[结果] 胜者: %s" % winner_name)
    else:
        print()
        print("[完成] 本次赛程全部跑完！")
        print("[下一步] python evolve.py next   # 汇总积分，进化出下一代")
        print("[停止]  记得在游戏里按 Shift → 关掉 Player1 的模块（或按 Delete 卸载 CONTROL）")
    if capture_receiver is not None:
        capture_receiver.stop()


if __name__ == "__main__":
    main()
