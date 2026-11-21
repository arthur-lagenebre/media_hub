# Les pop-up de l'interface

Toute fenêtre qui s'ouvre par-dessus la page est un **pop-up**. Il n'en existe que **quatre déclinaisons**, qui partagent un même gabarit (`.menu`, dans `hub/web/style.css`) et une même fabrique (`H.box`, dans `hub/web/js/core.js`). Un nouveau pop-up part de l'une d'elles : il ne construit jamais son `div role="dialog"` lui-même, ne crée pas de classe de taille ou de bouton à lui. `tests/test_popups.py` le vérifie.

| Déclinaison | Pour quoi | Fabrique | Exemples |
|---|---|---|---|
| **choice** | Choisir dans une liste ; un clic ferme. | `H.menu(titre, options, courant, onPick)` | Trier par, Genre, Langue, Audio / Sous-titres, Chapitres, **Vitesse** (lecteur vidéo et barre de musique : 0,75× à 2×, la vitesse en cours cochée, 1× marquée « normale »), menu `…` d'une carte ou d'une piste (sur une carte de « Parce que tu as aimé » : aussi « Pourquoi cette suggestion ? » et « Pas intéressé »), **Mon avis** (J'aime / Bof / Je n'aime pas / Effacer, avec l'avis actuel marqué), Ajouter à une playlist ou à une saga, « Que faire de… », **Sommaire** d'un EPUB (option `level` : les niveaux en retrait, l'entrée en cours cochée) |
| **confirm** | Une question, une action (rouge si elle détruit ou ne s'annule pas), Annuler. | `H.confirm({title, lead, ok, danger, done, cancel})` | Supprimer une saga, une playlist, un compte, les fichiers parasites ; retirer un lien ; envoyer un dossier (`cls: 'sendfolder'`, avec la liste dépliable des fichiers écartés) ; une information à lire, bouton « Compris » : **Pourquoi cette suggestion ?** (carte d'un rayon « Parce que tu as aimé », l'ancre puis chaque particularité partagée sur sa ligne `p.lead`) |
| **form** | Un ou plusieurs champs, ou un petit réglage, avec Valider / Annuler. | `H.form({title, lead, fields, extra, ok, submit, done, cancel})` ; `H.prompt(...)` pour un seul champ de texte | Ajouter un compte, mot de passe, droits d'un compte, nom d'une playlist, ajouter un lien, **renommer une bibliothèque** (champ prérempli et sélectionné, action seconde « Rétablir »), **modifier un album** (`A.albumTools`, `actions.js` : titre, année, genres et label prérempli, le type de sortie en puces dans `extra`, un « Rétablir » à côté de chaque champ déjà corrigé) ; **modifier un artiste** (`editArtist`, `music.js` : nom affiché, genres, résumé en `textarea`, pays, années, un « Rétablir » par champ corrigé) ; **proposer une amélioration** et **répondre à une suggestion** (un champ `type: 'textarea'` pour un texte long, qui garde ses retours à la ligne écrits avec Maj+Entrée ; Entrée seule passe au champ suivant ou valide, comme dans un champ d'une ligne ; la catégorie ou l'état en puces dans `extra`) ; **signaler un problème** (`A.report`, `actions.js` : les causes en puces dans `extra`, un message facultatif ; sur télé, les puces seules, sans champ, avec `kind: 'form'`) |
| **wide** | Contenu libre qui demande de la place : liste à gérer, recherche, image. Un pied de boutons. | `H.box('wide <nom>', titre, …enfants)` puis `H.overlay.open(box)` | File d'attente (`wide queue`), choix de films d'une saga (`wide picker`), choix de la fiche MusicBrainz d'un artiste (`wide picker` aussi : recherche, résultats, « Revenir au rapprochement automatique »), recadrage d'une photo (`wide crop`), centre de notifications (`wide notifs`, ouvert par la cloche de la barre du haut) |

Ce qui n'est **pas** un pop-up et reste à part : le **toast** (`H.toast`, message bref sans action, qui ne prend pas le focus), le **panneau d'erreur du lecteur** (`.err`, dans le lecteur plein écran) et les **barres** (musique, lecteur, liseuse).

## Une seconde action dans un form

`H.form({..., alt: {label, submit}})` ajoute un bouton entre l'action et Annuler (« Rétablir « Séries » » dans *Renommer la bibliothèque*, `Administration > Médiathèque > Bibliothèques`). Il passe par le même chemin que `submit` : un seul envoi à la fois, l'erreur du serveur s'affiche dans la fenêtre qui reste ouverte, `done` reçoit la réponse. Le pied passe en colonne (`stacked`). `fields[0].value` préremplit le champ, qui s'ouvre sélectionné. Le nom écrit dans le titre ou un bouton vient de l'utilisateur : `H.t('…{name}…', {name})` le met en texte, jamais en HTML.

## Un « Rétablir » par champ dans un form

Un champ qui peut revenir à sa valeur d'origine (*Modifier l'album*, `Musique`) reçoit `restore: function (valeurs)` : H.form pose à côté de son libellé un petit bouton « Rétablir » (`data-key="restore-<clé>"`). Il se pose sous le champ, à droite, dans une ligne à lui (sa place est réservée : il ne recouvre pas le libellé ; sur téléphone et tablette, une cible d'au moins 2,5 rem). Il passe par le même chemin que `submit` et `alt` (un seul envoi à la fois, l'erreur reste dans la fenêtre, `done` reçoit la réponse). `restore` reçoit les valeurs saisies, normalisées comme pour `submit` (`H.tidy`) : c'est à l'appelant d'y joindre ce qui a changé ailleurs, pour qu'un « Rétablir » ne jette pas une saisie (*Modifier l'album* envoie `{champ: null}` avec les autres champs modifiés). Un réglage qui n'est pas un champ de texte (des puces de `extra`) a le sien : un élément de `extra` peut être une fonction, qui reçoit `restoreButton(clé, action)` et rend le nœud à poser. Le bouton n'existe que pour ce qui est déjà corrigé : rien à rétablir, pas de bouton.

## Choisir la déclinaison

1. Une liste dont on prend un élément, puis c'est fini → **choice**.
2. Une question oui / non, avec ou sans phrase d'explication → **confirm**. Même quand le serveur doit répondre : `submit` rend une promesse, la fenêtre reste ouverte et affiche l'erreur s'il refuse.
3. Il faut saisir ou régler quelque chose → **form**.
4. Rien de cela : du contenu propre à la fenêtre → **wide**, avec un modificateur au nom de la fenêtre (`wide queue`). Si trois fenêtres `wide` se ressemblent, c'est qu'il manque une déclinaison : en parler avant d'en ajouter une.

Une déclinaison de plus se justifie par un besoin d'interaction que les quatre ne couvrent pas, pas par une envie d'apparence.

## Ce que le gabarit garantit (ne pas le refaire)

- **Voile, Retour, Échap** : `H.overlay` empile les fenêtres ; Retour (télécommande) ou Échap ferme celle du dessus, un clic sur le voile aussi. Le focus revient sur ce qui avait ouvert la fenêtre.
- **Plein écran** : le voile (et le toast) se pose sur l'élément en plein écran s'il y en a un (le lecteur), sinon dans `body`, et le suit quand on entre en plein écran ou qu'on en sort ; posé dans `body` pendant le plein écran, il serait invisible derrière le film.
- **Fermée sans avoir agi** : l'option `cancel` de `H.form` et de `H.confirm` est appelée une fois la fenêtre fermée sans avoir agi (Annuler, voile, Retour, Échap) ; `done` l'est quand la personne a agi.
- **Quitter une page** : une page de `H.route` peut définir `leave()`, qui rend `{title, lead, ok, accept}` pour demander confirmation avant de la quitter (un envoi en cours), ou rien si elle peut partir ; la question est un **confirm** dont `cancel` remet l'adresse de la page.
- **Focus** : `data-autofocus` désigne l'élément qui reçoit le focus (sinon le premier). Une fenêtre qui détruit quelque chose met le focus sur **Annuler** (Entrée ne détruit rien) ; un menu met le focus sur la valeur courante.
- **Atteignable à la télécommande** : tout contrôle porte la classe `f`. `H.button` et `.opt` la posent. Une fenêtre qui défile suit le focus (`H.box`).
- **Pied de boutons** : `<div class="btns">` en rangée de boutons égaux, l'action en premier ; en colonne (`stacked`) dès qu'il y a plusieurs champs. Bouton d'action : `primary`, ou `danger` si elle détruit.
- **Erreurs** : un `<p class="formerr" role="alert">`, jamais un toast : l'erreur reste devant la personne qui a une action à corriger.
- **Titre** : `h3`, aussi donné en `aria-label` à la fenêtre. Texte d'explication : `p.lead`. Tout en français, par `H.t`.
- **Cases sans action** et **données non fiables** : les règles de `.claude/agents/hub-ui.md` valent aussi ici (`textContent`, jamais `innerHTML` brut).

## Ajouter un pop-up

1. Choisir la déclinaison (ci-dessus) et prendre sa fabrique.
2. Un `wide` : `H.box('wide mon-nom', titre, contenu, h('div', { class: 'btns' }, …))`, puis `H.overlay.open(box, onClose)`. Les règles propres à `mon-nom` vont dans `style.css`, sous `.menu.mon-nom`, dans le bloc « Fenêtres » ; rien qui redéfinisse un gabarit commun.
3. Un test qui l'ouvre dans `tests/test_ui.py`, et, s'il est d'une nouvelle sorte de fenêtre, une ligne dans `TestPopupsInTheBrowser` (`tests/test_popups.py`).
4. Essayer à la télécommande (flèches, Entrée, Retour) et sur un téléphone étroit.
