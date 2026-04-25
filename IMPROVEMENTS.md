# Alarm 2.0 — Plan d'ameliorations

## Statut global
- Date d'analyse : 2026-04-06
- Derniere mise a jour : 2026-04-25
- Total : 22 points identifies (14 logiciel + 8 fonctionnel)
- Termines : 14/22
- Rejetes : 3/22
- Restants : 5/22

---

## LOGICIEL (bonnes pratiques)

### 1. [DONE] Securiser les endpoints API (authentification + roles)
La majorite des endpoints n'ont aucune auth. send alarm, delete user, resolve, config escalation — tout est ouvert.
- Proteger tous les endpoints sensibles avec Bearer token
- Separer les roles : admin vs utilisateur standard
- Endpoints concernes : POST /alarms/send, DELETE /users/{id}, POST /alarms/{id}/resolve, POST/GET /config/*, GET /users/, PATCH /users/{id}, GET /alarms/, GET /alarms/active, POST /alarms/reset

### 2. [DONE] Conditionner les endpoints de test a une variable d'environnement
/api/test/* permet de casser le systeme (advance-clock, simulate-watchdog-failure, reset).
- Variable ENABLE_TEST_ENDPOINTS=true/false
- Desactives par defaut en production

### 3. [DONE] Remplacer notified_user_ids CSV par une table de liaison
"1,3,5" en texte = fragile, pas de jointures SQL possibles.
- Creer table alarm_notifications(alarm_id, user_id, notified_at)
- Adapter les queries et le code d'escalade

### 4. [DONE] Restreindre CORS aux origines legitimes
allow_origins=["*"] expose l'API a tout le web.
- Variable ALLOWED_ORIGINS avec les domaines autorises
- Garder "*" uniquement en dev

### 5. [DONE] Verifier/externaliser le SECRET_KEY JWT
S'assurer qu'il est injecte via env var, pas hardcode.

### 6. [DONE] Validation des entrees (schemas Pydantic)
Pas de contrainte sur severity (enum), pas de max_length sur titre/message.
- Literal["low", "medium", "high", "critical"] pour severity
- max_length=200 sur title, max_length=2000 sur message

### 7. [DONE] Securiser les sessions DB dans les background tasks
escalation_loop, watchdog_loop — si exception, session peut rester ouverte.
- try/finally avec db.close() systematique dans chaque iteration

### 8. [DONE] Flag BuildConfig.DEBUG pour les credentials pre-remplies Android
user1/user123 pre-rempli dans le login = dangereux en prod.
- Conditionner au flag DEBUG

### 9. [DONE] Ajouter du rate limiting
Endpoint login vulnerable au brute-force.
- slowapi ou middleware custom
- Limiter /api/auth/login a N tentatives/minute par IP

### 10. [PHASE B] Passer au SSE au lieu du polling 3s (mode astreinte)
Le polling 3s fonctionne mais ajoute de la latence.
- SSE pour push temps reel des evenements (alarmes, escalades, resolutions)
- Polling en fallback si SSE deconnecte > 5s
- Le heartbeat POST 3s reste inchange (source de verite watchdog)
- Prevu en Phase B apres stabilisation FCM

### 11. [DONE] Logging structure (JSON)
Pour un systeme critique, logging basique insuffisant.
- Format JSON via JsonLogFormatter custom (logging_config.py)
- Correlation ID par requete (middleware + contextvars) + background tasks
- Tous les loggers (app, uvicorn) en sortie JSON structuree
- Compatible Docker json-file driver, pret pour ELK/Loki

---

## FONCTIONNEL

### 12. [REJETE] Escalade differenciee selon la severite
Toutes les alarmes suivent la meme chaine/delai.
- **Motif** : sur ce site, si c'est pas grave on n'appelle pas. Il n'y a pas de severites differentes en pratique — toutes les alarmes qui arrivent sont critiques.

### 13. [REJETE] Duree d'acquittement configurable
30 minutes fixes = trop rigide.
- **Motif** : c'est configurable dans la supervision en amont. Le systeme d'alarme ne fait que relayer, la duree d'ack est un parametre operationnel gere par la supervision.

### 14. [DONE] Audit trail (historique des actions)
Tracabilite complete de toutes les actions du systeme.
- Table audit_events (alarm_id, event_type, user_id, timestamp, details JSON, correlation_id)
- 11 types d'evenements : alarm_created/ack/resolved/escalated, user_login/login_failed/created/deleted, config_changed, escalation_timeout, watchdog_offline
- API GET /api/audit/ (admin only, pagine, filtrable par alarm_id, event_type, user_id, dates)
- Onglet Audit dans le dashboard frontend avec filtres et pagination
- Best-effort sur replicas (pas de crash si DB read-only)
- 12 tests E2E (TestAuditTrail)

### 15. [TODO] Planning on-call (rotation, calendrier)
Detection on-call limitee a position 1.
- Calendrier de garde, rotation automatique, echanges

### 16. [REJETE] Groupes/categories d'alarmes
Pas de tagging ni routage par zone/equipement.
- **Motif** : meme raison que le point 12 — si c'est pas grave on n'appelle pas. Le routage par zone/equipement est gere par la supervision, pas par le systeme d'alarme.

### 17. [DONE] Notifications push (FCM)
Si Android tue le foreground service, aucune notification.
- Firebase Cloud Messaging en complement du polling
- Mode astreinte (pos 1) : foreground service permanent + heartbeat 3s
- Mode veille (pos 2+) : FCM uniquement, cout batterie ~0
- Escalade uniforme : 15 min par palier (configurable via `escalation_delay_minutes`)
- FCM wake-up avant escalade si user courant offline (tenter de le reveiller)
- Envoi reel via FCM API v1 + OAuth2 (google-auth)
- AlarmFirebaseService + AlarmWakeUpHandler cote Android
- 14 tests backend (test_fcm.py) + 3 tests modes (test_user_modes.py)

### 18. [TODO] Circuit SMS + Appels vocaux complet
SmsQueue existe mais envoi pas clairement branche.
- **Décision** : Waveshare SIM7600E-H 4G HAT en USB sur le serveur on-site + Free 2€/mois
- Pas de Twilio/OVH (tout on-site, zéro dépendance cloud)
- SMS via AT commands (pyserial), appels vocaux + TTS + acquittement DTMF (Goertzel logiciel)
- Voir `ARCHITECTURE_SMS_VOIX.md` pour le détail complet

### 19. [TODO] Mode maintenance
Pas de mecanisme pour prevenir les fausses escalades pendant une MAJ.
- Flag maintenance + notification utilisateurs

---

## LOGICIEL (suite — chantiers post-audit 2026-04-25)

### 20. [DONE] Atteindre 80% de score en mutation testing — score final : 100% (2026-04-25)
Score initial : 71.7% (104 killed / 41 survived sur backend/app/logic/). Score final : **100%** (137/137 killed, 0 survived) verifie en local avec mutmut 2.5.0 en 2 min 46 s.

Livrables :
- `tests/unit/test_models.py` (NEW) : test parametre frozen=True sur 17 dataclasses + garde-fou exhaustivite. Kill 17 mutants en 1 test paramétré.
- `tests/unit/test_alarm_creation.py` : +2 assertions sur `email_reason` (cas override `requested_assigned_user_id is not None` + chaine vide). Kill 2 mutants.
- `tests/unit/test_sms_call_timers.py` : nouvelle classe `TestContinueSemantics` (4 tests pour `continue` -> `break` sur les 4 cas de skip). Kill 4 mutants.
- `tests/unit/test_escalation.py` : nouvelle classe `TestContinueSemantics` (3 tests pour `continue` -> `break` + 1 test pour `users_online.get(uid, True)` defaut). Kill 4 mutants.
- `tests/unit/test_oncall.py` : 2 nouvelles classes (`TestEscalatedStatusInExistingAlarms` pour les strings 'escalated' et `TestAssignmentDistinguishesChainFromFallback` pour _find_next_online_in_chain vs online_users[0]). Kill 10 mutants.
- INV-020 (user_id unique dans la chaine) : implemente sur `POST /api/config/escalation` single insert (409 Conflict). Etait deja sur `/bulk` (422). + integration test dans `tests/integration/test_escalation_config_contract.py`. + 4 `# pragma: no mutate` documentes dans `backend/app/logic/escalation.py` (lignes 95, 99, 103, 104) — equivalents prouves sous INV-020. INVARIANTS.md mis a jour ❌ -> ✅.
- `.github/workflows/pr.yml` : nouveau job `mutation_logic` (Tier 1.5) path-filtre sur logic/, tests/unit/ et pyproject.toml. Bloquant a 100% strict. Tourne en parallele de tier2_integration (~5-8 min en CI cloud).
- `requirements-dev.txt` : commentaire mis a jour pour signaler que mutmut est utilisable localement (~3 min) en plus du nightly.
- `.claude/CLAUDE.md` : section "Mutation testing local" ajoutee dans `## Commandes courantes` avec les 5 commandes mutmut + note PYTHONIOENCODING=utf-8 pour Windows.

### 21. [TODO] Rendre le failover bloquant en CI
Actuellement `tests/test_failback.py` (multi-node failover) tourne en tier 4 nightly seulement. Une regression sur le failover peut shipper plusieurs heures sans etre detectee, alors que c'est le coeur de la resilience du systeme.
- **Step 1 — optimiser** : mesurer le temps wall-clock actuel du test, identifier les goulots (boot Patroni, attente election leader, sync replica) et reduire les delais (fixtures preconfigurees, `synchronous_commit=remote_apply` au lieu de polling, etc.). Trop long pour tier 2/3 a l'etat actuel.
- **Step 2 — paralleliser le restant** : decouper la suite failover en sous-suites independantes (basic, split-brain, replica-promotion, etc.) sur N workers E2E concurrents
- **Step 3 — promouvoir** : 1 happy-path multi-node en tier 3 bloquant des que la partie failover < 5 min wall-clock
- Cf workflow pr.yml — ajouter la suite optimisee dans le matrix tier3

### 22. [TODO] Creer RUNBOOK_OPS.md (procedures d'exploitation)
La doc actuelle couvre l'archi (README, SITE_SECURITY_NOTES), la spec metier (tests/INVARIANTS) et le contexte IA (.claude/CLAUDE.md), mais pas les procedures operationnelles. Resultat : la connaissance op est dans la tete des devs en place, l'onboarding est long, l'astreinte est fragile, et un incident a 3h du matin oblige a relire l'archi.
- **Day-to-day** : ajouter/retirer un utilisateur, modifier la chaine d'escalade, forcer un ack manuel via curl quand l'app est HS, lire les logs d'un noeud specifique
- **Etat cluster** : verifier le leader Patroni (`patronictl list`), lag de replication, quel noeud sert les requetes Android, comment basculer volontairement le leader
- **Incidents** (le plus precieux) : alarme remontee mais non sonnee (arbre de diagnostic DB/backend/FCM/polling), Patroni split-brain (procedure de recuperation), gateway SMS muet (verif serie pyserial + credit SIM), heartbeat astreinte > 15 min sans alarme (vrai probleme ou fausse alerte)
- **Maintenance** : rotation SIM Free (date expiration + procedure remplacement), deploiement d'un hotfix sans casser le failover, bascule volontaire leader Patroni pour MAJ d'un noeud
- **Onboarding nouveau dev** : setup dev local en < 30 min (commande par commande), comptes de test, ou sont les secrets, qui ping pour quoi
- **Format pour chaque entree** : symptome -> diagnostic (commandes a executer) -> action (commandes exactes) -> validation
- **Source initiale** : commandes courantes deja dans `.claude/CLAUDE.md`, scenarios de SITE_SECURITY_NOTES.md, INVARIANTS pour les flux
- **Maintenance vivante** : a chaque incident reel rencontre, post-mortem -> nouvelle entree dans le runbook
