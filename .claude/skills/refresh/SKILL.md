---
name: refresh
description: >
  Met à jour main et supprime les branches locales déjà fusionnées dont la copie
  distante a disparu, sans demander. Use when the user runs /refresh or says
  "refresh branches" (ou « rafraîchis / nettoie les branches »). Pour ranger
  après UNE pull request précise, c'est /merged.
---

# refresh

Nettoyage général du dépôt : tout ce qui est **fusionné dans `main` et disparu du distant** s'en va, sans question. Rien d'autre n'est touché.

## Étapes

1. **Récupérer** : `git fetch --prune origin`.
2. **Mettre `main` à jour** :
   - sur `main` : `git pull --ff-only origin main` ;
   - sur une autre branche : `git fetch origin main:main` (avance `main` sans quitter la branche courante ; refuse tout ce qui n'est pas un avance rapide).
   Un échec se rapporte tel quel, il ne se contourne pas.
3. **Trouver les branches à supprimer** : `git branch -vv` donne celles dont l'amont est `gone`. Retiens celles qui sont aussi dans `git branch --merged main`. Une branche **sans amont** (jamais poussée) ou **non fusionnée** n'est jamais candidate.
4. **Supprimer** chaque candidate avec `git branch -d` : jamais `-D`.
   - La branche **courante** : si l'arbre est propre (`git status --short` vide), `git switch main` d'abord, puis suppression. S'il ne l'est pas, ne touche à rien : dis ce qui traîne, sans `stash`, `checkout --` ni `reset`.
   - Une branche **tenue par un worktree** (`git branch -d` refuse) : ne la force pas, signale le dossier (`git worktree list`) et propose `git worktree remove <dossier>`.
5. **Rendre compte**, en peu de lignes : ce qui a été supprimé, ce qui reste (`git branch`) et pourquoi (travail local pas encore poussé, non fusionné, en worktree), `main` au commit `git log --oneline -1`. Signale un `git stash list` non vide : un stash n'est pas une branche, tu ne le supprimes pas.

## Garde-fous

- Pas de `-D`, pas de `--force`, pas de `git push origin --delete`, pas de réécriture d'historique.
- Tu ne supprimes que des branches **locales**.
- Ne fusionne rien, ne pousse rien, ne crée pas d'étiquette.
