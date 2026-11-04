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
2. **Branche** : si on est sur `main`, crée d'abord une branche courte en kebab-case qui nomme le sujet (`accueil`, `titre-invisible`). Jamais de commit direct sur `main`.
3. **Commits** : petits, ordonnés, un sujet chacun. Message **en français**, titre `<Sujet> : <ce qui change>` (« Accueil : 24 nouveautés, 8 au plus par bibliothèque »), puis un corps en trois paragraphes, comme les commits existants :
   - **Pourquoi** : le besoin ou le défaut, tel que l'utilisateur l'a vu ;
   - **Quoi** : ce qui change, côté serveur et côté interface ;
   - **Preuve** : les tests ajoutés (écrits avant le code et vus échouer quand c'est le cas), les totaux de tests qui passent, mutator ou captures d'écran s'ils ont servi.
   Termine par la ligne d'attribution demandée par l'environnement.
4. **Ne commite jamais** un secret, une base de données, un cache, un fichier de la médiathèque ni de la configuration locale. Relis `git status` avant `git add` et ajoute les fichiers un par un, pas `git add -A`.
5. **Push** de la branche (`git push -u origin <branche>`), puis **PR** vers `main` avec `gh pr create` : titre court dans la même forme que les commits, description qui dit le *pourquoi*, ce qui change pour l'utilisateur, la preuve (tests) et les limites connues. Termine la description avec la ligne d'attribution demandée par l'environnement.
6. Donne le lien de la PR. Ne fusionne pas : c'est à l'utilisateur de le faire (ensuite, `/merged`).

## Garde-fous

- Pas de `--force`, pas de `--no-verify`, pas de réécriture d'un historique déjà poussé.
- Si une vérification échoue après le push (CI éventuelle), rapporte l'échec et sa cause (`gh run view --log-failed`) au lieu de pousser un correctif à l'aveugle.
