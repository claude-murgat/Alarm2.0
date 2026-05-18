"""Issue #105 : send_alert_email ne doit JAMAIS bloquer l'event loop indefiniment.

Cas en prod (cloud node3, 2026-05-18) : SMTP_HOST=host.docker.internal:1025 inexistant
→ TCP SYN timeout 110s default → coroutine asyncio bloquee pendant 110s →
escalation_loop ne tick plus → /health 503 silencieux pendant 5 jours.

Ces tests verrouillent :
1. SMTP_HOST vide/absent → return immediat (skip silencieux, _last_email maj quand meme).
2. SMTP_HOST sur port refused → fail immediat (pas de blocage TCP retry).
3. SMTP_TIMEOUT respecte (limite haute pour scenario timeout reseau).

Cf docs catalogue : pas d'INV pour l'instant, c'est un fix resilience pure infra.
"""
import os
import time

import pytest

from backend.app.email_service import (
    get_last_email,
    reset_last_email,
    send_alert_email,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_email_env(monkeypatch):
    """S'assure que les env vars SMTP_* du runtime de test ne fuitent pas
    dans nos cas. Chaque test set ce qu'il veut via monkeypatch."""
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM", "SMTP_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    reset_last_email()
    yield
    reset_last_email()


def test_smtp_host_vide_skip_immediat(monkeypatch):
    """Issue #105 cas 1 : SMTP_HOST="" → return en moins de 100ms, pas de tentative TCP."""
    monkeypatch.setenv("SMTP_HOST", "")

    start = time.monotonic()
    send_alert_email("subj", "body", "to@example.com")
    elapsed = time.monotonic() - start

    assert elapsed < 0.1, (
        f"SMTP_HOST vide doit retourner immediat (skip), got {elapsed:.3f}s — "
        "le code tente d'ouvrir un socket alors qu'il devrait return tot."
    )
    # _last_email maj quand meme (pour /api/test/last-email-sent)
    assert get_last_email() == {
        "sent": True,
        "subject": "subj",
        "body": "body",
        "to": "to@example.com",
    }


def test_smtp_host_absent_skip_immediat():
    """Issue #105 cas 1bis : SMTP_HOST non set (None) → idem return immediat."""
    # _clean_email_env a deja delete SMTP_HOST
    start = time.monotonic()
    send_alert_email("subj", "body", "to@example.com")
    elapsed = time.monotonic() - start

    assert elapsed < 0.1, (
        f"SMTP_HOST absent doit retourner immediat (skip), got {elapsed:.3f}s"
    )


def test_smtp_host_refused_fail_rapide_pas_exception(monkeypatch):
    """Issue #105 cas 2 : SMTP_HOST sur port refused → fail en moins de 1s,
    pas de raise (l'exception est loguee en ERROR mais avalee pour ne pas
    crasher l'appelant)."""
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_PORT", "1")  # port garanti refused
    monkeypatch.setenv("SMTP_TIMEOUT", "5")

    start = time.monotonic()
    # Doit ne pas raise
    send_alert_email("subj", "body", "to@example.com")
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, (
        f"Connection refused doit etre immediat (kernel renvoie ECONNREFUSED instant), "
        f"got {elapsed:.3f}s"
    )


def test_smtp_timeout_respecte_si_host_drop(monkeypatch):
    """Issue #105 cas 3 : SMTP_HOST drop SYN (host non routable) → fail en
    moins de SMTP_TIMEOUT + marge, pas 110s default kernel.

    Utilise 10.255.255.1 (TEST-NET-1 / RFC 5737 reserve) qui drop typiquement.
    SMTP_TIMEOUT=2 pour rester rapide en CI.
    """
    monkeypatch.setenv("SMTP_HOST", "10.255.255.1")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_TIMEOUT", "2")

    start = time.monotonic()
    send_alert_email("subj", "body", "to@example.com")
    elapsed = time.monotonic() - start

    # Marge : 2s SMTP_TIMEOUT + 1s pour DNS/setup, max 5s. Sans le fix, ce
    # test prendrait 110s (default kernel TCP timeout).
    assert elapsed < 5.0, (
        f"SMTP_TIMEOUT=2 doit etre respecte, got {elapsed:.1f}s "
        f"(sans le fix #105 : ~110s, regression infra critique)"
    )
