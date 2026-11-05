# À faire

Ce qui reste ouvert. Ce qui est fait est dans `git log` et dans les pull requests ; ce qui a été tranché, surtout les « non », est dans [decisions.md](decisions.md) : à lire avant de proposer quelque chose de nouveau.

## À essayer sur place

Les tests tournent dans un Chromium de bureau, pas sur la télé. Rien de tout cela ne bloque le reste.

- [ ] **À la télécommande**, sur la télé (Android TV, Fire TV, webOS) : les sagas, l'éditeur de sagas, le sélecteur de films, les cartes grisées, le bouton « … » des films, des sagas, des séries et des animations, les photos d'artistes, la liseuse et le lecteur.
- [ ] **Les téléchargements** sur un vrai navigateur : Chrome et Firefox demandent d'autoriser les téléchargements multiples la première fois. Vérifier aussi qu'un téléchargement long ne bloque pas les autres requêtes et ne compte pas dans `HUB_MAX_STREAMS`.

Si `test_admin_runs_an_update` échoue (il a échoué une fois sur une machine, jamais sur l'autre) : le relancer seul avec `python -W ignore -m unittest tests.test_ui.TestDetailPages.test_admin_runs_an_update` et garder les lignes de l'échec. Ce test attend au plus 30 s qu'un scan se termine.

## Limites connues, non traitées

- [ ] Une saga de `sagas.json` ne garde que la **première version** d'un film listé en double (`by_tmdb`, le premier gagne). Les sagas de l'étiquette, elles, numérotent les deux versions.
- [ ] Le conteneur `media_hub` **tourne en root** (avec `cap_drop: ALL` et deux capacités gardées, voir `deploy/README.md`).
- [ ] Le **jeton de session ne se périme pas**, et *Se déconnecter* n'efface que le cookie de l'appareil : seul *Déconnecter les autres appareils* révoque.
- [ ] Une **fiche TMDB fixée à la main** ne se retire pas, on en fait une autre. Le marqueur `{tmdb-…}` n'est pas écrit dans le nom du dossier : pour que `media_library` voie le même choix, il se tape à la main.

## Idées mises de côté

À reprendre seulement si l'envie vient.

- [ ] **Affiches TMDB** : les récupérer pour les films qui n'en ont pas, ou les proposer dans *Changer l'affiche*.
- [ ] **Écrire le marqueur `{tmdb-…}`** depuis le hub quand le volume n'est pas en lecture seule.
- [ ] **Accueil** : « À revoir » (suggestions calculées par compte), un accueil par personne (« depuis ta dernière visite »).
- [ ] **`ARTISTS` dans `media_library`** : le scan lit les tags lui-même (`hub/scan/music.py`, `read_info`) parce que `track_info` ne rend pas encore `ARTISTS` ; le jour où il le fera, `credits` pourra disparaître du hub.
- [ ] **Mesurer le JavaScript** de l'interface : aucun outil de couverture ni de complexité n'existe pour `hub/web`. Une mesure grossière (3 novembre 2026) donnait 10 fonctions sur 862 à 15 décisions ou plus, presque toutes du dispatch de touches (`core.js`, lecteurs vidéo et de livres) ; à reprendre avec un vrai outil.

## Pas relu

- Le dépôt `media_library` (modifications non commitées côté développement).
- `.claude/skills/`, volontairement laissé de côté.
