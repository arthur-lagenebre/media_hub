/* Le bouton « … » : voir / revoir, vu / non vu et mon avis, sur les cartes des films, des sagas et des séries, et sur chaque épisode.
   Le même menu s'ouvre au clic sur le bouton, au clic droit et à la touche « Menu » de la télécommande. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;
  var A = H.actions = {};

  function fail(e) { H.toast(e && e.message ? e.message : H.t('Impossible pour le moment.'), 5000); }
  function refresh() { var top = H.current(); if (top && top.view.refresh) top.view.refresh(); }
  function finished(it) { return it.seen > 0 && it.seen >= Math.max(1, it.n); }

  // Le texte de « vu / non vu » : une série se marque saison par saison, c'est donc toutes les saisons à la fois qu'on marque ici.
  A.seenLabel = function (type, done) {
    if (type === 'show') return done ? H.t('Marquer toutes les saisons non vu') : H.t('Marquer toutes les saisons comme vu');
    return done ? H.t('Marquer non vu') : H.t('Marquer comme vu');
  };

  // Les films d'une saga, comme la page de la saga les liste.
  function sagaFilms(it) {
    return H.api('/api/library/' + it.lib + '?group=' + encodeURIComponent(it.title) + '&sort=part&limit=600').then(function (d) { return d.items; });
  }

  // Lance la lecture d'un film ou d'une série : là où on s'est arrêté, ou depuis le début pour la revoir.
  function play(item, again) {
    return H.api('/api/item/' + item.id).then(function (d) {
      var entry = again ? d.entries[0] : d.entries.filter(function (e) { return e.id === d.resume; })[0] || d.entries[0];
      if (!entry) throw new Error('Rien à lire.');
      H.go(H.playHref(entry, again ? 0 : undefined));
    });
  }
  function playSaga(it, again) {
    return sagaFilms(it).then(function (films) {
      var next = again ? null : films.filter(function (f) { return !finished(f); })[0];
      return play(next || films[0], !next);
    });
  }
  function mark(it, done) {
    if (it.type !== 'saga') return H.api('/api/item/' + it.id + '/seen', { done: done });
    return sagaFilms(it).then(function (films) {
      return Promise.all(films.map(function (f) { return H.api('/api/item/' + f.id + '/seen', { done: done }); }));
    });
  }

  // --- Mon avis (films et séries entières seulement : ni saga, ni épisode, ni musique, ni livre) ------------------------
  A.ratable = function (it) { return it.type === 'movie' || it.type === 'show'; };
  // Pose l'avis (« up », « mid », « down ») ou l'efface (null) ; les cartes à l'écran repeignent leur pastille une fois le serveur d'accord. Rend une promesse : l'appelant montre l'échec.
  A.rate = function (it, value) {
    return H.api('/api/item/' + it.id + '/rating', { rating: value }).then(function (r) {
      var now = r && r.rating ? r.rating : null;
      H.rated(it.id, now);
      return now;
    });
  };
  // Le choix de l'avis (une fenêtre « choice ») : J'aime, Bof, Je n'aime pas, et Effacer quand il y en a un ; `current` est l'avis actuel (null sans avis).
  A.ratingMenu = function (it, current) {
    var options = H.ratings.map(function (r) { return { label: r.label, value: r.value }; }).concat([{ label: H.t('Effacer'), value: 'clear', disabled: !current }]);
    H.menu(H.t('Mon avis'), options, current, function (v) { A.rate(it, v === 'clear' ? null : v).catch(fail); });
  };

  // --- Suggestions (« Parce que tu as aimé ») : une carte qui porte `why` (GET /api/recommendations) --------------------
  // Une particularité partagée avec l'ancre, en toutes lettres ; null pour une sorte inconnue. La saga se traduit comme sur la carte (H.tr).
  function reason(w) {
    if (!w || typeof w.value !== 'string') return null;
    if (w.kind === 'director') return H.t('Même réalisateur : {value}', { value: w.value });
    if (w.kind === 'actor') return H.t('Même acteur : {value}', { value: w.value });
    if (w.kind === 'genre') return H.t('Même genre : {value}', { value: w.value });
    if (w.kind === 'saga') return H.t('Même saga : {value}', { value: H.tr(w.value) });
    if (w.kind === 'decade') return H.t('Même époque : années {value}', { value: w.value });
    return null;
  }
  // « Pourquoi cette suggestion ? » : une fenêtre d'information (déclinaison confirm, comme « Rien à envoyer »), l'ancre puis ce que l'élément partage avec elle.
  A.why = function (it) {
    var lead = (it.because ? [H.t('Parce que tu as aimé {title}', { title: it.because.title })] : []).concat((it.why || []).map(reason).filter(Boolean));
    H.confirm({ title: H.t('Pourquoi cette suggestion ?'), lead: lead, ok: H.t('Compris') });
  };

  // `d` : la fiche de l'élément (/api/item), qui sait si TMDB le connaît ; null pour une saga, ou quand la fiche ne vient pas.
  function showMenu(it, d) {
    var done = finished(it), tmdb = d && d.links ? d.links.filter(function (l) { return l.label === 'TMDB'; })[0] : null;
    var options = (A.canPlay(d && d.library && d.library.id) ? [{ label: done ? H.t('Revoir') : (it.seen || it.started ? H.t('Reprendre') : H.t('Voir')), value: 'play' }] : []).concat([{ label: A.seenLabel(it.type, done), value: 'seen' }]);
    var mine = d ? d.rating || null : it.rating || null, rated = H.ratingOf(mine);      // la fiche, quand elle est venue, est plus fraîche que la carte
    if (A.ratable(it)) options.push({ label: H.t('Mon avis'), detail: rated ? rated.label : '', value: 'rate' });
    if (it.why && A.ratable(it)) options.push({ label: H.t('Pourquoi cette suggestion ?'), value: 'why' }, { label: H.t('Pas intéressé'), detail: H.t('Noté « Je n’aime pas » : il n’est plus proposé'), value: 'down' });      // une carte de « Parce que tu as aimé » ; « Pas intéressé » la retire du rayon (H.rated)
    if (it.type === 'movie' && d && d.tmdb && H.isAdmin() && H.sagas) options.push({ label: H.t('Ajouter à une saga'), value: 'saga' });      // sans identifiant TMDB, un film ne peut pas entrer dans une saga
    if (tmdb) options.push({ label: 'TMDB', detail: H.t('Ouvre la fiche dans un nouvel onglet'), value: 'tmdb' });
    if (d && d.entries && d.entries.length && A.canDownload(d.library && d.library.id)) options.push({ label: H.t('Télécharger'), detail: d.entries.length > 1 ? H.t('{n} fichiers, l’un après l’autre', { n: d.entries.length }) : '', value: 'download' });
    if (it.type !== 'saga') options.push({ label: H.t('Signaler un problème'), value: 'report' });
    H.menu(it.title, options, null, function (v) {
      if (v === 'play') (it.type === 'saga' ? playSaga : play)(it, done).catch(fail);
      else if (v === 'rate') A.ratingMenu(it, mine);
      else if (v === 'why') A.why(it);
      else if (v === 'down') A.rate(it, 'down').catch(fail);
      else if (v === 'saga') H.sagas.addTo(d, refresh);
      else if (v === 'tmdb') window.open(tmdb.url, '_blank', 'noopener');
      else if (v === 'download') A.download(A.files(d));
      else if (v === 'report') A.report({ item: it.id, title: it.title });
      else mark(it, !done).then(refresh).catch(fail);
    });
  }
  var asking = false;      // la touche « Menu » envoie la touche ET le clic droit : un seul menu s'ouvre
  A.menu = function (it) {
    if (asking || H.overlay.isOpen()) return;
    if (it.type === 'saga') { showMenu(it, null); return; }
    asking = true;
    H.api('/api/item/' + it.id).then(function (d) { return d; }, function () { return null; }).then(function (d) { asking = false; showMenu(it, d); });
  };

  // Le bouton posé sur la carte (souris et doigt), et le menu au clic droit ou à la touche « Menu » (télécommande).
  A.tools = function (it) {
    return h('div', { class: 'qa' }, h('button', { class: 'qbtn', tabindex: '-1', 'aria-label': H.t('Plus d’actions'), title: H.t('Plus d’actions'),
      onclick: function (e) { e.preventDefault(); e.stopPropagation(); A.menu(it); } }, H.icon('more')));
  };
  A.bind = function (card, it) {
    card.addEventListener('contextmenu', function (e) { e.preventDefault(); A.menu(it); });
    card.addEventListener('keydown', function (e) { if (e.key === 'ContextMenu' || e.keyCode === 93) { e.preventDefault(); A.menu(it); } });
  };

  // --- Signaler un problème ---------------------------------------------------------------------
  // Un signalement est une suggestion de catégorie « bug » rattachée à un titre (POST /api/suggestions avec `item`, et `entry` pour un épisode, une piste ou le fichier qui ne se lit pas) : la cause est obligatoire, le message facultatif.
  // `target` : {item, entry?, title} ; `title` ne sert qu'à rappeler de quoi on parle. Sur télévision, la cause seule : pas de champ de texte (H.canSuggest), mais le signalement part.
  A.report = function (target) {
    var cause = H.suggestions.choice(H.suggestions.causes, null, H.t('Cause'), 'rp-cause-');
    cause.el.querySelector('.chip').setAttribute('data-autofocus', '1');          // sans champ de texte, le focus va à la première cause, pas à « Annuler »
    H.form({ kind: 'form', title: H.t('Signaler un problème'), ok: H.t('Envoyer le signalement'),
      lead: target.title ? [target.title] : [],
      fields: H.canSuggest() ? [{ key: 'text', label: H.t('Un mot pour expliquer (facultatif, 1000 caractères au plus)'), type: 'textarea', maxlength: 1000 }] : [],
      extra: [cause.el],
      submit: function (v) {
        if (!cause.current()) return H.t('Choisis la cause.');
        var body = { item: target.item, cause: cause.current(), text: v.text || '' };
        if (target.entry) body.entry = target.entry;
        return H.api('/api/suggestions', body);
      },
      done: function () { H.toast(H.t('Signalement envoyé')); } });
  };

  // --- Lire ---------------------------------------------------------------------------------
  // Le droit de lire en ligne (`play` de /api/state) : sans lui, la fiche reste visible mais ni bouton ni ligne ne lance la lecture (le serveur refuse de toute façon, 403).
  // Sans bibliothèque connue : permis si l'une au moins le permet.
  A.canPlay = function (libId) {
    var lib = libId ? H.lib(libId) : null;
    return lib ? lib.play !== false : H.libs.some(function (l) { return l.play !== false; });
  };
  A.canPlayKind = function (kind) {
    var libs = H.libs.filter(function (l) { return l.kind === kind; });
    return !libs.length || libs.some(function (l) { return l.play !== false; });
  };
  A.denyPlay = function () { H.toast(H.t('Ta lecture est fermée dans cette bibliothèque : demande à l’administrateur.'), 4000); };

  // --- Télécharger ----------------------------------------------------------------------
  // Toujours un fichier à la fois, jamais de zip : une coupure ne fait perdre qu'un fichier, et la reprise passe par `Range`.
  // Plusieurs fichiers (une saison, une série, un album) forment une file : le suivant part quand le serveur a fini d'envoyer le précédent (voir downloads.js).
  // Pas de téléchargement sur la télé (ses navigateurs téléchargent mal) ni dans une bibliothèque dont le compte n'a pas le droit (`download` de /api/state ; le serveur refuse de toute façon, 403).
  // Sans bibliothèque connue : permis si l'une au moins le permet.
  A.canDownload = function (libId) {
    if (H.device === 'tv') return false;
    var lib = libId ? H.lib(libId) : null;
    return lib ? lib.download !== false : H.libs.some(function (l) { return l.download !== false; });
  };
  A.canDownloadKind = function (kind) {          // musique, livres : la bibliothèque n'est pas toujours connue de l'endroit d'où l'on télécharge
    if (H.device === 'tv') return false;
    var libs = H.libs.filter(function (l) { return l.kind === kind; });
    return !libs.length || libs.some(function (l) { return l.download !== false; });
  };
  // `files` : [{id, title}], le titre affiché par la file.
  A.download = function (files) {
    H.downloads.add(files);
    H.toast(files.length > 1 ? H.t('Téléchargement de {n} fichiers, l’un après l’autre', { n: files.length }) : H.t('Téléchargement lancé'), 4000);
  };
  // Les fichiers d'une fiche (/api/item), ou ceux de `entries` : chacun avec son titre, celui de la fiche à défaut (un film n'a qu'un fichier).
  A.files = function (d, entries) {
    return (entries || d.entries).map(function (e) { return { id: e.id, title: e.title || d.title || '' }; });
  };
  // Le bouton de la fiche : une série à plusieurs saisons demande laquelle.
  A.downloadItem = function (d) {
    function files(entries) { return A.files(d, entries); }
    var seasons = d.type === 'show' && d.seasons ? d.seasons.filter(function (s) { return d.entries.some(function (e) { return e.season === s; }); }) : [];
    if (seasons.length < 2) { A.download(files(d.entries)); return; }
    var options = [{ label: H.t('Toute la série'), detail: H.plural(d.entries.length, 'épisode'), value: 'all' }].concat(seasons.map(function (s) {
      var of = d.entries.filter(function (e) { return e.season === s; });
      return { label: s === 0 ? H.t('Spéciaux') : H.t('Saison {n}', { n: s }), detail: H.plural(of.length, 'épisode'), value: String(s) };
    }));
    H.menu(H.t('Télécharger'), options, null, function (v) {
      A.download(files(v === 'all' ? d.entries : d.entries.filter(function (e) { return String(e.season) === v; })));
    });
  };
  // --- Ouvrir dans VLC (PC seulement) ----------------------------------------------------------
  // Le navigateur fabrique un .m3u8 (`POST /api/play/<id>/vlc` : l'adresse relative du fichier, avec un jeton qui ne vaut que pour lui, le titre, la reprise) et le télécharge : un double-clic l'ouvre dans VLC,
  // qui lit le fichier d'origine sans que le serveur le convertisse. L'adresse absolue est celle du navigateur (derrière un proxy ou Tailscale, le serveur ne la devine pas). La progression ne remonte pas au hub.
  // Sur téléphone, tablette et télé : rien (pas de VLC à ouvrir de là). Une vidéo seulement : un tome de livre ne passe pas par là.
  A.canVlc = function (libId) { return H.device === 'pc' && A.canPlay(libId); };
  var CONTROLS = /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, opening = false;
  // Un titre sur une seule ligne, sans caractère de contrôle ni retour à la ligne (le titre vient des fichiers, le serveur ne le nettoie pas), borné à `max` caractères.
  function oneLine(text, max) { return String(text == null ? '' : text).replace(CONTROLS, ' ').replace(/\s+/g, ' ').trim().slice(0, max).trim(); }
  // Le nom du fichier : le titre sans caractère interdit sous Windows (ni point ou espace final, ni nom réservé), puis `.m3u8`.
  function playlistName(title) {
    var base = oneLine(title, 100).replace(/[<>:"\/\\|?*]/g, ' ').replace(/\s+/g, ' ').replace(/[. ]+$/, '').trim();
    if (!base) base = 'media_hub';
    if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)/i.test(base)) base = '_' + base;
    return base + '.m3u8';
  }
  A.openInVlc = function (entryId) {
    if (opening) return;
    opening = true;
    H.api('/api/play/' + entryId + '/vlc', {}).then(function (r) {
      var title = oneLine(r.title, 200), lines = ['#EXTM3U', '#EXTINF:-1,' + title];
      if (typeof r.resume === 'number' && r.resume > 0) lines.push('#EXTVLCOPT:start-time=' + r.resume);
      lines.push(location.origin + String(r.url).replace(/[\u0000-\u0020\u007f-\u009f\u2028\u2029]/g, ''));
      H.saveBlob(new Blob([lines.join('\n') + '\n'], { type: 'audio/x-mpegurl;charset=utf-8' }), playlistName(title));
      if (!H.pref('vlcHint')) {          // la première fois seulement : ce qu'il faut faire du fichier
        H.pref('vlcHint', true);
        H.toast(H.t('Ouvre le fichier téléchargé : VLC lit la vidéo directement.'), 6000);
      }
    }).catch(fail).then(function () { opening = false; });
  };

  // --- Changer l'affiche d'un film ou d'une série, la pochette d'un album (administrateur) ------------------
  // L'image envoyée reste dans HUB_DATA : la médiathèque n'est pas touchée. Elle devient l'affiche, sans retour à l'ancienne, sauf pour un album, dont « Rétablir la pochette » rend celle des fichiers (`edited` de la fiche en dit
  // une choisie). Le sélecteur de fichiers d'une télé se pilote mal : rien n'y est proposé.
  // `d` : la fiche (/api/item) ; `done` est appelé une fois l'image changée. Rend les éléments à poser parmi les boutons de la fiche.
  A.posterTools = function (d, done) {
    var album = d.type === 'album';
    if ((d.type !== 'movie' && d.type !== 'show' && !album) || !H.isAdmin() || H.device === 'tv') return [];
    var picker = h('input', { type: 'file', accept: 'image/jpeg,image/png,image/webp', hidden: 'hidden', 'aria-hidden': 'true', onchange: function () {
      var file = this.files && this.files[0];
      this.value = '';
      if (file) H.api('/api/item/' + d.id + '/poster', file, { raw: true }).then(function () { H.toast(album ? H.t('Pochette enregistrée') : H.t('Affiche enregistrée')); done(); }).catch(fail);
    } });
    var change = H.button(album ? H.t('Changer la pochette') : H.t('Changer l’affiche'), 'edit', { 'data-key': 'poster', onclick: function () { picker.click(); } });
    var tools = [picker, change];
    if (album && (d.edited || []).indexOf('poster') >= 0) tools.push(H.button(H.t('Rétablir la pochette'), 'refresh', { 'data-key': 'poster-reset', onclick: function () {
      H.confirm({ title: H.t('Rétablir la pochette ?'), lead: [H.t('La pochette choisie est supprimée : celle des fichiers revient.')], ok: H.t('Supprimer la pochette choisie'), danger: true,
        submit: function () { return H.api('/api/item/' + d.id + '/edit', { poster: null }); },
        done: function () { H.toast(H.t('Pochette d’origine rétablie')); try { change.focus(); } catch (e) { /* tant pis */ } done(); } });      // son bouton disparaît : le focus va à « Changer la pochette »
    } }));
    return tools;
  };
  // --- Corriger un album (administrateur) ----------------------------------------------------
  // Titre, année, genres, label et type de sortie, gardés dans la base du hub : la médiathèque n'est pas touchée, et un scan ne les efface pas (docs/decisions.md, « Corriger dans le hub »). Pas sur télé (saisir à la télécommande se fait mal).
  // Seuls les champs changés partent ; chaque champ déjà corrigé (`edited` de la fiche) porte son « Rétablir », qui rend la valeur lue dans les étiquettes.
  var ALBUM_FIELDS = [['title', H.t('Titre'), 300], ['year', H.t('Année'), 4], ['genres', H.t('Genres (séparés par des virgules)'), 1500], ['label', H.t('Label'), 200]];
  function genreList(text) { return text.split(',').map(function (g) { return g.trim(); }).filter(function (g, i, all) { return g && all.indexOf(g) === i; }); }
  A.genreList = genreList;          // aussi pour « Modifier l'artiste » (music.js)
  // Ce que la fiche montre aujourd'hui, champ par champ, dans la forme où la fenêtre le rend (H.form normalise les espaces : un champ qu'on n'a pas touché doit se retrouver égal à son départ, sinon il part comme corrigé).
  function albumStart(d) {
    return { title: H.tidy(d.title || ''), year: d.year ? String(d.year) : '', genres: (d.genres || []).map(H.tidy).join(', '), label: H.tidy(d.label || ''), type: (d.release_types || [])[0] || null };
  }
  // Ce qui a changé entre ce que la fenêtre a reçu (`start`) et ce qui y est saisi (`values`, plus `type`, le type choisi).
  function albumChanges(start, values, type) {
    var changed = {};
    ['title', 'year', 'label'].forEach(function (k) { if (values[k] !== start[k]) changed[k] = values[k]; });
    var genres = genreList(values.genres);
    if (genres.join(', ') !== start.genres) changed.genres = genres;
    if (type !== start.type) changed.type = type;
    return changed;
  }
  // `types` : {valeur du serveur : libellé}, dans l'ordre où les puces se montrent.
  function editAlbum(d, done, types) {
    var edited = d.edited || [], start = albumStart(d);
    var type = H.suggestions.choice(Object.keys(types).map(function (k) { return [k, types[k]]; }), start.type, H.t('Type de sortie'), 'ae-type-');
    function send(body, message) { return H.api('/api/item/' + d.id + '/edit', body).then(function () { return message; }); }
    // « Rétablir » rend la valeur d'origine du champ, et envoie dans le même coup ce qui a été saisi ailleurs : on ne jette pas une saisie en rétablissant un autre champ.
    function restore(field) { return function (values) { var body = albumChanges(start, values, type.current()); body[field] = null; return send(body, H.t('Valeur d’origine rétablie')); }; }
    H.form({ title: H.t('Modifier l’album'), ok: H.t('Enregistrer'),
      lead: [H.t('La correction ne vaut que dans le hub. Pour corriger le fichier, passe par l’atelier.')],
      fields: ALBUM_FIELDS.map(function (f) { return { key: f[0], label: f[1], maxlength: f[2], value: start[f[0]], restore: edited.indexOf(f[0]) >= 0 ? restore(f[0]) : null }; }),
      extra: [function (restoreButton) { return edited.indexOf('type') < 0 ? type.el : h('div', { class: 'fld' }, type.el, restoreButton('type', restore('type'))); }],
      submit: function (values) {
        var changed = albumChanges(start, values, type.current());
        return Object.keys(changed).length ? send(changed, H.t('Album corrigé')) : Promise.resolve(null);
      },
      done: function (message) { if (message) { H.toast(message); done(); } } });
  }
  A.albumTools = function (d, done, types) {
    if (d.type !== 'album' || !H.isAdmin() || H.device === 'tv') return [];
    return [H.button(H.t('Modifier l’album'), 'edit', { 'data-key': 'album-edit', onclick: function () { editAlbum(d, done, types); } })];
  };
  // --- Changer la fiche TMDB (administrateur, séries) ----------------------------
  // Quand la recherche sur le nom du dossier tombe sur un homonyme. Le choix reste dans la base du hub : la médiathèque n'est pas touchée.
  // On cherche par le nom, ou on colle l'adresse de la fiche sur themoviedb.org (la façon sûre de tomber sur la bonne). `d` : la fiche (/api/item) ; `done` est appelé une fois la fiche changée.
  var TMDB_URL = /^https?:\/\/(www\.)?themoviedb\.org\/tv\/\d+/i;
  A.tmdbTools = function (d, done) {
    if (d.type !== 'show' || !H.isAdmin() || H.device === 'tv') return [];
    function choose(value) {
      H.api('/api/item/' + d.id + '/tmdb', { tmdb: value }).then(function () { H.toast(H.t('Fiche TMDB enregistrée')); done(); }).catch(fail);
    }
    function search(text) {
      H.api('/api/item/' + d.id + '/tmdb?q=' + encodeURIComponent(text)).then(function (r) {
        H.menu(H.t('Quelle série ?'), r.results.map(function (s) {
          var overview = s.overview.length > 140 ? s.overview.slice(0, 140) + '…' : s.overview;
          return { label: s.name + (s.year ? ' (' + s.year + ')' : ''), detail: [overview, 'TMDB ' + s.tmdb].filter(Boolean).join(' · '), value: s.tmdb };
        }), d.tmdb || null, choose);
      }).catch(fail);
    }
    var change = H.button(H.t('Changer la fiche TMDB'), 'edit', { 'data-key': 'tmdb', onclick: function () {
      H.prompt(H.t('Nom de la série, ou adresse de sa fiche TMDB'), d.title, H.t('Chercher'), function (text) { if (TMDB_URL.test(text)) choose(text); else search(text); });
    } });
    if (!d.pinned) return [change];
    // Une fiche choisie dans le hub (`pinned`) se retire : la série est enrichie de nouveau d'après le nom de son dossier. Si TMDB ne trouve rien sans ce choix, le serveur le garde et le dit (404) : le message reste dans la fenêtre.
    // Le bouton disparaît une fois la fiche rétablie : le focus passe d'abord sur « Changer la fiche TMDB », que la fiche redessinée retrouve.
    return [change, H.button(H.t('Revenir à la fiche automatique'), 'refresh', { 'data-key': 'tmdb-auto', onclick: function () {
      H.confirm({ title: H.t('Revenir à la fiche automatique ?'), ok: H.t('Revenir'),
        lead: [H.t('La fiche choisie à la main est oubliée : le hub cherche de nouveau la série d’après le nom de son dossier.')],
        submit: function () { return H.api('/api/item/' + d.id + '/tmdb', { tmdb: null }); },
        done: function () { H.toast(H.t('Fiche automatique rétablie')); try { change.focus(); } catch (e) { /* tant pis */ } done(); } });
    } })];
  };
  // Le bouton « … » d'une couverture de tome : le même menu que celui d'un épisode.
  A.entryTools = function (e, done, libId, item) {
    return h('div', { class: 'qa' }, h('button', { class: 'qbtn', tabindex: '-1', 'aria-label': H.t('Plus d’actions'), title: H.t('Plus d’actions'),
      onclick: function (ev) { ev.preventDefault(); ev.stopPropagation(); A.episodeMenu(e, done, libId, item); } }, H.icon('more')));
  };

  // Le menu d'un épisode : `done` est appelé une fois l'épisode marqué ; `libId` : sa bibliothèque (pour le droit de télécharger) ; `item` : l'élément dont il fait partie (pour le signaler).
  A.episodeMenu = function (e, done, libId, item) {
    var seen = !!(e.progress && e.progress.done);
    var options = (A.canPlay(libId) ? [{ label: seen ? H.t('Revoir') : H.t('Lire'), value: 'play' }] : []).concat([{ label: A.seenLabel('episode', seen), value: 'seen' }]);
    if (e.kind !== 'book' && A.canVlc(libId)) options.splice(A.canPlay(libId) ? 1 : 0, 0, { label: H.t('Ouvrir dans VLC'), value: 'vlc' });      // juste après « Lire » ; pas pour un tome de livre
    if (A.canDownload(libId)) options.push({ label: H.t('Télécharger'), value: 'download' });
    if (item) options.push({ label: H.t('Signaler un problème'), value: 'report' });
    var hidden = e.kind === 'video' && H.spoiler(e, 'title'), title = hidden ? H.epLabel(e) : e.title || '';      // un épisode dont le titre est masqué (voir H.spoiler) est nommé par son numéro, comme sur sa ligne floutée
    H.menu(title || H.t('Épisode'), options, null, function (v) {
      if (v === 'play') H.go(H.playHref(e, seen ? 0 : undefined));
      else if (v === 'vlc') A.openInVlc(e.id);
      else if (v === 'download') A.download([{ id: e.id, title: e.title || '' }]);
      else if (v === 'report') A.report({ item: item, entry: e.id, title: title });
      else H.api('/api/entry/' + e.id + '/seen', { done: !seen }).then(done).catch(fail);
    });
  };
})();
