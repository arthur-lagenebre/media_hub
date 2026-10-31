/* Les pages : accueil, bibliothèque, fiche, recherche, gestion. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;

  H.libs = [];
  H.lib = function (id) { return H.libs.filter(function (l) { return l.id === id; })[0] || null; };

  // --- Barre du haut ----------------------------------------------------------
  H.nav = {
    build: function () {
      var nav = H.clear(document.getElementById('nav'));
      nav.appendChild(h('button', { class: 'tool f back', 'aria-label': 'Retour', title: 'Retour', hidden: 'hidden', onclick: function () { H.back('#/'); } }, H.icon('back')));
      nav.appendChild(h('a', { class: 'brand f', href: '#/', 'data-nav': '#/' }, H.icon('home'), h('span', { class: 'name' }, 'Médiathèque')));
      var links = h('nav', { class: 'links' });
      H.libs.forEach(function (l) {
        links.appendChild(h('a', { class: 'link f', href: '#/l/' + l.id, 'data-nav': '#/l/' + l.id, 'data-lib': l.id }, h('i', { class: 'dot' }), l.label));
      });
      nav.appendChild(links);
      nav.appendChild(h('a', { class: 'tool f', href: '#/search', 'data-nav': '#/search', 'aria-label': 'Recherche' }, H.icon('search')));
      nav.appendChild(h('a', { class: 'tool f', href: '#/admin', 'data-nav': '#/admin', 'aria-label': 'Gestion' }, H.icon('gear')));
    },
    mark: function (hash) {
      var back = document.querySelector('#nav .back');
      if (back) back.hidden = hash === '#/' || hash === '' || !hash;       // rien à remonter depuis l'accueil
      Array.prototype.forEach.call(document.querySelectorAll('#nav [data-nav]'), function (a) {
        var key = a.getAttribute('data-nav');
        var on = key === '#/' ? (hash === '#/' || hash === '' || !hash) : hash.indexOf(key) === 0;
        a.classList.toggle('on', on);
        if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
      });
    }
  };

  // --- Vignettes -------------------------------------------------------------
  function initial(title) { var c = (title || '?').replace(/^[^A-Za-zÀ-ÿ0-9]+/, '').charAt(0); return (c || '?').toUpperCase(); }
  H.picture = function (src, title, cls) {
    var ph = h('span', { class: 'ph', style: '--h:' + H.hue(title || '') }, initial(title));
    if (!src) return h('div', { class: 'poster ' + (cls || '') }, ph);       // pas d'image à demander : la lettre sur fond de couleur
    var img = h('img', { src: src, alt: '', loading: 'lazy', decoding: 'async', onload: function () { this.classList.add('in'); }, onerror: function () { if (this.parentNode) this.parentNode.removeChild(this); } });
    return h('div', { class: 'poster ' + (cls || '') }, ph, img);
  };
  function itemSub(it, kind) {
    var parts = [];
    if (it.type === 'album') { if (it.grp) parts.push(it.grp); if (it.year) parts.push(it.year); }
    else if (it.type === 'show') { if (it.year) parts.push(it.year); parts.push(it.n + ' ép.'); }
    else if (it.type === 'series') { parts.push(H.plural(it.n, 'volume')); if (it.missing) parts.push(it.missing + ' manquant' + (it.missing > 1 ? 's' : '')); }
    else { if (it.year) parts.push(it.year); }
    return parts.join(' · ');
  }
  function sagaHref(libId, name) { return '#/saga/' + libId + '?g=' + encodeURIComponent(name); }
  // Les affiches d'une saga, empilées en léger décalé : la première devant, les suivantes qui dépassent derrière.
  H.stack = function (ids, title, width) {
    var box = h('div', { class: 'stack n' + Math.min(3, ids.length) });
    ids.slice(0, 3).reverse().forEach(function (id, i, list) {
      var depth = list.length - 1 - i;       // 0 = devant
      var pic = H.picture('/img/item/' + id + '?w=' + (width || 300), title + depth);
      pic.setAttribute('style', 'left:' + depth * 8 + '%;top:' + (16 - depth * 5.33).toFixed(2) + '%;z-index:' + (3 - depth));
      box.appendChild(pic);
    });
    return box;
  };
  function sagaCard(it) {
    var a = h('a', { class: 'card saga f', href: sagaHref(it.lib, it.title), 'data-key': it.id, title: it.title });
    var stack = H.stack(it.covers && it.covers.length ? it.covers : [it.id], it.title);
    if (it.seen >= it.n && it.seen > 0) stack.appendChild(h('span', { class: 'badge done' }, H.icon('check')));
    else if (it.seen || it.started) stack.appendChild(h('span', { class: 'badge' }, (it.seen || 0) + '/' + it.n));
    a.appendChild(stack);
    a.appendChild(h('div', { class: 't' }, it.title));
    var years = it.years && it.years.length ? (it.years[0] === it.years[1] ? it.years[0] : it.years[0] + '–' + it.years[1]) : '';
    a.appendChild(h('div', { class: 's' }, [H.plural(it.n, 'film'), years].filter(Boolean).join(' · ')));
    return a;
  }
  H.card = function (it, withGroup, withPart) {
    if (it.type === 'saga') return sagaCard(it);
    var square = it.type === 'album';
    var a = h('a', { class: 'card f' + (square ? ' sq' : ''), href: '#/i/' + it.id, 'data-key': it.id, 'data-type': it.type, title: it.title });
    var pic = H.picture('/img/item/' + it.id + '?w=300', it.title, square ? 'sq' : '');
    var total = it.n || 1;
    if (it.seen >= total && it.seen > 0) pic.appendChild(h('span', { class: 'badge done' }, H.icon('check')));
    else if (it.seen || it.started) pic.appendChild(h('span', { class: 'badge' }, it.type === 'movie' ? 'En cours' : (it.seen || 0) + '/' + total));
    if (square && H.music) pic.appendChild(H.music.cardTools(it));
    if (withPart && it.part !== undefined && it.part !== null) pic.appendChild(h('span', { class: 'part' }, it.part));
    a.appendChild(pic);
    a.appendChild(h('div', { class: 't' }, it.title));
    a.appendChild(h('div', { class: 's' }, (withGroup && it.grp ? it.grp + ' · ' : '') + itemSub(it)));
    return a;
  };
  function row(title, href, cards, more) {
    var wrap = h('section', { class: 'shelf' });
    wrap.appendChild(h('h2', null, href ? h('a', { class: 'f head', href: href }, title) : title));
    var r = h('div', { class: 'row' }, cards);
    if (more) r.appendChild(more);
    wrap.appendChild(r);
    return wrap;
  }
  function playHref(entry, start) {
    return (entry.kind === 'book' ? '#/read/' : '#/play/') + entry.id + (start === 0 ? '?t=0' : '');
  }
  // `onRemove(card)` : appelée une fois la carte retirée de « Reprendre » (clic sur ✕, clic droit / appui long, touche Suppr ou Menu)
  function resumeCard(c, onRemove) {
    var href = playHref(c);
    var label;
    if (c.item.type === 'show') label = 'S' + (c.season || 1) + ' · E' + (c.number || '?') + (c.title ? ' · ' + c.title : '');
    else if (c.kind === 'book') label = (c.label ? 'Tome ' + c.label : c.title);
    else label = c.item.type === 'movie' ? '' : c.title;
    var ratio = c.total ? Math.min(1, c.position / c.total) : 0;
    var note = c.resume ? (c.kind === 'book' ? 'Page ' + (Math.floor(c.position) + 1) : 'À ' + H.time(c.position)) : 'Épisode suivant';
    if (c.kind === 'book' && !c.resume) note = 'Volume suivant';
    var pic = h('div', { class: 'poster wide' }, h('span', { class: 'ph', style: '--h:' + H.hue(c.item.title) }, initial(c.item.title)));
    var tryItem = function () { this.onerror = function () { if (this.parentNode) this.parentNode.removeChild(this); }; this.src = '/img/item/' + c.item.id + '?w=480'; };
    pic.appendChild(h('img', { src: '/img/entry/' + c.id + '?w=480', alt: '', loading: 'lazy', onload: function () { this.classList.add('in'); }, onerror: tryItem }));
    if (c.resume && ratio > 0) pic.appendChild(h('div', { class: 'pbar' }, h('i', { style: 'width:' + Math.round(ratio * 100) + '%' })));
    var card;
    function remove() {
      H.menu('Retirer « ' + c.item.title + ' » de Reprendre ?', [{ label: 'Retirer de Reprendre', detail: 'La progression est gardée', value: 'yes' }, { label: 'Garder', value: 'no' }], 'no', function (v) {
        if (v !== 'yes') return;
        H.api('/api/item/' + c.item.id + '/dismiss', {}).then(function () { H.toast('Retiré de Reprendre'); onRemove(card); })
          .catch(function (e) { H.toast(e && e.message ? e.message : 'Impossible pour le moment.'); });
      });
    }
    card = h('a', { class: 'card wide f', href: href, 'data-key': 'c' + c.id,
      oncontextmenu: function (e) { e.preventDefault(); remove(); },
      onkeydown: function (e) { if (e.key === 'Delete' || e.key === 'ContextMenu' || e.keyCode === 46 || e.keyCode === 93) { e.preventDefault(); remove(); } } },
      pic, h('div', { class: 't' }, c.item.title), h('div', { class: 's' }, [label, note].filter(Boolean).join(' · ')));
    pic.appendChild(h('div', { class: 'qa' }, h('button', { class: 'qbtn', tabindex: '-1', 'aria-label': 'Retirer de Reprendre', title: 'Retirer de Reprendre',
      onclick: function (e) { e.preventDefault(); e.stopPropagation(); remove(); } }, H.icon('close'))));
    return card;
  }

  // --- Accueil -------------------------------------------------------------------
  H.route(/^\/$/, function () {
    var el = h('section', { class: 'home' });
    var timer = null;
    function dismissed(card) {
      var next = card.nextElementSibling && card.nextElementSibling.classList.contains('card') ? card.nextElementSibling : card.previousElementSibling;
      load(next && next.getAttribute('data-key'));
    }
    function load(focusKey) {
      return H.api('/api/home').then(function (data) {
        H.clear(el);
        var total = data.libraries.reduce(function (n, l) { return n + l.count; }, 0);
        if (!total) {
          el.appendChild(h('div', { class: 'blank' },
            h('h1', null, 'Ta médiathèque est vide pour l’instant'),
            h('p', null, !H.isAdmin() ? 'Rien n’est encore indexé : l’administrateur doit lancer une mise à jour de la médiathèque.' : H.libs.length ? 'Les dossiers sont lus au premier démarrage. Si tu viens d’y ajouter des fichiers, lance une mise à jour.' : 'Aucun dossier de bibliothèque trouvé : monte tes dossiers sous /srv (Films, Series, Animations, Musique, Livres), ou décris-les avec HUB_LIBRARIES.'),
            H.isAdmin() ? h('a', { class: 'btn f primary', href: '#/admin', 'data-autofocus': '1' }, 'Ouvrir la gestion') : null));
          clearTimeout(timer);
          timer = setTimeout(function () { if (el.isConnected && !document.hidden) load(); }, 4000);
          return;
        }
        if (data.continue.length) el.appendChild(row('Reprendre', null, data.continue.map(function (c) { return resumeCard(c, dismissed); })));
        data.libraries.forEach(function (lib) {
          if (!lib.recent.length) return;
          var more = lib.count > lib.recent.length ? h('a', { class: 'card more f', href: '#/l/' + lib.id }, h('div', null, H.icon('fwd'), h('span', null, 'Tout voir'), h('small', null, H.number(lib.count)))) : null;
          var shelf = row(lib.label, '#/l/' + lib.id, lib.recent.map(function (it) { return H.card(it, lib.kind === 'music'); }), more);
          shelf.setAttribute('data-lib', lib.id);
          el.appendChild(shelf);
        });
        if (typeof focusKey === 'string' && H.focus.key(el, focusKey)) return;
        var a = document.activeElement;
        if (!a || a === document.body || (a.closest && a.closest('#nav')) && el.style.display !== 'none') H.focus.first();
      }).catch(function (e) { H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, 'Le serveur ne répond pas'), h('p', null, e.message), h('button', { class: 'btn f', onclick: load, 'data-autofocus': '1' }, 'Réessayer'))); });
    }
    load();
    return { el: el, nav: '#/', refresh: load, destroy: function () { clearTimeout(timer); } };
  });

  // --- Bibliothèque ----------------------------------------------------------------
  var SORTS = {
    movies: [['title', 'Titre'], ['added', 'Ajouts récents'], ['year', 'Plus récents'], ['year-asc', 'Plus anciens']],
    books: [['title', 'Collection'], ['added', 'Ajouts récents'], ['year', 'Plus récents'], ['year-asc', 'Plus anciens']]
  };
  H.route(/^\/l\/([\w-]+)$/, function (m) {
    var libId = m[1], lib = H.lib(libId);
    var kind = lib ? lib.kind : 'movies';
    var sorts = SORTS[kind] || SORTS.movies;
    var st = { q: '', letter: '', genre: '', sort: kind === 'music' ? 'title' : (H.pref('sort.' + libId) || 'title'), unseen: false, section: kind === 'books' ? (H.pref('section.' + libId) || '') : '', sagas: kind === 'movies' && H.pref('sagas.' + libId) !== '0' };
    var items = [], total = 0, loading = false, token = 0, lastGrp = null, facets = { letters: [], genres: [], sections: [] };
    var grid = h('div', { class: 'grid' + (kind === 'music' ? ' sq' : '') });
    var sentinel = h('div', { class: 'sentinel' });
    var count = h('span', { class: 'count' });
    var search = h('input', { class: 'f field', type: 'search', placeholder: 'Filtrer ' + (lib ? lib.label.toLowerCase() : ''), 'aria-label': 'Filtrer',
      oninput: function () { clearTimeout(search.t); search.t = setTimeout(function () { st.q = search.value; reset(); }, 280); },
      onkeydown: function (e) { if (e.key === 'Enter') { e.preventDefault(); search.blur(); H.focus.set(grid.querySelector('.f')); } } });
    var bLetter = H.button('', null, { onclick: pickLetter }), bGenre = H.button('', null, { onclick: pickGenre }), bSort = H.button('', null, { onclick: pickSort });
    var bSeen = H.button('Non vus', null, { onclick: function () { st.unseen = !st.unseen; reset(); }, 'aria-pressed': 'false' });
    var bSagas = H.button('Sagas', null, { onclick: function () { st.sagas = !st.sagas; H.pref('sagas.' + libId, st.sagas ? '1' : '0'); reset(); }, 'aria-pressed': 'false' });
    var bLists = H.button('Playlists', 'queue', { onclick: function () { H.go('#/playlists'); } });
    var tools = h('div', { class: 'tools' }, search, bLetter, bGenre, kind === 'music' ? null : bSort, kind === 'music' ? null : bSeen, kind === 'movies' ? bSagas : null, kind === 'music' ? bLists : null, count);
    var quick = h('div', { class: 'chips quick', role: 'group', 'aria-label': 'Accès rapides', hidden: true });
    var empty = h('div', { class: 'blank', hidden: true });
    var el = h('section', { class: 'library' }, h('h1', null, lib ? lib.label : 'Bibliothèque'), kind === 'books' ? quick : null, tools, grid, empty, sentinel);

    function label(btn, name, value) { H.clear(btn).appendChild(h('span', { class: 'lbl' }, [h('small', null, name + ' '), value])); }
    function paint() {
      label(bLetter, 'Lettre', st.letter || 'Toutes');
      label(bGenre, 'Genre', st.genre || 'Tous');
      label(bSort, 'Tri', (sorts.filter(function (s) { return s[0] === st.sort; })[0] || sorts[0])[1]);
      bSeen.setAttribute('aria-pressed', st.unseen ? 'true' : 'false');
      bSeen.classList.toggle('on', st.unseen);
      bSagas.setAttribute('aria-pressed', st.sagas ? 'true' : 'false');
      bSagas.classList.toggle('on', st.sagas);
      bGenre.hidden = !facets.genres.length;
      var word = { movies: 'film', shows: lib && lib.id === 'animations' ? 'animation' : 'série', music: 'album', books: 'série' }[kind] || 'élément';
      count.textContent = total ? (kind === 'movies' && st.sagas && facets.sagas ? H.plural(facets.films, 'film') + ' · ' + H.plural(facets.sagas, 'saga') : H.plural(total, word)) : '';
    }
    // Les accès rapides des livres : une pastille par section (Manga, BD, Comics, Artbook…), avec ce qu'elle contient.
    function paintQuick() {
      var list = facets.sections || [];
      if (st.section && !list.some(function (x) { return x[0] === st.section; })) st.section = '';     // la section a disparu : on revient à tout
      quick.hidden = list.length < 2;
      if (quick.hidden) return;
      var all = list.reduce(function (n, x) { return n + x[1]; }, 0);
      var focus = quick.contains(document.activeElement) ? document.activeElement.getAttribute('data-section') : null;
      H.clear(quick);
      [['', 'Tout', all]].concat(list.map(function (x) { return [x[0], x[0], x[1]]; })).forEach(function (x) {
        var on = st.section === x[0];
        quick.appendChild(h('button', { class: 'chip f' + (on ? ' on' : ''), type: 'button', 'data-section': x[0], 'aria-pressed': on ? 'true' : 'false',
          onclick: function () { st.section = x[0]; H.pref('section.' + libId, x[0] || null); reset(); } }, x[1], h('small', null, ' ' + H.number(x[2]))));
      });
      if (focus !== null) { var again = quick.querySelector('[data-section="' + focus.replace(/"/g, '\\"') + '"]'); if (again) H.focus.set(again, true); }
    }
    function pickLetter() {
      var list = [{ label: 'Toutes', value: '' }].concat(facets.letters.map(function (l) { return { label: l[0], detail: String(l[1]), value: l[0] }; }));
      H.menu(kind === 'music' ? 'Artiste commençant par' : 'Commence par', list, st.letter, function (v) { st.letter = v; reset(); });
    }
    function pickGenre() {
      var list = [{ label: 'Tous les genres', value: '' }].concat(facets.genres.map(function (g) { return { label: g[0], detail: String(g[1]), value: g[0] }; }));
      H.menu('Genre', list, st.genre, function (v) { st.genre = v; reset(); });
    }
    function pickSort() {
      H.menu('Trier par', sorts.map(function (s) { return { label: s[1], value: s[0] }; }), st.sort, function (v) { st.sort = v; H.pref('sort.' + libId, v); reset(); });
    }
    function query(offset) {
      var q = ['offset=' + offset, 'limit=240', 'sort=' + encodeURIComponent(st.sort)];
      if (st.q) q.push('q=' + encodeURIComponent(st.q));
      if (st.letter) q.push('letter=' + encodeURIComponent(st.letter));
      if (st.genre) q.push('genre=' + encodeURIComponent(st.genre));
      if (st.section) q.push('section=' + encodeURIComponent(st.section));
      if (st.unseen) q.push('filter=unseen');
      if (st.sagas) q.push('sagas=1');
      return '/api/library/' + libId + '?' + q.join('&');
    }
    function append(list) {
      var frag = document.createDocumentFragment();
      list.forEach(function (it) {
        if ((kind === 'music' || kind === 'books') && st.sort === 'title' && it.grp !== lastGrp) {
          lastGrp = it.grp;
          var heading = it.grp || (kind === 'music' ? 'Artiste inconnu' : 'Sans collection');
          frag.appendChild(h('h3', { class: 'grp' }, kind === 'music' && it.artist_id ? h('a', { class: 'f head', href: '#/artist/' + libId + '/' + it.artist_id }, heading) : heading));
        }
        frag.appendChild(H.card(it, false));
      });
      grid.appendChild(frag);
      items = items.concat(list);
    }
    function fetchMore(first) {
      if (loading || (!first && items.length >= total)) return;
      loading = true;
      var mine = token;
      H.api(query(items.length)).then(function (res) {
        if (mine !== token) return;
        total = res.total;
        if (first) { facets = res; paintQuick(); }
        append(res.items);
        loading = false;
        paint();
        empty.hidden = items.length > 0;
        if (!items.length) showEmpty();
        if (first && !H.overlay.isOpen() && el.isConnected && el.style.display !== 'none' && document.activeElement !== search && (document.activeElement === document.body || el.contains(document.activeElement))) H.focus.set(grid.querySelector('.card') || search, true);
        if (items.length < total && sentinel.getBoundingClientRect().top < window.innerHeight + 600) fetchMore(false);
      }).catch(function (e) { loading = false; empty.hidden = false; H.clear(empty).appendChild(h('p', null, e.message)); });
    }
    function showEmpty() {
      var filtered = st.q || st.letter || st.genre || st.unseen || st.section;
      H.clear(empty).appendChild(h('div', null,
        h('h2', null, filtered ? 'Rien ne correspond' : 'Cette bibliothèque est vide'),
        h('p', null, filtered ? 'Change ou retire un filtre.' : 'Vérifie que le dossier est bien monté, puis lance une mise à jour.'),
        filtered ? h('button', { class: 'btn f', onclick: function () { st.q = st.letter = st.genre = st.section = ''; st.unseen = false; search.value = ''; H.pref('section.' + libId, null); reset(); } }, 'Retirer les filtres') : H.isAdmin() ? h('a', { class: 'btn f', href: '#/admin' }, 'Ouvrir la gestion') : null));
    }
    function reset() {
      token++; items = []; total = 0; lastGrp = null; loading = false;
      H.clear(grid);
      fetchMore(true);
    }
    if ('IntersectionObserver' in window) new IntersectionObserver(function (e) { if (e[0].isIntersecting) fetchMore(false); }, { rootMargin: '800px' }).observe(sentinel);
    window.addEventListener('scroll', onScroll, { passive: true });
    function onScroll() { if (el.isConnected && el.style.display !== 'none' && sentinel.getBoundingClientRect().top < window.innerHeight + 800) fetchMore(false); }
    grid.addEventListener('focusin', function (e) {
      var cards = grid.querySelectorAll('.card'), idx = Array.prototype.indexOf.call(cards, e.target);
      if (idx >= 0 && idx > cards.length - 40) fetchMore(false);
    });
    reset();
    paint();
    return {
      el: el, lib: libId, nav: '#/l/' + libId,
      refresh: function () {      // au retour d'une fiche : les pastilles « vu » ont pu changer
        var mine = ++token, keep = items.length;
        H.api(query(0).replace('limit=240', 'limit=' + Math.min(600, Math.max(240, keep)))).then(function (res) {
          if (mine !== token) return;
          var focus = document.activeElement && grid.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
          items = []; lastGrp = null; total = res.total; facets = res; H.clear(grid); append(res.items); paintQuick(); paint();
          if (focus) H.focus.key(grid, focus);
        }).catch(function () {});
      },
      destroy: function () { window.removeEventListener('scroll', onScroll); }
    };
  });

  // --- Fiche -----------------------------------------------------------------------
  // TMDB donne le statut d'une série en anglais, quelle que soit la langue demandée.
  var SHOW_STATUS = { 'Ended': 'Terminée', 'Returning Series': 'En cours', 'Canceled': 'Annulée', 'In Production': 'En production', 'Planned': 'Prévue', 'Pilot': 'Pilote' };
  function entryProgress(e) { return e.progress && !e.progress.done && e.progress.position >= (e.kind === 'book' ? 1 : 20) ? e.progress : null; }
  function epLabel(e) { return 'S' + (e.season === null || e.season === undefined ? 1 : e.season) + ' · E' + (e.number === null || e.number === undefined ? '?' : e.number); }

  H.route(/^\/i\/(\w+)$/, function (m) {
    var id = m[1], el = h('section', { class: 'detail' }), season = null, data = null;
    function load() {
      return H.api('/api/item/' + id).then(function (d) { data = d; render(); }).catch(function (e) {
        H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, 'Introuvable'), h('p', null, e.message), h('a', { class: 'btn f', href: '#/', 'data-autofocus': '1' }, 'Accueil')));
      });
    }
    function setSeen(done, seasonNo) { H.api('/api/item/' + id + '/seen', { done: done, season: seasonNo === undefined ? null : seasonNo }).then(load); }

    function render() {
      var d = data, focusKey = document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
      H.clear(el);
      el.setAttribute('data-lib', d.library.id);
      var square = d.type === 'album', entries = d.entries;
      var resume = entries.filter(function (e) { return e.id === d.resume; })[0] || entries[0];
      var prog = resume ? entryProgress(resume) : null;
      var allDone = entries.length > 0 && entries.every(function (e) { return e.progress && e.progress.done; });
      var meta = [];
      if (d.type === 'movie') { if (d.year) meta.push(d.year); var secs = entries.reduce(function (s, e) { return s + (e.duration || 0); }, 0); if (secs) meta.push(H.dur(secs)); }
      else if (d.type === 'show') { if (d.year) meta.push(d.year); if (d.seasons && d.seasons.length) meta.push(H.plural(d.seasons.filter(function (s) { return s > 0; }).length || 1, 'saison')); meta.push(H.plural(entries.length, 'épisode')); if (d.status) meta.push(SHOW_STATUS[d.status] || d.status); }
      else if (d.type === 'album') { if (d.artist && !d.artist_id) meta.push(d.artist); if (d.year) meta.push(d.year); meta.push(H.plural(entries.length, 'piste')); var t = entries.reduce(function (s, e) { return s + (e.duration || 0); }, 0); if (t) meta.push(H.dur(t)); if (d.formats && d.formats.length) meta.push(d.formats.join(' / ')); }
      else { meta.push(d.grp || 'Livres'); meta.push(H.plural(entries.length, d.unit || 'volume', d.units)); if (d.year) meta.push(d.year); }
      var genres = (d.genres || []).join(', ');

      var info = h('div', { class: 'info' }, h('h1', null, d.title), h('p', { class: 'meta' }, meta.filter(Boolean).join(' · ')));
      if (genres) info.appendChild(h('p', { class: 'genres' }, genres));
      if (d.directors && d.directors.length) info.appendChild(h('p', { class: 'credit' }, 'De ' + d.directors.join(', ')));
      if (d.authors && d.authors.length) info.appendChild(h('p', { class: 'credit' }, 'Par ' + d.authors.join(', ')));
      if (d.type === 'album' && d.artist && d.artist_id) info.appendChild(h('p', { class: 'credit' }, h('a', { class: 'f head', href: '#/artist/' + d.library.id + '/' + d.artist_id }, d.artist)));
      if (d.summary) info.appendChild(h('p', { class: 'synopsis' }, d.summary));

      var actions = h('div', { class: 'actions' });
      if (d.type === 'album') {
        actions.appendChild(H.button('Lire l’album', 'play', { class: 'primary', 'data-autofocus': '1', onclick: function () { H.audio.play(trackList(), 0); } }));
        actions.appendChild(H.button('Aléatoire', 'shuffle', { onclick: function () { H.audio.play(trackList(), 0, true); } }));
        actions.appendChild(H.button('Jouer ensuite', null, { onclick: function () { H.music.queueTracks(trackList(), 'next', d.title); } }));
        actions.appendChild(H.button('À la file', 'queue', { onclick: function () { H.music.queueTracks(trackList(), 'end', d.title); } }));
        actions.appendChild(H.button('Playlist', 'plus', { onclick: function () { H.music.addToPlaylist(trackList().map(function (t) { return t.id; }), d.title); } }));
      } else if (resume) {
        var what = d.type === 'show' ? epLabel(resume) : (d.type === 'series' ? 'tome ' + (resume.label || resume.number || '') : '');
        var main = prog ? 'Reprendre' + (what ? ' ' + what : '') + (resume.kind === 'book' ? ' (page ' + (Math.floor(prog.position) + 1) + ')' : ' à ' + H.time(prog.position)) : (d.type === 'movie' && allDone ? 'Revoir' : 'Lire' + (what ? ' ' + what : ''));
        actions.appendChild(H.button(main, 'play', { class: 'primary', 'data-autofocus': '1', onclick: function () { H.go(playHref(resume)); } }));
        if (prog) actions.appendChild(H.button('Depuis le début', 'rew', { onclick: function () { H.go(playHref(resume, 0)); } }));
      }
      if (d.type !== 'album' && entries.length) actions.appendChild(H.button(allDone ? 'Marquer non vu' : 'Marquer comme vu', 'check', { onclick: function () { setSeen(!allDone); } }));
      if (d.type === 'movie' && d.tmdb && H.isAdmin() && H.sagas) actions.appendChild(H.button('Ajouter à une saga', 'plus', { 'data-key': 'saga-add', onclick: function () { H.sagas.addTo(d, load); } }));      // sans identifiant TMDB, un film ne peut pas entrer dans une saga
      (d.links || []).forEach(function (l) { actions.appendChild(h('a', { class: 'btn f', href: l.url, target: '_blank', rel: 'noopener noreferrer' }, l.label)); });
      info.appendChild(actions);
      var hero = h('div', { class: 'hero' }, h('div', { class: 'hero-bg', style: 'background-image:url(/img/item/' + d.id + '?w=300)' }),
        h('div', { class: 'hero-in' }, H.picture('/img/item/' + d.id + '?w=500', d.title, square ? 'sq big' : 'big'), info));
      el.appendChild(hero);

      var body = h('div', { class: 'body' });
      if (d.type === 'show') body.appendChild(episodes(d));
      else if (d.type === 'album') body.appendChild(tracks(d));
      else if (d.type === 'series') body.appendChild(volumes(d));
      else if (entries.length > 1) body.appendChild(parts(d));
      if (d.actors && d.actors.length) body.appendChild(h('section', { class: 'cast' }, h('h2', null, 'Distribution'), h('ul', null, d.actors.slice(0, 14).map(function (a) { return h('li', null, h('b', null, a.name), a.role ? h('span', null, a.role) : null); }))));
      (d.sagas || []).forEach(function (saga) {
        var cards = saga.films.map(function (s) { var c = H.card(s, false, true); if (s.id === d.id) c.classList.add('now'); return c; });
        body.appendChild(h('section', { class: 'sagabox' },
          h('h2', null, h('a', { class: 'f head', href: sagaHref(d.library.id, saga.name) }, saga.name), h('span', { class: 'n' }, H.plural(saga.films.length, 'film'))),
          h('div', { class: 'grid small' }, cards)));
      });
      el.appendChild(body);
      if (focusKey) H.focus.key(el, focusKey); else H.focus.first();
    }

    function trackList() {
      return data.entries.map(function (e) { return { id: e.id, title: e.title, artist: e.artist || data.artist || '', album: data.title, item: data.id, duration: e.duration }; });
    }
    function tracks(d) {
      var list = h('div', { class: 'tracks' }), disc = null, multi = (d.discs || 1) > 1;
      d.entries.forEach(function (e, i) {
        if (multi && e.disc !== disc) { disc = e.disc; list.appendChild(h('h3', { class: 'grp' }, 'Disque ' + (disc || 1))); }
        var done = e.progress && e.progress.done;
        var track = trackList()[i];
        list.appendChild(h('div', { class: 'trkwrap' },
          h('button', { class: 'trk f', 'data-key': e.id, onclick: function () { H.audio.play(trackList(), i); } },
            h('span', { class: 'no' }, e.number || i + 1), h('span', { class: 'ti' }, e.title), done ? h('span', { class: 'seen' }, H.icon('check')) : null, h('span', { class: 'du' }, e.duration ? H.time(e.duration) : '')),
          H.button('', 'more', { class: 'trkmore', 'aria-label': 'Actions de la piste', 'data-key': 'm' + e.id, onclick: function () { H.music.trackMenu(track); } })));
      });
      var sec = h('section', null, list);
      H.audio.mark(list);
      return sec;
    }
    function episodes(d) {
      var sec = h('section', { class: 'episodes' });
      var seasons = d.seasons && d.seasons.length ? d.seasons : [1];
      if (season === null || seasons.indexOf(season) < 0) {
        var r = d.entries.filter(function (e) { return e.id === d.resume; })[0];
        season = r && seasons.indexOf(r.season) >= 0 ? r.season : seasons[0];
      }
      var list = h('div', { class: 'eps' }), blurb = h('p', { class: 'seasonsum' }), bSeason = null;
      function seasonDone() { return d.entries.filter(function (e) { return e.season === season; }).every(function (e) { return e.progress && e.progress.done; }); }
      function fill() {
        if (bSeason) {          // le bouton annonce ce qu'il va faire de la saison affichée
          var done = seasonDone();
          bSeason.classList.toggle('done', done);
          H.clear(bSeason).appendChild(h('span', null, H.icon(done ? 'close' : 'check'), done ? ' Marquer la saison non vue' : ' Marquer la saison vue'));
        }
        H.clear(list);
        var text = (d.season_summaries || {})[String(season)] || '';
        blurb.textContent = text; blurb.hidden = !text;
        d.entries.filter(function (e) { return (e.season === null || e.season === undefined ? 1 : e.season) === season; }).forEach(function (e) {
          var p = entryProgress(e), done = e.progress && e.progress.done, ratio = p && e.duration ? Math.min(1, p.position / e.duration) : 0;
          var thumb = h('div', { class: 'poster wide' }, h('span', { class: 'ph', style: '--h:' + H.hue(d.title + e.number) }, e.number === null || e.number === undefined ? '' : e.number),
            h('img', { src: '/img/entry/' + e.id + '?w=400', alt: '', loading: 'lazy', onload: function () { this.classList.add('in'); }, onerror: function () { if (this.parentNode) this.parentNode.removeChild(this); } }));
          if (ratio) thumb.appendChild(h('div', { class: 'pbar' }, h('i', { style: 'width:' + Math.round(ratio * 100) + '%' })));
          list.appendChild(h('button', { class: 'ep f', 'data-key': e.id, onclick: function () { H.go(playHref(e)); } }, h('div', { class: 'thumb' }, thumb),
            h('div', { class: 'txt' }, h('div', { class: 'ti' }, h('b', null, e.number === null || e.number === undefined ? '' : e.number + '.'), ' ', e.title || 'Épisode'),
              e.summary ? h('div', { class: 'su' }, e.summary) : null,
              h('div', { class: 'du' }, [e.duration ? H.dur(e.duration) : '', e.date || ''].filter(Boolean).join(' · '))),
            done ? h('span', { class: 'seen' }, H.icon('check')) : null));
        });
      }
      var chips = h('div', { class: 'chips' });
      if (seasons.length > 1 || seasons[0] !== 1) seasons.forEach(function (s) {
        chips.appendChild(h('button', { class: 'chip f' + (s === season ? ' on' : ''), 'data-key': 's' + s, 'aria-pressed': s === season ? 'true' : 'false', onclick: function () {
          season = s; Array.prototype.forEach.call(chips.querySelectorAll('.chip'), function (c) { var on = c.getAttribute('data-key') === 's' + s; c.classList.toggle('on', on); c.setAttribute('aria-pressed', on ? 'true' : 'false'); }); fill();
        } }, s === 0 ? 'Spéciaux' : 'Saison ' + s));
      });
      if (seasons.length > 1) {
        bSeason = h('button', { class: 'chip f ghost', 'data-key': 'sv', onclick: function () { setSeen(!seasonDone(), season); } });
        chips.appendChild(bSeason);
      }
      sec.appendChild(chips); sec.appendChild(blurb); sec.appendChild(list);
      fill();
      return sec;
    }
    function volumes(d) {
      var sec = h('section', { class: 'volumes' }, h('h2', null, d.units ? d.units.charAt(0).toUpperCase() + d.units.slice(1) : 'Volumes'));
      var grid = h('div', { class: 'grid small' });
      d.entries.forEach(function (e) {
        var p = entryProgress(e), done = e.progress && e.progress.done;
        var pic = H.picture('/img/entry/' + e.id + '?w=300', e.title || d.title);
        if (done) pic.appendChild(h('span', { class: 'badge done' }, H.icon('check')));
        else if (p) pic.appendChild(h('div', { class: 'pbar' }, h('i', { style: 'width:' + Math.round(Math.min(1, p.position / Math.max(1, p.duration || 1)) * 100) + '%' })));
        grid.appendChild(h('a', { class: 'card f', href: playHref(e), 'data-key': e.id }, pic, h('div', { class: 't' }, e.label ? 'N° ' + e.label : (e.title || '')), h('div', { class: 's' }, [(e.format || '').toUpperCase(), e.pages ? e.pages + ' p.' : ''].filter(Boolean).join(' · '))));
      });
      sec.appendChild(grid);
      if (d.missing && d.missing.length) {
        sec.appendChild(h('p', { class: 'missing' }, 'Manquants : ' + d.missing.map(function (x) { return (x.label ? x.label + ' ' : '') + x.numbers.join(', '); }).join(' ; ')));
      }
      return sec;
    }
    function parts(d) {
      var sec = h('section', { class: 'eps' }, h('h2', null, 'Parties'));
      d.entries.forEach(function (e) {
        sec.appendChild(h('button', { class: 'ep f', onclick: function () { H.go(playHref(e)); } }, h('div', { class: 'txt' }, h('div', { class: 'ti' }, e.title), h('div', { class: 'du' }, H.dur(e.duration)))));
      });
      return sec;
    }
    load();
    return { el: el, nav: null, refresh: load, get lib() { return data ? data.library.id : ''; } };
  });

  // --- Saga ------------------------------------------------------------------------
  H.route(/^\/saga\/([\w-]+)$/, function (m, query) {
    var libId = m[1], name = query.g || '', data = null;
    var el = h('section', { class: 'detail sagapage', 'data-lib': libId });
    function load() {
      return H.api('/api/library/' + libId + '?group=' + encodeURIComponent(name) + '&sort=part&limit=600').then(function (d) { data = d; render(); }).catch(function (e) {
        H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, 'Introuvable'), h('p', null, e.message), h('a', { class: 'btn f', href: '#/l/' + libId, 'data-autofocus': '1' }, 'Retour')));
      });
    }
    function render() {
      var focusKey = document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
      var films = data.items;
      H.clear(el);
      if (!films.length) { el.appendChild(h('div', { class: 'blank' }, h('h1', null, name), h('p', null, 'Cette saga ne contient plus de film.'), h('a', { class: 'btn f', href: '#/l/' + libId, 'data-autofocus': '1' }, 'Retour'))); return; }
      var done = films.filter(function (f) { return f.seen >= Math.max(1, f.n); }).length;
      var next = films.filter(function (f) { return !(f.seen >= Math.max(1, f.n)); })[0];
      var years = films.map(function (f) { return f.year; }).filter(Boolean).sort();
      var meta = [H.plural(films.length, 'film')];
      if (years.length) meta.push(years[0] === years[years.length - 1] ? years[0] : years[0] + '–' + years[years.length - 1]);
      if (done) meta.push(done === films.length ? 'Tous vus' : done + '/' + films.length + ' vus');
      var info = h('div', { class: 'info' }, h('h1', null, name), h('p', { class: 'meta' }, meta.join(' · ')));
      var actions = h('div', { class: 'actions' });
      var target = next || films[0];
      actions.appendChild(H.button(next ? (done ? 'Continuer : ' : 'Commencer : ') + next.title : 'Revoir : ' + films[0].title, 'play', { class: 'primary', 'data-autofocus': '1', onclick: function () { H.go('#/i/' + target.id); } }));
      info.appendChild(actions);
      el.appendChild(h('div', { class: 'hero' }, h('div', { class: 'hero-bg', style: 'background-image:url(/img/item/' + films[0].id + '?w=300)' }),
        h('div', { class: 'hero-in' }, h('div', { class: 'stack-big' }, H.stack(films.map(function (f) { return f.id; }), name, 500)), info)));
      el.appendChild(h('div', { class: 'body' }, h('section', { class: 'sagaall' }, h('h2', null, 'Les films de la saga'),
        h('div', { class: 'grid' }, films.map(function (f) { return H.card(f, false, true); })))));
      if (focusKey) H.focus.key(el, focusKey); else H.focus.first();
    }
    load();
    return { el: el, nav: '#/l/' + libId, lib: libId, refresh: load };
  });

  // --- Recherche -------------------------------------------------------------------
  H.route(/^\/search$/, function () {
    var out = h('div', { class: 'results' }), last = '', timer = null;
    var input = h('input', { class: 'f field big', type: 'search', placeholder: 'Titre, artiste, collection…', 'aria-label': 'Rechercher', 'data-autofocus': '1', autocomplete: 'off',
      oninput: function () { clearTimeout(timer); timer = setTimeout(run, 300); },
      onkeydown: function (e) { if (e.key === 'Enter') { e.preventDefault(); run(); var c = out.querySelector('.f'); if (c) { input.blur(); H.focus.set(c); } } } });
    function run() {
      var q = input.value.trim();
      if (q === last) return;
      last = q;
      if (q.length < 2) { H.clear(out).appendChild(h('p', { class: 'muted' }, 'Tape au moins deux lettres.')); return; }
      H.api('/api/search?q=' + encodeURIComponent(q)).then(function (res) {
        if (q !== last) return;
        H.clear(out);
        if (!res.results.length) { out.appendChild(h('p', { class: 'muted' }, 'Aucun résultat pour « ' + q + ' ».')); return; }
        res.results.forEach(function (g) {
          var sec = h('section', { class: 'shelf', 'data-lib': g.id }, h('h2', null, h('a', { class: 'f head', href: '#/l/' + g.id }, g.label)), h('div', { class: 'grid' + (g.kind === 'music' ? ' sq' : '') }, g.items.map(function (it) { return H.card(it, g.kind === 'music'); })));
          out.appendChild(sec);
        });
      }).catch(function (e) { H.clear(out).appendChild(h('p', { class: 'muted' }, e.message)); });
    }
    var el = h('section', { class: 'search' }, h('h1', null, 'Recherche'), input, out);
    return { el: el, nav: '#/search' };
  });

  // --- Gestion ---------------------------------------------------------------------
  function ago(ts) {
    if (!ts) return 'jamais';
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return 'à l’instant';
    if (s < 5400) return 'il y a ' + Math.round(s / 60) + ' min';
    if (s < 129600) return 'il y a ' + Math.round(s / 3600) + ' h';
    return 'il y a ' + Math.round(s / 86400) + ' j';
  }
  // Une fenêtre à champs (comptes) : o = {title, lead: [phrases], fields: [{key, label, type: 'text' | 'password', auto}], extra: [éléments sous les champs],
  // ok: libellé du bouton, danger, submit(valeurs) → texte (refus affiché dans la fenêtre) ou promesse, done(résultat) une fois la fenêtre fermée}.
  // Une erreur du serveur reste affichée dans la fenêtre, qui reste ouverte. Entrée passe au champ suivant, puis valide.
  function dialog(o) {
    var inputs = {}, list = [], busy = false, ok, err = h('p', { class: 'formerr', role: 'alert' });
    var box = h('div', { class: 'menu prompt form', role: 'dialog', 'aria-label': o.title }, h('h3', null, o.title));
    box.addEventListener('focusin', function (e) { try { e.target.scrollIntoView({ block: 'nearest' }); } catch (x) { /* tant pis */ } });   // petit écran ou télé : la fenêtre défile jusqu'au bouton atteint
    (o.lead || []).forEach(function (t) { box.appendChild(h('p', { class: 'lead' }, t)); });
    function show(text) { err.textContent = text || ''; }
    function send() {
      if (busy) return;
      var values = {};
      o.fields.forEach(function (f) { var v = inputs[f.key].value; values[f.key] = f.type === 'password' ? v : v.replace(/\s+/g, ' ').trim(); });
      var r = o.submit(values);
      if (typeof r === 'string') { show(r); return; }
      busy = true; show('');
      r.then(function (res) { H.overlay.close(function () { if (o.done) o.done(res); }); },
        function (e) { busy = false; show(e.message); });
    }
    o.fields.forEach(function (f, i) {
      var input = h('input', { class: 'f field big', type: f.type === 'password' ? 'password' : 'text', maxlength: f.type === 'password' ? '200' : '40', autocomplete: f.auto || 'off',
        'data-autofocus': i === 0 ? '1' : null,
        onkeydown: function (e) {
          if (e.key !== 'Enter') return;
          e.preventDefault();
          if (list[i + 1]) list[i + 1].focus(); else send();
        } });
      inputs[f.key] = input; list.push(input);
      box.appendChild(h('label', { class: 'flab' }, f.label, input));
    });
    (o.extra || []).forEach(function (x) { box.appendChild(x); });
    box.appendChild(err);
    ok = H.button(o.ok || 'Valider', o.danger ? 'trash' : 'check', { class: o.danger ? 'danger' : 'primary', onclick: send });
    box.appendChild(h('div', { class: 'btns' }, ok, H.button('Annuler', null, { 'data-autofocus': o.fields.length ? null : '1', onclick: function () { H.overlay.close(); } })));
    H.overlay.open(box);
    if (o.fields.length) setTimeout(function () { try { list[0].focus(); } catch (e) { /* tant pis */ } }, 30);
  }
  var ROLES = { admin: 'Administrateur', user: 'Utilisateur' };
  function checkPassword(pw, again) {
    if (pw.length < 8) return 'Le mot de passe doit faire au moins 8 caractères.';
    if (pw.length > 200) return 'Le mot de passe est trop long (200 caractères au plus).';
    if (pw !== again) return 'Les deux mots de passe ne sont pas identiques.';
    return '';
  }

  H.route(/^\/admin$/, function () {
    var el = h('section', { class: 'admin' }), timer = null, wasBusy = false, state = null, jobs = [], users = null, usersError = '', logPanel = null, lastPage = '', lastTasks = '';
    function qualityLabel(v) { return { auto: 'Qualité d’origine quand c’est possible', '1080': '1080p · 8 Mb/s', '720': '720p · 4 Mb/s', '480': '480p · 1,5 Mb/s' }[v] || v; }
    function start(path, body, text) {
      return H.api(path, body).then(function () { H.toast(text); poll(); }).catch(function (e) { H.toast(e.message, 5000); });
    }
    function render() {
      var focus = document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
      H.clear(el);
      el.appendChild(h('h1', null, 'Gestion'));
      var libs = h('section', { class: 'panel' }, h('div', { class: 'phead' }, h('h2', null, 'Bibliothèques'),
        H.button('Tout mettre à jour', 'refresh', { class: 'primary', 'data-autofocus': '1', 'data-key': 'scan-all', title: 'Relit les cinq bibliothèques ; seuls les fichiers modifiés sont relus.', onclick: function () { start('/api/scan', {}, 'Mise à jour lancée'); } })));
      state.libraries.forEach(function (l) {
        libs.appendChild(h('div', { class: 'line', 'data-lib': l.id },
          h('div', { class: 'what' }, h('b', null, l.label), h('span', null, l.exists ? H.plural(l.count, 'élément') + ' · mis à jour ' + ago(l.scanned) : 'dossier introuvable')),
          l.kind === 'shows' ? H.button('Enrichir (TMDB)', null, { 'data-key': 'enrich-' + l.id, onclick: function () { start('/api/enrich', { lib: l.id }, 'Enrichissement lancé : ' + l.label); } }) : null,
          l.kind === 'music' ? H.button('Fiches artistes', null, { 'data-key': 'enrich-' + l.id, onclick: function () { start('/api/enrich', { lib: l.id }, 'Fiches artistes demandées : ' + l.label); } }) : null,
          H.button('Mettre à jour', 'refresh', { 'data-key': 'scan-' + l.id, disabled: false, onclick: function () { start('/api/scan', { lib: l.id }, 'Mise à jour lancée : ' + l.label); } })));
      });
      if (H.isAdmin()) el.appendChild(libs);
      if (H.isAdmin()) el.appendChild(h('section', { class: 'panel' }, h('h2', null, 'Sagas'), h('div', { class: 'line' },
        h('div', { class: 'what' }, h('b', null, 'Sagas composées à la main'), h('span', null, 'Une trilogie que TMDB ne réunit pas, l’ordre du MCU… : des listes de films dans l’ordre de ton choix, en plus de la saga de l’étiquette.')),
        h('a', { class: 'btn f', href: '#/sagas', 'data-key': 'sagas' }, H.icon('edit'), h('span', { class: 'lbl' }, 'Composer')))));

      var play = h('section', { class: 'panel' }, h('h2', null, 'Lecture'));
      var q = H.pref('quality') || 'auto', surround = !!H.pref('surround');
      play.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, 'Qualité'), h('span', null, qualityLabel(q))),
        H.button('Changer', 'quality', { 'data-key': 'quality', onclick: function () {
          H.menu('Qualité de lecture', ['auto', '1080', '720', '480'].map(function (v) { return { label: qualityLabel(v), value: v }; }), q, function (v) { H.pref('quality', v); render(); H.focus.key(el, 'quality'); });
        } })));
      var chosen = H.pref('device') || 'auto', names = {};
      H.devices.forEach(function (d) { names[d[0]] = d[1]; });
      play.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, 'Appareil'),
        h('span', null, (chosen === 'auto' ? 'Détecté : ' : 'Choisi : ') + names[H.device] + ' — taille du texte, effets de survol, envoi de photo'), h('span', { class: 'ua' }, navigator.userAgent)),
        H.button('Changer', 'quality', { 'data-key': 'device', onclick: function () {
          H.menu('Type d’appareil', H.devices.map(function (d) { return { label: d[1], detail: d[0] === 'auto' ? 'Deviné d’après le navigateur (' + names[H.detectDevice()] + ')' : '', value: d[0] }; }), chosen, function (v) {
            H.pref('device', v === 'auto' ? null : v); H.applyDevice(); render(); H.focus.key(el, 'device');
          });
        } })));
      play.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, 'Son surround 5.1'), h('span', null, surround ? 'Activé : le 5.1 est envoyé tel quel si l’appareil le lit.' : 'Désactivé : le son est ramené en stéréo (le plus sûr).')),
        H.button(surround ? 'Désactiver' : 'Activer', 'audio', { 'data-key': 'surround', onclick: function () { H.pref('surround', !surround); render(); H.focus.key(el, 'surround'); } })));
      el.appendChild(play);

      var tools = h('section', { class: 'panel' }, h('h2', null, 'Entretien'));
      tools.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, 'Fichiers parasites'), h('span', null, 'Thumbs.db, desktop.ini, .DS_Store et les AlbumArt_{…}_Large / _Small.jpg semés par Windows et macOS ; en musique, toute image autre que cover.jpg (ou .png…), la seule pochette d’un album, y compris folder.jpg.')),
        H.button('Compter', null, { 'data-key': 'junk-sim', onclick: function () { start('/api/cleanup', {}, 'Simulation lancée'); } }),
        H.button('Supprimer', null, { 'data-key': 'junk-del', onclick: function () { start('/api/cleanup', { apply: true }, 'Nettoyage lancé'); } })));
      el.appendChild(accountPanel());
      if (H.session.auth && H.isAdmin()) el.appendChild(usersPanel());
      if (H.isAdmin()) el.appendChild(tools);

      var server = h('section', { class: 'panel' }, h('h2', null, 'Serveur'));
      var facts = [['Version', state.version], ['ffmpeg', state.ffmpeg ? 'présent' : 'absent : lecture directe uniquement'], ['TMDB', state.tmdb ? 'clé configurée' : 'pas de clé (TMDB_KEY)'],
        ['Conversions', state.streams + ' en cours sur ' + state.max_streams], ['Accès', state.auth ? 'protégé par mot de passe' : 'sans mot de passe']];
      server.appendChild(h('dl', null, facts.map(function (f) { return [h('dt', null, f[0]), h('dd', null, f[1])]; })));
      el.appendChild(server);

      logPanel = H.isAdmin() ? jobsPanel() : null;                      // le journal des tâches est réservé à l'administrateur (le serveur refuse ?log=1 aux autres)
      if (logPanel) el.appendChild(logPanel);
      if (H.overlay.isOpen()) return;                                   // une fenêtre de saisie est ouverte : on ne lui prend pas le focus
      if (!(focus && H.focus.key(el, focus, true))) H.focus.first();   // sans défilement
    }
    // Le compte connecté : nom, rôle, changement de mot de passe, déconnexion. Serveur ouvert : comment créer le premier compte.
    function accountPanel() {
      var panel = h('section', { class: 'panel' }, h('h2', null, 'Compte'));
      var me = H.session.user;
      if (!H.session.auth || !me) {
        panel.appendChild(h('p', { class: 'muted' }, 'Aucun compte n’existe : le serveur est ouvert, et toute personne qui peut l’atteindre voit et gère tout. Pour le protéger, crée le premier compte (il sera administrateur) sur le serveur avec ',
          h('code', null, 'python -m hub user add <nom>'), ', ou définis ', h('code', null, 'HUB_PASSWORD'), ' avant de démarrer le serveur. Les comptes suivants se gèrent ensuite ici.'));
        return panel;
      }
      panel.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, me.name), h('span', null, ROLES[me.role] || me.role)),
        H.button('Changer mon mot de passe', 'edit', { 'data-key': 'mypw', onclick: changeMine }),
        h('a', { class: 'btn f', href: '/api/logout', 'data-key': 'logout', onclick: function () { H.forgetAccount(); } }, 'Se déconnecter')));
      return panel;
    }
    function changeMine() {
      dialog({ title: 'Changer mon mot de passe', ok: 'Changer',
        fields: [{ key: 'current', label: 'Mot de passe actuel', type: 'password', auto: 'current-password' }, { key: 'next', label: 'Nouveau mot de passe (8 caractères au moins)', type: 'password', auto: 'new-password' }, { key: 'again', label: 'Nouveau mot de passe, encore une fois', type: 'password', auto: 'new-password' }],
        submit: function (v) {
          if (!v.current) return 'Saisis ton mot de passe actuel.';
          var bad = checkPassword(v.next, v.again);
          if (bad) return bad;
          return H.api('/api/me/password', { current: v.current, 'new': v.next });   // le serveur renouvelle le cookie : rien à garder ici
        },
        done: function () { H.toast('Mot de passe changé. Les autres appareils devront se reconnecter.', 5000); H.focus.key(el, 'mypw'); } });
    }
    // Gestion des comptes (administrateur, serveur avec comptes).
    function usersPanel() {
      var panel = h('section', { class: 'panel' }, h('div', { class: 'phead' }, h('h2', null, 'Comptes'),
        H.button('Ajouter un compte', 'plus', { 'data-key': 'user-add', onclick: addUser })));
      if (usersError) panel.appendChild(h('p', { class: 'muted' }, usersError));
      else if (!users) panel.appendChild(h('p', { class: 'muted' }, 'Chargement…'));
      (users || []).forEach(function (u) {
        var mine = H.session.user && u.id === H.session.user.id;
        panel.appendChild(h('div', { class: 'line', 'data-user': u.id }, h('div', { class: 'what' }, h('b', null, u.name + (mine ? ' (toi)' : '')), h('span', null, (ROLES[u.role] || u.role) + ' · créé le ' + new Date(u.created * 1000).toLocaleDateString('fr-FR'))),
          H.button('Mot de passe', 'edit', { 'data-key': 'pw-' + u.id, 'aria-label': 'Changer le mot de passe de ' + u.name, onclick: function () { passwordOf(u, mine); } }),
          H.button('Supprimer', 'trash', { 'data-key': 'del-' + u.id, 'aria-label': 'Supprimer le compte de ' + u.name, onclick: function () { deleteUser(u, mine); } })));
      });
      return panel;
    }
    function addUser() {
      var role = 'user', toggle = H.button('Rôle : ' + ROLES.user, null, { 'data-key': 'new-role', 'aria-label': 'Rôle du nouveau compte, à changer', onclick: function () {
        role = role === 'user' ? 'admin' : 'user';
        toggle.querySelector('.lbl').textContent = 'Rôle : ' + ROLES[role];
      } });
      dialog({ title: 'Ajouter un compte', ok: 'Créer le compte',
        lead: ['Un administrateur gère la médiathèque et les comptes ; un utilisateur lit, avec sa propre progression et ses propres playlists.'],
        fields: [{ key: 'name', label: 'Nom (40 caractères au plus)', type: 'text' }, { key: 'pw', label: 'Mot de passe (8 caractères au moins)', type: 'password', auto: 'new-password' }, { key: 'again', label: 'Mot de passe, encore une fois', type: 'password', auto: 'new-password' }],
        extra: [toggle],
        submit: function (v) {
          if (!v.name) return 'Donne un nom au compte.';
          var bad = checkPassword(v.pw, v.again);
          if (bad) return bad;
          return H.api('/api/users', { name: v.name, password: v.pw, role: role });
        },
        done: function (u) { H.toast('Compte créé : ' + u.name); refreshUsers('user-add'); } });
    }
    function passwordOf(u, mine) {
      if (mine) { changeMine(); return; }                              // son propre compte : même formulaire que « Changer mon mot de passe » (le serveur exige le mot de passe actuel)
      dialog({ title: 'Mot de passe de ' + u.name, ok: 'Changer',
        lead: ['Les sessions ouvertes de ce compte seront fermées ; la personne se reconnectera avec le nouveau mot de passe.'],
        fields: [{ key: 'pw', label: 'Nouveau mot de passe (8 caractères au moins)', type: 'password', auto: 'new-password' }, { key: 'again', label: 'Encore une fois', type: 'password', auto: 'new-password' }],
        submit: function (v) {
          var bad = checkPassword(v.pw, v.again);
          if (bad) return bad;
          return H.api('/api/user/' + encodeURIComponent(u.id) + '/password', { password: v.pw });
        },
        done: function () { H.toast('Mot de passe changé : ' + u.name); H.focus.key(el, 'pw-' + u.id); } });
    }
    function deleteUser(u, mine) {
      var lead = ['Supprimer le compte « ' + u.name + ' » ? Sa progression de lecture, ses playlists et ses éléments retirés de « Reprendre » seront perdus, et cela ne s’annule pas. Les fichiers de la médiathèque ne sont pas touchés.'];
      if (mine) lead.push('C’est le compte avec lequel tu es connecté : tu seras déconnecté. Il doit rester au moins un autre administrateur.');
      dialog({ title: 'Supprimer le compte de ' + u.name, ok: 'Supprimer définitivement', danger: true, lead: lead, fields: [],
        submit: function () { return H.api('/api/user/' + encodeURIComponent(u.id) + '/delete', {}); },
        done: function () {
          if (mine) { H.forgetAccount(); location.href = '/login'; return; }
          H.toast('Compte supprimé : ' + u.name);
          refreshUsers('user-add');
        } });
    }
    function refreshUsers(key) {
      poll().then(function () { if (!(key && H.focus.key(el, key))) H.focus.first(); });
    }
    function jobsPanel() {
      var busy = jobs.some(function (j) { return j.status === 'waiting' || j.status === 'running'; });
      var log = h('section', { class: 'panel' }, h('h2', null, 'Tâches', busy ? h('span', { class: 'busy' }, '…') : null));
      if (!jobs.length) log.appendChild(h('p', { class: 'muted' }, 'Aucune tâche pour l’instant.'));
      jobs.slice(0, 6).forEach(function (j) {
        var status = { waiting: 'en attente', running: 'en cours', done: 'terminée', failed: 'échec' }[j.status];
        log.appendChild(h('div', { class: 'job ' + j.status }, h('div', { class: 'jt' }, h('b', null, j.label), h('span', null, status)), j.log && j.log.length ? h('pre', null, j.log.slice(-8).join('\n')) : null));
      });
      return log;
    }
    function poll() {
      clearTimeout(timer);
      // Un non-administrateur n'interroge que l'état « occupé » (sans journal). Si son rôle a changé depuis le démarrage, le 403 se rattrape par un appel sans journal.
      var jobsCall = H.isAdmin() ? H.api('/api/jobs?log=1').catch(function (e) { if (e.status === 403) return H.api('/api/jobs'); throw e; }) : H.api('/api/jobs');
      return Promise.all([H.api('/api/state'), jobsCall]).then(function (r) {
        H.setSession(r[0]);
        if (!(r[0].auth && H.isAdmin())) { users = null; usersError = ''; return r; }
        return H.api('/api/users').then(function (res) { users = res.users; usersError = ''; return r; }, function (e) { users = null; usersError = e.message; return r; });
      }).then(function (r) {
        var page = JSON.stringify([r[0], users, usersError]), tasks = JSON.stringify(r[1].jobs);
        state = r[0]; jobs = r[1].jobs; H.libs = state.libraries;
        var busy = r[1].busy;
        if (wasBusy && !busy) H.toast('Terminé');
        wasBusy = busy;
        // Pendant une tâche on revient ici toutes les 1,5 s : reconstruire la page entière coupait le défilement (molette, tactile). On ne refait que ce qui a changé.
        if (page !== lastPage || (H.isAdmin() && !logPanel) || (!H.isAdmin() && logPanel)) render();
        else if (logPanel && tasks !== lastTasks) { var fresh = jobsPanel(); el.replaceChild(fresh, logPanel); logPanel = fresh; }
        lastPage = page; lastTasks = tasks;
        if (busy) timer = setTimeout(poll, 1500);
      }).catch(function (e) { H.toast(e.message, 5000); });
    }
    poll();
    return { el: el, nav: '#/admin', refresh: poll, destroy: function () { clearTimeout(timer); } };
  });
})();
