"""Exécution de commandes : LOCAL ou SSH vers un host autorisé.

L'agent ne décide jamais d'exécuter directement — il passe par run_on(host, cmd)
qui :
  1. Vérifie que host est dans ALLOWED_HOSTS
  2. Classifie cmd via policy.classify
  3. Si L4 → refuse + escalade
  4. Sinon exécute, audit log, retourne stdout/stderr/exit_code

Timeout par défaut 60s — escalade si dépassement.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from audit import log as audit_log
from policy import ALLOWED_HOSTS, Level, classify


@dataclass
class ExecResult:
    success: bool          # True si exit_code == 0 et pas de refus
    level: Level
    exit_code: Optional[int]
    stdout: str
    stderr: str
    refused_reason: Optional[str] = None
    matched_rule: Optional[str] = None
    cmd: str = ""
    host: str = ""

    def short_summary(self) -> str:
        if self.refused_reason:
            return f"[REFUSED:{self.level.name}] {self.refused_reason}"
        return f"[{self.level.name} exit={self.exit_code}] {self.stdout[:200]}"


def _build_ssh_argv(host_name: str, cmd: str) -> list[str]:
    h = ALLOWED_HOSTS[host_name]
    key_path = os.getenv(h["key_env"], "")
    if not key_path or not os.path.exists(key_path):
        raise RuntimeError(f"Clé SSH absente pour {host_name} (env {h['key_env']})")
    return [
        "ssh",
        "-p", str(h["port"]),
        "-i", key_path,
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{h['user']}@{h['host']}",
        cmd,
    ]


def run_on(
    host: str,
    cmd: str,
    *,
    reason: str,
    incident_id: str,
    timeout_s: int = 60,
) -> ExecResult:
    """Exécute cmd sur host. Toujours appelée via tools (jamais directement par Claude)."""
    if host not in ALLOWED_HOSTS:
        result = ExecResult(
            success=False, level=Level.L4, exit_code=None, stdout="", stderr="",
            refused_reason=f"host inconnu: {host}", cmd=cmd, host=host,
        )
        audit_log("exec_refused", incident_id=incident_id, host=host, cmd=cmd,
                  reason=reason, refused_reason=result.refused_reason)
        return result

    decision = classify(cmd)

    if decision.level == Level.L4:
        result = ExecResult(
            success=False, level=Level.L4, exit_code=None, stdout="", stderr="",
            refused_reason="hors allowlist", cmd=cmd, host=host,
        )
        audit_log("exec_refused", incident_id=incident_id, host=host, cmd=cmd,
                  reason=reason, refused_reason="hors allowlist")
        return result

    # OK on exécute
    if host == "LOCAL":
        argv = ["bash", "-c", cmd]
    else:
        argv = _build_ssh_argv(host, cmd)

    audit_log(
        "exec_start",
        incident_id=incident_id, host=host, level=decision.level.name,
        cmd=cmd, reason=reason, matched_rule=decision.matched_rule_description,
    )

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        result = ExecResult(
            success=(proc.returncode == 0),
            level=decision.level,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            matched_rule=decision.matched_rule_description,
            cmd=cmd, host=host,
        )
        audit_log(
            "exec_done",
            incident_id=incident_id, host=host, level=decision.level.name,
            cmd=cmd, exit_code=proc.returncode,
            stdout_len=len(proc.stdout), stderr_len=len(proc.stderr),
            stdout_preview=proc.stdout[:500],
            stderr_preview=proc.stderr[:500],
        )
        return result
    except subprocess.TimeoutExpired:
        audit_log("exec_timeout", incident_id=incident_id, host=host, cmd=cmd,
                  timeout_s=timeout_s)
        return ExecResult(
            success=False, level=decision.level, exit_code=None,
            stdout="", stderr=f"TIMEOUT après {timeout_s}s",
            cmd=cmd, host=host,
        )
    except Exception as e:
        audit_log("exec_exception", incident_id=incident_id, host=host, cmd=cmd,
                  exception=str(e))
        return ExecResult(
            success=False, level=decision.level, exit_code=None,
            stdout="", stderr=f"EXCEPTION: {e}",
            cmd=cmd, host=host,
        )
