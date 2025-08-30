import json
import os
from datetime import datetime
from typing import Any, Dict


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


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
    log_path = os.path.join(base_dir, f"{session_id}.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Never raise from audit logging; it's best-effort.
        pass

