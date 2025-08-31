import json
import os
from datetime import datetime
from typing import Any, Dict

# Maintain per-session filename base so all writes go to the same timestamped file
_SESSION_FILE_BASE: Dict[str, str] = {}


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _file_base_for(session_id: str) -> str:
    """Return a stable '<timestamp>_<session_id>' base for this process."""
    if session_id in _SESSION_FILE_BASE:
        return _SESSION_FILE_BASE[session_id]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{ts}_{session_id}"
    _SESSION_FILE_BASE[session_id] = base
    return base


def audit_write(session_id: str, record: Dict[str, Any]) -> None:
    """Append a structured JSON line to the per-session audit log.

    The file is stored under logs/sessions/<session_id>.log relative to repo root.
    """
    # Resolve logs dir relative to this file: ../../logs/sessions
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs", "sessions"))
    _ensure_dir(base_dir)
    record = dict(record)
    record.setdefault("ts", datetime.utcnow().isoformat() + "Z")
    record.setdefault("session_id", session_id)
    base = _file_base_for(session_id)
    log_path = os.path.join(base_dir, f"{base}.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Never raise from audit logging; it's best-effort.
        pass


def maplog_write(session_id: str, record: Dict[str, Any]) -> None:
    """Append a structured JSON line to the per-session .map log.

    The file is stored under logs/sessions/<session_id>.map and is intended
    to hold only map bootstrap and carrier move instructions for reproduction/testing.
    """
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "logs", "sessions"))
    _ensure_dir(base_dir)
    base = _file_base_for(session_id)
    log_path = os.path.join(base_dir, f"{base}.map")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
