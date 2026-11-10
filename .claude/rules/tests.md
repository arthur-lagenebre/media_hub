---
paths:
  - "tests/**"
---

# Tests

- `unittest` standard, un fichier `tests/test_<sujet>.py` par sujet. Le serveur est démarré pour de vrai sur une médiathèque factice (`tests/fixtures.build_library`), l'interface dans Chromium (Playwright) : jamais la vraie médiathèque, aucun appel à un service externe (TMDB, MusicBrainz).
- **Le test s'écrit avant le code et se voit échouer** pour la bonne raison ; un bug se reproduit d'abord par un test.
- Noms en anglais, en phrase qui dit le comportement (`test_a_scan_never_erases_enriched_data`) ; docstring de module en français.
- **Un test sauté n'est pas un test réussi** : sans Playwright ou ffmpeg, `test_ui.py` est ignoré, et on le dit.
- Un test d'interface lent ou instable se signale, il ne se supprime pas. Le test `test_a_film_card_plays_the_film_and_a_seen_one_is_watched_again_from_the_start` échoue sous charge quand deux suites tournent ensemble : ne pas lancer deux suites complètes en parallèle.
- La suite complète (~30 min) n'est lancée que par `/ship` ; entre-temps, `/check` lance les modules touchés.
