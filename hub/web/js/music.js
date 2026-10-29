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
  M.albumMenu = function (itemId, title) {
    H.menu(title, [
      { label: 'Lire l’album', value: 'play' }, { label: 'Lecture aléatoire', value: 'shuffle' },
      { label: 'Jouer ensuite', value: 'next' }, { label: 'Ajouter à la file d’attente', value: 'end' }, { label: 'Ajouter à une playlist…', value: 'list' }
    ], null, function (v) {
      if (v === 'play') M.playItem(itemId);
      else if (v === 'shuffle') M.playItem(itemId, true);
      else if (v === 'next' || v === 'end') M.queueItem(itemId, v, title);
      else M.tracksOf(itemId).then(function (list) { M.addToPlaylist(list.map(function (t) { return t.id; }), title); }).catch(fail);
    });
  };
  // `extra` : actions propres à l'endroit où l'on est (dans une playlist : monter, descendre, retirer) : [{label, value, run}]
  M.trackMenu = function (track, extra) {
    var options = [{ label: 'Jouer ensuite', value: 'next' }, { label: 'Ajouter à la file d’attente', value: 'end' }, { label: 'Ajouter à une playlist…', value: 'list' }].concat(extra || []);
    H.menu(track.title, options, null, function (v) {
      if (v === 'next' || v === 'end') queueTracks([track], v, track.title);
      else if (v === 'list') M.addToPlaylist([track.id], track.title);
      else (extra || []).forEach(function (x) { if (x.value === v) x.run(); });
    });
  };

  // --- Le bouton « lecture » posé sur une pochette (souris et doigt) -------------------
  M.cardTools = function (it) {
    function stop(e) { e.preventDefault(); e.stopPropagation(); }
    return h('div', { class: 'qa' },
      h('button', { class: 'qbtn', tabindex: '-1', 'aria-label': 'Lire ' + it.title, title: 'Lire', onclick: function (e) { stop(e); M.playItem(it.id); } }, H.icon('play')),
      h('button', { class: 'qbtn', tabindex: '-1', 'aria-label': 'Plus d’actions', title: 'Plus d’actions', onclick: function (e) { stop(e); M.albumMenu(it.id, it.title); } }, H.icon('more')));
  };
  // Touche « lecture / pause » de la télécommande sur une pochette : lance l'album
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'MediaPlayPause' && e.key !== 'MediaPlay' && e.keyCode !== 179) return;
    var a = document.activeElement;
    if (!a || !a.classList || !a.classList.contains('card') || a.getAttribute('data-type') !== 'album' || H.overlay.isOpen()) return;
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
  H.route(/^\/artist\/([\w-]+)\/(\w+)$/, function (m) {
    var lib = m[1], id = m[2], data = null, stamp = 0;
    var el = h('section', { class: 'detail artist', 'data-lib': lib });
    var picker = h('input', { type: 'file', accept: 'image/jpeg,image/png,image/webp', hidden: true, 'aria-hidden': 'true', tabindex: '-1',
      onchange: function () { var file = this.files && this.files[0]; this.value = ''; if (file) upload(file); } });
    function upload(file) {
      if (file.size > 10 * 1024 * 1024) { H.toast('Image trop grosse (10 Mo au plus).'); return; }
      H.api('/api/artist/' + lib + '/' + id + '/photo', file, { raw: true }).then(function () { stamp = Date.now(); H.toast('Photo enregistrée'); return load(); }).catch(fail);
    }
    function removeOwn() {
      H.menu('Retirer la photo envoyée ?', [{ label: 'Retirer ma photo', detail: 'Retour à celle de Wikipédia, ou à la pochette', value: 'yes' }, { label: 'Garder', value: 'no' }], 'no', function (v) {
        if (v === 'yes') H.api('/api/artist/' + lib + '/' + id + '/photo/delete', {}).then(function () { stamp = Date.now(); H.toast('Photo retirée'); return load(); }).catch(fail);
      });
    }
    function load() {
      return H.api('/api/artist/' + lib + '/' + id).then(function (d) { data = d; render(); }).catch(function (e) {
        H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, 'Artiste introuvable'), h('p', null, e.message), h('a', { class: 'btn f', href: '#/l/' + lib, 'data-autofocus': '1' }, 'Retour à la musique')));
      });
    }
    function render() {
      var d = data, info = d.info || null, list = d.tracks;
      var focusKey = document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
      H.clear(el);
      var meta = [H.plural(d.albums.length, 'album'), H.plural(list.length, 'piste'), d.duration ? H.dur(d.duration) : ''].filter(Boolean).join(' · ');
      var box = h('div', { class: 'info' }, h('h1', null, d.name));
      if (info && info.facts) box.appendChild(h('p', { class: 'meta' }, info.facts));
      var tags = [info && info.disambiguation ? info.disambiguation : ''].concat(d.genres || []).filter(Boolean).join(', ');
      if (tags) box.appendChild(h('p', { class: 'genres' }, tags));
      box.appendChild(h('p', { class: info && info.facts ? 'credit' : 'meta' }, meta));
      if (info && info.wiki) box.appendChild(h('p', { class: 'synopsis' }, info.wiki.extract, ' ', h('a', { class: 'src', href: info.wiki.url, target: '_blank', rel: 'noopener noreferrer' }, 'Wikipédia')));
      box.appendChild(h('div', { class: 'actions' },
        H.button('Tout lire', 'play', { class: 'primary', 'data-autofocus': '1', disabled: !list.length, onclick: function () { H.audio.play(list, 0); } }),
        H.button('Aléatoire', 'shuffle', { disabled: !list.length, onclick: function () { H.audio.play(list, 0, true); } }),
        H.button('Jouer ensuite', null, { disabled: !list.length, onclick: function () { queueTracks(list, 'next', d.name); } }),
        H.button('À la file', 'queue', { disabled: !list.length, onclick: function () { queueTracks(list, 'end', d.name); } }),
        H.button('Changer la photo', 'edit', { 'data-key': 'photo', onclick: function () { picker.click(); } }),
        d.own_photo ? H.button('Retirer ma photo', 'trash', { 'data-key': 'unphoto', onclick: removeOwn }) : null));
      var first = d.albums[0], face = d.photo ? '/img/artist/' + lib + '/' + id : '/img/item/' + first.id, v = stamp ? '&v=' + stamp : '';       // la photo de l'artiste, sinon la pochette de son premier album
      el.appendChild(picker);
      el.appendChild(h('div', { class: 'hero' }, h('div', { class: 'hero-bg', style: 'background-image:url(' + face + '?w=320' + v + ')' }),
        h('div', { class: 'hero-in' }, H.picture(face + '?w=480' + v, d.name, 'sq big'), box)));

      var body = h('div', { class: 'body' });
      body.appendChild(h('section', null, h('h2', null, 'Albums'), h('div', { class: 'grid' }, d.albums.map(function (a) { return H.card(a, false); }))));
      if (info && info.members && info.members.length) {
        body.appendChild(h('section', { class: 'cast' }, h('h2', null, 'Membres'), h('ul', null, info.members.map(function (p) {
          return h('li', null, h('b', null, p.name), [p.roles.join(', '), p.period].filter(Boolean).length ? h('span', null, [p.roles.join(', '), p.period].filter(Boolean).join(' · ')) : null);
        }))));
      }
      if (info && info.links && info.links.length) {
        body.appendChild(h('section', { class: 'links-out' }, h('h2', null, 'Liens'), h('div', { class: 'actions' }, info.links.map(function (l) {
          return h('a', { class: 'btn f', href: l.url, target: '_blank', rel: 'noopener noreferrer' }, l.label);
        }))));
      }
      if (!info) body.appendChild(h('p', { class: 'muted' }, 'Pas encore de fiche : lance « Fiches artistes » dans Gestion pour récupérer le résumé, les membres et les liens.'));
      el.appendChild(body);
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
      var actions = h('div', { class: 'actions' },
        H.button('Lire', 'play', { class: 'primary', 'data-autofocus': '1', disabled: !list.length, onclick: function () { H.audio.play(list, 0); } }),
        H.button('Aléatoire', 'shuffle', { disabled: !list.length, onclick: function () { H.audio.play(list, 0, true); } }),
        H.button('Jouer ensuite', null, { disabled: !list.length, onclick: function () { queueTracks(list, 'next', d.name); } }),
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
