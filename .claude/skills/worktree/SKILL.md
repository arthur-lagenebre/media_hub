---
name: worktree
description: >
  Crée ou retire le worktree d'un lot de travail, à côté du dépôt, pour qu'une
  session Claude travaille sans toucher aux fichiers d'une autre. Use only when
  the user runs /worktree (créer : `/worktree <lot>` ; retirer : `/worktree
  remove <lot>` ; lister : `/worktree list`).
disable-model-invocation: true
---

# worktree

Deux sessions ouvertes sur le même dossier partagent la branche et les fichiers non commités : un `checkout`, `stash` ou `clean` de l'une efface le travail de l'autre, et une suite de tests teste le code de l'autre (voir `docs/agents.md`). Un lot en parallèle = un worktree.

`<lot>` est un nom court en kebab-case (`lot2`, `titre-invisible`) : il sert au dossier **et** à la branche.

## Créer : `/worktree <lot> [branche]`

1. **Partir du dossier principal** : `git worktree list` — la première ligne est le dépôt principal. Le worktree se crée à côté de lui, dans le même dossier parent : `<parent>/media_hub-<lot>`. Si ce dossier ou la branche (`<branche>`, par défaut `<lot>`) existe déjà, arrête-toi et dis-le : rien n'est écrasé.
2. **Base à jour** : `git fetch origin main`.
3. **Créer** : `git worktree add ../media_hub-<lot> -b <branche> --no-track origin/main` (le chemin réel est celui de l'étape 1). `--no-track` : la branche ne doit pas suivre `origin/main`, elle aura son propre amont au premier `git push -u`.
4. **Vérifier le voisin** : `media_library` doit se trouver à côté de `media_hub` (ou être désigné par `HUB_REPO`), sans quoi aucun test ne démarre. Si `../media_library` n'existe pas et que `HUB_REPO` est vide, dis-le.
5. **Rendre compte** : le chemin absolu du worktree, la branche, et la phrase à reprendre pour l'agent ou la session qui y travaille : « travaille dans `<chemin absolu>` ». Rappelle qu'une session se démarre *dans* ce dossier, et que le dossier principal reste à celle qui y travaillait.

## Retirer : `/worktree remove <lot>`

1. `git worktree list` pour retrouver le dossier ; `git -C <dossier> status --short` doit être vide. Sinon, arrête-toi et dis ce qui traîne (pas de `--force`, pas de `stash`).
2. `git worktree remove <dossier>`, puis `git worktree prune`.
3. La branche n'est **pas** supprimée ici : propose `/merged` (une PR fusionnée) ou `/refresh` (tout ce qui est fusionné).

## Lister : `/worktree list`

`git worktree list`, avec la branche de chacun et son état (`git -C <dossier> status --short`, « propre » ou « n fichiers modifiés »).

## Garde-fous

- Jamais de `checkout` ni de `switch` dans le dossier principal pour « préparer » un lot.
- Jamais de `--force` sur `git worktree add` ni sur `remove`.
- Le worktree se crée **à côté** du dépôt, pas dedans.
