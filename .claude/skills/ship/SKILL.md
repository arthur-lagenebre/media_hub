---
name: ship
description: >
  Livre le travail en cours : vérifications, branche, commits, push et pull
  request au format du dépôt. Use only when the user runs /ship or explicitly
  asks to commit, push or open a PR.
disable-model-invocation: true
---

# ship

Le dépôt avance par **branches thématiques** (`accueil`, `titre-invisible`, `apercu-compter`…) fusionnées par pull request sur `main` (`Merge pull request #81 from arthur-lagenebre/titre-invisible`). `/ship` vaut accord de l'utilisateur pour pousser et ouvrir **cette** PR, pas les suivantes.

## Étapes

1. **Vérifier** : `python -m unittest discover -s tests` (serveur et interface). Rapporte le résultat exact (nombre de tests, échecs, erreurs, sautés) et ne continue pas si un point bloque. Une fonctionnalité ou une route nouvelle doit aussi se retrouver dans `docs/api.md` et `docs/TODO.md`.
2. **Version** : lis le diff contre `main` et règle `__version__` (`hub/__init__.py`) selon ce que la PR change pour l'utilisateur, en une seule fois par PR, même avec plusieurs commits :

   | La PR… | `__version__` |
   |---|---|
   | ajoute une fonction visible (route, écran, réglage `HUB_*`, comportement de scan ou de lecture) | **MINOR** : `0.2.0` → `0.3.0` |
   | corrige un défaut, sans fonction nouvelle | **PATCH** : `0.2.0` → `0.2.1` |
   | ne touche que les tests, la doc, `.claude/` ou `deploy/` | **inchangée** |

   Si le diff retire ou renomme un champ de l'API, `API_VERSION` monte aussi (MINOR au moins) ; s'il change le schéma, `SCHEMA_VERSION` monte avec sa migration dans `MIGRATIONS` : vérifie que c'est fait. On reste en `0.x` : passer en `1.0.0` est une décision de l'utilisateur, jamais un effet de `/ship`. Le choix MINOR ou PATCH est un jugement : écris-le dans la description de la PR (« version 0.2.0 → 0.3.0 : nouvelle route ») pour que l'utilisateur puisse le contester avant de fusionner. Le numéro monte dans le même commit que le changement qui le justifie, pas dans un commit à part.
3. **Branche** : si on est sur `main`, crée d'abord une branche courte en kebab-case qui nomme le sujet (`accueil`, `titre-invisible`). Jamais de commit direct sur `main`.
4. **Commits** : petits, ordonnés, un sujet chacun. Message **en français**, titre `<Sujet> : <ce qui change>` (« Accueil : 24 nouveautés, 8 au plus par bibliothèque »), puis un corps en trois paragraphes, comme les commits existants :
   - **Pourquoi** : le besoin ou le défaut, tel que l'utilisateur l'a vu ;
   - **Quoi** : ce qui change, côté serveur et côté interface ;
   - **Preuve** : les tests ajoutés (écrits avant le code et vus échouer quand c'est le cas), les totaux de tests qui passent, mutator ou captures d'écran s'ils ont servi.
   Termine par la ligne d'attribution demandée par l'environnement.
5. **Ne commite jamais** un secret, une base de données, un cache, un fichier de la médiathèque ni de la configuration locale. Relis `git status` avant `git add` et ajoute les fichiers un par un, pas `git add -A`.
6. **Push** de la branche (`git push -u origin <branche>`), puis **PR** vers `main` avec `gh pr create` : titre court dans la même forme que les commits, description qui dit le *pourquoi*, ce qui change pour l'utilisateur, la preuve (tests) et les limites connues. Termine la description avec la ligne d'attribution demandée par l'environnement.
7. Donne le lien de la PR. Ne fusionne pas : c'est à l'utilisateur de le faire (ensuite, `/merged`).

## Garde-fous

- Pas de `--force`, pas de `--no-verify`, pas de réécriture d'un historique déjà poussé.
- Si une vérification échoue après le push (CI éventuelle), rapporte l'échec et sa cause (`gh run view --log-failed`) au lieu de pousser un correctif à l'aveugle.
