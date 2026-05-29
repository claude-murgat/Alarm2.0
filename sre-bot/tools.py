"""Tool definitions exposées à Claude via l'API.

5 tools :
  - run_command   : exécute une commande shell (LOCAL ou SSH)
  - ask_user      : pose une question à l'utilisateur via Slack et ATTEND la réponse
  - report_user   : message d'avancement à l'utilisateur (pas d'attente)
  - escalate      : ping le sysadmin (= fin de la session)
  - finish        : clôt l'incident avec un résumé

ask_user et finish sont des "actions terminales" pour le tour courant :
  - ask_user => le bot se met en pause, attend le prochain message Slack
  - finish   => l'incident est clos, on archive et on libère la session
"""
from __future__ import annotations


TOOLS_SCHEMA = [
    {
        "name": "run_command",
        "description": (
            "Exécute une commande shell sur LOCAL (machine du bot) ou via SSH sur "
            "node3 / onsite-1 / onsite-2. La commande est matchée contre une "
            "policy d'allowlist (L1=lecture, L2=restart safe, L3=action prod "
            "réversible). Si hors allowlist (L4), refus immédiat et tu dois "
            "appeler escalate. Toujours fournir une raison concise (servira "
            "dans l'audit log et le rapport final)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "enum": ["LOCAL", "node3", "onsite-1", "onsite-2"],
                    "description": "Où exécuter la commande.",
                },
                "command": {
                    "type": "string",
                    "description": (
                        "Commande shell complète. Pour SSH, c'est ce qui sera "
                        "passé après `ssh user@host`. Le ssh-wrapper est ajouté "
                        "automatiquement."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Pourquoi tu exécutes ça (1 phrase). Ex: 'vérifier "
                        "que le replica est revenu en streaming après reinit'."
                    ),
                },
            },
            "required": ["host", "command", "reason"],
        },
    },
    {
        "name": "ask_user",
        "description": (
            "Pose une question à l'utilisateur via Slack et attend sa réponse. "
            "Utilise ce tool pour les informations que tu ne peux pas obtenir "
            "par diagnostic (ex: 'depuis quand vois-tu ce symptôme ?', 'as-tu "
            "fait un changement récent ?'). Pose une question CONCRÈTE et "
            "COMPRÉHENSIBLE pour un user non-technique. Pas de jargon."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Question à poser, en français, sans jargon.",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "report_user",
        "description": (
            "Envoie un message d'avancement à l'utilisateur sur Slack (pas "
            "d'attente de réponse). Utilise pour signaler une action en cours "
            "ou une découverte. En français lisible, pas de jargon. Reste "
            "synthétique (1-3 phrases)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Texte du message, en français lisible.",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "escalate",
        "description": (
            "Ping le sysadmin technique (par Slack DM) et termine la session. "
            "À utiliser quand : (a) une commande nécessaire est hors allowlist, "
            "(b) tu n'as plus d'hypothèse après plusieurs tentatives, (c) une "
            "vérification post-action a échoué, (d) le symptôme dépasse ton "
            "périmètre. NE PAS escalader pour des problèmes mineurs résolus."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "Résumé technique pour le sysadmin : ce qui a été "
                        "diagnostiqué, ce qui a été tenté, ce qui bloque."
                    ),
                },
            },
            "required": ["summary"],
        },
    },
    {
        "name": "finish",
        "description": (
            "Clôt l'incident. À appeler quand le problème est résolu (état "
            "vérifié) ou quand l'utilisateur a confirmé que le symptôme est "
            "parti. Rédige un résumé final en français (cause, action, "
            "vérification) qui sera envoyé à l'utilisateur."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "Résumé en français lisible pour l'utilisateur : "
                        "ce qui se passait, ce que tu as fait, l'état final."
                    ),
                },
            },
            "required": ["summary"],
        },
    },
]
