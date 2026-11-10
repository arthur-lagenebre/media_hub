# À faire

Ce qui reste ouvert. Ce qui est fait est dans `git log` et dans les pull requests ; ce qui a été tranché, surtout les « non », est dans [decisions.md](decisions.md) : à lire avant de proposer quelque chose de nouveau. Ce qui reste à essayer sur place : [essais-sur-place.md](essais-sur-place.md). Rien n'est en attente d'être développé.

## À essayer sur place

Les essais que les tests ne peuvent pas faire (NAS, Funnel, téléphone, tablette, télé) sont une séance à dérouler dans [essais-sur-place.md](essais-sur-place.md). Rien de tout cela ne bloque le reste.

## Limites connues, non traitées

- [ ] **`test_admin_runs_an_update` échoue parfois** (une fois sur une machine, jamais sur l'autre) : le relancer seul avec `python -W ignore -m unittest tests.test_ui.TestDetailPages.test_admin_runs_an_update` et garder les lignes de l'échec. Ce test attend au plus 30 s qu'un scan se termine.
- [ ] **Un refus précoce de plus de 64 Mo ou de 5 s d'envoi** : le serveur laisse arriver la réponse en jetant le corps dans ces bornes (`INBOX_DRAIN_BYTES`, `INBOX_DRAIN_SECONDS`) ; un refus qui tombe sur un fichier de plusieurs Go ferme ensuite la connexion avec des octets non lus, et le navigateur peut encore voir une erreur réseau (message neutre de la page « Envoyer »). Un `Content-Length` illisible ou un corps en morceaux (411, 400) n'est pas jeté non plus.
- [ ] **Pas de reprise d'un envoi interrompu** (`Content-Range`) : un envoi coupé se refait en entier ; la reprise viendra plus tard, si le besoin se confirme.
- [ ] **Réponses tardives et focus** : l'accueil ne prend plus le focus de la page courante quand sa réponse arrive après qu'on l'a quittée (c'était la cause des tests de la liseuse instables en CI : le focus allait sur la barre de la liseuse, dont les flèches ne tournent plus les pages avant son masquage, 6 s plus tard). Les autres pages qui se redessinent après une réponse du serveur (`render` de la fiche d'un film, `render` de la fiche d'un artiste, les réglages, `refreshUsers`) appellent encore `H.focus.first()` sans vérifier qu'elles sont toujours à l'écran : même défaut possible, non observé. De plus, les tests de la liseuse qui attendent la sauvegarde de la position (`wait_for_timeout(2000)` pour un minuteur de 1,5 s) échouent si le navigateur est ralenti d'un facteur 8 (`Emulation.setCPUThrottlingRate`) : à remplacer par une attente de la requête `/api/progress`.
- [ ] **Liseuse, double page** : passer de la page seule au double depuis une page paire donne des couples décalés (`[2,3]` au lieu de `[1,2]`) ; « Décaler d’une page » les recale. Une page paysage rompt aussi le couplage local : les couples ne suivent pas une grille fixe du livre.
- [ ] **Liseuse, images lentes** : les préchargements qui sortent de la fenêtre ne sont pas annulés (bande passante seulement), une image en erreur reste dans le cache et n'est pas retentée tant qu'elle y reste, et une image dont la requête ne répond jamais (ni chargement ni erreur) laisse les touches de la liseuse sans effet, faute de délai de garde (le curseur de progression reste utilisable).
- [ ] Une saga de `sagas.json` ne garde que la **première version** d'un film listé en double (`by_tmdb`, le premier gagne). Les sagas de l'étiquette, elles, numérotent les deux versions.
- [ ] Le conteneur `media_hub` **tourne en root** (avec `cap_drop: ALL` et deux capacités gardées, voir `deploy/README.md`).
- [ ] Le **jeton de session ne se périme pas**, et *Se déconnecter* n'efface que le cookie de l'appareil : seul *Déconnecter les autres appareils* révoque.
- [ ] Une **fiche TMDB fixée à la main** ne se retire pas, on en fait une autre. Le marqueur `{tmdb-…}` n'est pas écrit dans le nom du dossier : pour que `media_library` voie le même choix, il se tape à la main.
- [ ] Les sous-titres **VobSub (DVD)** ne sont pas affichés : seuls les PGS (Blu-ray) le sont. ffmpeg ne sait pas les extraire tels quels (pas de muxer `.idx`/`.sub`) : il faudrait lire les paquets du `.mkv` soi-même, avec la palette de leur `CodecPrivate`. Contournement : un fichier `.srt` à côté de la vidéo.
- [ ] Les PGS ne se **choisissent jamais d'office** à l'ouverture d'un film (l'extraction peut durer) : il faut les choisir une fois dans le menu « Sous-titres » ; la langue choisie est ensuite retenue, et un PGS de cette langue est repris s'il n'y a pas de piste texte.
- [ ] **Un élément disparu laisse ses références** : le scan efface ses lignes `items` et `entries`, mais ses clés `kv` restent (`enrich:<bibliothèque>:<id>` avec la note TMDB, `tmdb-pin:<id>`, `poster:<id>`, `dismiss:<compte>:<id>`). Elles resservent si le dossier revient au même chemin, mais un élément renommé ou déplacé change d'identifiant et laisse les anciennes clés sans emploi. À faire : les retirer (et leurs fichiers d'affiche) pour les éléments absents depuis un certain temps, avec un délai comme `ORPHAN_PROGRESS_DAYS` pour qu'un volume démonté n'efface rien. À vérifier d'abord : `artist:<mbid>`, `artistlinks:<mbid>`, `wanted:<bibliothèque>` et les listes de lecture.
- [ ] **Des tris restent sans départage par l'identifiant** : `rating_key` et le tri `added` du rayon « Pas encore vu » finissent par l'identifiant, mais pas `SORTS` (`added`, `title`, `year`, `year-asc`), `release_key`, le `ORDER BY added DESC, sort LIMIT 24` de `api_home`, ni le tri des fichiers et des albums. Deux films de même titre et de même année (ou deux ajouts de même date et de même titre) gardent l'ordre dans lequel la base les rend, qui change après un nouveau scan : leur rang, ou la coupe à 24 de « Nouveautés », peut alors changer d'un chargement à l'autre. Remède : `r["id"]` en dernier critère de ces clés et `, id` dans l'`ORDER BY`.
- [ ] **Boîte de réception : un dossier repris sans la mention « compte supprimé »** : un compte nommé « default » envoie (il obtient `default/`), puis il est supprimé ; l'administrateur d'identifiant `default`, qui n'avait pas encore de dossier, envoie à son tour. `folder_for` reprend alors `default/` pour lui (plus aucun compte ne le tient) : les fichiers de l'ancien compte apparaissent comme ceux de l'administrateur, sans la mention « (compte supprimé) ». Sans perte et sans effet pour un non-administrateur ; le remède serait de ne reprendre un dossier nommé d'après l'identifiant que lors de la migration, pas dans `folder_for`.
- [ ] **Le décompte des tests du README range des tests de navigateur dans la part « serveur »** : les 293 « interface » sont ceux de `tests/test_ui.py` seul ; les 15 tests de `tests/test_i18n.py` et celui de `tests/test_popups.py` qui pilotent Chromium sont comptés dans les « serveur », alors que la phrase qui suit laisse croire qu'ils sont dans les 293.
- [ ] **Le bouton « Télécharger » des lignes par bibliothèque de la boîte « Droits de {nom} » déborde de la fenêtre à 390 px** : vérifié sur `main` et sur la branche `interface-droits-films-accueil` (défaut déjà présent, non traité par le lot 4).

## Idées mises de côté

À reprendre seulement si l'envie vient.

- [ ] **Affiches TMDB** : les récupérer pour les films qui n'en ont pas, ou les proposer dans *Changer l'affiche*.
- [ ] **Écrire le marqueur `{tmdb-…}`** depuis le hub quand le volume n'est pas en lecture seule.
- [ ] **Accueil** : « À revoir » (suggestions calculées par compte), un accueil par personne (« depuis ta dernière visite »).
- [ ] **`ARTISTS` dans `media_library`** : le scan lit les tags lui-même (`hub/scan/music.py`, `read_info`) parce que `track_info` ne rend pas encore `ARTISTS` ; le jour où il le fera, `credits` pourra disparaître du hub.
- [ ] **Mesurer le JavaScript** de l'interface : aucun outil de couverture ni de complexité n'existe pour `hub/web`. Une mesure grossière (3 novembre 2026) donnait 10 fonctions sur 862 à 15 décisions ou plus, presque toutes du dispatch de touches (`core.js`, lecteurs vidéo et de livres) ; à reprendre avec un vrai outil.

## Pas relu

- `.claude/skills/`, volontairement laissé de côté.
