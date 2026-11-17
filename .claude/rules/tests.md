---
paths:
  - "tests/**"
---

# Tests

- `unittest` standard, un fichier `tests/test_<sujet>.py` par sujet. Le serveur est démarré pour de vrai sur une médiathèque factice (`tests/fixtures.build_library`), l'interface dans Chromium (Playwright) : jamais la vraie médiathèque, aucun appel à un service externe (TMDB, MusicBrainz).
- **Le test s'écrit avant le code et se voit échouer** pour la bonne raison ; un bug se reproduit d'abord par un test.
- Noms en anglais, en phrase qui dit le comportement (`test_a_scan_never_erases_enriched_data`) ; docstring de module en français.
- **Un test sauté n'est pas un test réussi** : sans Playwright ou ffmpeg, `test_ui.py` est ignoré, et on le dit.
- Un test d'interface lent ou instable se signale, il ne se supprime pas. Le test `test_a_film_card_plays_the_film_and_a_seen_one_is_watched_again_from_the_start` échoue sous charge quand deux lots de tests d'interface tournent ensemble : jamais deux sessions qui lancent l'interface en même temps.
- `/ship` lance l'interface en parallèle (`python -m tests.parallel`, environ 4 min) et laisse la suite du serveur à la CI ; entre-temps, `/check` lance les modules touchés. Un test qui échoue seulement en parallèle compte sur le temps : on corrige son attente (reproduire avec `HUB_UI_SLOWDOWN=8`), on n'ajoute pas de délai.
