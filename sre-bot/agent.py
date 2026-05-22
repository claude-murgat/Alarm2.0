"""Boucle agent Claude pour une session d'incident.

Chaque thread Slack = une instance d'IncidentSession. La session conserve
l'historique des messages et le state. Sur chaque nouveau message user, on
appelle Claude API en boucle :
  - Claude renvoie soit du texte (ignoré), soit des tool_use
  - On exécute les tools, on rejoue Claude jusqu'à ask_user / finish / escalate
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import anthropic

from audit import log as audit_log
from executor import run_on
from tools import TOOLS_SCHEMA


SYSTEM_PROMPT = """\
Tu es un agent SRE pour le système Alarme Murgat. Tu réponds aux utilisateurs
sur Slack qui signalent un dysfonctionnement et tu diagnostiques + résouds en
autonomie quand possible.

# Périmètre
- 3 nœuds prod : `node3` (cloud OVH, IP 51.210.105.102), `onsite-1` (LAN
  172.16.1.121), `onsite-2` (LAN 172.16.1.120).
- Stack par nœud : container docker `node{1,2,3}-{backend,patroni,etcd}-1`,
  PostgreSQL via Patroni (cluster `alarm-cluster`), backend FastAPI port 8000,
  Patroni REST port 8008, etcd port 2379.
- Gateway SIM7600 (SMS/voix/contact sec) sur onsite-2 : process Python
  `modem_gateway.py` lancé en nohup par l'user `alarm`.

# Règles de comportement
1. **Parle français lisible** à l'utilisateur. Pas de jargon technique, pas
   de listes de commandes shell. L'utilisateur n'est PAS un dev. Ex au lieu
   de "le replica node3 est sur la timeline 70 alors que le cluster est à
   72" → "Le serveur de Paris n'arrive plus à se synchroniser avec le
   reste, je le relance proprement".
2. **Pose des questions concrètes** si le symptôme est ambigu. Ex : "vois-
   tu encore l'alarme rouge dans l'app ?", "à quelle heure as-tu remarqué
   ça ?". Pas de question type "donne-moi la stacktrace".
3. **Diagnostic descendant** : du symptôme externe → cause racine. Ne devine
   pas, observe (logs, état des process, queries DB).
4. **Une action à la fois**. Vérifie après chaque action que ça a marché
   (re-curl /health, ps, query DB). Si vérification échoue → escalade,
   ne réessaie pas en boucle.
5. **L'audit est obligatoire** : chaque `run_command` doit avoir une `reason`
   explicite, c'est ce qui figurera dans l'historique.

# Niveaux d'action
- L1 (lecture) : tu peux exécuter librement (logs, status, SELECT).
- L2 (restart safe : kill orphelin, restart container) : exécute en signalant
  à l'user.
- L3 (action prod réversible : patroni reinit, switchover) : exécute en
  signalant à l'user. Annonce avant pour donner 1-2 phrases de raison.
- L4 (hors allowlist) : refus automatique → appelle `escalate` avec un
  résumé technique pour le sysadmin.

# Critères d'escalade
- Symptôme inconnu après 3 hypothèses testées.
- Vérification post-action échouée.
- Commande nécessaire hors allowlist.
- Doute sur l'impact d'une action.

# Fin de session
- Si résolu : appelle `finish` avec un résumé en français pour l'user.
- Si bloqué : appelle `escalate` avec un résumé technique pour le sysadmin.
- Si tu as besoin d'info de l'user : appelle `ask_user` (le bot se met en
  pause jusqu'à la prochaine réponse Slack).

Tu travailles incident par incident. Ne mélange pas les contextes.
"""


@dataclass
class IncidentSession:
    """Une conversation/incident = un thread Slack."""
    incident_id: str
    slack_channel: str
    slack_thread_ts: str
    user_slack_id: str
    messages: list[dict] = field(default_factory=list)
    closed: bool = False
    awaiting_user: bool = False
    last_activity_at: float = field(default_factory=time.time)

    @classmethod
    def new(cls, slack_channel: str, slack_thread_ts: str, user_slack_id: str) -> "IncidentSession":
        return cls(
            incident_id=f"inc-{uuid.uuid4().hex[:8]}",
            slack_channel=slack_channel,
            slack_thread_ts=slack_thread_ts,
            user_slack_id=user_slack_id,
        )

    def touch(self) -> None:
        """Marque une activité (user msg ou tool call). Empêche l'auto-close."""
        self.last_activity_at = time.time()


# Callbacks injectés par slack_bot.py — défère les appels Slack pour ne pas
# coupler agent et slack ici (testable en standalone)
SlackPostFn = Callable[[str, str, str], None]
# signature: post(channel, thread_ts, text) -> None


def _build_anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def handle_user_message(
    session: IncidentSession,
    user_text: str,
    slack_post: SlackPostFn,
) -> None:
    """Reçoit un message user, fait tourner l'agent jusqu'à un point d'arrêt
    (ask_user, finish, escalate, ou max_turns)."""

    if session.closed:
        slack_post(session.slack_channel, session.slack_thread_ts,
                   "Incident déjà clos. Ouvre un nouveau thread pour autre chose.")
        return

    session.awaiting_user = False
    session.touch()
    session.messages.append({"role": "user", "content": user_text})
    audit_log("user_message",
              incident_id=session.incident_id,
              user=session.user_slack_id,
              text=user_text)

    client = _build_anthropic_client()
    model = os.getenv("CLAUDE_MODEL", "claude-opus-4-7")
    max_turns = 25  # garde-fou anti-boucle

    for turn in range(max_turns):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS_SCHEMA,
            messages=session.messages,
        )

        # Assistant message ajouté dans l'historique tel quel
        assistant_content = response.content
        session.messages.append({"role": "assistant", "content": assistant_content})
        audit_log("assistant_turn",
                  incident_id=session.incident_id,
                  turn=turn,
                  stop_reason=response.stop_reason,
                  blocks=[b.type for b in assistant_content])

        # Si pas de tool_use → l'agent a juste répondu en texte. On ne fait
        # rien (les messages user doivent passer par les tools). On boucle
        # avec un nudge.
        tool_uses = [b for b in assistant_content if b.type == "tool_use"]
        if not tool_uses:
            session.messages.append({
                "role": "user",
                "content": "Utilise un tool : ask_user / report_user / run_command / finish / escalate.",
            })
            continue

        tool_results = []
        terminal_tool: Optional[str] = None

        for tu in tool_uses:
            name = tu.name
            args = tu.input
            session.touch()
            audit_log("tool_call",
                      incident_id=session.incident_id,
                      tool=name, args=args, tool_use_id=tu.id)

            if name == "run_command":
                r = run_on(
                    host=args["host"],
                    cmd=args["command"],
                    reason=args["reason"],
                    incident_id=session.incident_id,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": (
                        f"level={r.level.name} exit={r.exit_code} "
                        f"refused={r.refused_reason or 'no'}\n"
                        f"--- stdout ---\n{r.stdout[:4000]}\n"
                        f"--- stderr ---\n{r.stderr[:2000]}"
                    ),
                    "is_error": not r.success,
                })

            elif name == "report_user":
                slack_post(session.slack_channel, session.slack_thread_ts,
                           args["message"])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": "envoyé",
                })

            elif name == "ask_user":
                slack_post(session.slack_channel, session.slack_thread_ts,
                           f"❓ {args['question']}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": "question posée, je me mets en pause",
                })
                terminal_tool = "ask_user"

            elif name == "escalate":
                escalate_id = os.getenv("ESCALATE_SLACK_USER_ID", "")
                ping = f"<@{escalate_id}> " if escalate_id else ""
                slack_post(session.slack_channel, session.slack_thread_ts,
                           f"{ping}🚨 Escalade — incident `{session.incident_id}`:\n"
                           f"{args['summary']}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": "escaladé, session terminée",
                })
                session.closed = True
                terminal_tool = "escalate"

            elif name == "finish":
                slack_post(session.slack_channel, session.slack_thread_ts,
                           f"✅ {args['summary']}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": "incident clos",
                })
                session.closed = True
                terminal_tool = "finish"

            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": f"tool inconnu: {name}",
                    "is_error": True,
                })

        session.messages.append({"role": "user", "content": tool_results})

        if terminal_tool == "ask_user":
            session.awaiting_user = True
            return
        if terminal_tool in ("escalate", "finish"):
            return

    # Boucle anti-fugue
    slack_post(session.slack_channel, session.slack_thread_ts,
               "⚠️ Max turns atteint, j'arrête pour éviter une boucle. "
               "Reformule ta demande ou ping un sysadmin.")
    audit_log("max_turns_reached", incident_id=session.incident_id)
    session.closed = True
