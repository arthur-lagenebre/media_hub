# Utiliser les agents Claude Code

Le dépôt contient six **sous-agents** dans [.claude/agents/](../.claude/agents/). Un sous-agent est un assistant spécialisé : Claude Code lui confie une tâche, il travaille dans son propre contexte avec ses propres outils, puis ne renvoie que son compte rendu. Cela garde la conversation principale lisible et donne à chaque tâche les bonnes consignes du projet.

## Qui fait quoi

| Agent | À appeler pour… | Peut modifier des fichiers ? |
| --- | --- | --- |
| `hub-server` | une route, la base SQLite, un scan, ffmpeg / plan de lecture, TMDB / MusicBrainz, un réglage `HUB_*` | oui |
| `hub-ui` | une vue, le lecteur, la liseuse, la barre audio, la navigation à la télécommande, l'adaptation PC / téléphone / tablette / TV | oui |
| `hub-tester` | ajouter un test, reproduire un bug par un test, comprendre un test qui échoue | oui (tests seulement) |
| `hub-reviewer` | relire un changement avant commit / PR | **non** (lecture seule) |
| `hub-deploy` | `deploy/` : docker-compose, Tailscale, `.env`, ffmpeg sur le NAS, dépannage | oui |
| `hub-architect` | discuter de conception : où placer une fonctionnalité, forme d'une API ou d'un schéma, client natif / PWA, compromis | **non** (lecture seule) |

Règle de partage : `hub-server` ne touche pas à `hub/web`, `hub-ui` ne touche pas à `hub/server`. Quand une tâche demande les deux, chacun décrit ce qu'il attend de l'autre.

## Comment les lancer

Trois façons, dans une conversation Claude Code (terminal ou extension VS Code) :

1. **Par la demande** : nomme l'agent.
   > Utilise l'agent hub-reviewer pour relire ma branche.
2. **Par mention** : tape `@` puis choisis l'agent dans la liste (`@agent-hub-reviewer`).
3. **Automatiquement** : sans les nommer, Claude peut déléguer de lui-même quand la demande correspond à la description d'un agent (« ajoute une route… » → `hub-server`). Nommer l'agent reste plus fiable.

La commande `/agents` liste les agents disponibles, permet de les consulter et de les modifier. Les agents sont chargés au démarrage de la session : après avoir créé ou changé un fichier de `.claude/agents/`, relance la session (ou passe par `/agents`) si l'agent n'apparaît pas.

### Discuter avec l'architecte

`hub-architect` est fait pour l'échange, et un sous-agent appelé depuis la conversation ne répond qu'une fois puis disparaît. Pour une vraie discussion en plusieurs tours, lance Claude Code **avec cet agent comme session principale** :

```bash
claude --agent hub-architect
```

Toute la conversation se tient alors avec lui (il lit le code, ne modifie rien). Quand une décision est prise, reviens à une session normale et confie l'implémentation à `hub-server`, `hub-ui`… en leur collant le résumé qu'il t'a préparé. Appelé depuis la session normale (`Utilise hub-architect : …`), il convient à un avis ponctuel et autonome.

## Exemples de demandes

Plus la demande est précise, meilleur est le résultat : un agent démarre **sans connaître la conversation**, il ne sait que ce que tu lui écris.

```text
Utilise hub-server : ajoute une route GET /api/stats qui renvoie le nombre d'éléments
par bibliothèque. Documente-la dans le README.

Utilise hub-ui : sur la fiche d'un album, le focus à la télécommande saute du bouton
« Lire » à la dernière piste. Corrige-le pour que ↓ aille à la première piste.

Utilise hub-tester : écris un test qui reproduit « un scan efface les photos d'artistes
enrichies » (hub/scan), sans corriger le code.

Utilise hub-reviewer : relis le diff de la branche courante contre main.

Utilise hub-architect : faut-il une PWA ou une application Android TV native ? Qu'est-ce que l'API actuelle
doit changer pour l'accepter (authentification, URL de flux pour Chromecast) ?

Utilise hub-deploy : ajoute une bibliothèque « Documentaires » dans le compose et dans le README.
```

## Enchaînements utiles

**Nouvelle fonctionnalité**
1. `hub-server` écrit la route et ses tests (ou `hub-tester` si tu préfères séparer).
2. `hub-ui` branche l'interface sur la route.
3. `hub-reviewer` relit l'ensemble.
4. Tu corriges ce qu'il signale, puis commit / PR.

**Bug**
1. `hub-tester` écrit un test qui échoue pour la bonne raison.
2. `hub-server` ou `hub-ui` corrige jusqu'à ce que le test passe.
3. `hub-reviewer` vérifie qu'il n'y a pas de régression.

**Mise en production sur le NAS**
`hub-deploy` pour le compose et le `.env.example`, puis il te donne la commande exacte à lancer sur le NAS (il n'y a pas accès).

Plusieurs agents peuvent travailler en parallèle s'ils touchent des fichiers différents (par exemple `hub-ui` et `hub-server` sur deux parties indépendantes) : demande-le explicitement. Pour deux sessions distinctes, voir plus bas.

## Plusieurs sessions en parallèle : un worktree chacune

Deux sessions Claude Code ouvertes sur le même dossier partagent la même branche et les mêmes fichiers non commités. Un `git checkout`, `git stash` ou `git clean` de l'une efface ou déplace le travail de l'autre, et une suite de tests lancée dans l'une teste le code de l'autre (elle peut échouer, ou réussir, pour une raison qui n'est pas la sienne). Quand tu mènes deux lots en même temps, donne à chaque session son propre **worktree** :

```bash
git worktree add ../media_hub-<lot> -b <branche> main
```

- **À côté du dépôt**, dans le même dossier parent : `media_library` doit rester voisin de `media_hub` (ou désigné par `HUB_REPO`), sans quoi aucun test ne démarre.
- **Une session par worktree**, et le dossier principal reste à celle qui y travaille déjà : ne change pas de branche dans un dossier où une autre session a des modifications non commitées.
- **Écris le dossier dans la demande** que reçoit l'agent d'implémentation (« travaille dans `D:\Projets\media_hub-lot2` »), puisqu'il démarre sans connaître la conversation.
- **Un relecteur ne change jamais de branche** : il lit avec `git diff` et `git show`, et lance ses tests sur une copie extraite par `git archive`.
- **Après la fusion** : `git worktree remove ../media_hub-<lot>`, puis `/merged` pour ranger les branches.

## Ce qu'ils ne font pas

- **Ils ne commitent pas et n'ouvrent pas de PR** : c'est toi (ou la conversation principale) qui décides.
- **Entre 23 h et 6 h, une confirmation est demandée** avant de lancer `hub-server`, `hub-ui`, `hub-deploy` ou `hub-tester` : le hook `.claude/hooks/confirm-night.py` (déclaré dans `.claude/settings.json`, sur le lancement d'un agent) lit l'heure de la machine ; `hub-architect` et `hub-reviewer`, en lecture seule, partent sans question.
- **Ils ne voient pas ta télé ni ton NAS.** Ils testent sur une médiathèque factice ; la lecture réelle (codecs, HDR, son) et le déploiement restent à essayer sur place, et ils doivent le dire dans leur compte rendu.
- **Les tests sautés ne comptent pas.** Sans Playwright ou ffmpeg, `tests/test_ui.py` est ignoré ; les agents doivent le signaler au lieu d'annoncer « tout passe ».
- **`media_library` doit être à côté de `media_hub`** (ou désigné par `HUB_REPO`), sinon aucun test ne démarre.
- Leurs consignes sont tirées de la lecture du code : si un agent applique une règle devenue fausse, corrige son fichier.

## Modifier ou ajouter un agent

Un agent est un fichier Markdown dans `.claude/agents/` :

```markdown
---
name: hub-exemple
description: Quand l'appeler (c'est ce texte qui sert à la délégation automatique).
tools: Read, Grep, Glob, Edit, Write, Bash
model: sonnet
---

Les consignes de l'agent, en Markdown libre.
```

- `description` : sois concret sur *quand* l'utiliser ; c'est ce que Claude lit pour choisir.
- `tools` : donne le minimum. Un relecteur n'a pas besoin de `Edit` ni de `Write`.
- Garde les consignes courtes et vérifiables (invariants du projet, commandes à lancer, format du compte rendu).
- Ne mets **pas** d'autre fichier `.md` dans `.claude/agents/` : chaque fichier y est lu comme un agent. Cette documentation vit donc dans `docs/`.
