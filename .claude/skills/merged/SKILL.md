---
name: merged
description: >
  Range le dépôt une fois une pull request fusionnée par l'utilisateur :
  synchronise main, supprime la branche fusionnée en local, vérifie que sa
  copie distante a disparu. Use when the user runs /merged or says a PR is
  merged ("merged", "c'est merged").
---

# merged

L'utilisateur fusionne lui-même ses PR (voir `/ship`). Ce skill fait le rangement qui suit : il ne fusionne rien, ne pousse rien et ne supprime rien qui ne soit déjà fusionné dans `main`.

## Étapes

1. **Quelle PR ?** Celle de la branche courante (`git branch --show-current`), sinon celle que l'utilisateur nomme (`#82`) ; sur `main` sans indication, la dernière PR ouverte par lui (`gh pr list --author @me --state all --limit 5`). Vérifie que **GitHub la dit fusionnée** : `gh pr view <n> --json state,mergeCommit,headRefName`. Si elle n'est pas `MERGED`, arrête-toi et dis où elle en est (ouverte, CI en cours ou rouge, fermée sans fusion) : rien n'est supprimé.
2. **Arbre propre ?** `git status --short` doit être vide. Sinon, arrête-toi et dis ce qui traîne : tu n'utilises ni `git stash`, ni `git checkout -- `, ni `git reset --hard` pour « arranger » ça, ce travail n'est pas à toi.
3. **Synchroniser** : `git switch main`, puis `git pull --ff-only origin main`. Un échec du `--ff-only` se rapporte tel quel, il ne se contourne pas.
4. **Supprimer la branche locale** avec `git branch -d <branche>` — jamais `-D`. S'il refuse (« not fully merged »), la fusion n'est pas dans `main` comme on le croyait : montre-le et ne force rien.
5. **Nettoyer le distant** : `git fetch --prune`, puis `git branch -a`. La branche distante doit avoir disparu (GitHub la supprime à la fusion). Si elle est encore là, dis-le sans la supprimer.
6. **Rendre compte** en peu de lignes : le commit de fusion (`git log --oneline -3`), les branches qui restent (`git branch`), l'état propre de l'arbre. Signale ce qui dépendait de cette PR : une autre branche empilée dessus qu'il faut maintenant rebaser, un point des « Limites » de la PR resté à faire, une ligne de `docs/TODO.md` à cocher. Ne le fais pas toi-même : propose-le.

   **Version à étiqueter ?** Si `__version__` a changé dans cette PR (`git diff <mergeCommit>^1 <mergeCommit> -- hub/__init__.py`, avec le commit de fusion lu à l'étape 1 : `mergeCommit.oid`) et que `git tag -l v<X.Y.Z>` ne renvoie rien, ajoute après le compte rendu une ligne :
   `Version X.Y.Z : git tag vX.Y.Z <commit de fusion> && git push origin vX.Y.Z (publie l'image sur ghcr.io, à toi de décider)`. Tu ne lances pas cette commande. Si le diff est vide ou si l'étiquette existe déjà, ne dis rien.

## Garde-fous

- Pas de `--force`, pas de `-D`, pas de `git push origin --delete`, pas de réécriture d'historique.
- Une seule branche est supprimée : celle de la PR fusionnée. Les autres branches locales ne sont pas touchées, même si elles ont l'air mortes (pour un nettoyage général, c'est `/refresh`).
- Tu ne fusionnes jamais une PR toi-même, même si la CI est verte.
- Tu ne crées ni ne pousses jamais d'étiquette (`git tag`, `git push origin v…`) : tu proposes la commande, c'est l'utilisateur qui la lance.
