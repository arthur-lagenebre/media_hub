---
name: release
description: >
  Pose l'étiquette de version vX.Y.Z sur le commit de fusion qui a introduit
  cette version dans main, la pousse (ce qui publie l'image sur ghcr.io) et
  suit la construction de l'image. Use only when the user runs /release
  vX.Y.Z.
disable-model-invocation: true
---

# release

Une étiquette `vX.Y.Z` poussée sur GitHub **publie une image** : le workflow `Image` (`.github/workflows/image.yml`) la construit et la pousse sur `ghcr.io`. Ce geste est le moins réversible du flux. `/release` ne se lance donc que si l'utilisateur le tape, avec le numéro voulu : taper `/release v0.25.0` vaut accord pour **cette** étiquette, pas pour les suivantes. Aucun autre skill ni agent ne pose d'étiquette (voir `docs/decisions.md`).

## Étapes

1. **Lire le numéro.** L'argument est `vX.Y.Z` ou `X.Y.Z`. Sans argument, ou s'il n'a pas cette forme, arrête-toi et demande-le. Rien n'est deviné.
2. **L'étiquette existe-t-elle ?** Lance `git fetch -q origin --tags`, puis `git tag -l vX.Y.Z` et `git ls-remote --tags origin vX.Y.Z`. Si l'étiquette existe déjà, en local ou sur GitHub, arrête-toi et dis sur quel commit elle pointe : on ne la déplace pas et on ne la recrée pas.
3. **Trouver le commit.** Lance `git fetch -q origin main`. Le commit visé est **le commit de fusion de `origin/main` qui a introduit `__version__ = "X.Y.Z"`**. Pour le trouver :
   - lance `git log origin/main --first-parent --diff-merges=first-parent --no-patch --format=%H -G '__version__ = "X\.Y\.Z"' -- hub/__init__.py` et garde le commit **le plus ancien** de la liste (la dernière ligne) : `-G` trouve aussi la fusion qui a *remplacé* ce numéro par le suivant, et seule la plus ancienne l'a introduit ;
   - vérifie que `git show <commit>:hub/__init__.py` contient bien `__version__ = "X.Y.Z"` ;
   - vérifie que c'est un commit de fusion (`git rev-list --parents -n 1 <commit>` donne trois identifiants).

   Si aucun commit ne correspond (version jamais fusionnée, PR encore ouverte) ou si une vérification échoue, arrête-toi et dis pourquoi.
4. **Le commit est-il vert ?** Lance `gh run list --commit <commit> --workflow Tests --json status,conclusion,url`.
   - **Réussi** : continue.
   - **En cours** : dis-le et attends la fin. Ne pose pas l'étiquette avant.
   - **Échoué, ou aucun passage** : arrête-toi, montre le lien et la cause (`gh run view <id> --log-failed`). Une étiquette sur un commit rouge n'est posée que si l'utilisateur le redemande en connaissance de cause.
5. **Montrer avant de poser.** En deux ou trois lignes, donne :
   - le numéro et le commit (`git log --oneline -1 <commit>`) ;
   - les PR que contient cette version, c'est-à-dire les fusions depuis l'étiquette précédente : `git log --first-parent --oneline <étiquette précédente>..<commit>`, où l'étiquette précédente est `git describe --tags --abbrev=0 <commit>^`.

   Puis continue sans redemander : l'utilisateur a déjà donné son accord en tapant la commande.
6. **Poser et pousser** : `git tag vX.Y.Z <commit>`, puis `git push origin vX.Y.Z`. L'étiquette est **légère**, comme les précédentes. On ne pousse que cette étiquette : jamais `--tags`, jamais `--force`.
7. **Suivre l'image.** Le workflow `Image` démarre sur l'étiquette. Retrouve son passage avec `gh run list --workflow Image --branch vX.Y.Z --limit 1`, puis suis-le jusqu'au bout avec `gh run watch <id> --exit-status`. Il dure quelques minutes.
   - **Réussi** : donne le nom de l'image publiée (`ghcr.io/<propriétaire>/media_hub:X.Y.Z`, d'après le résumé du passage).
   - **Échoué** : donne la cause (`gh run view <id> --log-failed`). L'étiquette reste en place : ne la supprime pas. Dis ce qu'il faudrait faire (une nouvelle version corrigée) et laisse l'utilisateur décider.
8. **Rendre compte** en peu de lignes : l'étiquette, son commit, les PR qu'elle contient, le résultat du workflow `Image`.

## Garde-fous

- Une seule étiquette par appel : celle qui est demandée.
- Jamais `git tag -f`, `git push --force`, `git push --tags`, `git push origin :vX.Y.Z` ni `git tag -d` sur une étiquette déjà poussée.
- Le numéro vient de l'utilisateur. Tu ne le calcules pas et tu n'en choisis pas un « plus logique ». Si `__version__` sur `main` est plus récent que le numéro demandé, c'est permis (on peut étiqueter une version plus ancienne qui n'a pas d'étiquette) : dis-le simplement.
- Rien n'est modifié dans le dépôt : ni commit, ni branche. Ce skill marche depuis n'importe quel dossier du dépôt, puisqu'il ne lit que `origin/main`.
