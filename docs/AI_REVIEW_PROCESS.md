# Process de review des PRs du bot `alarm-murgat-bot`

> **Audience** : humain (Mathieu) ou Claude session interactive qui review une PR ouverte par `alarm-murgat-bot[bot]`.
> **Source de vérité business** : `tests/INVARIANTS.md`. **Stratégie bot** : `docs/AI_STRATEGY.md`.
> **Version** : 2026-05-18. **Statut** : process opérationnel testé sur 11 PRs bot (5 pilotes initiaux + 6 autopilote Bloc B).

Ce document décrit le **process** de review et de décision. Il ne reproduit pas les règles business (cf `tests/INVARIANTS.md`) ni les principes bot (cf `docs/AI_STRATEGY.md` P1-P6).

---

## 1. Quand l'utiliser

Ce process couvre **3 types de PRs** à reviewer. Adapter selon le type :

### 1.1 PR du bot `alarm-murgat-bot[bot]` (cas principal)
- Déclenchée par cron `0 */4 * * *` ou trigger manuel `gh workflow run ai-bot.yml`
- Attend label `ai:approved` (humain) pour déclencher l'auto-merge phase 4
- Cleanup post-merge requis : `gh issue edit <N> --remove-label "ai:queue" --remove-label "ai:fix"` + close issue
- `/ai-retry` disponible si retouche nécessaire

### 1.2 PR humaine co-signée Claude (worktree spawn ou session interactive)
- Auteur GH = `claude-murgat` (l'utilisateur), avec `Co-Authored-By: Claude` dans le commit
- Applique le même process (calibration C/H/M/L, triplet sur [C], anti-patterns à signaler)
- **Adaptations** :
  - **Pas de `/ai-retry`** — les templates §5 ne s'appliquent pas, l'auteur push un commit directement après ton retour
  - **Pas de cleanup labels `ai:queue`/`ai:fix`** — ces labels n'existent pas sur ces PRs
  - Si retouche : poste ton retour en commentaire normal sur la PR (le destinataire est humain ou Claude, pas le bot)

### 1.3 PR humaine pure (autre contributeur)
- Process applicable si demandé. Mêmes adaptations que 1.2.

### 1.4 Mode automatique (workflows `ai-bot-review.yml` + `ai-bot-replay.yml`)
- Depuis 2026-05-22, deux workflows automatisent ce process :
  - **`ai-bot-review.yml` (Pipeline 1)** : 3 triggers possibles —
    - `pull_request_target.opened/synchronize/reopened` (auteur bot) → review auto à chaque push du bot
    - `pull_request_target.labeled` avec label `ai:review` → review d'une PR humaine (sessions Claude interactives)
    - `workflow_dispatch` manuel pour les cas exceptionnels
    - `check_run.completed` → re-tente quand la CI repasse verte après un fail
    
    Lance le triplet Claude headless, parse le verdict JSON, pose `ai:approved` / poste `/ai-retry` / pose `ai:abandoned`. Sort Mathieu du goulot review pour les PR bot ; permet le triplet sur PR humaine via simple pose de label. Veto humain : retirer `ai:approved` avant le merge auto.
  - **`ai-bot-replay.yml` (Pipeline 2)** : trigger sur `issues.labeled ai:needs-human` (sauf si `ai:replayed` déjà). Pose `ai:replayed` (anti-boucle), déclenche `ai-bot.yml` pour re-tenter. Si l'issue produit une PR → Pipeline 1 la review automatiquement.
- Le process documenté ci-dessous (§2–§7) **reste la spec** : les workflows automatisés en sont l'implémentation. Toute évolution du process (calibration, affinements trancheur, anti-patterns) doit être propagée dans le prompt `.github/ai-bot/review-prompt.md`.

#### Comportement adapté PR humaine vs PR bot

Quand le verdict est APPROVE / APPROVE_FOLLOWUP, le workflow différencie selon l'auteur de la PR :

| Auteur PR | Label `ai:approved` posé ? | Merge auto ? | Action humaine attendue |
|---|---|---|---|
| `alarm-murgat-bot[bot]` | OUI | OUI (`ai-merge.yml`) | Veto possible en retirant le label avant le merge auto |
| `claude-murgat` (session Claude) | OUI | OUI (`ai-merge.yml`) | Veto possible en retirant le label avant le merge auto |
| Autre humain | NON | NON | L'humain merge lui-même (`gh pr merge` ou UI) après lecture du commentaire récap |

**Pourquoi `claude-murgat` est autorisé au merge auto (décision 2026-05-28)** : les PRs de session Claude interactive sont produites avec le même process TDD que les PRs bot (RED→GREEN, tests prouvés, INV-aware) et passent par le même triplet review. Imposer une revalidation humaine systématique recréait le goulot review que `ai-bot-review.yml` cherchait à éliminer. Le triplet reste le filtre de qualité ; le merge auto n'a lieu que sur convergence 3/3 APPROVE + CI verte. Le veto humain reste toujours possible en retirant le label avant que `ai-merge.yml` ne tourne.

**Pourquoi les autres humains restent manuels** : par défaut on n'auto-merge qu'une identité maîtrisée. Élargir la liste demandera une décision explicite par auteur.

**Si BEHIND détecté par le triplet** : la note "Cas A routine, `gh pr update-branch` avant merge" est ajoutée au commentaire (cf §7.1). Pour les auteurs auto-mergés (bot + `claude-murgat`), `ai-merge.yml` fait le `pr update-branch` au besoin via son fallback §7.1.

---

Tu (humain ou Claude) es le **goulot review** : sans ton verdict, la PR reste ouverte.

## 2. Cadre général

### 2.1 Pull + checklist préflight (30s, obligatoire)

```bash
git fetch origin
gh pr view <N> --json body,files,additions,deletions,headRefName,statusCheckRollup,mergeStateStatus
gh pr diff <N>
git diff origin/master..origin/<headRefName> --stat
```

Lis le **body PR** (résumé de l'agent), les **fichiers touchés**, le **diff complet**. Identifie l'**invariant ciblé** (INV-XXX) et sa **criticité** dans `tests/INVARIANTS.md`.

#### ✅ Checklist préflight (à exécuter AVANT de commencer la review)

- [ ] **`mergeStateStatus`** — valeur ? `CLEAN`, `BEHIND`, `DIRTY`, `BLOCKED` ? Si `BEHIND` ou `DIRTY` → traiter cf §7.1 AVANT de continuer la review (pas après l'approve).
- [ ] **Diff vs master** — `git diff origin/master..origin/<branch> --stat` : aucun fichier récemment mergé (ces derniers jours) n'est-il **supprimé** par cette PR ? Si oui → 🔴 cf §7.1 cas dangereux (BEHIND avec collision = bloquant immédiat).
- [ ] **Scope vs body** — les `files` annoncés dans le body PR correspondent aux fichiers réellement touchés dans le diff ? Si écart → scope creep silencieux à investiguer.
- [ ] **Denylist** (PRs bot uniquement) — intersection entre les fichiers du diff et `.github/ai-bot/denylist.txt` ? Si oui → `ai:denied` aurait dû être posé, vérifier pourquoi la PR existe.

Sans cette checklist, on rate les cas où une branche fraîche écrase plusieurs centaines de lignes mergées entre temps — invisible si on ne fait pas le `git diff` vs master AVANT la review.

### 2.2 Calibration par criticité (règle d'or)

La rigueur de review s'adapte à la criticité de l'invariant. Appliquer une grille [C] à un [L] = sur-zèle qui gaspille les itérations bot. Appliquer une grille [L] à un [C] = trou de qualité dans un système zéro-bug.

| Crit | Posture review | Action si trou détecté |
|---|---|---|
| **[C] critical** | Zero tolerance. Mutation-proof exigée. Scénarios alternatifs couverts. Triplet review systématique. | `/ai-retry` si le moindre trou réel (boundary, mutant constant, call site non testé) |
| **[H] high** | Strict mais pas paranoïaque. Vérifier preuve invariant central + 1-2 cas alternatifs. | `/ai-retry` si trou structurel (path négatif absent, mutation par raisonnement seul sur un point sensible) |
| **[M] medium** | Tester l'invariant central proprement. Pas exiger l'exhaustivité. | Approve + issue follow-up si élargissement utile |
| **[L] low** | Prouver l'existence du comportement suffit. Pas d'exigence mutation-proof exhaustive (mutmut nightly fait le reste). | Approve direct. Issue follow-up que si vraiment important. |

**Exception** : un INV [L] sur un endpoint touchant la sonnerie/alarme remonte d'un cran (par proximité avec le métier critique). Un INV [C] sur de l'observability/stats peut descendre d'un cran.

## 3. Le pattern triplet review

### 3.1 Quand l'utiliser

- **OBLIGATOIRE sur [C]** — pas d'exception. Pas de mode "review consultative" (où on te demande juste un avis pour comparer ou pour décision humaine, sans pouvoir d'action) qui contournerait. Même dans ce cas, fais le triplet et présente les 3 phases dans le rapport. Le shortcut "j'ai juste à analyser, pas à agir → je peux skipper Phase 2/3" n'est **pas autorisé** pour [C]. Si tu te surprends à raisonner "l'utilisateur veut juste comparer / ce n'est qu'une éval" et donc à shortcuter — c'est un post-hoc de fatigue ou de pression, fais le triplet.
- **Optionnel** sur [H] si tu as un doute légitime (sécurité, edge case complexe)
- **Pas la peine** sur [M] ou [L] sauf cas exceptionnel — review simple suffit

### 3.2 Les 3 phases

**Phase 1 — Ma review** (analyse principale)
- Maximum **4 retours**. Au-delà, je dilue mon attention.
- Pour chaque retour : sévérité réelle (pas que pertinence)
- Verdict provisoire : approve / retouche / reject

**Phase 2 — Sous-agent critique**
Tool : `Agent(subagent_type="general-purpose")` avec prompt explicite contenant :
- Contexte business résumé (INV ciblé, criticité, sous-cas)
- Diff de la PR (les morceaux clés)
- Mes 4 retours
- Mon verdict provisoire
- Demande : pour chaque retour, est-il pertinent ? sévérité bien calibrée ? quels angles manqués ?

**Clause obligatoire** dans le prompt :
> "IMPORTANT : tu ne dois faire AUCUN appel `gh`, AUCUN `git`, AUCUNE modification de fichier. Ta sortie unique est ta réponse texte. Je posterai moi-même les commentaires et labels."

Sans cette clause, le sous-agent peut prendre l'initiative de poster un `/ai-retry` ou un label sans validation (vécu sur PR #101 — cf `feedback_subagent_action_boundary.md`).

**Phase 3 — Sous-agent trancheur**
Avec ma review + la critique en input. Décision finale parmi :
1. APPROUVER tel quel
2. APPROUVER + issue follow-up (titre exact à proposer)
3. `/ai-retry` (texte exact à coller)
4. REJETER

Même clause "aucune action" obligatoire.

#### 3.2bis Checklist du trancheur (affinements 2026-05-22)

Pour éviter que le trancheur dérive vers la flagornerie (synthétiser deux avis valides) ou la sur-couverture (suivre tous les angles du critique), 5 garde-fous obligatoires dans son prompt :

**1. Défaut par criticité (grille d'or).** Le trancheur doit *justifier l'écart au défaut*, pas le défaut lui-même.

| Crit | Défaut | Écart à justifier |
|---|---|---|
| **[C]** | RETRY au moindre trou mutation-proof réel | "APPROUVER direct" exige justification forte |
| **[H]** | APPROUVER sauf trou structurel | "RETRY" exige un trou structurel cité |
| **[M]** | APPROUVER | "RETRY" exige un cas exceptionnel |
| **[L]** | APPROUVER direct | Toute action exige une justification grille |

**2. Citation obligatoire de la grille.** Le trancheur doit citer le passage exact de §2.2 qui justifie son verdict. Pas de "feeling". Exemple :
> "Sur [L], grille §2.2 = 'Prouver l'existence du comportement suffit'. Le critique trouve un mutant frontière `>=` → `>`. INVALIDE — la grille [L] ne demande pas mutation-proof, mutmut nightly s'en chargera. Pas d'action."

**3. Anti-comité.** Le trancheur n'est **PAS obligé** d'adresser chaque point du critique. Pour chaque retour, il doit demander :
- (a) Le claim est-il **factuellement correct** ? Si vérifié faux → INVALIDE.
- (b) L'action demandée est-elle supportée par la grille du niveau de l'INV ? Si la grille [L] dit "approve direct" et le critique demande "ajoute test mutation-proof" → INVALIDE (action hors grille).
- (c) Le trou pointe-t-il l'invariant central ou un détail marginal ? Mutants frontière marginaux (`>=` → `>`) sur [M]/[H] → mutmut nightly rattrappe, pas de retry.

**4. Heuristique mutants frontière marginaux.** Sur [M]/[H], les mutants qui touchent une demi-seconde, un offset d'1 unité, ou un cas limite arithmétique sans impact business → laisser à mutmut nightly. Réserver l'action aux mutants qui touchent l'invariant central ou un call site nommément cité dans le catalogue.

**5. Format de sortie obligatoire (avec section invalidés).** Le trancheur doit produire ces 4 sections en sortie, dans cet ordre, **toutes obligatoires** :

```
## Vérifications indépendantes
[liste des verifs gh/grep/git diff faites, avec résultats factuels]

## Retours critique retenus
1. [retour] — action recommandée + citation grille qui la supporte

## Retours critique INVALIDÉS
1. [retour] — INVALIDE car (a) fait vérifié faux OU (b) action hors grille [criticité]
[Si rien à invalider, écrire EXPLICITEMENT "Aucun retour invalidé" — ne JAMAIS omettre cette section.]

## Décision
DECISION : APPROVE | APPROVE_FOLLOWUP | RETRY | REJECT
JUSTIFICATION : 2-3 phrases citant la grille du niveau de l'INV
```

La section "Retours INVALIDÉS" **explicite** est l'affinement critique. Sans elle, le trancheur a un biais "tous les retours sont valides, j'arbitre entre". Avec elle, il doit *se poser la question* pour chaque retour. Si rien à invalider, c'est OK — mais c'est une décision consciente, pas un oubli.

### 3.3 Vérifier les claims factuels du sous-agent

Les sous-agents peuvent **halluciner** des faits (fichiers touchés, lignes, contenu). Cas vécu sur PR #102 : critique a inventé "DÉRIVE SCOPE CRITIQUE" (808 lignes hors INV-018, gateway + INVARIANTS.md) alors que le diff ne contenait que 7 fichiers attendus.

**Avant d'agir sur un claim bloquant** :
- Si claim factuel (nb lignes, fichiers, contenu) : `gh pr view <N> --json files,additions,deletions` puis `gh pr diff <N>` pour vérifier
- Si claim analytique (ce test ne couvre pas X) : pas besoin de vérifier, jugement

Distinguer "ce test ne prouve pas le cas Y" (analyse, à jauger) vs "la PR contient 808 lignes hors scope" (fait, à vérifier).

## 4. Anti-patterns à signaler dans une review

### 4.1 Tests faibles
- `try/except: pass` autour d'un appel critique en cleanup (masque les erreurs ET pollue les tests suivants)
- `assert response.status_code == 200` comme seule vérification (P2 — ne prouve rien sur le contenu)
- Test paramétré qui passe par le même chemin sans varier le code testé (faux contrôle, ex: `(0)` et `(-5)` qui testent la même branche `max(1, ...)`)
- Sensibilité prouvée par **raisonnement** ("si l'implé faisait X, ça failerait") au lieu de mutation empirique (commit + revert)
- Test couplé à un détail d'implémentation (`r.json()["status"] == "resolved"` au lieu de vérifier la propriété observable de l'invariant)

### 4.2 Couverture
- Bot modifie N call sites mais le test n'en couvre que 1 (vrai trou [C], ex: INV-018 PR #102)
- Boundary du seuil non testée (ex: `> 3min` sans test à 2'59 / 3'00 / 3'01 — mutant `>=` survit)
- Anti-flapping non testé alors que c'est le cœur de l'invariant (ex: INV-085 série interrompue)
- Path négatif absent (ex: INV-074 — refresh avec token expiré → 401 pas testé)

### 4.3 Scope
- Bot piggy-back un autre INV / feature dans la PR (à vérifier via `gh pr view --files`)
- Bot descope unilatéralement des points listés dans l'issue (peut être justifié P5, mais à valider)

### 4.4 Hygiène
- Body PR avec headers vides (`## Resume agent` suivi de rien)
- Pas de `Closes #N` dans le body (l'issue ne se ferme pas auto au merge)
- Imports privés `_underscore` dans les tests (couplage à l'API interne — toléré si pattern projet déjà établi)
- Couplage strict à la seed (`admin/admin123`) sans fixture — toléré (convention projet)

## 5. Templates `/ai-retry`

Toujours préciser :
- L'INV concerné + sa criticité
- Le **mutant qui survit** ou le trou exact
- L'action attendue (1-2 tests max, jamais redo from scratch)
- Le budget P4 restant (max 5 tests)
- "Garde le reste, ne re-écris rien"

### 5.1 Mutation symétrique (paramétrisation seuil)
> Le test actuel a un trou mutation-mécanique : un mutant qui hardcoderait la valeur de ce test précis (`return X` au lieu de `return float(cfg.value) if cfg else ...`) passerait. Ajoute le scénario symétrique : config=Y, observation=Z (l'inverse) → résultat opposé attendu. Aucun X fixe ne pourra satisfaire les 2 cas.

### 5.2 Boundary exacte (seuil temporel)
> Ajoute un test paramétré 3 cas : `seuil_strict-1s` (False), `seuil_strict exact` (False car `>` strict), `seuil_strict+1s` (True). Tue le mutant `>` ↔ `>=`.

### 5.3 Anti-flapping (série interrompue)
> Injecter dans l'historique une obs saine au milieu d'obs non-saines. La série continue qui touche NOW dure moins que le seuil → résultat doit rester "pas perdu". Tue le mutant `break` ↔ `continue` qui détruit l'anti-flapping.

### 5.4 Path négatif (auth, validation)
> INV-X dit "avec token valide → nouveau token". Le path négatif (token invalide, expiré, signature corrompue) doit aussi être verrouillé : forger un JWT avec exp passé / signature wrong-secret, asserter 401 + absence d'access_token dans la réponse.

### 5.5 Couverture call sites (refactor N points)
> Le diff modifie N call sites identifiés mais les tests n'en couvrent que K. Ajoute K..N tests qui exercent les call sites manquants (oncall, gateway, etc.). Sans ça, un mutant qui retire `original_created_at=_now` dans un des call sites non testés survit.

### 5.6 Fallback ValueError (endpoint sans validation)
> Le `try/except` dans le helper est l'unique défense contre une valeur invalide poussée par l'endpoint (qui n'a pas de validation). Ajoute un test qui POST `value="abc"` puis vérifie que le helper retourne le DEFAULT au lieu de crash.

## 6. Workflow d'action

### 6.1 APPROUVER

```bash
gh pr edit <N> --add-label "ai:approved"
# ai-merge.yml se déclenche automatiquement (phase 4)
# watch le run, vérifier merge réussi
gh run watch <ai-merge-run-id> --exit-status --interval 5
gh pr view <N> --json state,mergedAt
```

Si auto-merge échoue (BEHIND, conflit, etc.) :
- **BEHIND** : `gh pr update-branch <N>` puis re-trigger via toggle label (`--remove-label ai:approved` + `--add-label ai:approved`)
- **CONFLIT** : checkout branche + résoudre localement + push (cf section 7)
- **Tier 3 fail** : souvent CI-BUG-10 port collision (re-run `gh run rerun <run-id> --failed`)

Cleanup post-merge :
```bash
gh issue edit <issue_source> --remove-label "ai:queue" --remove-label "ai:fix"
gh issue close <issue_source> --reason completed --comment "Fixed by PR #<N> (mergé <date>). [Résumé tests ajoutés]. Statut INV-XXX ⚠️/🐛 → ✅ à batcher."
```

Ajouter à la mémoire `project_invariants_to_update.md` pour le prochain batch catalogue.

### 6.2 `/ai-retry`

```bash
gh pr comment <N> --body "$(cat <<'EOF'
/ai-retry

[Texte précis du retour — voir templates section 5]
EOF
)"
```

Le bot redémarre automatiquement (event `issue_comment` avec filtre `/ai-retry`). Watch le run ai-bot.yml. Si le bot ne push pas dans les ~10 min, fallback : faire les modifs soi-même sur la branche (cf section 7.4).

**Anti-pattern** : poster plusieurs `/ai-retry` consécutifs. Le bot a un compteur N/3 — au 3e retry sans succès, il abandonne avec `ai:abandoned`.

### 6.3 REJETER (rare)

```bash
gh pr close <N> --comment "Rejet : [raison]. Issue #<X> reformulée si besoin avant re-trigger."
gh issue edit <issue_source> --remove-label "ai:queue"
```

Si le rejet vient d'une ambiguïté de l'issue : reformuler l'issue (cf cas PR #95 INV-078) plutôt que de fermer.

### 6.4 Follow-up sans bloquer

Si un trou est identifié mais que la PR mérite le merge (cohérence avec précédents, scope respecté, etc.) :

```bash
gh issue create --title "[INV-XXX] [Type] [Description courte]" --label "ai:queue" --body "[Trou identifié + tests attendus + budget]"
```

Note dans la mémoire `project_invariants_to_update.md` que l'INV principal passe à ✅ mais qu'une issue follow-up existe.

## 7. Gérer les cas spéciaux

### 7.1 PR BEHIND (master avance pendant la review)

**Cas A — BEHIND simple (routine)**

La branche a été créée à un commit master plus ancien, mais aucune collision de suppressions. Cas typique : 1-2 commits master mergés pendant la review.

```bash
gh pr update-branch <N>
# Si succès : nouvelle CI démarre, attendre, re-pose le label
# Si conflit : passer à 7.2
```

**Cas B — 🔴 BEHIND avec suppressions de code récemment mergé (BLOQUANT)**

Symptôme : `git diff origin/master..origin/<branch> --stat` montre des **`-` (suppressions) sur des fichiers ajoutés ou modifiés via des PRs mergées récemment**.

Cause typique : la branche a été créée AVANT plusieurs merges critiques, et son commit de merge interne (rebase ou merge master old) a écrasé les modifs récentes en silence.

**Action immédiate** :
1. **NE PAS approuver** la PR. Même si la CI est verte, le merge effacerait du code mergé entre temps.
2. Pour CHAQUE fichier en suppression nette, vérifier si c'est intentionnel (refactor légitime) ou collision (oubli) :
   - `git log origin/master -- <file>` pour voir si le fichier a été touché récemment par d'autres PRs
   - Si oui → collision, à traiter via §7.2
3. Suivre §7.2 (résolution conflit manuelle) — JAMAIS de `gh pr update-branch` sans inspection (le merge peut "réussir" silencieusement en gardant les suppressions).
4. Si la PR auteur est un humain : signaler immédiatement dans un commentaire ce qui serait perdu, demander rebase de leur côté.

**Symptôme typique** : une branche créée il y a plusieurs jours, pendant lesquels d'autres PRs ont mergé sur master en touchant ou ajoutant des fichiers que la branche n'a pas vus. Le `mergeStateStatus: BEHIND` est souvent le seul indice visible avant le `git diff` détaillé.

**Règle d'or** : un `BEHIND` détecté à la checklist préflight §2.1 doit toujours déclencher `git diff origin/master..origin/<branch> --stat` AVANT de continuer la review. Pas après l'approve.

### 7.2 Conflit de merge

```bash
git fetch origin
git checkout <branch_pr>
git merge origin/master --no-edit
# Résoudre les conflits manuellement (l'éditeur sait quoi garder)
git add <files_résolus>
GIT_AUTHOR_NAME=claude-murgat GIT_AUTHOR_EMAIL=direction_technique@charlesmurgat.com \
GIT_COMMITTER_NAME=claude-murgat GIT_COMMITTER_EMAIL=direction_technique@charlesmurgat.com \
  git commit -m "merge master: [description résolution]"
git push
# CI re-tourne, re-pose ai:approved après vert
```

**Cas vécu** : PR #99 et PR #101 modifient toutes deux `watchdog.py`. PR #99 a mergé en premier (helper `_run_watchdog_check`). PR #101 (helper `_get_watchdog_tick_seconds`) conflictait. Résolu en gardant les 2 helpers et en chaînant les appels dans `watchdog_loop`.

### 7.3 CI-BUG-10 port collision (Tier 3 fail bizarre)

Logs montrent `port is already allocated` sur etcd/patroni. C'est un bug d'infra CI documenté dans `docs/AI_STRATEGY.md §8bis`. Solution :

```bash
gh run rerun <failed_run_id> --failed
# La 2e tentative passe presque toujours
```

### 7.4 Bot ne retry pas (event filter)

Si le `/ai-retry` n'a pas déclenché un push du bot dans ~15 min :
- Vérifier `gh run list --workflow ai-bot.yml --limit 5` — chercher un run `cancelled` ou avec `Agent fix: skipped`
- Possible cause : concurrency `ai-bot` global, commentaire posté pendant qu'un autre run tournait, event squashé

**Solution rapide** : faire les modifs manuellement sur la branche (cf cas PR #103 — j'ai ajouté les 2 tests boundary + anti-flapping + fermé les 4 mutants survivants à la main, ~30 min vs attendre indéfiniment).

### 7.5 Mutmut tier 1.5 fail (seuil 100% strict)

Si CI fail sur "Mutation score X% < 100% strict" :

```bash
# Télécharger l'artifact mutation
gh run download <run_id> --pattern "mutation-pr-reports-*" --dir /tmp/mut
cat /tmp/mut/mutation-pr-reports-*/results.txt
# Inspecter le HTML pour voir les mutants survivants
grep -A2 "Mutant " /tmp/mut/mutation-pr-reports-*/html/path/to/file.py.html
```

Pour chaque mutant survivant :
- **Mutant équivalent** (ex: `frozen=True` → `frozen=False` sur dataclass jamais mutée) : pragma `# pragma: no mutate (INV-XXX — explication)` directement sur la ligne
- **Mutant non-équivalent** : ajouter un test qui le tue

## 8. Cleanup post-session

À la fin d'une session de review :
1. Toutes les PRs validées → mergées ou en cours de merge
2. Toutes les issues mergées → closed, labels `ai:queue`/`ai:fix` retirés
3. Mémoire `project_invariants_to_update.md` à jour avec les INV à passer ⚠️/🐛 → ✅
4. Quand on a 3-5 INV à updater → créer une PR docs batch (cf PR #98 exemple)
5. Slack récap optionnel sur `D0B326EUZ51` si la session a produit beaucoup de merges

## 9. Cas pédagogiques (session 2026-05-18)

Cinq PRs review en série, chacune avec une leçon :

### PR #104 INV-074 path négatif [H] — approve direct sans triplet
- Test simple, pertinent, sensibilité prouvée par mutation explicite, stratégie d'attribution (sub=admin pour éliminer "user not found" comme cause)
- Pas de doute, pas de triplet, approve direct. [H] n'exige pas le triplet systématique.

### PR #99 INV-084 watchdog [C] — triplet → `/ai-retry` (mutation manquante)
- 1 seul test "config=30, heartbeat=35s → offline". Mutant trivial `timeout_seconds = 30` (hardcode valeur du test) passe.
- `/ai-retry` pour test miroir "config=120, heartbeat=70s → reste online". Ensemble, élimine tout mutant constant.
- Bot a livré + assert pré-tick + check log_event. Merged.

### PR #101 INV-084 ticks [C] — triplet → `/ai-retry` + conflit merge manuel
- PR mieux que #99 (3 tests dont isolement croisé 2 clés). Mais trou : pas de test ValueError sur endpoint sans validation.
- `/ai-retry` pour 4e test `value="abc"` → fallback DEFAULT. Bot livre.
- Conflit `watchdog.py` avec PR #99 (mergée entre temps). Résolu manuellement (cf section 7.2).

### PR #102 INV-018 modèle [C] — triplet → critique hallucine, `/ai-retry` ciblé
- Sous-agent critique invente une "DÉRIVE SCOPE CRITIQUE" (gateway + 808 lignes) qui n'existe pas dans le diff réel.
- Vérification `gh pr view --files` : 7 fichiers attendus, +4 lignes par call site.
- Vrai trou (validé) : couverture call sites — 3 tests touchent seulement `alarms.py::send_alarm`, pas `alarms_internal.py` (gateway) ni `_apply_oncall_heartbeat`.
- `/ai-retry` pour 2 tests sur oncall + gateway. Bot livre 5 tests total.

### PR #103 INV-085 détection [C] — triplet → `/ai-retry` non triggered, fix manuel
- 4 tests initiaux. Trous : boundary 3min exacte non testée (mutant `>=` survit), anti-flapping série interrompue non testée (mutant `break→continue` survit).
- `/ai-retry` posté. Bot ne push pas (filtre dispatch, run cancelled).
- Fix manuel : ajout 2 tests + docstring "agrégation à la charge de l'appelant".
- Mutmut tier 1.5 : 97.5% (4 survivants). Téléchargement artifact, identification : 2 équivalents (frozen=True dataclass), 1 filter `<` vs `<=`, 1 init `lost_since`.
- Pragma sur les 2 équivalents + 2 tests pour fermer les 2 autres. Mutmut 100%. Merged.

---

## 10. Références croisées

- `tests/INVARIANTS.md` — catalogue invariants business (source de vérité)
- `docs/AI_STRATEGY.md` — principes bot P1-P6, grilles CI, bugs CI documentés
- `docs/ai_backlog.md` — backlog des issues `ai:queue` rédigées
- `.github/workflows/ai-bot.yml` — workflow du bot (dispatch, agent, abandon)
- `.github/workflows/ai-bot-cron.yml` — cron pioche 4h
- `.github/workflows/ai-merge.yml` — auto-merge phase 4 sur `ai:approved`
- `.github/ai-bot/prompt.md` — system prompt du bot (P1-P6 + regression-lock)
- `.github/ai-bot/denylist.txt` — fichiers que le bot ne peut pas modifier
