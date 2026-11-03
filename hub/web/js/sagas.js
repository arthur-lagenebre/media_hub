/* Les sagas composées à la main (administrateur) : la liste, une saga, le choix des films, et « Ajouter à une saga » depuis la fiche d'un film.
   Elles vivent dans sagas.json côté serveur (voir docs/api.md) ; chaque changement est envoyé aussitôt, comme pour les playlists. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;
  var S = H.sagas = {};

  function fail(e) { H.toast(e && e.message ? e.message : 'Impossible pour le moment.', 5000); }
  function editHref(name) { return '#/sagas?g=' + encodeURIComponent(name); }
  // Un film absent de la médiathèque porte le titre que l'enrichissement TMDB a trouvé (« Sagas (TMDB) » dans Gestion), sinon rien.
  function filmTitle(f) { return f.item ? f.item.title : (f.title || 'Film absent de la médiathèque'); }
  var NEW = {};                       // la valeur de « Nouvelle saga… » dans un menu : aucun nom de saga ne peut l'égaler
  function inLibrary(saga) { return saga.films.filter(function (f) { return f.item; }).length; }
  function count(saga) {
    var here = inLibrary(saga);
    return H.plural(saga.films.length, 'film') + (here < saga.films.length ? ' · ' + here + ' dans la médiathèque' : '');
  }
  // Ce que le serveur attend pour tout remplacer : les identifiants TMDB, dans l'ordre.
  function payload(sagas, version) {
    return { version: version, sagas: sagas.map(function (s) { return { name: s.name, films: s.films.map(function (f) { return f.tmdb; }) }; }) };
  }
  function copy(sagas) { return sagas.map(function (s) { return { name: s.name, films: s.films.slice() }; }); }
  function swap(list, i, j) { var x = list[i]; list[i] = list[j]; list[j] = x; }
  // Du plus ancien au plus récent ; un film sans date (absent de la médiathèque) passe après les autres, et les égalités gardent leur ordre (le tri des vieux navigateurs n'est pas stable).
  function byDate(list) {
    return list.map(function (f, i) { return { f: f, i: i }; }).sort(function (a, b) {
      var x = a.f.date, y = b.f.date;
      if (x !== y) return !x ? 1 : (!y ? -1 : (x < y ? -1 : 1));
      return a.i - b.i;
    }).map(function (e) { return e.f; });
  }

  // --- Choisir des films -------------------------------------------------------------
  // `has(tmdb)` : le film est-il déjà dans la saga ? `add(films)` : les ajoute (promesse) ; la fenêtre reste ouverte pour en ajouter d'autres ; `onClose` : à sa fermeture.
  // « Tout ajouter » ne vaut que pour le résultat d'une recherche écrite (jamais pour la liste du début, ni pour celle d'une recherche d'avant) : une touche Entrée de trop ne verse pas 40 films dans la saga.
  function pickFilms(title, has, add, onClose) {
    var found = [], total = 0, seq = 0, timer = null, busy = false, ready = false, first = true;
    var tv = H.device === 'tv';
    var input = h('input', { class: 'f field big', type: 'search', placeholder: 'Titre ou saga…', maxlength: '80', 'aria-label': 'Chercher un film', autocomplete: 'off',
      'data-autofocus': tv ? null : '1',
      oninput: function () { ready = false; marks(); clearTimeout(timer); timer = setTimeout(search, 250); },
      onkeydown: function (e) {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        input.blur();                                                  // le clavier à l'écran se ferme ; le focus passe au premier résultat
        var one = list.querySelector('.opt');
        if (one) H.focus.set(one, true);
      } });
    var list = h('div', { class: 'menu-list picklist' });
    var every = H.button('Tout ajouter', 'plus', { 'data-key': 'pick-all', disabled: true, onclick: function () { run(missing()); } });
    var close = H.button('Fermer', null, { 'data-key': 'pick-close', 'data-autofocus': tv ? '1' : null, onclick: function () { H.overlay.close(); } });
    var box = h('div', { class: 'menu form picker', role: 'dialog', 'aria-label': title }, h('h3', null, title), input, list, h('div', { class: 'btns' }, every, close));
    box.addEventListener('focusin', function (e) { try { e.target.scrollIntoView({ block: 'nearest' }); } catch (x) { /* tant pis */ } });      // la liste défile jusqu'au résultat atteint à la télécommande
    function missing() { return byDate(found.filter(function (f) { return !has(f.tmdb); })); }   // dans l'ordre de sortie : une saga entière se range d'elle-même
    function marks() {
      Array.prototype.forEach.call(list.querySelectorAll('[data-tmdb]'), function (b) {
        var on = has(Number(b.getAttribute('data-tmdb')));
        b.classList.toggle('on', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        H.clear(b.querySelector('.mark'));
        if (on) b.querySelector('.mark').appendChild(H.icon('check'));
      });
      var n = missing().length;
      every.querySelector('.lbl').textContent = n > 1 ? 'Tout ajouter (' + n + ')' : 'Tout ajouter';
      every.disabled = !(ready && n > 1);
      if (every.disabled && document.activeElement === every) H.focus.first(box);          // un bouton désactivé qui garde le focus bloquerait les flèches sur certains navigateurs de télé
    }
    function run(films) {
      if (!films.length) return;
      if (busy) { H.toast('Un instant…'); return; }
      busy = true;
      add(films).then(function () { busy = false; marks(); }, function (e) { busy = false; fail(e); });
    }
    function draw() {
      H.clear(list);
      found.forEach(function (f) {
        list.appendChild(h('button', { class: 'opt f', 'data-tmdb': f.tmdb, 'data-key': 'p' + f.tmdb, onclick: function () {
          if (has(f.tmdb)) H.toast('Déjà dans la saga : ' + filmTitle(f)); else run([f]);
        } }, h('span', { class: 'mark' }), h('span', { class: 'lbl' }, filmTitle(f)), f.item && f.item.year ? h('span', { class: 'det' }, f.item.year) : null));
      });
      if (!found.length) list.appendChild(h('p', { class: 'muted' }, 'Aucun film trouvé. Seuls les films étiquetés par Metadata.py (identifiant TMDB) peuvent entrer dans une saga.'));
      else if (total > found.length) list.appendChild(h('p', { class: 'muted' }, 'Les ' + found.length + ' premiers sur ' + total + ' : précise la recherche.'));
      marks();
    }
    function search() {
      var mine = ++seq;
      H.api('/api/sagas/films?limit=40&q=' + encodeURIComponent(input.value.replace(/\s+/g, ' ').trim())).then(function (res) {
        if (mine !== seq) return;
        found = res.films; total = res.total; ready = input.value.trim() !== ''; draw();
        if (first) {
          first = false;
          var one = list.querySelector('.opt');
          if (tv && one && document.activeElement === close) H.focus.set(one, true);               // à la télécommande : sur le premier résultat, pas sur « Fermer »
        }
      }).catch(function (e) { if (mine === seq) fail(e); });
    }
    H.overlay.open(box, function () { clearTimeout(timer); seq++; if (onClose) onClose(); });
    if (H.device !== 'tv') setTimeout(function () { try { input.focus(); } catch (e) { /* tant pis */ } }, 30);
    search();
  }

  // --- La liste, et une saga -----------------------------------------------------------
  H.route(/^\/sagas$/, function (m, query) {
    var name = query.g || '', data = null, busy = false;
    var el = h('section', { class: 'admin sagaedit' });
    function load() {
      return H.api('/api/sagas').then(function (d) { data = d; render(); }).catch(function (e) {
        H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, 'Sagas'), h('p', null, e.status === 403 ? 'Les sagas se composent depuis un compte administrateur.' : e.message),
          h('a', { class: 'btn f', href: '#/settings?tab=gestion', 'data-autofocus': '1' }, 'Retour à la gestion')));
      });
    }
    function find(n) { for (var i = 0; i < data.sagas.length; i++) if (data.sagas[i].name === n) return i; return -1; }
    // Tout remplacer d'un coup, avec la version lue : si un autre est passé par là, le serveur refuse (409) et on relit.
    function commit(sagas, focusKey, then, leaving) {
      if (busy) { H.toast('Un instant…'); return; }
      busy = true;
      H.api('/api/sagas', payload(sagas, data.version)).then(function (d) {
        busy = false; data = d;
        if (!leaving) render(focusKey);                                // `leaving` : on change de page juste après, inutile de redessiner celle-ci
        if (then) then();
      }).catch(function (e) { busy = false; fail(e); if (e.status === 409) load(); });
    }
    function render(focusKey) {
      focusKey = focusKey || (document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null);
      H.clear(el);
      var i = name ? find(name) : -1;
      if (name && i < 0) {
        el.appendChild(h('div', { class: 'blank' }, h('h1', null, 'Saga introuvable'), h('p', null, '« ' + name + ' » n’est pas (ou plus) dans tes sagas.'),
          h('a', { class: 'btn f', href: '#/sagas', 'data-autofocus': '1' }, 'Toutes les sagas')));
        if (!H.overlay.isOpen()) H.focus.first();
        return;
      }
      if (name) showSaga(i); else showList();
      if (H.overlay.isOpen()) return;                                 // une fenêtre est ouverte (choix de films) : on ne lui prend pas le focus
      if (!document.body.contains(el) || el.style.display === 'none') return;       // une réponse tardive, la page a été quittée : on ne vole pas le focus de la page courante
      if (!(focusKey && H.focus.key(el, focusKey, true))) H.focus.first();
    }

    function show(node) { if (node) el.appendChild(node); }
    // Le fichier existe mais le serveur ne sait pas le relire : il refuse d'y écrire (409) tant qu'on ne l'a pas corrigé.
    function warning() {
      return data.damaged ? h('p', { class: 'warn lead', role: 'alert' }, 'Le fichier sagas.json n’est pas lisible (JSON invalide ?) : corrige-le, ou supprime-le, sur le serveur. En attendant, rien ne peut être modifié ici.') : null;
    }

    // La liste des sagas
    function showList() {
      el.appendChild(h('h1', null, 'Sagas'));
      show(warning());
      el.appendChild(h('p', { class: 'muted lead' }, 'Une saga est une liste de films dans l’ordre de ton choix : une trilogie que TMDB ne réunit pas, l’ordre du MCU… Elle s’ajoute à la saga de l’étiquette du fichier. ' +
        'Quand un film est dans plusieurs sagas, la grille le range dans la première de cette liste : monte celle que tu veux voir en vignette.'));
      el.appendChild(h('div', { class: 'tools' }, H.button('Nouvelle saga', 'plus', { class: 'primary', 'data-autofocus': '1', 'data-key': 'new', onclick: create }),
        H.button('Retour à la gestion', 'back', { onclick: function () { H.go('#/settings?tab=gestion'); } })));
      if (!data.sagas.length) el.appendChild(h('p', { class: 'muted lead' }, 'Aucune saga pour l’instant.'));
      var panel = h('section', { class: 'panel' });
      data.sagas.forEach(function (s, k) {
        panel.appendChild(h('div', { class: 'line', 'data-saga': k }, h('div', { class: 'what' }, h('b', null, s.name), h('span', null, count(s))),
          h('a', { class: 'btn f', href: editHref(s.name), 'data-key': 'e' + k, 'aria-label': 'Modifier la saga ' + s.name }, H.icon('edit'), h('span', { class: 'lbl' }, 'Modifier')),
          H.button('', 'up', { class: 'ico', 'aria-label': 'Monter la saga ' + s.name, title: 'Monter', 'data-key': 'u' + k, disabled: k === 0, onclick: function () { move(k, -1); } }),
          H.button('', 'down', { class: 'ico', 'aria-label': 'Descendre la saga ' + s.name, title: 'Descendre', 'data-key': 'd' + k, disabled: k === data.sagas.length - 1, onclick: function () { move(k, 1); } })));
      });
      if (data.sagas.length) el.appendChild(panel);
      hidden();
    }
    // Les films qu'on a demandé de ne plus voir comme manquants : de quoi les rétablir.
    function hidden() {
      var ignored = data.ignored || [];
      if (!ignored.length) return;
      var panel = h('section', { class: 'panel' }, h('h2', null, 'Films cachés'),
        h('p', { class: 'muted' }, 'La collection TMDB de leur saga les compte, mais ils ne sont pas montrés comme manquants.'));
      ignored.forEach(function (f) {
        var title = f.title || 'Film TMDB ' + f.tmdb;
        panel.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, title), h('span', null, f.year || '')),
          H.button('Afficher à nouveau', 'refresh', { 'data-key': 'show-' + f.tmdb, 'aria-label': 'Afficher à nouveau ' + title + ' comme manquant', onclick: function () { unhide(f); } })));
      });
      el.appendChild(panel);
    }
    function unhide(f) {
      if (busy) { H.toast('Un instant…'); return; }
      busy = true;
      H.api('/api/sagas/ignore', { tmdb: f.tmdb, ignore: false }).then(function (d) { busy = false; data = d; render('new'); }).catch(function (e) { busy = false; fail(e); });
    }
    function create() {
      H.prompt('Nom de la nouvelle saga', '', 'Créer', function (n) {
        var next = copy(data.sagas);
        next.push({ name: n, films: [] });
        commit(next, null, function () { H.go(editHref(n)); });
      });
    }
    function move(k, dir) {
      var next = copy(data.sagas), to = k + dir;
      swap(next, k, to);
      var again = dir < 0 ? (to > 0 ? 'u' : 'd') : (to < next.length - 1 ? 'd' : 'u');      // le même bouton, sauf au bout de la liste (il y est désactivé)
      commit(next, again + to);
    }

    // Les films d'une saga
    function showSaga(i) {
      var saga = data.sagas[i], films = saga.films;
      function edit(mutate, focusKey, then) { var next = copy(data.sagas); mutate(next[i].films); commit(next, focusKey, then); }
      el.appendChild(h('h1', null, saga.name));
      show(warning());
      el.appendChild(h('p', { class: 'muted lead' }, count(saga) + '. Le numéro de chaque film est sa place dans la liste : en laissant un film absent à sa place, ceux qui suivent gardent leur numéro.'));
      el.appendChild(h('div', { class: 'tools' },
        H.button('Ajouter des films', 'plus', { class: 'primary', 'data-autofocus': '1', 'data-key': 'add', onclick: addFilms }),
        H.button('Trier par date de sortie', 'refresh', { 'data-key': 'sort', disabled: films.length < 2, title: films.some(function (f) { return !f.item && !f.date; }) ? 'Les films absents dont la date est inconnue passent à la fin : « Sagas (TMDB) » dans Gestion les date.' : 'Du plus ancien au plus récent.',
          onclick: function () { edit(function (list) { var sorted = byDate(list); list.length = 0; Array.prototype.push.apply(list, sorted); }, 'sort'); } }),
        H.button('Renommer', 'edit', { 'data-key': 'rename', onclick: rename }),
        H.button('Supprimer la saga', 'trash', { 'data-key': 'delete', onclick: remove }),
        H.button('Toutes les sagas', 'back', { onclick: function () { H.go('#/sagas'); } })));
      if (!films.length) { el.appendChild(h('p', { class: 'muted lead' }, 'Cette saga est vide : ajoute-lui des films.')); return; }
      var panel = h('section', { class: 'panel' });
      films.forEach(function (f, j) {
        var title = filmTitle(f), last = j === films.length - 1;
        panel.appendChild(h('div', { class: 'line sfilm' + (f.item ? '' : ' absent'), 'data-film': f.tmdb },
          h('span', { class: 'no' }, j + 1),
          h('div', { class: 'what' }, h('b', null, title), h('span', null, f.item ? [f.item.year, f.item.grp].filter(Boolean).join(' · ') : (f.title ? [f.year, 'absent de la médiathèque'].filter(Boolean).join(' · ') : 'TMDB ' + f.tmdb))),
          H.button('', 'up', { class: 'ico', 'aria-label': 'Monter ' + title, title: 'Monter', 'data-key': 'u' + j, disabled: j === 0, onclick: function () {
            edit(function (list) { swap(list, j, j - 1); }, j - 1 > 0 ? 'u' + (j - 1) : 'd' + (j - 1));
          } }),
          H.button('', 'down', { class: 'ico', 'aria-label': 'Descendre ' + title, title: 'Descendre', 'data-key': 'd' + j, disabled: last, onclick: function () {
            edit(function (list) { swap(list, j, j + 1); }, j + 1 < films.length - 1 ? 'd' + (j + 1) : 'u' + (j + 1));
          } }),
          H.button('', 'trash', { class: 'ico', 'aria-label': 'Retirer ' + title + ' de la saga', title: 'Retirer de la saga', 'data-key': 'r' + j, onclick: function () {
            edit(function (list) { list.splice(j, 1); }, films.length > 1 ? 'r' + Math.min(j, films.length - 2) : 'add');
          } })));
      });
      el.appendChild(panel);
    }
    function addFilms() {
      var n = name;
      pickFilms('Ajouter à « ' + n + ' »', function (tmdb) {
        var i = find(n);
        return i >= 0 && data.sagas[i].films.some(function (f) { return f.tmdb === tmdb; });
      }, function (films) {
        return H.api('/api/sagas/add', { name: n, films: films.map(function (f) { return f.tmdb; }) }).then(function (d) { data = d; render(); });
      }, function () { H.focus.key(el, 'add', true); });                  // la page a été redessinée derrière la fenêtre : son bouton d'origine n'existe plus, le focus y revient
    }
    function rename() {
      H.prompt('Renommer la saga', name, 'Renommer', function (n) {
        if (n === name) return;                                          // même nom : rien à envoyer, rien à changer de page
        var next = copy(data.sagas);
        next[find(name)].name = n;
        commit(next, null, function () { H.go(editHref(n), true); }, true);
      });
    }
    function remove() {
      H.menu('Supprimer « ' + name + ' » ?', [{ label: 'Supprimer la saga', detail: 'Les films ne sont pas touchés', value: 'yes' }, { label: 'Garder', value: 'no' }], 'no', function (v) {
        if (v !== 'yes') return;
        var next = copy(data.sagas);
        next.splice(find(name), 1);
        commit(next, null, function () { H.toast('Saga supprimée'); H.back('#/sagas'); }, true);          // la liste, d'où l'on vient, se relit seule en réapparaissant
      });
    }

    load();
    return { el: el, nav: '#/settings', refresh: load };
  });

  // --- « Ajouter à une saga » (fiche d'un film) ---------------------------------------------
  // `d` : la fiche du film (/api/item) ; `done` est appelé une fois le film ajouté.
  S.addTo = function (d, done) {
    function add(name) {
      H.api('/api/sagas/add', { name: name, items: [d.id] }).then(function (r) { H.toast('Ajouté à « ' + r.name + ' »'); if (done) done(); }).catch(fail);
    }
    H.api('/api/sagas').then(function (res) {
      var options = [{ label: 'Nouvelle saga…', value: NEW }].concat(res.sagas.map(function (s) {
        var has = s.films.some(function (f) { return f.item && f.item.id === d.id; });
        return { label: s.name, detail: has ? 'Le film y est déjà' : count(s), value: s.name, disabled: has };
      }));
      H.menu('Ajouter « ' + d.title + ' » à une saga', options, null, function (v) {
        if (v === NEW) H.prompt('Nom de la nouvelle saga', '', 'Créer', add); else add(v);
      });
    }).catch(fail);
  };
})();
