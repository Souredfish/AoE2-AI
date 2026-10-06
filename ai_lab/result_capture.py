"""Validate and retain the optional, read-only AoE2Control result capture PoC.

This module writes diagnostic JSONL only. It deliberately has no ledger or
fitness imports so captured observations cannot affect evolutionary scoring.
"""

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:  # package import in tests; script import when launched from auto_runner.py
    from .result_capture_ipc import CaptureIPCRejected, load_raw_capture
except ImportError:
    from result_capture_ipc import CaptureIPCRejected, load_raw_capture


CAPTURE_PREFIX = "EVOLAB_RESULT_CAPTURE_V1:"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CaptureRejected(ValueError):
    """Raised when raw telemetry or its match association is incomplete."""


def parse_observation(text):
    """Parse exactly one raw CONTROL log payload, or a plain JSON object."""
    payloads = []
    for line in text.splitlines():
        if CAPTURE_PREFIX in line:
            payloads.append(line.split(CAPTURE_PREFIX, 1)[1].strip())
    if payloads:
        if len(payloads) != 1:
            raise CaptureRejected("PoC 原始记录必须且只能包含一次终局采集")
        text = payloads[0]
    try:
        raw = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CaptureRejected("PoC 原始记录不是有效 JSON") from exc
    if not isinstance(raw, dict):
        raise CaptureRejected("PoC 原始记录必须是 JSON 对象")
    return raw


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def context_from_runner_evidence(evidence_path, recording_path, game_version,
                                 control_version, modules_see_everything_confirmed):
    """Bind a capture to the unique runner association for one replay file."""
    replay = Path(recording_path).resolve()
    if not replay.is_file():
        raise CaptureRejected("关联录像不存在，不能生成 PoC 记录")
    target = str(replay).casefold()
    try:
        events = [json.loads(line) for line in Path(evidence_path).read_text(
            encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise CaptureRejected("runner_evidence JSONL 缺失或损坏") from exc
    matches = []
    for event in events:
        if event.get("event") != "recording_associated":
            continue
        association = event.get("association") or {}
        record = association.get("recording") or {}
        if str(record.get("path", "")).casefold() == target:
            matches.append((event, association, record))
    if len(matches) != 1:
        raise CaptureRejected("录像必须在 runner_evidence 中唯一关联到一个 match_id")
    event, association, record = matches[0]
    match_id = event.get("match_id")
    prepared = [item for item in events
                if item.get("event") == "match_prepared" and item.get("match_id") == match_id]
    if len(prepared) != 1 or association.get("match_id") != match_id:
        raise CaptureRejected("录像关联与唯一 match_prepared 身份证据不一致")
    prepared = prepared[0]
    if (prepared.get("slots_by_alias") != association.get("slots_by_alias")
            or prepared.get("installed") != association.get("installed")):
        raise CaptureRejected("录像关联的槽位/.per 身份与赛前证据矛盾")
    if record.get("new_since_snapshot") is not True:
        raise CaptureRejected("录像未被 runner 唯一识别为本场新产物")
    candidates = record.get("new_candidates")
    if not isinstance(candidates, list) or len(candidates) != 1 or str(candidates[0]).casefold() != target:
        raise CaptureRejected("runner 录像候选不唯一")
    if not isinstance(record.get("record_id"), str) or not record["record_id"].strip():
        raise CaptureRejected("runner 缺少录像 record_id")
    if not game_version or not str(game_version).strip():
        raise CaptureRejected("必须提供 Windows 游戏可执行文件版本")
    if not control_version or not str(control_version).strip():
        raise CaptureRejected("必须提供 Windows AoE2Control 可执行文件版本")
    installed = association.get("installed")
    slots = association.get("slots_by_alias")
    if not isinstance(installed, dict) or not isinstance(slots, dict):
        raise CaptureRejected("runner 证据缺少槽位或已安装 .per 身份")
    stat = replay.stat()
    if record.get("size_bytes") != stat.st_size or record.get("mtime_ns") != stat.st_mtime_ns:
        raise CaptureRejected("录像大小/mtime 已变化，不能确认与 runner 关联时为同一文件")
    replay_hash = _sha256(replay)
    if record.get("sha256") and record["sha256"] != replay_hash:
        raise CaptureRejected("录像 SHA-256 与 runner 证据不一致")
    return {
        "match_id": event.get("match_id"),
        "game_version": str(game_version).strip(),
        "control_version": str(control_version).strip(),
        "modules_see_everything_confirmed": bool(modules_see_everything_confirmed),
        "slots_by_alias": slots,
        "installed": installed,
        "recording": {"path": str(replay), "record_id": record.get("record_id"),
                      "sha256": replay_hash},
        "captured_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _read(raw, key, expected_type=None):
    item = raw.get(key)
    if not isinstance(item, dict) or item.get("ok") is not True:
        raise CaptureRejected("%s 读取缺失或 API/权限失败" % key)
    value_type = item.get("value_type")
    if value_type == "nil" or "value" not in item:
        raise CaptureRejected("%s 缺少有效终值" % ("CURRENT_SCORE" if key == "current_score" else key))
    value = item["value"]
    if expected_type is not None and value_type != expected_type:
        raise CaptureRejected("%s 类型不符合约定" % key)
    return value


def validate_capture(raw, context):
    """Reject incomplete/ambiguous telemetry and return a diagnostic record."""
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise CaptureRejected("不支持的 PoC 采集 schema")
    if raw.get("capture_sequence") != 1:
        raise CaptureRejected("终局采集不是唯一的首次调用")
    if not isinstance(context, dict):
        raise CaptureRejected("PoC 赛程关联上下文缺失")
    if context.get("modules_see_everything_confirmed") is not True:
        raise CaptureRejected("未确认 Modules See Everything 权限，拒绝采信双方比分")
    for field in ("match_id", "game_version", "control_version", "captured_at_utc"):
        if not isinstance(context.get(field), str) or not context[field].strip():
            raise CaptureRejected("PoC 上下文缺少 %s" % field)
    try:
        datetime.fromisoformat(context["captured_at_utc"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaptureRejected("采集时间戳无效") from exc
    if raw.get("match_id") != context["match_id"]:
        raise CaptureRejected("sentinel match_id 与 runner 录像关联不一致")

    slots = context.get("slots_by_alias")
    installed = context.get("installed")
    if (not isinstance(slots, dict) or set(slots) != {"A", "B"}
            or len(set(slots.values())) != 2 or not isinstance(installed, dict)):
        raise CaptureRejected("赛程槽位关联缺失或歧义")
    for alias in ("A", "B"):
        player = installed.get(alias)
        if not isinstance(player, dict) or not _SHA256_RE.fullmatch(
                str(player.get("per_sha256", "")).lower()):
            raise CaptureRejected("参赛者 %s 的 .per SHA-256 缺失或无效" % alias)
    recording = context.get("recording")
    if (not isinstance(recording, dict)
            or not _SHA256_RE.fullmatch(str(recording.get("sha256", "")).lower())):
        raise CaptureRejected("录像 SHA-256 缺失或无效")

    speed_set = raw.get("game_speed_set")
    speed = _read(raw, "game_speed_readback", "number")
    if not isinstance(speed_set, dict) or speed_set.get("call_ok") is not True:
        raise CaptureRejected("SetGameSpeed(2.0) 调用失败")
    if isinstance(speed, bool) or not math.isfinite(float(speed)) or float(speed) != 2.0:
        raise CaptureRejected("SetGameSpeed(2.0) 未成功或 GetGameSpeed() 读回不一致")
    game_time = _read(raw, "game_time_seconds", "number")
    if isinstance(game_time, bool) or not math.isfinite(float(game_time)) or float(game_time) <= 0:
        raise CaptureRejected("终局游戏时间无效")

    victory = raw.get("victory_player")
    if not isinstance(victory, dict) or victory.get("ok") is not True or victory.get("value_type") != "Player":
        raise CaptureRejected("GetVictoryPlayer() 缺失或读取失败")
    winner_slot = victory.get("player_id")
    participants = raw.get("players")
    if not isinstance(participants, list) or len(participants) != 2:
        raise CaptureRejected("双方终局采集记录缺失")
    by_slot = {}
    for player in participants:
        if not isinstance(player, dict) or player.get("slot") in by_slot:
            raise CaptureRejected("玩家槽位记录重复或无效")
        slot = player.get("slot")
        if slot not in slots.values():
            raise CaptureRejected("采集到的玩家槽位与赛程不符")
        won = _read(player, "has_won", "boolean")
        if not isinstance(won, bool):
            raise CaptureRejected("HasWon() 原始值不是布尔值")
        score = _read(player, "current_score", "number")
        if isinstance(score, bool) or not math.isfinite(float(score)):
            raise CaptureRejected("CURRENT_SCORE 不是有限数值")
        by_slot[slot] = {"slot": slot, "name": player.get("name"),
                         "has_won": won, "current_score": score,
                         "has_won_raw": player["has_won"],
                         "current_score_raw": player["current_score"]}
    if set(by_slot) != set(slots.values()):
        raise CaptureRejected("采集的玩家槽位不完整")
    flagged = [slot for slot, player in by_slot.items() if player["has_won"]]
    if len(flagged) != 1 or winner_slot != flagged[0]:
        raise CaptureRejected("GetVictoryPlayer() 与双方 HasWon() 标记矛盾或不唯一")

    return {
        "schema_version": 1,
        "kind": "read_only_game_result_capture",
        "ledger_eligible": False,
        "match_id": context["match_id"],
        "captured_at_utc": context["captured_at_utc"],
        "game_version": context["game_version"],
        "control_version": context["control_version"],
        "capture_sequence": raw.get("capture_sequence"),
        "game_time_seconds": game_time,
        "game_speed_set": raw["game_speed_set"],
        "game_speed_readback": raw["game_speed_readback"],
        "winner_slot": winner_slot,
        "victory_player_raw": victory,
        "slots_by_alias": slots,
        "installed": installed,
        "recording": recording,
        "players": [by_slot[slots[alias]] for alias in ("A", "B")],
        "source_observation": raw,
    }


def append_capture(path, event):
    """Append one diagnostic capture; reject duplicate match/capture rows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            prior = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
        except (OSError, ValueError) as exc:
            raise CaptureRejected("既有 PoC JSONL 损坏，拒绝追加") from exc
        if any(row.get("match_id") == event.get("match_id") for row in prior):
            raise CaptureRejected("该 match_id 已有 PoC 记录，拒绝重复采集")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate one read-only AoE2 result capture")
    parser.add_argument("--raw-capture", required=True,
                        help="auto_runner 保存的原始 AoE2Control IPC JSONL")
    parser.add_argument("--runner-evidence", required=True, help="gen_N.jsonl from auto_runner")
    parser.add_argument("--recording", required=True, help="exact associated .aoe2record path")
    parser.add_argument("--game-version", required=True, help="AoE2DE_s.exe ProductVersion")
    parser.add_argument("--control-version", required=True, help="AoE2Control.exe ProductVersion")
    parser.add_argument("--modules-see-everything-confirmed", action="store_true",
                        help="operator verified CONTROL Modules See Everything is enabled")
    parser.add_argument("--output", default="lab_data/runner_evidence/result_capture_poc.jsonl")
    args = parser.parse_args(argv)
    try:
        ctx = context_from_runner_evidence(
            args.runner_evidence, args.recording, args.game_version, args.control_version,
            args.modules_see_everything_confirmed)
        raw_row = load_raw_capture(args.raw_capture, ctx["match_id"], {
            "match_id": ctx["match_id"],
            "slots_by_alias": ctx["slots_by_alias"],
            "installed": ctx["installed"],
            "recording": ctx["recording"],
        })
        raw = parse_observation(raw_row["raw_sentinel"])
        event = validate_capture(raw, ctx)
        event["raw_capture_source"] = {
            "kind": raw_row["kind"],
            "received_at_utc": raw_row["received_at_utc"],
            "raw_sentinel_sha256": raw_row["raw_sentinel_sha256"],
        }
        append_capture(args.output, event)
    except (OSError, CaptureRejected, CaptureIPCRejected) as exc:
        print("[PoC 拒绝] %s" % exc, file=sys.stderr)
        return 2
    print("[PoC 已保留] match_id=%s；只读 JSONL=%s；ledger_eligible=false" % (
        event["match_id"], args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
