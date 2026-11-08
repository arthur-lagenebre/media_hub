# À faire

Ce qui reste ouvert. Ce qui est fait est dans `git log` et dans les pull requests ; ce qui a été tranché, surtout les « non », est dans [decisions.md](decisions.md) : à lire avant de proposer quelque chose de nouveau.

## Prochaines fonctionnalités

Dans l'ordre où on les prend (7 novembre 2026), un lot par PR (ou deux).

- [ ] **Lot 3, à part : les films manquants d'une personne** (le reste du lot 3 est livré : noms cliquables, `#/person?n=`, personnes dans la recherche). Les étiquettes n'ont que le nom, pas d'identifiant TMDB de personne : il faudrait un appel TMDB et un enrichissement par personne dans `kv` (`enrich:`), donc une clé TMDB.
- [ ] **Lot 4 : boîte de réception (téléversement).** Des comptes qui ont le droit « Envoyer » déposent films, séries, musique ou livres ; l'administrateur les range lui-même. *Retenu :*
  - dépôt dans `HUB_DATA/inbox/<compte>/`, jamais dans une bibliothèque (les volumes restent en `:ro`), et pas de bouton qui déplace un fichier dans la médiathèque ;
  - un droit global par compte (colonne dans `users`, migration qui garde l'existant, administrateur toujours autorisé) ; pas d'envoi sans compte, pas de lien d'invitation ;
  - réception en flux : morceaux de 1 Mo vers un `.part` renommé à la fin (`body_bytes` lit tout en mémoire, 10 Mo : inutilisable ici), `Content-Length` obligatoire, limite par fichier, quota par compte, contrôle d'espace libre, extensions autorisées, nom nettoyé sans écrasement, nettoyage des `.part` abandonnés ;
  - jamais de ffprobe ni de scan sur un fichier déposé ; servi à l'administrateur seulement, en `attachment` ;
  - interface : page « Envoyer » (glisser-déposer, XHR avec progression, masquée sur la télé) et onglet « Boîte de réception » dans Gestion.

  *À essayer sur place :* un envoi de 1 à 2 Go à travers Funnel (débit et délais non configurables) et l'espace libre réel sous `/data`. La reprise d'un envoi interrompu (`Content-Range`) viendra plus tard, si le besoin se confirme.
- [ ] **Lot 5 : fiche manga, boîte « collection ».** Comme la saga d'un film : les autres séries de la même collection, avec ce qui manque (`meta.collection`, `grp` et `missing` existent déjà). Vérifier d'abord, sur un exemple réel, que les étiquettes regroupent bien des séries. Le regroupement dans la grille, avec le badge « 3/4 » des films, ne vient qu'ensuite, si la fiche plaît.
- [ ] **Lot 6 : manga en double page** (`reader.js`, deux images côte à côte). À trancher d'abord : le sens de lecture (japonais, de droite à gauche ; le champ `Manga` de ComicInfo peut le dire, à vérifier dans `media_library`) et la couverture seule en première page. À essayer sur une tablette.
- [ ] **Recherche sans accents** : déjà en place, `fold()` retire accents et casse (recherche globale et filtre de bibliothèque). Si un cas échoue, noter la saisie exacte : ce serait une ligature ou un caractère que `fold` ne gère pas.

## À essayer sur place

Les tests tournent dans un Chromium de bureau, pas sur la télé. Rien de tout cela ne bloque le reste.

- [ ] **À la télécommande**, sur la télé (Android TV, Fire TV, webOS) : les sagas, l'éditeur de sagas, le sélecteur de films, les cartes grisées, le bouton « … » des films, des sagas, des séries et des animations, les photos d'artistes, la liseuse et le lecteur.
- [ ] **Les pop-up unifiés** ([popups.md](popups.md)) à la télécommande et au tactile : le pied de boutons est maintenant une rangée de boutons égaux (en colonne seulement pour plusieurs champs), y compris dans la file d'attente et le choix de films ; vérifier que Gauche / Droite passent d'un bouton à l'autre et que la file d'attente tient sur un téléphone étroit.
- [ ] **Les liens d'une fiche artiste** au tactile, sur un vrai téléphone : la croix de retrait à côté de chaque lien se touche sans viser à côté, et la fenêtre « Ajouter un lien » ne se laisse pas cacher par le clavier. Sur la télé, ces contrôles sont absents (saisir une adresse à la télécommande se fait mal).
- [ ] **Les téléchargements** sur un vrai navigateur : Chrome et Firefox demandent d'autoriser les téléchargements multiples la première fois. Vérifier aussi qu'un téléchargement long ne bloque pas les autres requêtes et ne compte pas dans `HUB_MAX_STREAMS`.
- [ ] **L'interface en anglais** sur la télé (Android TV, Fire TV) et sur un téléphone étroit : les textes anglais sont parfois plus longs que les français (boutons des Paramètres, barre du lecteur, fenêtres de droits). Les tests la parcourent à 1920 px et vérifient que rien ne manque, pas que rien ne déborde.
- [ ] **Reprendre un film en « Son converti »** sur le serveur (ffmpeg 7.0.2, MKV H.264 avec images B et son DTS, comme « La reine des damnés ») : l'image, le son et les sous-titres restent calés après une reprise, après un saut aux flèches et après un changement de piste audio. Les tests tournent avec le ffmpeg de la machine de développement, pas avec la version fixée dans `deploy/fetch-ffmpeg.sh`
- [ ] **Les sous-titres PGS** sur un vrai remux Blu-ray : le calage dans le temps, la position sur l'image quand le `.mkv` est recadré (le canvas suit la taille de l'écran du Blu-ray, pas celle de l'image recadrée), la durée de la première extraction sur un fichier de 30 Go (délai de 15 minutes), la fluidité du décodage dans le navigateur de la télé (Android TV, Fire TV). Les tests n'ont qu'un `.sup` fabriqué à la main.

- [ ] **Les notes TMDB** avec la vraie clé : un premier « Sagas (TMDB) » sur toute la bibliothèque de films (une requête par film étiqueté, sans le cache : durée réelle), puis un rescan, après lequel les notes doivent rester. Sur la télé et sur un téléphone étroit : le disque de la note (anneau SVG) en bas à gauche de la vignette ne gêne ni le bouton « … » ni la barre de progression, et reste lisible de loin.

Si `test_admin_runs_an_update` échoue (il a échoué une fois sur une machine, jamais sur l'autre) : le relancer seul avec `python -W ignore -m unittest tests.test_ui.TestDetailPages.test_admin_runs_an_update` et garder les lignes de l'échec. Ce test attend au plus 30 s qu'un scan se termine.

## Limites connues, non traitées

- [ ] Une saga de `sagas.json` ne garde que la **première version** d'un film listé en double (`by_tmdb`, le premier gagne). Les sagas de l'étiquette, elles, numérotent les deux versions.
- [ ] Le conteneur `media_hub` **tourne en root** (avec `cap_drop: ALL` et deux capacités gardées, voir `deploy/README.md`).
- [ ] Le **jeton de session ne se périme pas**, et *Se déconnecter* n'efface que le cookie de l'appareil : seul *Déconnecter les autres appareils* révoque.
- [ ] Une **fiche TMDB fixée à la main** ne se retire pas, on en fait une autre. Le marqueur `{tmdb-…}` n'est pas écrit dans le nom du dossier : pour que `media_library` voie le même choix, il se tape à la main.
- [ ] Les sous-titres **VobSub (DVD)** ne sont pas affichés : seuls les PGS (Blu-ray) le sont. ffmpeg ne sait pas les extraire tels quels (pas de muxer `.idx`/`.sub`) : il faudrait lire les paquets du `.mkv` soi-même, avec la palette de leur `CodecPrivate`. Contournement : un fichier `.srt` à côté de la vidéo.
- [ ] Les PGS ne se **choisissent jamais d'office** à l'ouverture d'un film (l'extraction peut durer) : il faut les choisir une fois dans le menu « Sous-titres » ; la langue choisie est ensuite retenue, et un PGS de cette langue est repris s'il n'y a pas de piste texte.

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
