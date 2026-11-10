---
name: check
description: >
  Lance les tests des modules touchés par le travail en cours (et tests.test_ui
  si l'interface a bougé), jamais la suite complète. Use when the user runs
  /check or asks to "tester les modules touchés" ; la suite complète est celle
  de /ship.
---

# check

Le contrôle rapide d'un lot en cours. La suite complète dure environ 30 minutes (surtout l'interface) et **n'est lancée que par `/ship`**, sur le commit qui part en PR : deux suites en même temps se ralentissent et font échouer le test instable de la carte de film.

## Étapes

1. **Ce qui a changé** : `git diff --name-only main` (commits de la branche) plus `git status --short` (fichiers non commités). Dans un worktree, travaille dans ce dossier-là.
2. **Quels tests** : pour chaque fichier `hub/<x>.py`, `hub/<dossier>/<x>.py`, cherche ceux qui l'importent :
   `grep -lE "hub(\.| import ).*\b<x>\b" tests/test_*.py`
   Ajoute les tests de même nom (`tests/test_<x>*.py`). Cas particuliers :

   | Changement | Tests |
   | --- | --- |
   | `hub/web/**` | `tests.test_ui`, `tests.test_popups`, `tests.test_i18n` |
   | `hub/server/app.py`, `hub/urls.py` | tous les `tests/test_server*.py` |
   | `hub/server/jobs.py` | `tests.test_jobs`, `tests.test_autoscan` |
   | `hub/db.py` | `tests.test_database` + ceux que `grep` trouve |
   | `hub/config.py`, `hub/__main__.py` | `tests.test_config`, `tests.test_cli` |
   | `.claude/hooks/**` | `tests.test_hooks` |
   | `.github/scripts/**` | `tests.test_ci_version` |
   | `tests/<fichier>` modifié | ce fichier |

3. **Lancer** : `python -m unittest <modules>` (par exemple `python -m unittest tests.test_database tests.test_scan`), depuis la racine du dépôt. N'ajoute `tests.test_ui` que si `hub/web` a changé.
4. **Rendre compte** avec les chiffres exacts : nombre de tests, échecs, erreurs, **sautés**. Un test sauté (Playwright ou ffmpeg absent, `media_library` introuvable) n'est pas un test réussi : dis-le. Les échecs se rapportent tels quels, sans les « arranger ».
5. Rappelle en une ligne que la suite complète reste à `/ship`.

## Garde-fous

- Jamais `python -m unittest discover -s tests` ici, sauf pour reproduire un problème précis, et jamais en parallèle d'une autre suite.
- Ne modifie ni test ni code pour faire passer le contrôle : rapporte.
