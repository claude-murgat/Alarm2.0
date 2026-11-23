"""INV-191 : le mailer des scripts hote (/opt/alarm/send-alert-email.py, sauvegardes, disque) lit les
vrais secrets SMTP. Incident du 2026-11-10 sur NODE3 : `.env.prod.secrets.example` (fichier du depot,
present dans /opt/alarm) passait apres `.env.prod.secrets` dans le tri et ecrasait SMTP_USER/SMTP_PASS
par les valeurs factices : « email KO: Connection unexpectedly closed »."""
import importlib.util
import pathlib
from unittest import mock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.no_mutation]  # script hote, hors backend/app/logic

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "send-alert-email.py"


def load_mailer(env_files):
    spec = importlib.util.spec_from_file_location("send_alert_email", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch("glob.glob", return_value=[str(p) for p in env_files]):
        spec.loader.exec_module(mod)
    return mod


def test_example_file_never_overrides_real_secrets(tmp_path):
    node = tmp_path / ".env.prod.node3"
    node.write_text("SMTP_HOST=smtp.gmail.com\nSMTP_PORT=587\n")
    secrets = tmp_path / ".env.prod.secrets"
    secrets.write_text("SMTP_USER=vrai@exemple.fr\nSMTP_PASS=vrai-mot-de-passe\n")
    example = tmp_path / ".env.prod.secrets.example"
    example.write_text("SMTP_USER=changeme@example.com\nSMTP_PASS=changeme\n")
    mailer = load_mailer([node, secrets, example])
    assert mailer.cfg("SMTP_USER") == "vrai@exemple.fr"
    assert mailer.cfg("SMTP_PASS") == "vrai-mot-de-passe"
    assert mailer.cfg("SMTP_HOST") == "smtp.gmail.com"


def test_secrets_still_override_per_node_files(tmp_path):
    node = tmp_path / ".env.prod.node3"
    node.write_text("SMTP_USER=noeud@exemple.fr\n")
    secrets = tmp_path / ".env.prod.secrets"
    secrets.write_text("SMTP_USER=vrai@exemple.fr\n")
    assert load_mailer([node, secrets]).cfg("SMTP_USER") == "vrai@exemple.fr"
