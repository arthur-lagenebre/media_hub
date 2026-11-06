/* La musique côté interface : lire depuis une pochette, la file d'attente, les playlists. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;
  var M = H.music = {};

  function musicLib() { return H.libs.filter(function (l) { return l.kind === 'music'; })[0] || null; }
  M.libId = function () { var l = musicLib(); return l ? l.id : 'musique'; };

  // --- Des pistes, au format du lecteur ---------------------------------------------
  M.fromDetail = function (d) {
    return d.entries.map(function (e) { return { id: e.id, title: e.title, artist: e.artist || d.artist || '', album: d.title, item: d.id, duration: e.duration }; });
  };
  M.tracksOf = function (itemId) { return H.api('/api/item/' + itemId).then(M.fromDetail); };

  // Les invités d'une piste, à montrer sous son titre : « Kavinsky feat. Havoc » (l'artiste de la piste) ou « Protovision (feat. Havoc) » (le titre, qui perd alors sa parenthèse).
  var GUEST_ARTIST = /\s(?:feat|ft|featuring)\.?\s+(.+)$/i, GUEST_TITLE = /\s*[(\[]\s*(?:feat|ft|featuring)\.?\s+([^)\]]+?)\s*[)\]]/i;
  M.guests = function (title, artist) {
    var inArtist = GUEST_ARTIST.exec(artist || ''), inTitle = GUEST_TITLE.exec(title || ''), bare = inTitle ? title.replace(inTitle[0], '').trim() : '';
    return { title: bare || title, guests: inArtist ? inArtist[1] : inTitle ? inTitle[1] : '' };
  };
  // La ligne sous le titre d'une piste, dans la liste des pistes d'un album : ses artistes quand ils diffèrent de ceux de l'album (une compilation, une bande originale),
  // et seulement ce qui s'y ajoute quand la piste est de l'artiste de l'album.
  // Ce qui s'y ajoute vient de la liste des artistes de la piste (`artists`, le tag ARTISTS : un nom par artiste, sans liaison) : « Poppy », « Poppy, Bloodywood ».
  // Un fichier qui n'a pas cette liste (album pas encore repassé par media_library) garde la règle sur le tag ARTIST : « feat. Poppy ».
  M.GUEST_SEPARATOR = ', ';        // ce qui sépare les invités dans la ligne : un seul endroit à changer
  function same(a, b) { return String(a || '').replace(/\s+/g, ' ').trim().toLowerCase() === String(b || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
  // Les invités de la liste, ou null quand la liste ne s'applique pas (absente, ou l'artiste de l'album n'est pas de la piste : une compilation).
  // Un artiste de la piste est de l'album si son identifiant est parmi ceux de l'album ; faute d'identifiants pour tous, si son nom est celui de l'album.
  function listedGuests(artists, albumArtist, albumIds) {
    if (!artists || !artists.length) return null;
    var ids = (albumIds || []).filter(Boolean), byId = ids.length > 0 && artists.every(function (a) { return a.id; });
    function own(a) { return byId ? ids.indexOf(a.id) >= 0 : same(a.name, albumArtist); }
    return artists.some(own) ? artists.filter(function (a) { return !own(a); }).map(function (a) { return a.name; }) : null;
  }
  M.credit = function (title, artist, albumArtist, artists, albumIds) {
    var listed = listedGuests(artists, albumArtist, albumIds);
    if (listed) {
      var inTitle = M.guests(title, '');          // « Titre (feat. X) » : géré comme avant quand la liste n'a pas d'invité
      return { title: inTitle.title, line: listed.length ? listed.join(M.GUEST_SEPARATOR) : inTitle.guests ? 'feat. ' + inTitle.guests : '' };
    }
    var shown = M.guests(title, artist), found = GUEST_ARTIST.exec(artist || ''), own = (found ? artist.slice(0, found.index) : artist || '').trim();
    if (!own || same(own, albumArtist)) return { title: shown.title, line: shown.guests ? 'feat. ' + shown.guests : '' };
    return { title: shown.title, line: own + (shown.guests ? ' feat. ' + shown.guests : '') };
  };
  function fail(e) { H.toast(e && e.message ? e.message : 'Impossible pour le moment.'); }

  M.playItem = function (itemId, shuffle) { return M.tracksOf(itemId).then(function (list) { if (list.length) H.audio.play(list, 0, shuffle); }).catch(fail); };
  M.queueItem = function (itemId, where, title) {
    return M.tracksOf(itemId).then(function (list) { queueTracks(list, where, title); }).catch(fail);
  };
  function queueTracks(list, where, title) {
    var playing = H.audio.active();
    var n = H.audio.enqueue(list, where);
    if (!n) return;
    H.toast(playing ? (where === 'next' ? 'Jouée ensuite : ' : 'Ajouté à la file : ') + (title || H.plural(n, 'piste')) : 'Lecture : ' + (title || H.plural(n, 'piste')));
  }
  M.queueTracks = queueTracks;

  // --- Ajouter à une playlist ---------------------------------------------------------
  M.addToPlaylist = function (entryIds, label) {
    H.api('/api/playlists').then(function (res) {
      var options = [{ label: 'Nouvelle playlist…', value: '+' }].concat(res.playlists.map(function (p) { return { label: p.name, detail: H.plural(p.count, 'piste'), value: p.id }; }));
      H.menu('Ajouter à une playlist', options, null, function (v) {
        if (v === '+') {
          H.prompt('Nom de la nouvelle playlist', '', 'Créer', function (name) {
            H.api('/api/playlists', { name: name, entries: entryIds }).then(function (p) { H.toast('Ajouté à « ' + p.name + ' »'); }).catch(fail);
          });
        } else {
          H.api('/api/playlist/' + v + '/add', { entries: entryIds }).then(function (p) { H.toast('Ajouté à « ' + p.name + ' »' + (label ? ' : ' + label : '')); }).catch(fail);
        }
      });
    }).catch(fail);
  };

  // --- Menus d'actions ---------------------------------------------------------------
  function canHear(libId) { return libId ? H.actions.canPlay(libId) : H.actions.canPlayKind('music'); }
  function canGet(libId) { return libId ? H.actions.canDownload(libId) : H.actions.canDownloadKind('music'); }          // une file ou une playlist mêle les bibliothèques : sans la sienne, une bibliothèque de musique au moins
  M.albumMenu = function (itemId, title, libId) {
    H.menu(title, (canHear(libId) ? [
      { label: 'Lire l’album', value: 'play' }, { label: 'Lecture aléatoire', value: 'shuffle' },
      { label: 'Jouer ensuite', value: 'next' }, { label: 'Ajouter à la file d’attente', value: 'end' }] : []).concat([{ label: 'Ajouter à une playlist…', value: 'list' }]).concat(canGet(libId) ? [{ label: 'Télécharger l’album', value: 'download' }] : []), null, function (v) {
      if (v === 'download') M.tracksOf(itemId).then(function (list) { H.actions.download(list.map(function (t) { return t.id; })); }).catch(fail);
      else if (v === 'play') M.playItem(itemId);
      else if (v === 'shuffle') M.playItem(itemId, true);
      else if (v === 'next' || v === 'end') M.queueItem(itemId, v, title);
      else M.tracksOf(itemId).then(function (list) { M.addToPlaylist(list.map(function (t) { return t.id; }), title); }).catch(fail);
    });
  };
  // `extra` : actions propres à l'endroit où l'on est (dans une playlist : monter, descendre, retirer) : [{label, value, run}]
  M.trackMenu = function (track, extra, libId) {
    var options = (canHear(libId) ? [{ label: 'Jouer ensuite', value: 'next' }, { label: 'Ajouter à la file d’attente', value: 'end' }] : []).concat([{ label: 'Ajouter à une playlist…', value: 'list' }]).concat(canGet(libId) ? [{ label: 'Télécharger', value: 'download' }] : [], extra || []);
    H.menu(track.title, options, null, function (v) {
      if (v === 'download') H.actions.download([track.id]);
      else if (v === 'next' || v === 'end') queueTracks([track], v, track.title);
      else if (v === 'list') M.addToPlaylist([track.id], track.title);
      else (extra || []).forEach(function (x) { if (x.value === v) x.run(); });
    });
  };

  // --- Le bouton « lecture » posé sur une pochette (souris et doigt) -------------------
  M.cardTools = function (it) {
    function stop(e) { e.preventDefault(); e.stopPropagation(); }
    return h('div', { class: 'qa' },
      canHear() ? h('button', { class: 'qbtn', tabindex: '-1', 'aria-label': 'Lire ' + it.title, title: 'Lire', onclick: function (e) { stop(e); M.playItem(it.id); } }, H.icon('play')) : null,
      h('button', { class: 'qbtn', tabindex: '-1', 'aria-label': 'Plus d’actions', title: 'Plus d’actions', onclick: function (e) { stop(e); M.albumMenu(it.id, it.title); } }, H.icon('more')));
  };
  // Touche « lecture / pause » de la télécommande sur une pochette : lance l'album
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'MediaPlayPause' && e.key !== 'MediaPlay' && e.keyCode !== 179) return;
    var a = document.activeElement;
    if (!a || !a.classList || !a.classList.contains('card') || a.getAttribute('data-type') !== 'album' || H.overlay.isOpen() || !canHear()) return;
    e.preventDefault();
    M.playItem(a.getAttribute('data-key'));
  });

  // --- La file d'attente ---------------------------------------------------------------
  M.openQueue = function () {
    var list = h('div', { class: 'menu-list qlist' });
    var title = h('h3', null, 'File d’attente');
    var box = h('div', { class: 'menu queue', role: 'dialog', 'aria-label': 'File d’attente' }, title, list);
    var footer = h('div', { class: 'btns' });
    box.appendChild(footer);

    function paint(focusKey) {
      var s = H.audio.state();
      H.clear(list); H.clear(footer);
      title.textContent = s.queue.length ? 'File d’attente · ' + H.plural(s.queue.length, 'piste') : 'File d’attente';
      if (!s.queue.length) list.appendChild(h('p', { class: 'muted' }, 'Rien en attente. Ajoute un album ou une piste avec « Jouer ensuite » ou « Ajouter à la file d’attente ».'));
      s.queue.forEach(function (t, i) {
        var now = i === s.idx;
        list.appendChild(h('div', { class: 'qrow' + (now ? ' now' : '') },
          h('button', { class: 'qt f', 'data-key': 'q' + i, 'data-autofocus': now ? '1' : null, onclick: function () { H.audio.jump(i); paint('q' + i); } },
            h('span', { class: 'no' }, now ? H.icon('play') : i + 1), h('span', { class: 'ti' }, h('b', null, t.title), h('small', null, [t.artist, t.album].filter(Boolean).join(' · '))), h('span', { class: 'du' }, t.duration ? H.time(t.duration) : '')),
          H.button('', 'up', { class: 'qb', 'aria-label': 'Monter', disabled: i === 0, onclick: function () { H.audio.move(i, i - 1); paint('qu' + (i - 1)); }, 'data-key': 'qu' + i }),
          H.button('', 'down', { class: 'qb', 'aria-label': 'Descendre', disabled: i === s.queue.length - 1, onclick: function () { H.audio.move(i, i + 1); paint('qd' + (i + 1)); }, 'data-key': 'qd' + i }),
          H.button('', 'close', { class: 'qb', 'aria-label': 'Retirer de la file', onclick: function () { H.audio.remove(i); if (H.audio.state().queue.length) paint('q' + Math.min(i, H.audio.state().queue.length - 1)); else H.overlay.close(); }, 'data-key': 'qx' + i })));
      });
      if (s.queue.length) {
        footer.appendChild(H.button('Enregistrer en playlist', 'plus', { onclick: function () { M.addToPlaylist(H.audio.state().queue.map(function (t) { return t.id; }), 'la file d’attente'); } }));
        if (s.idx < s.queue.length - 1) footer.appendChild(H.button('Vider la suite', null, { onclick: function () { H.audio.clearNext(); paint(); } }));
        footer.appendChild(H.button('Tout arrêter', 'close', { onclick: function () { H.audio.clear(); H.overlay.close(); } }));
      }
      footer.appendChild(H.button('Fermer', null, { onclick: function () { H.overlay.close(); } }));
      if (focusKey) H.focus.key(box, focusKey);
      var now = list.querySelector('.qrow.now');
      if (now && !focusKey) now.scrollIntoView({ block: 'center' });
    }
    H.overlay.open(box);
    paint();
  };

  // --- Playlists : la liste ------------------------------------------------------------
  function mosaic(p) {
    var box = h('div', { class: 'poster sq mosaic n' + Math.min(4, p.covers.length) }, h('span', { class: 'ph', style: '--h:' + H.hue(p.name) }, (p.name || '?').charAt(0).toUpperCase()));
    p.covers.slice(0, 4).forEach(function (id) {
      box.appendChild(h('img', { src: '/img/item/' + id + '?w=200', alt: '', loading: 'lazy', onload: function () { this.classList.add('in'); }, onerror: function () { if (this.parentNode) this.parentNode.removeChild(this); } }));
    });
    return box;
  }
  function subOf(p) { return [H.plural(p.count, 'piste'), p.duration ? H.dur(p.duration) : ''].filter(Boolean).join(' · '); }

  H.route(/^\/playlists$/, function () {
    var lib = M.libId();
    var el = h('section', { class: 'library playlists', 'data-lib': lib });
    var grid = h('div', { class: 'grid sq' });
    function create() {
      H.prompt('Nom de la nouvelle playlist', '', 'Créer', function (name) {
        H.api('/api/playlists', { name: name }).then(function (p) { H.go('#/playlist/' + p.id); }).catch(fail);
      });
    }
    function load() {
      return H.api('/api/playlists').then(function (res) {
        H.clear(grid);
        res.playlists.forEach(function (p) {
          grid.appendChild(h('a', { class: 'card sq f', href: '#/playlist/' + p.id, 'data-key': p.id, title: p.name }, mosaic(p), h('div', { class: 't' }, p.name), h('div', { class: 's' }, subOf(p))));
        });
        empty.hidden = res.playlists.length > 0;
      }).catch(function (e) { H.clear(grid); H.clear(empty).appendChild(h('p', null, e.message)); empty.hidden = false; });
    }
    var empty = h('div', { class: 'blank', hidden: true }, h('h2', null, 'Aucune playlist pour l’instant'),
      h('p', null, 'Crée une playlist, puis ajoute-y des pistes ou des albums entiers avec « Ajouter à une playlist » (bouton ⋯ d’une pochette, d’un album ou d’une piste).'));
    var tools = h('div', { class: 'tools' }, H.button('Nouvelle playlist', 'plus', { class: 'primary', 'data-autofocus': '1', onclick: create }), H.button('Retour à la musique', 'back', { onclick: function () { H.go('#/l/' + lib); } }));
    el.appendChild(h('h1', null, 'Playlists')); el.appendChild(tools); el.appendChild(grid); el.appendChild(empty);
    load();
    return { el: el, nav: '#/l/' + lib, lib: lib, refresh: load };
  });

  // --- La fiche d'un artiste ou d'un groupe ---------------------------------------------
  // Les albums possédés et ceux qui manquent, rangés ensemble par année puis par titre (les possédés arrivent déjà dans cet ordre ; ceux sans année restent à la fin)
  function firstIndex(list, test) {            // Array.prototype.findIndex est de l'ES2015 : absent des navigateurs de télé les plus anciens
    for (var i = 0; i < list.length; i++) if (test(list[i])) return i;
    return -1;
  }
  function shelf(d) {
    var out = d.albums.slice();
    (d.wanted || []).forEach(function (w) {
      var after = firstIndex(out, function (a) { return a.year && (a.year > w.year || (a.year === w.year && a.title.toLowerCase() > w.title.toLowerCase())); });
      var undated = firstIndex(out, function (a) { return !a.year; });
      var at = after >= 0 ? after : (undated >= 0 ? undated : out.length);
      out.splice(at, 0, { wanted: true, title: w.title, year: w.year, cover: w.cover });
    });
    return out;
  }
  // Un album qui manque : sa pochette grisée, rien à ouvrir ni à lire (la case n'est donc pas dans la navigation à la télécommande)
  function wantedCard(w) {
    return h('div', { class: 'card wanted', title: w.title + ' (manquant)' },
      H.picture(w.cover ? w.cover + '?w=300' : '', w.title, 'sq'),
      h('div', { class: 't' }, w.title), h('div', { class: 's' }, [w.year, 'Manquant'].join(' · ')));
  }
  // Recadrer une photo : un cadre carré (la vignette) sur l'image qu'on déplace et qu'on agrandit. `src` est l'image à montrer ; `onOk(cadre)` envoie le cadre (x, y, largeur, hauteur en fractions de l'image) et rend une promesse : la fenêtre se ferme si elle réussit, reste ouverte avec le message si elle échoue. Ce qui est hors du cadre ne sera pas gardé.
  function cropPhoto(src, onOk, onClosed) {
    var S = 0, pad = 0, F = 0;       // côté de la fenêtre de l'image, marge autour du cadre, côté du cadre (voir size)
    var nw = 0, nh = 0, base = 1, zoom = 1, ox = 0, oy = 0, busy = false, drag = null;
    var err = h('p', { class: 'formerr', role: 'alert' });
    var ok = H.button('Valider', null, { class: 'primary', disabled: true, onclick: validate });
    var img = h('img', { alt: '', draggable: 'false', onload: function () {
      nw = this.naturalWidth; nh = this.naturalHeight;
      if (!nw || !nh) { err.textContent = 'Image illisible.'; return; }
      ok.disabled = false; layout();
    }, onerror: function () { err.textContent = 'Image illisible.'; } });
    var slider = h('input', { type: 'range', class: 'f', min: '0', max: '100', step: '1', value: '0', 'aria-label': 'Zoom', oninput: function () { zoomTo(1 + this.value / 25); } });
    var frameEl = h('div', { class: 'crop-frame' });
    var stage = h('div', { class: 'crop-stage f', tabindex: '0', role: 'application', 'aria-label': 'Photo à recadrer : flèches pour déplacer, + et - pour agrandir, Entrée pour valider', 'data-autofocus': '1' }, img, frameEl);
    function size(side) {
      S = Math.round(side); pad = Math.round(S * 0.08); F = S - 2 * pad;
      stage.style.width = stage.style.height = S + 'px';
      frameEl.style.left = frameEl.style.top = pad + 'px'; frameEl.style.width = frameEl.style.height = F + 'px';
    }
    function layout() {       // l'image remplit le cadre, centrée, sans zoom
      if (nw) {
        base = F / Math.min(nw, nh); zoom = 1; slider.value = '0';
        ox = pad + (F - nw * base) / 2; oy = pad + (F - nh * base) / 2;
        place();
      }
    }
    function place() {       // l'image garde toujours le cadre plein : jamais de bord vide dans la vignette
      var w = nw * base * zoom, ht = nh * base * zoom;
      ox = Math.min(pad, Math.max(pad + F - w, ox)); oy = Math.min(pad, Math.max(pad + F - ht, oy));
      img.style.width = w + 'px'; img.style.height = ht + 'px'; img.style.left = ox + 'px'; img.style.top = oy + 'px';
    }
    function zoomTo(z, ax, ay) {       // autour du point (ax, ay) de la fenêtre, par défaut le centre du cadre
      if (!nw) return;
      z = Math.min(5, Math.max(1, z));
      ax = ax === undefined ? pad + F / 2 : ax; ay = ay === undefined ? pad + F / 2 : ay;
      var k = z / zoom;
      ox = ax - (ax - ox) * k; oy = ay - (ay - oy) * k; zoom = z;
      slider.value = String(Math.round((z - 1) * 25));
      place();
    }
    function frame() {
      var w = nw * base * zoom, ht = nh * base * zoom, x = (pad - ox) / w, y = (pad - oy) / ht;
      x = Math.min(1, Math.max(0, x)); y = Math.min(1, Math.max(0, y));
      return { x: +x.toFixed(5), y: +y.toFixed(5), w: +Math.min(1 - x, F / w).toFixed(5), h: +Math.min(1 - y, F / ht).toFixed(5) };
    }
    function validate() {
      if (busy || !nw) return;
      busy = true; ok.disabled = true; err.textContent = '';
      Promise.resolve(onOk(frame())).then(function () { H.overlay.close(); }, function (e) { busy = false; ok.disabled = false; err.textContent = e && e.message ? e.message : 'Impossible pour le moment.'; });
    }
    stage.addEventListener('pointerdown', function (e) {
      if (e.button) return;
      drag = { x: e.clientX, y: e.clientY, ox: ox, oy: oy };
      try { stage.setPointerCapture(e.pointerId); } catch (x) { /* tant pis */ }
      e.preventDefault();
    });
    stage.addEventListener('pointermove', function (e) { if (drag) { ox = drag.ox + e.clientX - drag.x; oy = drag.oy + e.clientY - drag.y; place(); } });
    ['pointerup', 'pointercancel'].forEach(function (name) { stage.addEventListener(name, function () { drag = null; }); });
    stage.addEventListener('wheel', function (e) {
      e.preventDefault();
      var r = stage.getBoundingClientRect();
      zoomTo(zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    stage.addEventListener('keydown', function (e) {
      var step = e.shiftKey ? 40 : 10, k = e.key;
      if (k === 'ArrowLeft') ox += step; else if (k === 'ArrowRight') ox -= step; else if (k === 'ArrowUp') oy += step; else if (k === 'ArrowDown') oy -= step;
      else if (k === '+' || k === '=') zoomTo(zoom * 1.1); else if (k === '-') zoomTo(zoom / 1.1);
      else if (k === 'Enter') validate(); else return;
      e.preventDefault(); place();
    });
    img.src = src;       // après les gestionnaires : une image déjà en cache déclenche onload tout de suite
    var box = h('div', { class: 'menu crop', role: 'dialog', 'aria-label': 'Recadrer la photo' },
      h('h3', null, 'Recadrer la photo'),
      h('p', { class: 'lead' }, 'Déplace l’image et agrandis-la : seule la partie dans le cadre est gardée, le reste est retiré de la photo.'),
      stage, h('label', { class: 'zoom' }, h('span', null, 'Zoom'), slider), err,
      h('div', { class: 'btns' }, ok, H.button('Annuler', null, { onclick: function () { H.overlay.close(); } })));
    size(Math.max(160, Math.min(560, window.innerWidth * 0.92 - 80)));
    H.overlay.open(box, onClosed);
    // La fenêtre ne dépasse jamais l'écran, quelle que soit la taille du texte (rem) : l'image prend ce qui reste une fois le reste mesuré
    var rest = box.scrollHeight - S, room = box.clientHeight || window.innerHeight * 0.84;
    if (box.scrollHeight > room || S > room - rest - 24) { size(Math.max(120, Math.min(S, room - rest - 24))); layout(); }
  }

  H.route(/^\/artist\/([\w-]+)\/(\w+)$/, function (m) {
    var lib = m[1], id = m[2], data = null, stamp = 0;
    var el = h('section', { class: 'detail artist', 'data-lib': lib });
    var picker = h('input', { type: 'file', accept: 'image/jpeg,image/png,image/webp', hidden: true, 'aria-hidden': 'true', tabindex: '-1',
      onchange: function () { var file = this.files && this.files[0]; this.value = ''; if (file) upload(file); } });
    function saved(message) { stamp = Date.now(); H.toast(message); return load(); }
    function upload(file) {       // l'image choisie s'ouvre dans le recadrage ; rien n'est envoyé avant « Valider »
      if (file.size > 10 * 1024 * 1024) { H.toast('Image trop grosse (10 Mo au plus).'); return; }
      var url = URL.createObjectURL(file);
      cropPhoto(url, function (f) {
        return H.api('/api/artist/' + lib + '/' + id + '/photo?crop=' + [f.x, f.y, f.w, f.h].join(','), file, { raw: true }).then(function () { return saved('Photo enregistrée'); });
      }, function () { URL.revokeObjectURL(url); });
    }
    function recrop() {       // la photo qu'il a déjà, rognée une seconde fois
      cropPhoto('/img/artist/' + lib + '/' + id + '?w=1280&v=' + (stamp || 1), function (f) {
        return H.api('/api/artist/' + lib + '/' + id + '/photo/crop', f).then(function () { return saved('Photo recadrée'); });
      });
    }
    function load() {
      return H.api('/api/artist/' + lib + '/' + id).then(function (d) { data = d; render(); }).catch(function (e) {
        H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, 'Artiste introuvable'), h('p', null, e.message), h('a', { class: 'btn f', href: '#/l/' + lib, 'data-autofocus': '1' }, 'Retour à la musique')));
      });
    }
    // Les boutons de la fiche d'un artiste : lire, mettre en file, et pour l'administrateur la photo.
    function artistActions(d, list) {
      var hear = canHear(d.library && d.library.id);
      return h('div', { class: 'actions' },
        hear ? H.button('Tout lire', 'play', { class: 'primary', 'data-autofocus': '1', disabled: !list.length, onclick: function () { H.audio.play(list, 0); } }) : null,
        hear ? H.button('Aléatoire', 'shuffle', { disabled: !list.length, onclick: function () { H.audio.play(list, 0, true); } }) : null,
        hear ? H.button('Jouer ensuite', null, { disabled: !list.length, onclick: function () { queueTracks(list, 'next', d.name); } }) : null,
        hear ? H.button('À la file', 'queue', { disabled: !list.length, onclick: function () { queueTracks(list, 'end', d.name); } }) : null,
        H.device === 'tv' || !H.isAdmin() ? null : H.button('Changer la photo', 'edit', { 'data-key': 'photo', onclick: function () { picker.click(); } }),       // le sélecteur de fichiers d'une télé se pilote mal ; la photo est réservée à l'administrateur
        H.device === 'tv' || !H.isAdmin() || !d.photo ? null : H.button('Recadrer', 'full', { 'data-key': 'crop', onclick: recrop }));
    }
    // Le texte à droite de la photo : nom, faits, genres, totaux, résumé Wikipédia, boutons.
    function artistInfo(d, info) {
      var list = d.tracks;
      var meta = [H.plural(d.albums.length, 'album'), H.plural(list.length, 'piste'), d.duration ? H.dur(d.duration) : ''].filter(Boolean).join(' · ');
      var box = h('div', { class: 'info' }, h('h1', null, d.name));
      if (info && info.facts) box.appendChild(h('p', { class: 'meta' }, info.facts));
      var tags = [info && info.disambiguation ? info.disambiguation : ''].concat(d.genres || []).filter(Boolean).join(', ');
      if (tags) box.appendChild(h('p', { class: 'genres' }, tags));
      box.appendChild(h('p', { class: info && info.facts ? 'credit' : 'meta' }, meta));
      if (info && info.wiki) box.appendChild(h('p', { class: 'synopsis' }, info.wiki.extract, ' ', h('a', { class: 'src', href: info.wiki.url, target: '_blank', rel: 'noopener noreferrer' }, 'Wikipédia')));
      box.appendChild(artistActions(d, list));
      return box;
    }
    function artistHero(d, box) {
      var first = d.albums[0], face = d.photo ? '/img/artist/' + lib + '/' + id : '/img/item/' + first.id, v = stamp ? '&v=' + stamp : '';       // la photo de l'artiste, sinon la pochette de son premier album
      return h('div', { class: 'hero' }, h('div', { class: 'hero-bg', style: 'background-image:url(' + face + '?w=320' + v + ')' }),
        h('div', { class: 'hero-in' }, H.picture(face + '?w=480' + v, d.name, 'sq big'), box));
    }
    // Sur la fiche d'un artiste, le nom de l'artiste sous chaque pochette serait répété : la carte n'a que l'année.
    function withoutGroup(a) {
      var copy = {};
      for (var k in a) if (Object.prototype.hasOwnProperty.call(a, k) && k !== 'grp') copy[k] = a[k];
      return copy;
    }
    function albumsSection(d) {
      return h('section', null, h('h2', null, 'Albums'), h('div', { class: 'grid' }, shelf(d).map(function (a) { return a.wanted ? wantedCard(a) : H.card(withoutGroup(a), false); })));
    }
    // Les membres du groupe : les actuels, puis les anciens.
    function memberSections(info) {
      if (!(info && info.members && info.members.length)) return [];
      var current = info.members.filter(function (p) { return !p.former; }), former = info.members.filter(function (p) { return p.former; });
      var members = function (title, list) {
        if (!list.length) return null;
        return h('section', { class: 'cast' }, h('h2', null, title), h('ul', null, list.map(function (p) {
          return h('li', null, h('b', null, p.name), [p.roles.join(', '), p.period].filter(Boolean).length ? h('span', null, [p.roles.join(', '), p.period].filter(Boolean).join(' · ')) : null);
        })));
      };
      return [members(former.length ? 'Membres actuels' : 'Membres', current), members('Anciens membres', former)];       // « actuels » ne veut dire quelque chose que s'il y en a d'anciens
    }
    function linksSection(info) {
      if (!(info && info.links && info.links.length)) return null;
      return h('section', { class: 'links-out' }, h('h2', null, 'Liens'), h('div', { class: 'actions' }, info.links.map(function (l) {
        return h('a', { class: 'btn f', href: l.url, target: '_blank', rel: 'noopener noreferrer' }, l.label);
      })));
    }
    function noSheetNote(info) {
      if (info) return null;
      return h('p', { class: 'muted' }, H.isAdmin() ? 'Pas encore de fiche : lance « Fiches artistes » dans Paramètres > Gestion pour récupérer le résumé, les membres et les liens.' : 'Pas encore de fiche pour cet artiste.');
    }
    function artistBody(d, info) {
      var body = h('div', { class: 'body' });
      [albumsSection(d)].concat(memberSections(info), [linksSection(info), noSheetNote(info)]).forEach(function (node) { if (node) body.appendChild(node); });
      return body;
    }
    function render() {
      var d = data, info = d.info || null;
      var focusKey = document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
      H.clear(el);
      el.appendChild(picker);
      el.appendChild(artistHero(d, artistInfo(d, info)));
      el.appendChild(artistBody(d, info));
      H.audio.mark();
      if (focusKey) H.focus.key(el, focusKey); else H.focus.first();
    }
    load();
    return { el: el, nav: '#/l/' + lib, lib: lib, refresh: load };
  });

  // --- Playlists : une playlist --------------------------------------------------------
  H.route(/^\/playlist\/(\w+)$/, function (m) {
    var pid = m[1], lib = M.libId(), data = null;
    var el = h('section', { class: 'detail playlist', 'data-lib': lib });
    function load() {
      return H.api('/api/playlist/' + pid).then(function (d) { data = d; render(); }).catch(function (e) {
        H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, 'Playlist introuvable'), h('p', null, e.message), h('a', { class: 'btn f', href: '#/playlists', 'data-autofocus': '1' }, 'Toutes les playlists')));
      });
    }
    function edit(action, body, focus) { return H.api('/api/playlist/' + pid + '/' + action, body).then(function (d) { data = d; render(focus); }).catch(fail); }
    function tracks() { return data.tracks.map(function (t) { return { id: t.id, title: t.title, artist: t.artist, album: t.album, item: t.item, duration: t.duration }; }); }
    function render(focusKey) {
      var d = data, list = tracks();
      focusKey = focusKey || (document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null);
      H.clear(el);
      var meta = [H.plural(d.count, 'piste'), d.duration ? H.dur(d.duration) : ''].filter(Boolean).join(' · ');
      var info = h('div', { class: 'info' }, h('h1', null, d.name), h('p', { class: 'meta' }, meta));
      if (d.missing) info.appendChild(h('p', { class: 'muted' }, H.plural(d.missing, 'piste') + ' introuvable' + (d.missing > 1 ? 's' : '') + ' (fichier déplacé ou supprimé).'));
      var hear = canHear();
      var actions = h('div', { class: 'actions' },
        hear ? H.button('Lire', 'play', { class: 'primary', 'data-autofocus': '1', disabled: !list.length, onclick: function () { H.audio.play(list, 0); } }) : null,
        hear ? H.button('Aléatoire', 'shuffle', { disabled: !list.length, onclick: function () { H.audio.play(list, 0, true); } }) : null,
        hear ? H.button('Jouer ensuite', null, { disabled: !list.length, onclick: function () { queueTracks(list, 'next', d.name); } }) : null,
        H.button('Renommer', 'edit', { onclick: function () { H.prompt('Renommer la playlist', d.name, 'Renommer', function (name) { edit('rename', { name: name }); }); } }),
        H.button('Supprimer', 'trash', { onclick: function () {
          H.menu('Supprimer « ' + d.name + ' » ?', [{ label: 'Supprimer la playlist', value: 'yes' }, { label: 'Garder', value: 'no' }], 'no', function (v) {
            if (v === 'yes') H.api('/api/playlist/' + pid + '/delete', {}).then(function () { H.toast('Playlist supprimée'); H.go('#/playlists', true); }).catch(fail);
          });
        } }));
      info.appendChild(actions);
      var hero = h('div', { class: 'hero' }, h('div', { class: 'hero-bg', style: d.covers[0] ? 'background-image:url(/img/item/' + d.covers[0] + '?w=300)' : '' }), h('div', { class: 'hero-in' }, mosaic(d), info));
      el.appendChild(hero);
      var body = h('div', { class: 'body' });
      if (!list.length) body.appendChild(h('div', { class: 'blank' }, h('h2', null, 'Cette playlist est vide'), h('p', null, 'Ajoute des pistes depuis un album : bouton ⋯ d’une piste, ou « Playlist » en haut de l’album.')));
      else {
        var box = h('div', { class: 'tracks' });
        list.forEach(function (t, i) {
          var tools = [];
          if (i > 0) tools.push({ label: 'Monter', value: 'up', run: function () { edit('move', { from: i, to: i - 1 }, 'p' + (i - 1)); } });
          if (i < list.length - 1) tools.push({ label: 'Descendre', value: 'down', run: function () { edit('move', { from: i, to: i + 1 }, 'p' + (i + 1)); } });
          tools.push({ label: 'Retirer de la playlist', value: 'remove', run: function () { edit('remove', { index: i }, 'p' + Math.min(i, list.length - 2)); } });
          box.appendChild(h('div', { class: 'trkwrap' },
            h('button', { class: 'trk f', 'data-key': 'p' + i, 'data-track': t.id, onclick: function () { H.audio.play(list, i); } },
              h('span', { class: 'no' }, i + 1), h('span', { class: 'ti' }, h('b', null, t.title), h('small', null, [t.artist, t.album].filter(Boolean).join(' · '))), h('span', { class: 'du' }, t.duration ? H.time(t.duration) : '')),
            H.button('', 'more', { class: 'trkmore', 'aria-label': 'Actions de la piste', 'data-key': 'pm' + i, onclick: function () { M.trackMenu(t, tools); } })));
        });
        body.appendChild(box);
      }
      el.appendChild(body);
      H.audio.mark();
      if (focusKey) H.focus.key(el, focusKey); else H.focus.first();
    }
    load();
    return { el: el, nav: '#/l/' + lib, lib: lib, refresh: load };
  });
})();
