"""Durable checkpoint notifications through the native ``codex queue`` CLI.

Queue acceptance is not agent acknowledgement and never releases F10 approval.
Only metadata is queued; screenshots and raw artifacts remain in the run folder.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid


DEFAULT_CODEX = "codex"
_SENSITIVE = re.compile(r"(?i)(password|passwd|api[ _-]?key|authorization|bearer|ssh[ _-]?command|credential|secret|private[ _-]?key|access[ _-]?token)")


def _utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _write(path, value):
    temporary = path.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _safe(value):
    if isinstance(value, str):
        return "[redacted sensitive text]" if _SENSITIVE.search(value) else value[:2000]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items() if not _SENSITIVE.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value[:32]]
    return value if value is None or isinstance(value, (bool, int, float)) else str(type(value).__name__)


@contextmanager
def _run_lock(path):
    """OS file locks release on process death; lock contents contain no secrets."""
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _run_state(run):
    if hasattr(run, "path") and hasattr(run, "load"):
        return Path(run.path), run.load()
    folder = Path(run)
    return folder, json.loads((folder / "state.json").read_text(encoding="utf-8-sig"))


def _evidence(folder, items):
    if items is None:
        return []
    if isinstance(items, (str, Path, dict)):
        items = [items]
    result = []
    for item in items:
        if isinstance(item, (str, Path)):
            item = {"path": str(item)}
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("Notification evidence must reference a path")
        name = item["path"]
        if any(part.lower() in ("secrets", ".env") for part in re.split(r"[\\/]", name)) or _SENSITIVE.search(name):
            raise ValueError("Credential paths cannot be notification evidence")
        digest = item.get("sha256")
        if digest is None:
            path = Path(name)
            path = path if path.is_absolute() else folder / path
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            raise ValueError("Notification evidence needs a SHA-256")
        result.append({"path": name, "sha256": digest.lower()})
    return result


class Notifier:
    def __init__(self, config):
        if isinstance(config, (str, Path)):
            config = json.loads(Path(config).read_text(encoding="utf-8-sig"))
        self.options = dict(config.get("notifications", {}))
        self.data_config = {"telemetry_db": config.get("telemetry_db")}
        self.enabled = self.options.get("enabled", False) is True
        self.dry_run = self.options.get("dry_run", False) is True

    def _thread(self):
        value = self.options.get("thread_id") or os.environ.get("CODEX_THREAD_ID")
        if not value:
            raise ValueError("An explicit notifications.thread_id or CODEX_THREAD_ID is required")
        # Never guess --last or silently select/start a new conversation.
        return str(uuid.UUID(str(value)))

    def _send(self, folder, record, path, *, dry_run=False, force=False):
        if record["status"] == "queued":
            return {"status": "already_queued", "event_id": record["event_id"]}
        if not self.enabled:
            return {"status": "disabled", "event_id": record["event_id"]}
        if dry_run or self.dry_run:
            return {"status": "dry_run", "event_id": record["event_id"], "payload": record["payload"]}
        previous = record.get("last_attempt_utc")
        if previous and not force:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(previous)).total_seconds()
            if age < float(self.options.get("retry_seconds", 30)):
                return {"status": "deferred", "event_id": record["event_id"]}
        record["last_attempt_utc"] = _utc()
        record["attempts"] = record.get("attempts", 0) + 1
        try:
            thread = self._thread()
            message = ("NanoKVM脚本检查点。通知不构成F10放行；请读取本地证据后判断。\n"
                       + json.dumps(record["payload"], ensure_ascii=False, sort_keys=True))
            command = [str(self.options.get("codex_executable", DEFAULT_CODEX)), "queue",
                       "--thread", thread, "--message", message]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=float(self.options.get("timeout_seconds", 20)),
                                    shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                record["last_error"] = {"type": "queue_exit", "returncode": result.returncode}
                _write(path, record)
                return {"status": "pending", "event_id": record["event_id"], "error": record["last_error"]}
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            # CLI diagnostics can contain machine configuration. Persist the
            # class, never unreviewed stdout/stderr or credential-bearing args.
            record["last_error"] = {"type": type(error).__name__}
            _write(path, record)
            return {"status": "pending", "event_id": record["event_id"], "error": record["last_error"]}
        record.update(status="queued", queued_utc=_utc(), thread_id=thread)
        record.pop("last_error", None)
        _write(path, record)
        return {"status": "queued", "event_id": record["event_id"], "thread_id": thread,
                "meaning": "accepted by native queue; agent handling not acknowledged"}

    def notify(self, run, event, summary, evidence=None, *, dry_run=False):
        folder, state = _run_state(run)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", event):
            raise ValueError("Invalid notification event")
        expected = state.get("expected_changes")
        request = folder / "review_request.json"
        if expected is None and event in ("f10", "f10_review", "awaiting_f10_review") and request.is_file():
            expected = json.loads(request.read_text(encoding="utf-8-sig")).get("expected_changes")
        chosen = evidence if evidence is not None else state.get("evidence", [])
        metadata = {"run_id": str(state["run_id"]), "event": event,
                    "stage": _safe(str(state.get("stage", "unknown"))),
                    "expected_changes": _safe(expected), "evidence": _evidence(folder, chosen),
                    "run_path": str(folder.resolve())}
        # Polling summary wording and timestamps are deliberately excluded:
        # unchanged state/evidence must not wake the agent again.
        event_id = _hash(metadata)
        payload = dict(metadata, event_id=event_id, summary=_safe(str(summary)))
        if event in ("test_finished", "test_interrupted", "test_artifact_error", "worker_failure"):
            from .evidence_status import data_sources, availability_summary
            sources = data_sources(self.data_config, folder, state)
            payload["data_sources"] = _safe(sources)
            payload["summary"] = _safe(str(summary) + " " + availability_summary(sources))
        store = folder / "notifications"
        store.mkdir(exist_ok=True)
        path = store / (event_id + ".json")
        try:
            with _run_lock(store / ".lock"):
                if path.is_file():
                    record = json.loads(path.read_text(encoding="utf-8"))
                else:
                    record = {"event_id": event_id, "status": "pending", "created_utc": _utc(),
                              "attempts": 0, "payload": payload}
                    _write(path, record)
                return self._send(folder, record, path, dry_run=dry_run)
        except (BlockingIOError, PermissionError):
            return {"status": "busy", "event_id": event_id}

    def outbox(self, run):
        folder, _ = _run_state(run)
        store = folder / "notifications"
        if not store.exists():
            return []
        records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(store.glob("*.json"))]
        return [record for record in records if record["status"] == "pending"]

    def drain(self, run, *, dry_run=False, force=False):
        folder, _ = _run_state(run)
        store = folder / "notifications"
        if not store.exists():
            return []
        results = []
        try:
            with _run_lock(store / ".lock"):
                for path in sorted(store.glob("*.json")):
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if record["status"] == "pending":
                        results.append(self._send(folder, record, path, dry_run=dry_run, force=force))
        except (BlockingIOError, PermissionError):
            return [{"status": "busy"}]
        return results
