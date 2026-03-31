"""
Service d'envoi d'emails.
En prod : utilise smtplib.
En test : stocke le dernier email dans une variable module-level.
"""

import logging
import smtplib
import os
from email.mime.text import MIMEText

logger = logging.getLogger("email_service")

# Module-level storage for the last email sent (used in tests)
_last_email = None


def send_alert_email(subject: str, body: str, to: str):
    """Send an alert email. In test/dev mode, just stores the email for retrieval."""
    global _last_email
    _last_email = {
        "sent": True,
        "subject": subject,
        "body": body,
        "to": to,
    }
    logger.info(f"Alert email stored: to={to}, subject={subject}")

    # In production with SMTP configured, actually send
    smtp_host = os.getenv("SMTP_HOST")
    if smtp_host:
        try:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = os.getenv("SMTP_FROM", "alarms@system.local")
            msg["To"] = to
            with smtplib.SMTP(smtp_host, int(os.getenv("SMTP_PORT", "587"))) as server:
                server.send_message(msg)
            logger.info(f"Email actually sent to {to}")
        except Exception as e:
            logger.error(f"Failed to send email: {e}")


def get_last_email():
    """Return the last email sent, or {'sent': False} if none."""
    if _last_email is None:
        return {"sent": False}
    return _last_email


def reset_last_email():
    """Reset the last email (for testing)."""
    global _last_email
    _last_email = None
