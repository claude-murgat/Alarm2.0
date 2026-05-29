"""Entry point — Slack Socket Mode bot.

Pré-requis :
  - Variables d'env (voir .env.example)
  - Slack App configurée avec :
      * Socket Mode ON
      * Event Subscriptions : message.im, app_mention
      * Scopes bot: chat:write, im:history, im:read, app_mentions:read

Lance avec :
  python main.py

Architecture :
  - Single process Python
  - Sessions multiplexées par thread Slack (= 1 thread = 1 incident)
  - Dictionnaire en mémoire `SESSIONS: thread_ts -> IncidentSession`
  - Si le bot redémarre, les sessions en cours sont perdues (POC)
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

# override=True : sinon une var vide existante dans le shell parent (ex: venv,
# CI, env hérité) bloque la valeur réelle du .env.
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("alarm-sre-bot")

# Import après load_dotenv pour que ANTHROPIC_API_KEY soit chargée
from agent import IncidentSession, handle_user_message  # noqa: E402
from audit import log as audit_log  # noqa: E402


SESSIONS: dict[str, IncidentSession] = {}
SESSIONS_LOCK = threading.Lock()

# Auto-close : ferme une session inactive depuis plus de N secondes.
# Évite d'accumuler des sessions zombies (et leur historique qui croît la
# facture Anthropic à chaque relance par l'user).
AUTOCLOSE_AFTER_S = int(os.getenv("AUTOCLOSE_AFTER_S", "1800"))   # 30 min
AUTOCLOSE_SCAN_INTERVAL_S = int(os.getenv("AUTOCLOSE_SCAN_INTERVAL_S", "300"))  # 5 min


def _autoclose_loop(post: callable) -> None:
    """Thread daemon : ferme les sessions inactives au-delà du seuil.
    Le message Slack signale gentiment à l'user que la conv est archivée ;
    un nouveau message ouvrira automatiquement un nouvel incident."""
    while True:
        time.sleep(AUTOCLOSE_SCAN_INTERVAL_S)
        now = time.time()
        to_close: list[IncidentSession] = []
        with SESSIONS_LOCK:
            for ts, sess in list(SESSIONS.items()):
                if sess.closed:
                    # Cleanup des sessions déjà closes (par finish/escalate)
                    del SESSIONS[ts]
                    continue
                if now - sess.last_activity_at > AUTOCLOSE_AFTER_S:
                    to_close.append(sess)
                    del SESSIONS[ts]

        for sess in to_close:
            sess.closed = True
            audit_log(
                "incident_auto_close",
                incident_id=sess.incident_id,
                inactive_s=int(now - sess.last_activity_at),
            )
            try:
                post(
                    sess.slack_channel, sess.slack_thread_ts,
                    f"⏱️ Session `{sess.incident_id}` fermée pour inactivité "
                    f"(>{AUTOCLOSE_AFTER_S // 60} min). Tu peux rouvrir une "
                    f"nouvelle conversation en écrivant à nouveau.",
                )
            except Exception as e:
                logger.exception(f"Slack post autoclose failed: {e}")


def _slack_post(client: WebClient):
    """Closure qui renvoie une fonction post(channel, thread_ts, text)."""
    def post(channel: str, thread_ts: str, text: str) -> None:
        try:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text,
            )
        except Exception as e:
            logger.exception(f"Slack post failed: {e}")
    return post


def _get_or_create_session(channel: str, thread_ts: str, user: str) -> IncidentSession:
    with SESSIONS_LOCK:
        sess = SESSIONS.get(thread_ts)
        if sess is None:
            sess = IncidentSession.new(channel, thread_ts, user)
            SESSIONS[thread_ts] = sess
            audit_log("incident_open",
                      incident_id=sess.incident_id,
                      channel=channel, thread_ts=thread_ts, user=user)
        return sess


def _handle_message_event(client: WebClient, event: dict[str, Any]) -> None:
    """Traite un event message Slack (DM ou mention)."""
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return  # nos propres messages

    user = event.get("user", "")
    channel = event.get("channel", "")
    text = (event.get("text") or "").strip()
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or ts

    if not text or not user:
        return

    sess = _get_or_create_session(channel, thread_ts, user)
    post = _slack_post(client)

    logger.info(f"[{sess.incident_id}] user msg: {text[:120]}")
    try:
        handle_user_message(sess, text, post)
    except Exception as e:
        logger.exception(f"Agent crash for {sess.incident_id}")
        audit_log("agent_exception",
                  incident_id=sess.incident_id, exception=str(e))
        post(channel, thread_ts,
             f"⚠️ erreur interne du bot ({type(e).__name__}). "
             f"Incident `{sess.incident_id}` — un sysadmin doit regarder.")


def _process_socket_request(client: SocketModeClient, req: SocketModeRequest) -> None:
    ack = SocketModeResponse(envelope_id=req.envelope_id)
    client.send_socket_mode_response(ack)

    if req.type != "events_api":
        return

    event = req.payload.get("event", {})
    event_type = event.get("type")

    if event_type in ("message", "app_mention"):
        # On gère DM (channel_type=im) et mentions du bot
        if event_type == "message" and event.get("channel_type") != "im":
            # Pour POC, on ne répond qu'aux DM (évite de polluer les channels)
            return
        _handle_message_event(client.web_client, event)


def main() -> None:
    bot_token = os.getenv("SLACK_BOT_TOKEN")
    app_token = os.getenv("SLACK_APP_TOKEN")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    missing = [k for k, v in [
        ("SLACK_BOT_TOKEN", bot_token),
        ("SLACK_APP_TOKEN", app_token),
        ("ANTHROPIC_API_KEY", anthropic_key),
    ] if not v]
    if missing:
        logger.error(f"Variables manquantes: {missing}. Voir .env.example")
        sys.exit(1)

    web = WebClient(token=bot_token)
    socket = SocketModeClient(app_token=app_token, web_client=web)
    socket.socket_mode_request_listeners.append(_process_socket_request)

    auth = web.auth_test()
    logger.info(f"Connecté à Slack en tant que {auth['user']} (équipe {auth['team']})")
    audit_log("bot_start", bot_user=auth.get("user"), team=auth.get("team"))

    # Démarre le thread auto-close (daemon → meurt avec le process)
    post = _slack_post(web)
    threading.Thread(
        target=_autoclose_loop, args=(post,), daemon=True, name="autoclose",
    ).start()
    logger.info(
        f"Auto-close: ferme sessions inactives >{AUTOCLOSE_AFTER_S}s "
        f"(scan toutes les {AUTOCLOSE_SCAN_INTERVAL_S}s)"
    )

    socket.connect()
    logger.info("Socket Mode connecté. En écoute. Ctrl+C pour arrêter.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        logger.info("Arrêt demandé.")
        audit_log("bot_stop")


if __name__ == "__main__":
    main()
