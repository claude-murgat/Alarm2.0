# Alarm 2.0 — Plan d'ameliorations

## Statut global
- Date d'analyse : 2026-04-06
- Derniere mise a jour : 2026-04-08
- Total : 19 points identifies (11 logiciel + 8 fonctionnel)
- Termines : 11/19
- Rejetes : 3/19
- Restants : 5/19

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

### 11. [TODO] Logging structure (JSON)
Pour un systeme critique, logging basique insuffisant.
- Format JSON, niveaux, correlation IDs
- Centralisation (ELK, Loki)

---

## FONCTIONNEL

### 12. [REJETE] Escalade differenciee selon la severite
Toutes les alarmes suivent la meme chaine/delai.
- **Motif** : sur ce site, si c'est pas grave on n'appelle pas. Il n'y a pas de severites differentes en pratique — toutes les alarmes qui arrivent sont critiques.

### 13. [REJETE] Duree d'acquittement configurable
30 minutes fixes = trop rigide.
- **Motif** : c'est configurable dans la supervision en amont. Le systeme d'alarme ne fait que relayer, la duree d'ack est un parametre operationnel gere par la supervision.

### 14. [TODO] Audit trail (historique des actions)
Pas de trace de qui a fait quoi.
- Table alarm_events(alarm_id, event_type, user_id, timestamp, details)
- Dashboard de performance

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
- Escalade 2 paliers : astreinte 15 min, veille 2 min
- Envoi reel via FCM API v1 + OAuth2 (google-auth)
- AlarmFirebaseService + AlarmWakeUpHandler cote Android
- 14 tests backend (test_fcm.py) + 3 tests modes (test_user_modes.py)

### 18. [TODO] Circuit SMS complet
SmsQueue existe mais envoi pas clairement branche.
- Integration Twilio/OVH SMS
- Retry avec backoff exponentiel

### 19. [TODO] Mode maintenance
Pas de mecanisme pour prevenir les fausses escalades pendant une MAJ.
- Flag maintenance + notification utilisateurs
