"""Receive and preserve raw terminal sentinels over AoE2Control's named-pipe IPC.

Only the Windows adapter imports pywin32. Persisted rows retain the original
sentinel string verbatim; parsed conclusions belong in the separate PoC output.
"""

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


PIPE_NAME = "EvoLabResultCaptureV1"
PIPE_PATH = "\\\\.\\pipe\\" + PIPE_NAME
CAPTURE_PREFIX = "EVOLAB_RESULT_CAPTURE_V1:"
_MATCH_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class CaptureIPCRejected(ValueError):
    """The IPC frame cannot be trusted as one raw capture event."""


def _validate_sentinel(raw_sentinel):
    if not isinstance(raw_sentinel, str) or not raw_sentinel.startswith(CAPTURE_PREFIX):
        raise CaptureIPCRejected("IPC payload 缺少原始 sentinel 前缀")
    try:
        raw = json.loads(raw_sentinel[len(CAPTURE_PREFIX):])
    except (TypeError, json.JSONDecodeError) as exc:
        raise CaptureIPCRejected("IPC 原始 sentinel JSON 无效") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise CaptureIPCRejected("IPC 原始 sentinel schema 无效")
    if raw.get("capture_sequence") != 1:
        raise CaptureIPCRejected("IPC 原始 sentinel 重复或序号无效")
    if not isinstance(raw.get("match_id"), str) or not raw["match_id"].strip():
        raise CaptureIPCRejected("IPC 原始 sentinel 缺少 runner match_id")
    return raw


def _decode_envelope(message):
    try:
        envelope = json.loads(message)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CaptureIPCRejected("AoE2Control IPC envelope 无效") from exc
    if not isinstance(envelope, dict) or envelope.get("type") != "module_message":
        raise CaptureIPCRejected("AoE2Control IPC envelope 类型无效")
    if envelope.get("pipeName") != PIPE_NAME:
        raise CaptureIPCRejected("AoE2Control IPC pipe 不匹配")
    source = envelope.get("source")
    if (not isinstance(source, dict)
            or source.get("moduleName") != "evolab_driver"
            or source.get("assignedPlayerId") != 1):
        raise CaptureIPCRejected("AoE2Control IPC 来源不是 evolab_driver Player 1")
    return envelope


def _is_capture_ready(envelope):
    payload = envelope.get("payload")
    return (isinstance(payload, dict)
            and payload.get("action") == "capture_ready"
            and payload.get("protocol_version") == 1)


def _capture_hello_request():
    return {
        "target": {"assignedPlayerId": 1, "moduleName": "evolab_driver"},
        "payload": {"action": "capture_hello", "protocol_version": 1},
    }


def _binding_request(match_id):
    if not isinstance(match_id, str) or not match_id.strip():
        raise CaptureIPCRejected("runner match_id 为空，不能请求 IPC 绑定")
    return {
        "target": {"assignedPlayerId": 1, "moduleName": "evolab_driver"},
        "payload": {"action": "bind_match", "match_id": match_id},
    }


def _send_capture_hellos(write, ready_event, stop_event, interval_s=0.5,
                         on_attempt=None):
    """Repeat hello until CONTROL answers or the bounded receiver stops."""
    attempts = 0
    encoded = json.dumps(_capture_hello_request()).encode("utf-8")
    while not ready_event.is_set() and not stop_event.is_set():
        write(encoded)
        attempts += 1
        if on_attempt is not None:
            on_attempt(attempts)
        if ready_event.wait(interval_s) or stop_event.is_set():
            break
    return attempts


def parse_ipc_envelope(message):
    """Validate a CONTROL outgoing capture envelope and retain payload exactly."""
    envelope = _decode_envelope(message)
    raw_sentinel = envelope.get("payload")
    raw = _validate_sentinel(raw_sentinel)
    return {"source": envelope["source"], "raw_sentinel": raw_sentinel,
            "match_id": raw["match_id"]}


def append_raw_capture(path, message, match_id, received_at_utc=None):
    """Create one immutable source JSONL file without replacing prior evidence."""
    if not isinstance(match_id, str) or not match_id.strip():
        raise CaptureIPCRejected("原始 sentinel 缺少 match_id")
    raw_sentinel = message.get("raw_sentinel") if isinstance(message, dict) else None
    raw = _validate_sentinel(raw_sentinel)
    if raw["match_id"] != match_id:
        raise CaptureIPCRejected("原始 sentinel match_id 与 runner 赛程不一致")
    received_at_utc = received_at_utc or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z")
    try:
        datetime.fromisoformat(received_at_utc.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CaptureIPCRejected("IPC 接收时间无效") from exc
    row = {
        "schema_version": 1,
        "kind": "raw_control_ipc_capture",
        "match_id": match_id,
        "received_at_utc": received_at_utc,
        "source": message.get("source"),
        "raw_sentinel": raw_sentinel,
        "raw_sentinel_sha256": hashlib.sha256(raw_sentinel.encode("utf-8")).hexdigest(),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise CaptureIPCRejected("该 match_id 的原始 IPC JSONL 已存在，拒绝覆盖/重复追加") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return row


def append_capture_association(path, association):
    """Append runner-owned identity and replay hashes after the replay is stable."""
    if not isinstance(association, dict) or not isinstance(association.get("match_id"), str):
        raise CaptureIPCRejected("runner 原始采集关联字段缺失")
    path = Path(path)
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except (OSError, ValueError) as exc:
        raise CaptureIPCRejected("关联原始 sentinel 前 JSONL 缺失或损坏") from exc
    if (len(rows) != 1 or rows[0].get("kind") != "raw_control_ipc_capture"
            or rows[0].get("match_id") != association["match_id"]):
        raise CaptureIPCRejected("原始 IPC JSONL 缺失、重复或 match_id 不一致")
    normalized = {
        "match_id": association["match_id"],
        "slots_by_alias": association.get("slots_by_alias"),
        "installed": association.get("installed"),
        "recording": association.get("recording"),
    }
    if (not isinstance(normalized["slots_by_alias"], dict)
            or not isinstance(normalized["installed"], dict)
            or not isinstance(normalized["recording"], dict)
            or not normalized["recording"].get("sha256")):
        raise CaptureIPCRejected("runner 原始采集关联缺少槽位、.per 或录像 hash")
    row = {"schema_version": 1, "kind": "runner_capture_association", **normalized}
    encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND)
    except OSError as exc:
        raise CaptureIPCRejected("无法向原始 IPC JSONL 追加赛程关联") from exc
    with os.fdopen(fd, "ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return row


def raw_capture_path(directory, match_id):
    if not isinstance(match_id, str) or not _MATCH_ID_RE.fullmatch(match_id):
        raise CaptureIPCRejected("match_id 不能安全用于原始 IPC JSONL 文件名")
    return Path(directory) / ("result_capture_raw_%s.jsonl" % match_id)


def load_raw_capture(path, expected_match_id, expected_association=None):
    """Load raw source plus runner association and verify both immutable rows."""
    try:
        rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except (OSError, ValueError) as exc:
        raise CaptureIPCRejected("原始 IPC JSONL 缺失或损坏") from exc
    if len(rows) != 2:
        raise CaptureIPCRejected("原始 IPC JSONL 必须且只能包含 sentinel 与关联两条记录")
    source_rows = [row for row in rows if isinstance(row, dict)
                   and row.get("kind") == "raw_control_ipc_capture"]
    association_rows = [row for row in rows if isinstance(row, dict)
                        and row.get("kind") == "runner_capture_association"]
    if len(source_rows) != 1 or len(association_rows) != 1:
        raise CaptureIPCRejected("原始 IPC JSONL sentinel 或关联记录重复/缺失")
    row = source_rows[0]
    association = association_rows[0]
    if (row.get("schema_version") != 1 or association.get("schema_version") != 1):
        raise CaptureIPCRejected("原始 IPC JSONL 记录类型无效")
    if (row.get("match_id") != expected_match_id
            or association.get("match_id") != expected_match_id):
        raise CaptureIPCRejected("原始 IPC JSONL match_id 与 runner 关联不一致")
    source = row.get("source")
    if (not isinstance(source, dict) or source.get("moduleName") != "evolab_driver"
            or source.get("assignedPlayerId") != 1):
        raise CaptureIPCRejected("原始 IPC JSONL 来源无效")
    raw_sentinel = row.get("raw_sentinel")
    _validate_sentinel(raw_sentinel)
    digest = hashlib.sha256(raw_sentinel.encode("utf-8")).hexdigest()
    if row.get("raw_sentinel_sha256") != digest:
        raise CaptureIPCRejected("原始 sentinel SHA-256 校验失败")
    association = {key: association.get(key) for key in (
        "match_id", "slots_by_alias", "installed", "recording")}
    if expected_association is not None:
        expected = {key: expected_association.get(key) for key in (
            "match_id", "slots_by_alias", "installed", "recording")}
        if association != expected:
            raise CaptureIPCRejected("原始 JSONL 的槽位/.per/录像关联与 runner_evidence 不一致")
    return {**row, "runner_association": association}


class WindowsPipeCaptureReceiver:
    """Connect to the CONTROL IPC server and persist each received source frame."""

    def __init__(self, path, match_id, connect_timeout_s=600, diagnostic=None):
        self.path = Path(path)
        self.match_id = match_id
        self.connect_timeout_s = connect_timeout_s
        self._stop = threading.Event()
        self._received = threading.Event()
        self._bound = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._hello_thread = None
        self._handle = None
        self._error = None
        self._row = None
        self._handshake_phase = "connecting"
        self._diagnostic = diagnostic
        self._write_lock = threading.Lock()
        self._connection_attempts = 0
        self._hello_attempts = 0
        self._binding_sent = False

    @property
    def handshake_phase(self):
        return self._handshake_phase

    def _emit(self, stage, **details):
        if self._diagnostic is not None:
            self._diagnostic({"stage": stage, "phase": self._handshake_phase, **details})

    def start(self):
        if os.name != "nt":
            raise OSError("AoE2Control named-pipe capture is Windows-only")
        self._emit("connect_started", pipe=PIPE_PATH)
        self._thread = threading.Thread(
            target=self._receive, name="AoE2ControlCaptureIPC", daemon=True)
        self._thread.start()

    def _receive(self):
        import pywintypes
        import win32file
        import win32pipe

        deadline = time.monotonic() + self.connect_timeout_s
        try:
            while not self._stop.is_set():
                try:
                    self._connection_attempts += 1
                    self._handle = win32file.CreateFile(
                        PIPE_PATH, win32file.GENERIC_READ | win32file.GENERIC_WRITE, 0, None,
                        win32file.OPEN_EXISTING, 0, None)
                    break
                except pywintypes.error as exc:
                    if exc.winerror not in (2, 231):
                        raise
                    if time.monotonic() >= deadline:
                        self._emit("connect_timeout", attempts=self._connection_attempts,
                                   winerror=exc.winerror)
                        raise TimeoutError("等待 AoE2Control IPC 命名管道超时")
                    if self._connection_attempts == 1 or self._connection_attempts % 20 == 0:
                        self._emit("connect_retry", attempts=self._connection_attempts,
                                   winerror=exc.winerror)
                    self._stop.wait(0.5)
            if self._stop.is_set():
                return
            self._handshake_phase = "waiting_for_control_ready"
            self._emit("pipe_connected", attempts=self._connection_attempts)
            win32pipe.SetNamedPipeHandleState(
                self._handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
            self._hello_thread = threading.Thread(
                target=self._send_hellos, name="AoE2ControlCaptureHello", daemon=True)
            self._hello_thread.start()
            while not self._stop.is_set():
                parts = []
                while True:
                    status, data = win32file.ReadFile(self._handle, 64 * 1024)
                    parts.append(data)
                    if status == 0:
                        break
                    if status != 234:  # ERROR_MORE_DATA
                        raise OSError("AoE2Control IPC ReadFile 返回错误 %s" % status)
                envelope = _decode_envelope(b"".join(parts).decode("utf-8"))
                payload = envelope.get("payload")
                if _is_capture_ready(envelope):
                    self._ready.set()
                    self._emit("capture_ready_received", hello_attempts=self._hello_attempts)
                    if not self._binding_sent:
                        binding = _binding_request(self.match_id)
                        with self._write_lock:
                            win32file.WriteFile(
                                self._handle, json.dumps(binding).encode("utf-8"))
                        self._binding_sent = True
                        self._handshake_phase = "waiting_for_match_bound"
                        self._emit("bind_match_sent", match_id=self.match_id)
                    continue
                if (isinstance(payload, dict) and payload.get("action") == "match_bound"
                        and payload.get("match_id") == self.match_id):
                    if not self._binding_sent:
                        raise CaptureIPCRejected("收到绑定确认前未发送 runner bind_match")
                    if self._bound.is_set():
                        raise CaptureIPCRejected("重复收到 runner match_id 绑定确认")
                    self._bound.set()
                    self._handshake_phase = "bound"
                    self._emit("match_bound_received", match_id=self.match_id)
                    continue
                if not self._bound.is_set():
                    raise CaptureIPCRejected("收到原始 sentinel 前未确认 runner match_id 绑定")
                message = parse_ipc_envelope(json.dumps(envelope, ensure_ascii=False))
                self._row = append_raw_capture(self.path, message, self.match_id)
                self._received.set()
        except Exception as exc:
            self._error = exc
            self._handshake_phase = "failed"
            self._emit("handshake_failed", error="%s: %s" % (type(exc).__name__, exc))
            self._bound.set()
            self._received.set()

    def _send_hellos(self):
        import win32file

        def write(encoded):
            with self._write_lock:
                win32file.WriteFile(self._handle, encoded)

        def attempted(count):
            self._hello_attempts = count
            if count == 1 or count % 5 == 0:
                self._emit("capture_hello_sent", attempts=count)

        try:
            _send_capture_hellos(write, self._ready, self._stop, on_attempt=attempted)
        except Exception as exc:
            self._error = exc
            self._handshake_phase = "failed"
            self._emit("hello_send_failed", error="%s: %s" % (type(exc).__name__, exc))
            self._bound.set()
            self._received.set()

    def wait_for_capture(self, timeout_s):
        self._received.wait(timeout_s)
        if self._error is not None:
            raise CaptureIPCRejected("AoE2Control IPC 采集失败：%s" % self._error) from self._error
        return self._row

    def wait_until_bound(self, timeout_s):
        if not self._bound.wait(timeout_s):
            if self._error is not None:
                raise CaptureIPCRejected("runner match_id 绑定失败：%s" % self._error) from self._error
            self._emit("handshake_timeout", timeout_s=timeout_s)
            raise CaptureIPCRejected(
                "runner match_id 绑定超时，握手阶段=%s" % self._handshake_phase)
        if self._error is not None:
            raise CaptureIPCRejected("runner match_id 绑定失败：%s" % self._error) from self._error
        return True

    def stop(self):
        self._stop.set()
        if self._handle is not None:
            try:
                import win32file
                win32file.CloseHandle(self._handle)
            except Exception:
                pass
        if self._hello_thread is not None:
            self._hello_thread.join(timeout=2)
            if self._hello_thread.is_alive():
                raise RuntimeError("AoE2Control IPC hello 线程未能有界退出")
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("AoE2Control IPC 接收线程未能有界退出")
