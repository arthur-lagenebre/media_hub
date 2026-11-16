---
name: merged
description: >
  Range le dépôt une fois une pull request fusionnée par l'utilisateur :
  synchronise main, retire le worktree et la branche fusionnée, vérifie que
  sa copie distante a disparu, puis met à jour les cartes Notion de la PR.
  Use when the user runs /merged or says a PR is merged ("merged", "c'est
  merged").
---

# merged

L'utilisateur fusionne lui-même ses PR (voir `/ship`). Ce skill fait le rangement qui suit. Il ne fusionne rien, ne pousse rien et ne supprime rien qui ne soit déjà fusionné dans `main`. Il ne pose jamais d'étiquette de version : c'est `/release`.

## Étapes

1. **Quelle PR ?** Celle de la branche courante (`git branch --show-current`), sinon celle que l'utilisateur nomme (`#82`). Sur `main` sans indication, prends la dernière PR ouverte par lui (`gh pr list --author @me --state all --limit 5`). Vérifie que **GitHub la dit fusionnée** : `gh pr view <n> --json state,mergeCommit,headRefName,body`. Si elle n'est pas `MERGED`, arrête-toi et dis où elle en est (ouverte, CI en cours ou rouge, fermée sans fusion) : rien n'est supprimé.
2. **Arbres propres ?** `git status --short` doit être vide dans le dossier où tu es. Si la branche de la PR est extraite dans un worktree (`git worktree list`), `git -C <dossier> status --short` doit l'être aussi. Sinon, arrête-toi et dis ce qui traîne. Tu n'utilises ni `git stash`, ni `git checkout -- `, ni `git reset --hard` pour « arranger » ça : ce travail n'est pas à toi.
3. **Retirer le worktree de la branche**, s'il y en a un et qu'il est propre : `git worktree remove <dossier>` puis `git worktree prune`. Jamais `--force`. Sans cette étape, git refuse de supprimer une branche encore extraite. Ne touche pas aux autres worktrees.
4. **Synchroniser** : dans le dossier principal (déjà sur `main`, sinon `git switch main`), lance `git pull --ff-only origin main`. Un échec du `--ff-only` se rapporte tel quel, il ne se contourne pas.
5. **Supprimer la branche locale** avec `git branch -d <branche>`, jamais `-D`. S'il refuse (« not fully merged »), la fusion n'est pas dans `main` comme on le croyait : montre-le et ne force rien.
6. **Nettoyer le distant** : `git fetch --prune`, puis `git branch -a`. La branche distante doit avoir disparu (GitHub la supprime à la fusion). Si elle est encore là, dis-le sans la supprimer.
7. **Mettre à jour Notion** avec le connecteur MCP du projet (`.mcp.json`) :
   - **Les cartes de la PR.** Prends d'abord les liens `Carte Notion :` de la description de la PR (voir `/ship`). Sans lien, cherche dans le backlog les cartes « En cours » dont le titre correspond au sujet de la PR. Si plusieurs cartes peuvent correspondre ou si aucune ne correspond clairement, demande laquelle au lieu de deviner.
   - **Les passer en « Terminé »**, en ajoutant en tête de chacune un encadré qui dit : corrigé ou livré par la PR #n, à quelle date, en quelle version, et ce qui reste ouvert. Une correction **sans preuve** (défaut jamais reproduit) le dit et demande de rouvrir la carte si le problème revient.
   - **Les limites de la PR qui demandent un essai sur place** (NAS, téléphone, tablette, télévision) deviennent des lignes de la page « Essais sur place », statut « À faire », avec la séance qui convient.
   - **Les autres limites** (un travail resté ouvert) ne deviennent pas de nouvelles cartes de backlog sans accord : propose-les, avec une priorité suggérée. Choisir une priorité et découper le travail demandent un jugement.
   - Sans accès à Notion, rends le texte à l'utilisateur pour qu'il le reporte.
8. **Rendre compte** en peu de lignes :
   - le commit de fusion (`git log --oneline -3`), les branches et worktrees qui restent (`git branch`, `git worktree list`), l'état propre de l'arbre ;
   - les cartes et lignes Notion modifiées ou créées, avec leur lien ;
   - ce qui dépendait de cette PR : une autre branche empilée dessus qu'il faut maintenant rebaser, ou une branche ouverte qui touche les mêmes fichiers (version comprise) et devra fusionner `main`.

   **Version à étiqueter ?** Si `__version__` a changé dans cette PR (`git diff <mergeCommit>^1 <mergeCommit> -- hub/__init__.py`, avec `mergeCommit.oid` lu à l'étape 1) et que `git tag -l vX.Y.Z` ne renvoie rien, termine par la ligne : `Version X.Y.Z prête : /release vX.Y.Z publie l'image sur ghcr.io (à toi de décider), puis mets X.Y.Z dans l'app TrueNAS.` Si le diff est vide ou si l'étiquette existe déjà, ne dis rien.

## Garde-fous

- Pas de `--force`, pas de `-D`, pas de `git push origin --delete`, pas de réécriture d'historique.
- Une seule branche est supprimée, avec son worktree : celle de la PR fusionnée. Les autres branches et worktrees ne sont pas touchés, même s'ils ont l'air morts (pour un nettoyage général, c'est `/refresh`).
- Tu ne fusionnes jamais une PR toi-même, même si la CI est verte.
- Tu ne crées ni ne pousses jamais d'étiquette (`git tag`, `git push origin v…`) : c'est `/release`, lancé par l'utilisateur.
- Dans Notion, tu ne mets à jour que les cartes de cette PR et tu n'ajoutes que les lignes d'essai qui en découlent. Tu ne supprimes rien.
