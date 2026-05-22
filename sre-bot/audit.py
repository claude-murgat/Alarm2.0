"""Audit log JSON append-only.

Chaque action (commande exécutée, refus policy, vérification, escalade) est
loggée en une ligne JSON. Ne JAMAIS supprimer/modifier l'audit log — c'est
le seul moyen de revenir en arrière sur ce que le bot a fait.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _audit_path() -> Path:
    p = Path(os.getenv("AUDIT_LOG_PATH", "./audit.log"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def log(event_type: str, **fields: Any) -> None:
    """Append une ligne JSON à l'audit log."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ts_unix": time.time(),
        "event": event_type,
        **fields,
    }
    with _audit_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
