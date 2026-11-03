/* Le bouton « … » : voir / revoir et vu / non vu, sur les cartes des films, des sagas, des séries et des animations, et sur chaque épisode.
   Le même menu s'ouvre au clic sur le bouton, au clic droit et à la touche « Menu » de la télécommande. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;
  var A = H.actions = {};

  function fail(e) { H.toast(e && e.message ? e.message : 'Impossible pour le moment.', 5000); }
  function refresh() { var top = H.current(); if (top && top.view.refresh) top.view.refresh(); }
  function finished(it) { return it.seen > 0 && it.seen >= Math.max(1, it.n); }

  // Le texte de « vu / non vu » : une série se marque saison par saison, c'est donc toutes les saisons à la fois qu'on marque ici.
  A.seenLabel = function (type, done) {
    if (type === 'show') return done ? 'Marquer toutes les saisons non vu' : 'Marquer toutes les saisons comme vu';
    return done ? 'Marquer non vu' : 'Marquer comme vu';
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

  // `d` : la fiche de l'élément (/api/item), qui sait si TMDB le connaît ; null pour une saga, ou quand la fiche ne vient pas.
  function showMenu(it, d) {
    var done = finished(it), tmdb = d && d.links ? d.links.filter(function (l) { return l.label === 'TMDB'; })[0] : null;
    var options = [{ label: done ? 'Revoir' : (it.seen || it.started ? 'Reprendre' : 'Voir'), value: 'play' }, { label: A.seenLabel(it.type, done), value: 'seen' }];
    if (it.type === 'movie' && d && d.tmdb && H.isAdmin() && H.sagas) options.push({ label: 'Ajouter à une saga', value: 'saga' });      // sans identifiant TMDB, un film ne peut pas entrer dans une saga
    if (tmdb) options.push({ label: 'TMDB', detail: 'Ouvre la fiche dans un nouvel onglet', value: 'tmdb' });
    if (d && d.entries && d.entries.length && A.canDownload()) options.push({ label: 'Télécharger', detail: d.entries.length > 1 ? d.entries.length + ' fichiers, l’un après l’autre' : '', value: 'download' });
    H.menu(it.title, options, null, function (v) {
      if (v === 'play') (it.type === 'saga' ? playSaga : play)(it, done).catch(fail);
      else if (v === 'saga') H.sagas.addTo(d, refresh);
      else if (v === 'tmdb') window.open(tmdb.url, '_blank', 'noopener');
      else if (v === 'download') A.download(d.entries.map(function (e) { return e.id; }));
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
    return h('div', { class: 'qa' }, h('button', { class: 'qbtn', tabindex: '-1', 'aria-label': 'Plus d’actions', title: 'Plus d’actions',
      onclick: function (e) { e.preventDefault(); e.stopPropagation(); A.menu(it); } }, H.icon('more')));
  };
  A.bind = function (card, it) {
    card.addEventListener('contextmenu', function (e) { e.preventDefault(); A.menu(it); });
    card.addEventListener('keydown', function (e) { if (e.key === 'ContextMenu' || e.keyCode === 93) { e.preventDefault(); A.menu(it); } });
  };

  // --- Télécharger ----------------------------------------------------------------------
  // Toujours un fichier à la fois, jamais de zip : une coupure ne fait perdre qu'un fichier, et la reprise passe par `Range`.
  // Plusieurs fichiers (une saison, une série, un album) : on les demande l'un après l'autre ; le navigateur peut demander d'autoriser les téléchargements multiples la première fois.
  var GAP = 1200;
  A.canDownload = function () { return H.device !== 'tv'; };        // les navigateurs de télé téléchargent mal
  A.download = function (ids) {
    ids.forEach(function (id, i) {
      setTimeout(function () {
        var link = document.createElement('a');
        link.href = '/media/' + id + '/file?download=1';
        link.setAttribute('download', '');
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
      }, i * GAP);
    });
    H.toast(ids.length > 1 ? 'Téléchargement de ' + ids.length + ' fichiers, l’un après l’autre' : 'Téléchargement lancé', 4000);
  };
  // Le bouton de la fiche : une série à plusieurs saisons demande laquelle.
  A.downloadItem = function (d) {
    function ids(entries) { return entries.map(function (e) { return e.id; }); }
    var seasons = d.type === 'show' && d.seasons ? d.seasons.filter(function (s) { return d.entries.some(function (e) { return e.season === s; }); }) : [];
    if (seasons.length < 2) { A.download(ids(d.entries)); return; }
    var options = [{ label: 'Toute la série', detail: H.plural(d.entries.length, 'épisode'), value: 'all' }].concat(seasons.map(function (s) {
      var of = d.entries.filter(function (e) { return e.season === s; });
      return { label: s === 0 ? 'Spéciaux' : 'Saison ' + s, detail: H.plural(of.length, 'épisode'), value: String(s) };
    }));
    H.menu('Télécharger', options, null, function (v) {
      A.download(ids(v === 'all' ? d.entries : d.entries.filter(function (e) { return String(e.season) === v; })));
    });
  };
  // --- Changer l'affiche (administrateur) ---------------------------------------------------
  // L'image envoyée reste dans HUB_DATA : la médiathèque n'est pas touchée, et « Affiche d'origine » la retire. Le sélecteur de fichiers d'une télé se pilote mal : rien n'y est proposé.
  // `d` : la fiche (/api/item) ; `done` est appelé une fois l'affiche changée. Rend les éléments à poser parmi les boutons de la fiche.
  A.posterTools = function (d, done) {
    if ((d.type !== 'movie' && d.type !== 'show') || !H.isAdmin() || H.device === 'tv') return [];
    var picker = h('input', { type: 'file', accept: 'image/jpeg,image/png,image/webp', hidden: 'hidden', 'aria-hidden': 'true', onchange: function () {
      var file = this.files && this.files[0];
      this.value = '';
      if (file) H.api('/api/item/' + d.id + '/poster', file, { raw: true }).then(function () { H.toast('Affiche enregistrée'); done(); }).catch(fail);
    } });
    var tools = [picker, H.button('Changer l’affiche', 'edit', { 'data-key': 'poster', onclick: function () { picker.click(); } })];
    if (d.poster_custom) tools.push(H.button('Affiche d’origine', 'refresh', { 'data-key': 'poster-reset', onclick: function () {
      H.api('/api/item/' + d.id + '/poster/delete', {}).then(function () { H.toast('Affiche d’origine rétablie'); done(); }).catch(fail);
    } }));
    return tools;
  };
  // --- Changer la fiche TMDB (administrateur, séries et animations) ----------------------------
  // Quand la recherche sur le nom du dossier tombe sur un homonyme. Le choix reste dans la base du hub : la médiathèque n'est pas touchée.
  // On cherche par le nom, ou on colle l'adresse de la fiche sur themoviedb.org (la façon sûre de tomber sur la bonne). `d` : la fiche (/api/item) ; `done` est appelé une fois la fiche changée.
  var TMDB_URL = /^https?:\/\/(www\.)?themoviedb\.org\/tv\/\d+/i;
  A.tmdbTools = function (d, done) {
    if (d.type !== 'show' || !H.isAdmin() || H.device === 'tv') return [];
    function choose(value) {
      H.api('/api/item/' + d.id + '/tmdb', { tmdb: value }).then(function () { H.toast('Fiche TMDB enregistrée'); done(); }).catch(fail);
    }
    function search(text) {
      H.api('/api/item/' + d.id + '/tmdb?q=' + encodeURIComponent(text)).then(function (r) {
        H.menu('Quelle série ?', r.results.map(function (s) {
          var overview = s.overview.length > 140 ? s.overview.slice(0, 140) + '…' : s.overview;
          return { label: s.name + (s.year ? ' (' + s.year + ')' : ''), detail: [overview, 'TMDB ' + s.tmdb].filter(Boolean).join(' · '), value: s.tmdb };
        }), d.tmdb || null, choose);
      }).catch(fail);
    }
    return [H.button('Changer la fiche TMDB', 'edit', { 'data-key': 'tmdb', onclick: function () {
      H.prompt('Nom de la série, ou adresse de sa fiche TMDB', d.title, 'Chercher', function (text) { if (TMDB_URL.test(text)) choose(text); else search(text); });
    } })];
  };
  // Le bouton « … » d'une couverture de tome : le même menu que celui d'un épisode.
  A.entryTools = function (e, done) {
    return h('div', { class: 'qa' }, h('button', { class: 'qbtn', tabindex: '-1', 'aria-label': 'Plus d’actions', title: 'Plus d’actions',
      onclick: function (ev) { ev.preventDefault(); ev.stopPropagation(); A.episodeMenu(e, done); } }, H.icon('more')));
  };

  // Le menu d'un épisode : `done` est appelé une fois l'épisode marqué.
  A.episodeMenu = function (e, done) {
    var seen = !!(e.progress && e.progress.done);
    var options = [{ label: seen ? 'Revoir' : 'Lire', value: 'play' }, { label: A.seenLabel('episode', seen), value: 'seen' }];
    if (A.canDownload()) options.push({ label: 'Télécharger', value: 'download' });
    H.menu(e.title || 'Épisode', options, null, function (v) {
      if (v === 'play') H.go(H.playHref(e, seen ? 0 : undefined));
      else if (v === 'download') A.download([e.id]);
      else H.api('/api/entry/' + e.id + '/seen', { done: !seen }).then(done).catch(fail);
    });
  };
})();
