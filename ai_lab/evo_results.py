"""Stable schedules and validation for generation result ledgers."""
import hashlib
import json
import re
from pathlib import Path


def individual_name(gen, index):
    return "EvoAI_G%dP%d" % (gen, index)


def build_schedule(gen, pairs):
    matches = []
    for index, pair in enumerate(pairs):
        a, b = pair
        matches.append({
            "match_id": "g%d-m%04d" % (gen, index + 1),
            "players": [individual_name(gen, a), individual_name(gen, b)],
        })
    return {"schema_version": 1, "generation": gen, "matches": matches}


def schedule_path(lab, gen):
    return Path(lab) / "schedules" / ("gen_%d.json" % gen)


def write_results(path, results):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def save_schedule(lab, gen, pairs):
    manifest = build_schedule(gen, pairs)
    path = schedule_path(lab, gen)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    return manifest


def load_schedule(lab, gen, pairs=None, legacy_results=None, population=None, expected_matches=None):
    path = schedule_path(lab, gen)
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("generation") != gen or not isinstance(manifest.get("matches"), list):
            raise ValueError("第 %d 代赛程清单格式无效" % gen)
        return manifest
    if pairs is None:
        raise ValueError("第 %d 代没有赛程清单，需用确定性旧格式兼容赛程恢复" % gen)
    # Older manual reports did not persist their randomized schedule. Preserve
    # every unambiguous completed pair, then fill the remaining slots from the
    # deterministic auto_runner schedule so recovery never shifts old results.
    migrated_pairs = []
    for row in legacy_results or []:
        names = _result_names(row)
        ids = [individual_from_name(name, gen, population) for name in names]
        if None in ids or ids[0] == ids[1]:
            raise ValueError("旧格式结果无法映射到第 %d 代个体" % gen)
        pair = tuple(ids)
        if frozenset(pair) in {frozenset(p) for p in migrated_pairs}:
            raise ValueError("旧格式账本重复记录参赛组合: %s" % (names,))
        migrated_pairs.append(pair)
    target_count = expected_matches if expected_matches is not None else len(pairs)
    if len(migrated_pairs) > target_count:
        raise ValueError("旧格式结果场数超过计划赛程，无法安全迁移")
    known = {frozenset(pair) for pair in migrated_pairs}
    for pair in pairs:
        if frozenset(pair) not in known and len(migrated_pairs) < target_count:
            migrated_pairs.append(pair)
            known.add(frozenset(pair))
    if len(migrated_pairs) != target_count:
        raise ValueError("无法从旧账本与兼容赛程重建完整计划")
    pairs = migrated_pairs
    return save_schedule(lab, gen, pairs)


def _match_names(row):
    players = row.get("players")
    if not isinstance(players, list) or len(players) != 2 or not all(isinstance(n, str) for n in players):
        raise ValueError("对局参赛个体格式无效")
    if players[0] == players[1]:
        raise ValueError("同一个体不能与自身对局")
    return players


def _result_names(row):
    players = row.get("players")
    if isinstance(players, list) and len(players) == 2 and all(
            isinstance(item, (list, tuple)) and len(item) >= 1 and isinstance(item[0], str)
            for item in players):
        names = [item[0] for item in players]
    elif isinstance(players, list) and len(players) == 2 and all(isinstance(n, str) for n in players):
        names = players
    else:
        raise ValueError("结果参赛个体格式无效")
    if names[0] == names[1]:
        raise ValueError("结果不能由同一个体自我对局")
    return names


def _record_key(value):
    if not isinstance(value, str) or not value.strip():
        return None
    return str(Path(value).expanduser().resolve()).casefold()


def reconcile_results(results, manifest):
    """Validate and normalize legacy rows; return rows indexed by scheduled match."""
    if not isinstance(results, list):
        raise ValueError("结果账本必须是列表")
    scheduled = manifest["matches"]
    by_id = {m["match_id"]: m for m in scheduled}
    by_pair = {frozenset(_match_names(m)): m for m in scheduled}
    if len(by_id) != len(scheduled) or len(by_pair) != len(scheduled):
        raise ValueError("赛程中存在重复单局标识或重复参赛组合")
    accepted, seen_records = {}, {}
    for row in results:
        if not isinstance(row, dict):
            raise ValueError("结果账本包含非法记录")
        names = _result_names(row)
        match_id = row.get("match_id")
        if match_id is None:
            match = by_pair.get(frozenset(names))
            if match is None:
                raise ValueError("旧格式账本中的参赛组合不属于计划赛程: %s" % (names,))
            match_id = match["match_id"]
            row["match_id"] = match_id
        match = by_id.get(match_id)
        if match is None:
            raise ValueError("结果包含未知单局标识: %s" % match_id)
        if frozenset(names) != frozenset(match["players"]):
            raise ValueError("单局 %s 的参赛个体与赛程不符" % match_id)
        if match_id in accepted:
            raise ValueError("赛程重复记账: %s" % match_id)
        winners = row.get("winners")
        if not isinstance(winners, list) or len(winners) != 1 or winners[0] not in match["players"]:
            raise ValueError("单局 %s 的胜者无效，必须且只能是一个参赛个体" % match_id)
        record_id = row.get("record_id")
        if record_id is not None and not isinstance(record_id, str):
            raise ValueError("录像标识格式无效")
        record = "id:" + record_id if record_id else _record_key(row.get("record"))
        if record:
            prior = seen_records.get(record)
            if prior:
                raise ValueError("录像重复记账: %s (已用于 %s)" % (row["record"], prior))
            seen_records[record] = match_id
        accepted[match_id] = row
    return accepted


def validate_schedule(manifest, population, expected_matches):
    if len(manifest["matches"]) != expected_matches:
        raise ValueError("计划场数错误：预期 %d，赛程实际 %d" % (expected_matches, len(manifest["matches"])))
    participants = {name for match in manifest["matches"] for name in _match_names(match)}
    if len({frozenset(_match_names(match)) for match in manifest["matches"]}) != len(manifest["matches"]):
        raise ValueError("赛程包含重复参赛组合")
    expected_participants = {individual_name(manifest["generation"], i) for i in range(population)}
    if participants != expected_participants:
        raise ValueError("赛程参赛个体覆盖不完整")
    return {m["match_id"]: m for m in manifest["matches"]}


def validate_complete(results, manifest, population, expected_matches):
    validate_schedule(manifest, population, expected_matches)
    accepted = reconcile_results(results, manifest)
    missing = [m["match_id"] for m in manifest["matches"] if m["match_id"] not in accepted]
    if missing:
        raise ValueError("赛程缺少 %d 场结果（%s）" % (len(missing), missing[0]))
    return accepted


def pending_matches(results, manifest):
    accepted = reconcile_results(results, manifest)
    return [m for m in manifest["matches"] if m["match_id"] not in accepted]


def match_id_for_recording(record_path):
    """Content-based recording ID survives renames and copy-based re-imports."""
    digest_obj = hashlib.sha256()
    with open(record_path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest_obj.update(chunk)
    digest = digest_obj.hexdigest()[:24]
    return "record-" + digest


def individual_from_name(name, gen, pop):
    if not isinstance(name, str):
        return None
    for index in range(pop):
        expected = re.escape(individual_name(gen, index))
        if re.search(r"(?<![A-Za-z0-9_])" + expected + r"(?![0-9])", name):
            return index
    return None
