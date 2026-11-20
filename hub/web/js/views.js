/* Les pages : accueil, bibliothèque, fiche, recherche, paramètres (l'administration est dans admin.js). */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;

  H.libs = [];
  H.lib = function (id) { return H.libs.filter(function (l) { return l.id === id; })[0] || null; };

  // --- Le nom d'une bibliothèque -------------------------------------------------------
  // L'identifiant (`films`) ne change jamais ; le libellé affiché (`label`) est celui de la configuration (`default_label`, « Séries ») ou, si l'administrateur l'a changé, son nom à lui.
  // Seul le nom d'origine se traduit (« Séries » → « TV shows ») ; un nom choisi vient de la personne et s'écrit tel quel (même s'il est, par hasard, une clé du dictionnaire : « Paramètres »).
  // /api/state donne `default_label` ; /api/home, /api/search, /api/person n'en ont pas : on le retrouve par l'identifiant dans H.libs (le nom d'origine ne change jamais).
  function originalName(lib) {
    if (typeof lib.default_label === 'string') return lib.default_label;
    var known = lib.id ? H.lib(lib.id) : null;
    return known && typeof known.default_label === 'string' ? known.default_label : null;
  }
  // Vrai si ce nom a été choisi (ce n'est pas celui d'origine). Faux quand on ne peut pas le savoir.
  H.libRenamed = function (lib) { var d = lib ? originalName(lib) : null; return d !== null && lib.label !== d; };
  // LE point de passage de tout libellé de bibliothèque affiché : traduit le nom d'origine, rend un nom choisi tel quel (et, sans moyen de savoir, tel quel aussi : mieux vaut du français que le texte d'un autre).
  H.libLabel = function (lib) {
    if (!lib) return '';
    var d = originalName(lib);
    return d !== null && lib.label === d ? H.tr(lib.label) : lib.label;
  };
  // Ce texte est-il le nom choisi d'une bibliothèque ? i18n.js s'en sert pour ne pas traduire un nom choisi cité dans un message du serveur (« Mise à jour · Feuilletons »).
  H.isChosenLabel = function (text) { return H.libs.some(function (l) { return l.label === text && H.libRenamed(l); }); };
  // Remplace la liste des bibliothèques (relue de /api/state). Si un nom a changé : la barre du haut est refaite et les pages gardées en réserve sont jetées (Retour les refait avec le bon nom).
  function libsKey(list) { return JSON.stringify(list.map(function (l) { return [l.id, l.label]; })); }
  H.setLibs = function (list) {
    var changed = H.libs.length > 0 && libsKey(list) !== libsKey(H.libs);
    H.libs = list;
    if (!changed) return;
    H.nav.build();
    var top = H.current();
    H.nav.mark(top ? (top.view.nav || top.hash) : '#/');
    H.dropParked();
  };

  // --- Barre du haut ----------------------------------------------------------
  // Le compte peut déposer des fichiers dans la boîte de réception (droit « Envoyer »). Jamais sur une télévision : choisir des fichiers à la télécommande ne marche pas.
  H.canUpload = function () { var me = H.session.user; return !!(me && me.can_upload) && H.device !== 'tv'; };
  // Rédiger une suggestion ou y répondre (un texte long) : pas sur une télévision non plus. Écrire à la télécommande est pénible et, dans le champ de texte de H.form, les flèches déplacent le focus, pas le curseur.
  H.canSuggest = function () { return H.device !== 'tv'; };
  H.nav = {
    build: function () {
      var nav = H.clear(document.getElementById('nav'));
      nav.appendChild(h('button', { class: 'tool f back', 'aria-label': H.t('Retour'), title: H.t('Retour'), hidden: 'hidden', onclick: function () { H.back('#/'); } }, H.icon('back')));
      nav.appendChild(h('a', { class: 'brand f', href: '#/', 'data-nav': '#/' }, H.icon('home'), h('span', { class: 'name' }, H.t('Médiathèque'))));
      var links = h('nav', { class: 'links' });
      H.libs.forEach(function (l) {
        var name = H.libLabel(l);
        links.appendChild(h('a', { class: 'link f', href: '#/l/' + l.id, 'data-nav': '#/l/' + l.id, 'data-lib': l.id, title: H.libRenamed(l) ? name : null }, h('i', { class: 'dot' }), h('span', { class: 'txt' }, name)));
      });
      nav.appendChild(links);
      nav.appendChild(h('a', { class: 'tool f', href: '#/search', 'data-nav': '#/search', 'aria-label': H.t('Recherche') }, H.icon('search')));
      if (H.canUpload()) nav.appendChild(h('a', { class: 'tool f', href: '#/upload', 'data-nav': '#/upload', 'aria-label': H.t('Envoyer'), title: H.t('Envoyer') }, H.icon('upload')));
      if (H.isAdmin()) nav.appendChild(h('a', { class: 'tool f', href: '#/admin', 'data-nav': '#/admin', 'aria-label': H.t('Administration'), title: H.t('Administration') }, H.icon('admin')));
      nav.appendChild(h('button', { class: 'tool f bell', 'aria-label': H.t('Notifications'), title: H.t('Notifications'), onclick: H.openNotifications }, H.icon('bell')));
      nav.appendChild(h('a', { class: 'tool f', href: '#/settings', 'data-nav': '#/settings', 'aria-label': H.t('Paramètres'), title: H.t('Paramètres') }, H.icon('gear')));
      H.nav.pending();
    },
    // Les demandes en attente et les suggestions pas encore traitées, additionnées en pastille sur l'entrée Administration : /api/state n'en donne le nombre qu'à l'administrateur (0 pour les autres, qui n'ont pas cette entrée).
    pending: function () {
      var admin = document.querySelector('#nav [data-nav="#/admin"]');
      if (!admin) return;
      var asked = H.session.pending || 0, told = H.session.suggestions || 0, n = asked + told, old = admin.querySelector('.pend');
      if (old) admin.removeChild(old);
      if (n > 0) admin.appendChild(h('span', { class: 'pend', 'aria-hidden': 'true' }, String(n)));
      var parts = [asked ? H.plural(asked, 'demande') : '', told ? H.plural(told, 'suggestion') : ''].filter(Boolean);
      admin.setAttribute('aria-label', n > 0 ? H.t('Administration · {n} en attente', { n: parts.join(', ') }) : H.t('Administration'));
    },
    // Après une relecture de /api/state : la barre suit un droit « Envoyer » ou un rôle d'administrateur donné ou retiré ailleurs, sans recharger, et la pastille le nombre de demandes.
    sync: function () {
      var upload = !!document.querySelector('#nav [data-nav="#/upload"]'), admin = !!document.querySelector('#nav [data-nav="#/admin"]');
      if (H.canUpload() !== upload || H.isAdmin() !== admin) {
        H.nav.build();
        var top = H.current();
        H.nav.mark(top ? (top.view.nav || top.hash) : (location.hash || '#/'));
      } else H.nav.pending();
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

  // --- Nouveautés du hub et centre de notifications -----------------------------------
  function newsDate(text) {          // « 2026-11-17 » dans la langue de l'interface ; illisible : tel quel
    var p = String(text).split('-'), d = new Date(+p[0], +p[1] - 1, +p[2]);
    return p.length === 3 && !isNaN(d.getTime()) ? H.date(d.getTime() / 1000) : text;
  }
  // Une version du journal. C'est un arrêt de la télécommande (sans action) : sans lui, un texte plus haut que l'écran ne défilerait pas aux flèches.
  H.newsEntry = function (v) {
    return h('article', { class: 'news f', tabindex: '0', 'data-key': 'news-' + v.version },
      h('h3', null, h('b', null, v.version), ' ', h('span', null, newsDate(v.date))),
      h('ul', null, (v.items || []).map(function (text) { return h('li', null, text); })));
  };
  // `versions` : le journal, la plus récente d'abord ; null si la route a échoué.
  function showNotifications(versions) {
    var latest = versions && versions[0], btns = h('div', { class: 'btns' });
    if (latest) btns.appendChild(H.button(H.t('Voir toutes les nouveautés'), null, { class: 'primary', onclick: function () {
      H.overlay.close(function () { H.go('#/settings?tab=apropos'); });          // après la fermeture : l'entrée d'historique de la fenêtre est retirée, Retour ramène à la page d'où l'on vient
    } }));
    btns.appendChild(H.button(H.t('Fermer'), null, { onclick: function () { H.overlay.close(); } }));
    var body = latest ? [h('h4', null, H.t('Nouveautés du hub')), H.newsEntry(latest)]
      : [h('p', { class: 'lead' }, versions ? H.t('Aucune notification pour le moment.') : H.t('Impossible de charger les notifications.'))];
    var box = H.box('wide notifs', H.t('Notifications'), body, btns);
    H.overlay.open(box);
  }
  // La cloche : une seule demande à la fois, et une réponse arrivée après un changement de page n'ouvre rien (elle tomberait sur un film en cours).
  var notifsAsked = false;
  H.openNotifications = function () {
    if (notifsAsked) return;
    notifsAsked = true;
    var from = location.hash;
    H.api('/api/changelog').then(function (d) { return (d && d.versions) || []; }).catch(function () { return null; }).then(function (versions) {
      notifsAsked = false;
      if (location.hash === from && !H.overlay.isOpen()) showNotifications(versions);
    });
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
    else if (it.type === 'show') { if (it.year) parts.push(it.year); parts.push(H.t('{n} ép.', { n: it.n })); }
    else if (it.type === 'series') { parts.push(H.plural(it.n, 'volume')); if (it.missing) parts.push(H.plural(it.missing, 'manquant')); }
    else { if (it.year) parts.push(it.year); }
    return parts.join(' · ');
  }
  function sagaHref(libId, name) { return '#/saga/' + libId + '?g=' + encodeURIComponent(name); }
  function personHref(name) { return '#/person?n=' + encodeURIComponent(name); }
  // La ligne des genres d'une fiche (élément ou artiste) : « a, b, c » comme avant, chaque genre ouvrant la bibliothèque `libId` filtrée sur lui (la valeur exacte du filtre du serveur), ou null sans genre.
  H.genreLine = function (libId, genres) {
    var list = (genres || []).filter(Boolean);
    if (!list.length) return null;
    var line = h('p', { class: 'genres' });
    list.forEach(function (genre, i) {
      if (i) line.appendChild(document.createTextNode(', '));
      line.appendChild(h('a', { class: 'f who', href: '#/l/' + libId + '?genre=' + encodeURIComponent(genre), 'data-key': 'genre-' + genre }, genre));
    });
    return line;
  };
  // Les couvertures empilées d'une carte : au plus STACK_MAX (le serveur n'en envoie pas davantage, voir scan/common.py).
  var STACK_MAX = 3;
  // Les affiches d'une saga ou d'une série, empilées en léger décalé : la première devant, les suivantes qui dépassent derrière.
  // `urls` : les adresses des images (le `cover_urls` du serveur) ; `width` : la largeur demandée.
  H.stack = function (urls, title, width) {
    var box = h('div', { class: 'stack n' + Math.min(STACK_MAX, urls.length) });
    urls.slice(0, STACK_MAX).reverse().forEach(function (url, i, list) {
      var depth = list.length - 1 - i;       // 0 = devant
      var pic = H.picture(url + (url.indexOf('?') < 0 ? '?' : '&') + 'w=' + (width || 300), title + depth);
      pic.setAttribute('style', 'left:' + depth * 8 + '%;top:' + (16 - depth * 5.33).toFixed(2) + '%;z-index:' + (STACK_MAX - depth));
      box.appendChild(pic);
    });
    return box;
  };
  function itemCover(id) { return '/img/item/' + id; }
  // La carte à pile d'affiches d'une saga ou d'une série de plusieurs tomes : la pile avec son badge de lecture, le titre, le sous-titre. `cls` en fait la variante (saga, multi).
  function stackedCard(it, cls, href, covers, sub) {
    var a = h('a', { class: 'card ' + cls + ' f', href: href, 'data-key': it.id, title: it.title });
    var stack = H.stack(covers, it.title);
    if (it.seen >= it.n && it.seen > 0) stack.appendChild(h('span', { class: 'badge done' }, H.icon('check')));
    else if (it.seen || it.started) stack.appendChild(h('span', { class: 'badge' }, (it.seen || 0) + '/' + it.n));
    a.appendChild(stack);
    a.appendChild(h('div', { class: 't' }, it.title));
    a.appendChild(h('div', { class: 's' }, sub));
    return a;
  }
  function sagaCard(it) {
    var years = it.years && it.years.length ? (it.years[0] === it.years[1] ? it.years[0] : it.years[0] + '–' + it.years[1]) : '';
    var covers = it.cover_urls && it.cover_urls.length ? it.cover_urls : (it.covers && it.covers.length ? it.covers : [it.id]).map(itemCover);
    var a = stackedCard(it, 'saga', sagaHref(it.lib, it.title), covers, [H.plural(it.n, 'film'), years].filter(Boolean).join(' · '));
    a.querySelector('.stack').appendChild(H.actions.tools(it));
    H.actions.bind(a, it);
    return a;
  }
  // --- Demander un film absent (voir docs/api.md, « Demandes ») ------------------------------------
  // Une demande n'est qu'une liste d'attente que l'administrateur lit : le hub ne télécharge rien. `request` d'une case grisée : {status, mine}, absent quand personne ne l'a demandé.
  H.canRequest = function () { return !!H.session.canRequest; };
  H.canSearchRequests = function () { return !!H.session.requestSearch; };          // chercher dans TMDB un titre à demander : le droit, et une clé TMDB sur le serveur
  // Ce que montre la case selon la demande : `line` (l'état, en toutes lettres), `pill` (ce que fait Entrée), `act` (« ask » : demander, « withdraw » : retirer la sienne) ; sans `act`, rien à faire.
  var ASK = {
    none: { pill: H.t('Demander'), act: 'ask' },
    mine: { pill: H.t('Demandé'), act: 'withdraw' },
    other: { line: H.t('Déjà demandé'), pill: H.t('Demander aussi'), act: 'ask' },        // un autre compte l'a demandé : ni qui, ni combien
    declined: { line: H.t('Refusé') },
    added: { line: H.t('Ajouté') }                                                       // arrivé, ou marqué ajouté : rien à faire tant que la page n'est pas rechargée
  };
  function askState(r) {
    if (!r) return 'none';
    if (r.status === 'added' || r.status === 'declined') return r.status;
    return r.mine ? 'mine' : 'other';
  }
  function askLabel(state, title) {
    if (state === 'mine') return H.t('Demandé : « {title} ». Retirer ta demande', { title: title });
    if (state === 'other') return H.t('Déjà demandé : « {title} ». Demander aussi', { title: title });
    return H.t('Demander « {title} »', { title: title });
  }
  // La fenêtre « Demander » (un form : le message facultatif), pour un film ou une série (`m.kind` : « show » ; un film sans `kind`). Le refus du serveur (plafond, refusée, déjà là) reste dans la fenêtre ;
  // un titre déjà dans la médiathèque propose de l'ouvrir. `done(demande)` reçoit la réponse du serveur une fois la fenêtre fermée.
  function askFor(m, done) {
    var found = null, show = m.kind === 'show';
    var open = H.button(H.t('Ouvrir la fiche'), 'play', { 'data-key': 'req-open', hidden: true, onclick: function () { var id = found; H.overlay.close(function () { H.go('#/i/' + id); }); } });
    H.form({ title: H.t('Demander « {title} »', { title: m.title }), ok: H.t('Demander'),
      lead: [show ? H.t('L’administrateur verra ta demande et ajoutera la série s’il le peut. Rien n’est téléchargé tout seul.') : H.t('L’administrateur verra ta demande et ajoutera le film s’il le peut. Rien n’est téléchargé tout seul.')],
      fields: [{ key: 'note', label: H.t('Un mot pour l’administrateur (facultatif, 200 caractères au plus)'), maxlength: 200 }],
      extra: [open],
      submit: function (v) {
        open.hidden = true;
        var body = { kind: show ? 'show' : 'movie', tmdb: m.tmdb, title: m.title };
        if (v.note) body.note = v.note;
        if (/^\d{4}$/.test(String(m.year || ''))) body.year = String(m.year);
        return H.api('/api/requests', body).catch(function (e) {
          found = e.data && typeof e.data.item === 'string' ? e.data.item : null;
          open.hidden = !found;
          if (found) H.focus.set(open, true);          // à la télécommande : Entrée ouvre la fiche
          throw e;
        });
      },
      done: done });
  }
  // Une demande déjà retirée ou supprimée ailleurs (404 « demande inconnue ») : le but est atteint, la fenêtre se ferme et la case ou la liste se refait.
  function goneAlready(e) { if (e && e.status === 404) return null; throw e; }
  // Retirer sa demande (un confirm, Annuler par défaut). La case porte l'identifiant de sa propre demande (`request.id`) ; s'il manque (un serveur plus ancien), on le retrouve dans « Mes demandes ».
  function withdrawFrom(m, done) {
    H.confirm({ title: H.t('Retirer ta demande ?'), lead: [H.t('« {title} » ne sera plus dans tes demandes.', { title: m.title })], ok: H.t('Retirer'), danger: true,
      submit: function () {
        var id = m.request && m.request.id;
        if (typeof id === 'number') return H.api('/api/requests/' + id + '/delete', {}).catch(goneAlready);
        return H.api('/api/requests?mine=1').then(function (res) {
          var mine = (res.requests || []).filter(function (r) { return r.kind === (m.kind || 'movie') && r.tmdb === m.tmdb; })[0];
          return mine ? H.api('/api/requests/' + mine.id + '/delete', {}).catch(goneAlready) : null;            // déjà retirée (un autre appareil) : rien à faire
        });
      },
      done: done });
  }

  // Un film qui manque à une saga ou à une personne : son affiche grisée, rien à ouvrir ni à lire. Si le compte peut demander, la case se demande :
  // elle entre dans la navigation à la télécommande (Entrée ouvre « Demander », ou « Retirer ta demande ? ») ; sinon, ou quand il n'y a rien à faire (refusé, ajouté, pas encore sorti), ni focus ni clic.
  // `opts.hide` (administrateur, film que seule la collection TMDB compte) : un bouton « Ne pas afficher » sous la case.
  // Sans `rank`, pas de pastille de rang (les films d'une personne ne se suivent pas) ; `note` remplace « Manquant » sous le titre (son rôle : « Acteur »).
  function missingCard(m, opts) {
    opts = opts || {};
    var pic = H.picture(m.cover ? m.cover + '?w=300' : '', m.title, '');
    if (m.rank) pic.appendChild(h('span', { class: 'part' }, m.rank));
    var state = m.coming ? H.t('pas encore sorti') : 'manquant';           // un film annoncé est grisé comme les autres, mais il ne manque à personne
    var asked = H.canRequest() && !m.coming && m.tmdb ? askState(m.request) : null, look = asked ? ASK[asked] : null, act = look && look.act, outer;
    function changed(request) {           // la case se refait sur place avec le nouvel état ; le focus y reste
      if (request) m.request = request; else delete m.request;
      if (!outer.parentNode) return;
      var fresh = missingCard(m, opts);
      outer.parentNode.replaceChild(fresh, outer);
      var target = fresh.classList.contains('card') ? fresh : fresh.querySelector('.card');
      if (target && target.classList.contains('f')) H.focus.set(target, true);
    }
    var card = h('div', { class: 'card wanted' + (act ? ' f ask' : ''), title: m.title + ' (' + state + ')', 'data-key': act ? 'req-' + m.tmdb : null, role: act ? 'button' : null, tabindex: act ? '0' : null,
      'aria-label': act ? askLabel(asked, m.title) : null,
      onclick: act ? function () {
        if (act === 'withdraw') withdrawFrom(m, function () { H.toast(H.t('Demande retirée : {title}', { title: m.title })); changed(null); });
        else askFor(m, function (res) { H.toast(H.t('Demandé : {title}', { title: m.title })); changed({ status: res.status, mine: true, id: res.id }); });
      } : null },
      pic, h('div', { class: 't' }, m.title), h('div', { class: 's' }, [m.year, m.note || (m.coming ? H.t('À venir') : H.t('Manquant'))].filter(Boolean).join(' · ')),
      look ? h('div', { class: 'rq' }, look.line ? h('span', { class: 'rqstate' }, look.line) : null,
        look.pill ? h('span', { class: 'rqbtn' + (act === 'withdraw' ? ' on' : ''), 'aria-hidden': 'true' }, H.icon(act === 'withdraw' ? 'check' : 'plus'), h('span', null, look.pill)) : null) : null);
    outer = opts.hide && m.ignorable ? h('div', { class: 'mcell' }, card, H.button(H.t('Ne pas afficher'), 'close', { class: 'mhide', 'data-key': 'hide-' + m.tmdb, 'aria-label': H.t('Ne pas afficher {title} comme manquant', { title: m.title }), onclick: function () { opts.hide(m); } })) : card;
    return outer;
  }
  // Combien de ces films manquent vraiment : ceux qui ne sont pas encore sortis ne comptent pas.
  function lacking(absent) { return (absent || []).filter(function (m) { return !m.coming; }).length; }
  // Les films qu'on a et ceux qui manquent, rangés ensemble par leur rang dans la saga ; `card(film)` fait la case d'un film qu'on a.
  function sagaCells(films, absent, card, opts) {
    var cells = films.map(function (f, i) { return { rank: f.part === undefined || f.part === null ? i + 1 : f.part, node: card(f) }; });
    (absent || []).forEach(function (m) { cells.push({ rank: m.rank, node: missingCard(m, opts) }); });
    cells.sort(function (a, b) { return a.rank - b.rank; });
    return cells.map(function (c) { return c.node; });
  }
  // La carte d'un artiste (vue « Artistes » de la musique) : sa photo, à défaut la pochette de son plus ancien album, à défaut une lettre ; un clic ouvre sa fiche.
  function artistCard(it) {
    var pic = H.picture('', it.title, 'sq'), fallback = false;
    pic.appendChild(h('img', { src: '/img/artist/' + it.lib + '/' + it.id + '?w=300', alt: '', loading: 'lazy', decoding: 'async', onload: function () { this.classList.add('in'); }, onerror: function () {
      if (fallback || !it.album) { if (this.parentNode) this.parentNode.removeChild(this); return; }
      fallback = true;
      this.src = '/img/item/' + it.album + '?w=300';
    } }));
    return h('a', { class: 'card f sq', href: '#/artist/' + it.lib + '/' + it.id, 'data-key': it.id, 'data-type': 'artist', title: it.title }, pic, h('div', { class: 't' }, it.title), h('div', { class: 's' }, H.plural(it.n, 'album')));
  }
  // La note TMDB comme TMDB la montre (« User Score ») : un disque sombre, le pourcentage au centre, un anneau qui se remplit d'autant,
  // vert à partir de 70 %, jaune à partir de 40 %, rouge en dessous. L'anneau est un SVG (stroke-dasharray sur un cercle de périmètre 100) :
  // conic-gradient manque aux vieux navigateurs de télé. Le serveur ne donne la note qu'au-delà de 25 votes : « votes » toujours au pluriel.
  // Rien dans le SVG ne vient d'ailleurs que de ce nombre, borné et arrondi : il peut passer par `html`.
  H.scoreRing = function (it, big) {
    var pct = Math.max(0, Math.min(100, Math.round(Number(it.tmdb_rating) * 10)));
    var tone = pct >= 70 ? 'hi' : pct >= 40 ? 'mid' : 'lo';
    var label = H.t('Note des utilisateurs TMDB : {score} % ({votes} votes)', { score: pct, votes: H.number(it.tmdb_votes) });
    return h('span', { class: 'score ' + tone + (big ? ' big' : '') + (pct === 100 ? ' c3' : ''), role: 'img', 'aria-label': label, title: label,
      html: '<svg viewBox="0 0 36 36" aria-hidden="true"><circle class="tr" cx="18" cy="18" r="15.9155"/><circle class="val" cx="18" cy="18" r="15.9155" stroke-dasharray="' + pct + ' 100"/></svg>' +
            '<span class="v" aria-hidden="true">' + pct + '<sup>%</sup></span>' });
  };
  // La qualité d'image d'un fichier, d'une saison ou d'un film (`quality`, `hdr` : voir docs/api.md) : ['4K', 'HDR'], ['Full HD'], ou [] quand l'API n'en dit rien.
  // Une valeur que ce client ne connaît pas s'affiche en majuscules. La fiche en fait son premier groupe de badges, la vignette d'un film son cartouche.
  var QUALITY = { '4k': '4K', fullhd: 'Full HD', hd: 'HD', sd: 'SD' };
  H.pictureTags = function (b) {
    var out = [];
    if (typeof b.quality === 'string' && b.quality) out.push(QUALITY[b.quality] || b.quality.toUpperCase());
    if (b.hdr === true) out.push('HDR');
    return out;
  };
  // Les pastilles d'une affiche de film, de série ou d'album, en haut à droite : l'avis du compte (une icône, ni bouton ni focus) prend la place de la coche « Vu » ;
  // l'avancement d'un élément en cours (« En cours », « 10/25 ») reste toujours, sous l'avis quand il y en a un.
  function cardMarks(pic, it) {
    var total = it.n || 1, done = it.seen >= total && it.seen > 0, rated = it.type === 'movie' || it.type === 'show' ? H.ratingOf(it.rating) : null;
    if (rated) pic.appendChild(h('span', { class: 'badge mine', 'data-rating': rated.value, role: 'img', 'aria-label': rated.label, title: rated.label }, H.icon(rated.icon)));
    if (done) { if (!rated) pic.appendChild(h('span', { class: 'badge done' }, H.icon('check'))); }
    else if (it.seen || it.started) pic.appendChild(h('span', { class: 'badge' }, it.type === 'movie' ? H.t('En cours') : (it.seen || 0) + '/' + total));
  }
  // L'avis d'un élément a changé (`value` : « up », « mid », « down », ou rien) : toutes ses cartes à l'écran, dans toutes les pages gardées, repeignent leurs pastilles sans recharger la liste.
  H.rated = function (id, value) {
    Array.prototype.forEach.call(document.querySelectorAll('.card'), function (card) {
      var it = card.hubItem, pic = it && it.id === id ? card.querySelector('.poster') : null;
      if (!pic) return;
      if (value) it.rating = value; else delete it.rating;
      Array.prototype.forEach.call(pic.querySelectorAll('.badge'), function (b) { pic.removeChild(b); });
      cardMarks(pic, it);
    });
    ratedOutOfPage(id, value);
    if (value === 'down') Array.prototype.forEach.call(document.querySelectorAll(SUGGESTED_CARDS), function (card) { if (card.hubItem && card.hubItem.id === id) dropCard(card); });
  };
  // Les cartes qu'un rayon a coupées ne sont pas dans la page : l'avis se note sur leur élément, leur carte sera refaite si elle revient, et « Je n'aime pas » les retire des rayons qui suggèrent.
  function ratedOutOfPage(id, value) {
    Array.prototype.forEach.call(document.querySelectorAll('.grid'), function (grid) {
      if (!grid.hubEntries) return;
      var leaves = value === 'down' && grid.closest('.shelf.unseen, .shelf.recommend');
      grid.hubEntries = grid.hubEntries.filter(function (e) {
        if (e.item.id !== id) return true;
        if (value) e.item.rating = value; else delete e.item.rating;
        var cut = !e.card || !e.card.parentNode;
        if (cut) e.card = null;
        return !(leaves && cut);
      });
    });
  }
  // Une carte notée « Je n'aime pas » quitte les rayons qui suggèrent (« Pas encore vu », « Parce que tu as aimé ») sans recharger la page ; un rayon vidé disparaît.
  // Si elle avait le focus, il passe à sa voisine (la suivante, sinon la précédente), sinon au rayon d'après (ou d'avant), sinon au premier élément de la page : jamais dans le vide.
  // Le dernier rayon parti, l'accueil se redessine (`hubRedraw`) : d'autres suggestions, ou les bibliothèques en tuiles, la première avec le focus ; jamais « Bonjour » seul.
  var SUGGESTED_CARDS = '.shelf.unseen .card, .shelf.recommend .card';
  function firstIn(node) { return node.classList && node.classList.contains('f') ? node : node.querySelector && node.querySelector('.f'); }
  function nearShelf(shelf) {
    var x;
    for (x = shelf.nextElementSibling; x; x = x.nextElementSibling) if (!x.hidden && firstIn(x)) return firstIn(x);
    for (x = shelf.previousElementSibling; x; x = x.previousElementSibling) if (!x.hidden && firstIn(x)) return firstIn(x);
    return null;
  }
  function dropCard(card) {
    var grid = card.parentNode, shelf = card.closest('.shelf'), page = shelf.parentNode, had = card.contains(document.activeElement);
    var at = Array.prototype.indexOf.call(grid.children, card);
    grid.hubEntries = grid.hubEntries.filter(function (e) { return e.card !== card; });
    grid.removeChild(card);
    if (page && page.hubFit) page.hubFit();          // la carte suivante du rayon prend la place
    var next = grid.children[at] || grid.children[at - 1];
    if (!next) {
      next = nearShelf(shelf);
      if (page) page.removeChild(shelf);
      if (page && page.hubRedraw && !page.querySelector('.shelf:not([hidden]), .libtiles')) { page.hubRedraw(); return; }
    }
    if (had && !H.focus.set(next)) H.focus.first();
  }
  H.card = function (it, withGroup, withPart) {
    if (it.type === 'saga') return sagaCard(it);
    if (it.type === 'artist') return artistCard(it);
    if (it.type === 'series' && it.cover_urls && it.cover_urls.length > 1) {         // une série de plusieurs tomes : la pile d'une saga, mais un clic ouvre sa fiche
      var multi = stackedCard(it, 'multi', '#/i/' + it.id, it.cover_urls, itemSub(it));
      multi.setAttribute('data-type', it.type);
      return multi;
    }
    var square = it.type === 'album';
    var a = h('a', { class: 'card f' + (square ? ' sq' : ''), href: '#/i/' + it.id, 'data-key': it.id, 'data-type': it.type, title: it.title });
    var pic = H.picture('/img/item/' + it.id + '?w=300', it.title, square ? 'sq' : '');
    cardMarks(pic, it);
    if (square && H.music) pic.appendChild(H.music.cardTools(it));
    if (it.type === 'movie' || it.type === 'show') { pic.appendChild(H.actions.tools(it)); H.actions.bind(a, it); a.hubItem = it; }      // `hubItem` : de quoi repeindre ses pastilles quand l'avis change (H.rated)
    var numbered = withPart && it.part !== undefined && it.part !== null;
    if (numbered) pic.appendChild(h('span', { class: 'part' }, it.part));
    var picture = it.type === 'movie' ? H.pictureTags(it) : [];                       // la qualité d'un film (voir docs/api.md), sous son numéro dans une saga ; ni langues, ni focus
    if (picture.length) pic.appendChild(h('span', { class: 'qual' + (numbered ? ' below' : '') }, picture.join(' ')));
    if (it.tmdb_rating) pic.appendChild(H.scoreRing(it));
    a.appendChild(pic);
    a.appendChild(h('div', { class: 't' }, it.title));
    a.appendChild(h('div', { class: 's' }, (withGroup && it.grp ? H.tr(it.grp) + ' · ' : '') + itemSub(it)));
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
  H.playHref = playHref;
  // `onRemove(card)` : appelée une fois la carte retirée de « Reprendre » (clic sur ✕, clic droit / appui long, touche Suppr ou Menu)
  // La ligne « S1 · E2 · Titre » (série), « Tome 3 » (livre), ou le titre de l'épisode ; rien pour un film.
  function resumeLabel(c) {
    if (c.item.type === 'show') return 'S' + (c.season || 1) + ' · E' + (c.number || '?') + (c.title ? ' · ' + c.title : '');
    if (c.kind === 'book') return c.label ? H.t('Tome {n}', { n: c.label }) : c.title;
    return c.item.type === 'movie' ? '' : c.title;
  }
  // Où on en est : « À 12:30 », « Page 4 », ou ce qui vient ensuite.
  function resumeNote(c) {
    if (c.resume) return c.kind === 'book' ? H.t('Page {n}', { n: Math.floor(c.position) + 1 }) : H.t('À {time}', { time: H.time(c.position) });
    return c.kind === 'book' ? H.t('Volume suivant') : H.t('Épisode suivant');
  }
  // La vignette : l'image du fichier, à défaut celle de l'élément, à défaut une lettre ; une barre dit où on en est.
  function resumePicture(c) {
    var ratio = c.total ? Math.min(1, c.position / c.total) : 0;
    var pic = h('div', { class: 'poster wide' }, h('span', { class: 'ph', style: '--h:' + H.hue(c.item.title) }, initial(c.item.title)));
    var tryItem = function () { this.onerror = function () { if (this.parentNode) this.parentNode.removeChild(this); }; this.src = '/img/item/' + c.item.id + '?w=480'; };
    pic.appendChild(h('img', { src: '/img/entry/' + c.id + '?w=480', alt: '', loading: 'lazy', onload: function () { this.classList.add('in'); }, onerror: tryItem }));
    if (c.resume && ratio > 0) pic.appendChild(h('div', { class: 'pbar' }, h('i', { style: 'width:' + Math.round(ratio * 100) + '%' })));
    return pic;
  }
  function resumeCard(c, onRemove) {
    var pic = resumePicture(c), card;
    function fail(e) { H.toast(e && e.message ? e.message : H.t('Impossible pour le moment.')); }
    // Garder (rien ne change), Retirer de Reprendre (la progression est gardée : l'élément y revient dès qu'on le relance), Remettre à zéro (la progression de tout l'élément est effacée).
    function remove() {
      H.menu(H.t('Que faire de « {title} » ?', { title: c.item.title }), [
        { label: H.t('Garder'), detail: H.t('Rien ne change'), value: 'keep' },
        { label: H.t('Retirer de Reprendre'), detail: H.t('La progression est gardée'), value: 'hide' },
        { label: H.t('Remettre à zéro'), detail: H.t('La progression est effacée'), value: 'reset' }], 'keep', function (v) {
        if (v === 'hide') H.api('/api/item/' + c.item.id + '/dismiss', {}).then(function () { H.toast(H.t('Retiré de Reprendre')); onRemove(card); }).catch(fail);
        if (v === 'reset') H.api('/api/item/' + c.item.id + '/seen', { done: false }).then(function () { H.toast(H.t('Remis à zéro')); onRemove(card); }).catch(fail);
      });
    }
    card = h('a', { class: 'card wide f', href: playHref(c), 'data-key': 'c' + c.id,
      oncontextmenu: function (e) { e.preventDefault(); remove(); },
      onkeydown: function (e) { if (e.key === 'Delete' || e.key === 'ContextMenu' || e.keyCode === 46 || e.keyCode === 93) { e.preventDefault(); remove(); } } },
      pic, h('div', { class: 't' }, c.item.title), h('div', { class: 's' }, [resumeLabel(c), resumeNote(c)].filter(Boolean).join(' · ')));
    pic.appendChild(h('div', { class: 'qa' }, h('button', { class: 'qbtn', tabindex: '-1', 'aria-label': H.t('Retirer ou remettre à zéro'), title: H.t('Retirer ou remettre à zéro'),
      onclick: function (e) { e.preventDefault(); e.stopPropagation(); remove(); } }, H.icon('close'))));
    return card;
  }

  // --- Accueil -------------------------------------------------------------------
  // Les derniers ajouts de toutes les bibliothèques, mêlés, du plus récent au plus ancien (à date égale, l'ordre des bibliothèques puis celui de leur liste) ; chacun garde sa bibliothèque.
  // `n` : au plus ce nombre en tout (sans `n`, tout : c'est la coupe du rayon qui décide). Le serveur a déjà fait ce qui est propre à une bibliothèque : en musique, l'arrivée d'un artiste n'est qu'une carte (voir recent_music).
  H.latest = function (libraries, n) {
    var all = [];
    libraries.forEach(function (lib, i) { lib.recent.forEach(function (it, k) { all.push({ item: it, lib: lib, at: it.added || 0, order: i * 1000 + k }); }); });
    all.sort(function (a, b) { return b.at - a.at || a.order - b.order; });
    return all.slice(0, n).map(function (x) { return { item: x.item, lib: x.lib }; });
  };
  // La carte d'un ajout récent : la carte ordinaire, avec devant sa ligne sous le titre un point de la couleur de sa bibliothèque (« ● 2010 », « ● Artiste · 2010 »).
  function latestCard(entry) {
    var card = H.card(entry.item, entry.lib.kind === 'music'), sub = card.querySelector('.s');
    card.setAttribute('data-lib', entry.lib.id);
    if (sub) sub.insertBefore(h('i', { class: 'dot' }), sub.firstChild);
    return card;
  }
  // Les rayons coupés en rangées : chacun montre colonnes × `home_shelf_rows` cartes, et une dernière rangée incomplète s'il n'a pas de quoi la remplir.
  // Un rayon, même ajouté plus tard, n'a qu'à faire sa grille avec `cutGrid` : la coupe, la recoupe et le focus suivent. « Reprendre » et « Tes demandes sont arrivées » n'y passent pas : on les voit en entier.
  // Une carte n'est fabriquée que la première fois qu'elle se montre (les autres ne demandent pas leur image), puis gardée pour revenir ; une carte coupée n'est pas dans la page : ni le clavier ni la télécommande ne s'y posent.
  var SHELF_ROWS = 2;
  function cutGrid(entries) {          // `entries` : [{item, make}], `make()` fabrique la carte de `item`
    var grid = h('div', { class: 'grid' });
    grid.hubEntries = entries;
    return grid;
  }
  function plainEntries(items) {
    return items.map(function (it) { return { item: it, make: function () { return H.card(it, false); } }; });
  }
  // Les colonnes de la grille telle qu'elle est mise en page, lues et non recalculées : elles dépendent de la largeur, de la taille du texte et de l'appareil. 0 : la grille n'est pas affichée (la page est en réserve).
  function columnsOf(grid) {
    if (!grid.offsetWidth) return 0;
    var tracks = getComputedStyle(grid).gridTemplateColumns;
    return tracks && tracks !== 'none' ? tracks.trim().split(/\s+/).length : 1;
  }
  // La carte qui avait le focus vient d'être coupée : il passe à celle qui prend sa place, sinon à la dernière du rayon, sinon au rayon voisin.
  // Une fenêtre ouverte depuis cette carte (`under`) garde le focus : c'est à sa fermeture qu'il reviendra sur la remplaçante.
  function refocusShelf(grid, at, under) {
    var next = grid.children[Math.min(at, grid.children.length - 1)] || nearShelf(grid.parentNode);
    if (under) under.opener = next;
    else if (!H.focus.set(next)) H.focus.first();
  }
  // Coupe un rayon, sans les éléments de `skip` ; rend ce qu'il montre. Un rayon à qui il ne reste rien se cache, et revient si la coupe des autres lui rend des cartes.
  function fitShelf(grid, skip) {
    var shelf = grid.parentNode;
    shelf.hidden = false;          // un rayon caché n'a pas de colonnes à lire
    var cols = columnsOf(grid);
    if (!cols) return [];
    var kept = grid.hubEntries.filter(function (e) { return !skip[e.item.id]; }).slice(0, cols * (parseInt(H.setting('home_shelf_rows'), 10) || SHELF_ROWS));
    var wanted = kept.map(function (e) { return e.card || (e.card = e.make()); });
    var before = Array.prototype.slice.call(grid.children), at = -1, under = H.overlay.bottom();
    var held = under ? under.opener : document.activeElement;
    before.forEach(function (card, i) { if (card.contains(held)) at = i; });
    before.forEach(function (card) { if (wanted.indexOf(card) < 0) grid.removeChild(card); });
    wanted.forEach(function (card, i) { if (grid.children[i] !== card) grid.insertBefore(card, grid.children[i] || null); });
    shelf.hidden = !wanted.length;
    if (at >= 0 && wanted.indexOf(before[at]) < 0) refocusShelf(grid, at, under);
    return kept;
  }
  function cutGrids(home) {
    return Array.prototype.filter.call(home.querySelectorAll('.shelf .grid'), function (grid) { return grid.hubEntries; });
  }
  // Coupe tous les rayons, dans l'ordre de la page : « Pas encore vu » retire ce que « Nouveautés » montre réellement, une fois coupé.
  function fitShelves(home) {
    var latest = {};
    cutGrids(home).forEach(function (grid) {
      var classes = grid.parentNode.classList, kept = fitShelf(grid, classes.contains('unseen') ? latest : {});
      if (classes.contains('latest')) kept.forEach(function (e) { latest[e.item.id] = true; });
    });
  }
  // Recoupe les rayons quand leurs grilles changent de taille (fenêtre, rotation, taille du texte), une fois par image. `refresh` : les rayons viennent de changer ; `stop` : la page s'en va.
  function shelfWatcher(home) {
    var frame = 0;
    function later() {
      if (!frame) frame = requestAnimationFrame(function () { frame = 0; fitShelves(home); });
    }
    var observer = window.ResizeObserver ? new ResizeObserver(later) : null;
    if (!observer) window.addEventListener('resize', later);          // une vieille télé : la fenêtre seule
    return {
      refresh: function () {
        if (observer) { observer.disconnect(); cutGrids(home).forEach(function (grid) { observer.observe(grid); }); }
        fitShelves(home);
      },
      stop: function () {
        cancelAnimationFrame(frame);
        if (observer) { observer.disconnect(); observer = null; } else window.removeEventListener('resize', later);
      }
    };
  }
  function cardsSection(cls, title, entries) {
    return h('section', { class: 'shelf ' + cls }, h('h2', null, title), cutGrid(entries.map(function (e) { return { item: e.item, make: function () { return latestCard(e); } }; })));
  }
  // Le rayon d'une bibliothèque : son titre est un lien vers elle, précédé du point de sa couleur (comme dans la recherche).
  function libraryShelf(cls, title, lib, items) {
    return h('section', { class: 'shelf ' + cls, 'data-lib': lib.id },
      h('h2', null, h('a', { class: 'f head', href: '#/l/' + lib.id }, title + ' · ' + H.libLabel(lib))),
      cutGrid(plainEntries(items)));
  }
  // « Parce que tu as aimé <titre> » : un rayon par ancre que le serveur propose, en cartes comme les autres ; chaque carte garde son ancre (`because`) pour « Pourquoi cette suggestion ? ».
  function recommendShelf(s) {
    return h('section', { class: 'shelf recommend', 'data-anchor': s.anchor.id },
      h('h2', null, H.t('Parce que tu as aimé {title}', { title: s.anchor.title })),
      cutGrid(plainEntries(s.items.map(function (it) { it.because = s.anchor; return it; }))));
  }
  // Les rayons de suggestions arrivent après l'accueil, sans le retenir : ils se posent devant `spot` (juste après « Reprendre »), seulement si l'accueil qui les a demandés est toujours celui-là (`still()`).
  // Un échec ou une réponse vide ne montre rien. Une carte que la personne a choisie (aux flèches, à la souris, ou après avoir fait défiler) garde le focus et reste en vue si les rayons l'ont poussée vers le bas.
  // Sinon, le focus que l'accueil avait posé de lui-même (`hubAuto`, page encore en haut) passe à la première suggestion, sans défilement : on arrive sur « Bonjour », pas au milieu de la page.
  // Ces rayons sont du contenu comme les autres : sur un accueil qui n'en avait aucun, ils prennent la place de la phrase et des tuiles des bibliothèques (`libraryTiles`), et le focus d'une tuile passe à leur première carte.
  function recommendations(spot, still, fit) {
    H.api('/api/recommendations').then(function (data) {
      var shelves = (data && data.shelves || []).filter(function (s) { return s.anchor && s.items && s.items.length; });
      var home = spot.parentNode;
      if (!shelves.length || !still() || !home) return;
      var a = document.activeElement, untouched = a && a === home.hubAuto && !window.pageYOffset;          // lu avant d'insérer les rayons
      var drawn = shelves.map(function (s) { return home.insertBefore(recommendShelf(s), spot); });
      fit();
      var tiles = home.querySelector('.libtiles'), note = home.querySelector('.libtiles-note');
      var lost = tiles && a && tiles.contains(a);
      if (note) note.parentNode.removeChild(note);
      if (tiles) tiles.parentNode.removeChild(tiles);
      if (home.style.display === 'none' || H.overlay.isOpen()) return;
      var below = a && home.contains(a) && (spot.compareDocumentPosition(a) & 4);
      if (lost) H.focus.set(firstIn(drawn[0]));
      else if (untouched && below) H.focus.set(firstIn(drawn[0]), true);
      else if (below) H.focus.set(a);
    }).catch(function () { /* rien : l'accueil reste tel qu'il est */ });
  }
  // La pochette d'une tuile de bibliothèque : celle de son dernier ajout (pour un artiste, celle de son album), ou rien.
  function tileCover(lib) {
    var last = lib.recent && lib.recent[0];
    if (!last) return '';
    if (last.type === 'artist') return last.album ? '/img/item/' + last.album + '?w=300' : '';
    return '/img/item/' + last.id + '?w=300';
  }
  // Une bibliothèque en tuile : sa pochette (à défaut, la couleur de la bibliothèque, comme le point de ses rayons), son nom et ce qu'elle compte ; un vrai lien, atteignable à la télécommande.
  function libraryTile(lib) {
    var cover = tileCover(lib), thumb = h('span', { class: 'thumb', 'aria-hidden': 'true' });
    if (cover) thumb.appendChild(h('img', { src: cover, alt: '', loading: 'lazy', decoding: 'async', onload: function () { this.classList.add('in'); }, onerror: function () { if (this.parentNode) this.parentNode.removeChild(this); } }));
    return h('a', { class: 'libtile f', href: '#/l/' + lib.id, 'data-lib': lib.id, 'data-key': 'lib-' + lib.id }, thumb,
      h('span', { class: 'what' }, h('b', null, h('i', { class: 'dot' }), H.libLabel(lib)), h('span', null, H.plural(lib.count, 'élément'))));
  }
  // Quand aucun rayon n'est affiché : une phrase (les rayons sont masqués dans les réglages, ou il n'y a rien à proposer), puis les bibliothèques en tuiles ; la première prend le focus.
  function libraryTiles(libraries) {
    var masked = H.setting('home_latest') === 'hide' && H.setting('home_unseen') === 'hide';
    var tiles = libraries.map(libraryTile);
    if (tiles.length) tiles[0].setAttribute('data-autofocus', '1');
    return [h('p', { class: 'libtiles-note muted' }, masked ? H.t('Tu as masqué tous les rayons de l’accueil : Paramètres > Préférences, pour les remettre') : H.t('Rien à reprendre ni à découvrir pour l’instant')),
      h('div', { class: 'libtiles' }, tiles)];
  }
  // « Tes demandes sont arrivées » : les éléments que le compte avait demandés et qui sont là (`requests_ready`), en cartes comme les autres rayons.
  // Ouvrir une carte dit au serveur que la demande est vue (`ack`) : elle quitte le rayon, qui disparaît avec la dernière.
  function readyShelf(list) {
    return h('section', { class: 'shelf ready' }, h('h2', null, H.t('Tes demandes sont arrivées')), h('div', { class: 'grid' }, list.map(function (r) {
      var card = H.card(r.card, false);
      card.setAttribute('data-request', r.id);
      card.addEventListener('click', function (e) {
        if (e.target && e.target.closest && e.target.closest('button')) return;          // le menu « … » de la carte n'ouvre pas l'élément
        H.api('/api/requests/' + r.id + '/ack', {}, { keepalive: true }).catch(function () { /* tant pis : elle reviendra au prochain accueil */ });
      });
      return card;
    })));
  }
  H.route(/^\/$/, function () {
    var el = h('section', { class: 'home' });
    var timer = null, drawn = 0;          // `drawn` : le numéro du dernier dessin de l'accueil (une réponse tardive des suggestions ne se pose que sur celui qui l'a demandée)
    var shelves = shelfWatcher(el);
    el.hubFit = shelves.refresh;          // une carte vient de quitter un rayon (dropCard)
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
            h('h1', null, H.t('Ta médiathèque est vide pour l’instant')),
            h('p', null, !H.isAdmin() ? H.t('Rien n’est encore indexé : l’administrateur doit lancer une mise à jour de la médiathèque.') : H.libs.length ? H.t('Les dossiers sont lus au premier démarrage. Si tu viens d’y ajouter des fichiers, lance une mise à jour.') : H.t('Aucun dossier de bibliothèque trouvé : monte tes dossiers sous /srv (Films, Series, Musique, Livres), ou décris-les avec HUB_LIBRARIES.')),
            H.isAdmin() ? h('a', { class: 'btn f primary', href: '#/admin', 'data-autofocus': '1' }, H.t('Ouvrir l’administration')) : null));
          clearTimeout(timer);
          timer = setTimeout(function () { if (document.body.contains(el) && !document.hidden) load(); }, 4000);
          return;
        }
        var me = H.session.auth && H.session.user && H.session.user.name;
        el.appendChild(h('h1', { class: 'greeting' }, me ? H.t('Bonjour {name}', { name: me }) : H.t('Bonjour')));
        var ready = H.setting('home_requests') === 'hide' ? [] : (data.requests_ready || []);          // masquable comme les autres rayons (le serveur rend alors déjà une liste vide)
        if (ready.length) el.appendChild(readyShelf(ready));
        if (data.continue.length) el.appendChild(row(H.t('Reprendre'), null, data.continue.map(function (c) { return resumeCard(c, dismissed); })));
        var spot = el.appendChild(document.createComment('recommend')), mine = ++drawn;          // la place des rayons « Parce que tu as aimé », qui arrivent plus tard
        var latest = H.setting('home_latest') === 'hide' ? [] : H.latest(data.libraries);          // chaque rayon se masque dans les réglages du compte
        if (latest.length) el.appendChild(cardsSection('latest', H.t('Nouveautés'), latest));
        // « Pas encore vu » : un rayon par bibliothèque que le serveur dit avoir un `unseen` (films, séries… et toute bibliothèque vidéo ajoutée plus tard), dans l'ordre des bibliothèques.
        var unseen = H.setting('home_unseen') === 'hide' ? [] : data.libraries.filter(function (lib) { return lib.unseen && lib.unseen.length; });
        unseen.forEach(function (lib) {
          el.appendChild(libraryShelf('unseen', H.t('Pas encore vu'), lib, lib.unseen));
        });
        if (!data.continue.length && !latest.length && !unseen.length && !ready.length) libraryTiles(data.libraries).forEach(function (node) { el.appendChild(node); });       // aucun rayon : les bibliothèques, pour ne pas laisser une page nue
        shelves.refresh();
        if (H.setting('home_recommend') !== 'hide') recommendations(spot, function () { return mine === drawn && spot.parentNode === el; }, shelves.refresh);       // masqué : la route n'est pas appelée
        if (!H.focus.shown(el)) return;       // une réponse tardive, la page a été quittée : voir `H.focus.shown`
        if (typeof focusKey === 'string' && H.focus.key(el, focusKey)) return;
        var a = document.activeElement;
        if (!a || a === document.body || (a.closest && a.closest('#nav'))) { H.focus.first(); el.hubAuto = document.activeElement; }       // posé par l'accueil, pas par la personne (voir `recommendations`)
      }).catch(function (e) { H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, H.t('Le serveur ne répond pas')), h('p', null, e.message), h('button', { class: 'btn f', onclick: load, 'data-autofocus': '1' }, H.t('Réessayer')))); });
    }
    el.hubRedraw = function () { load(); };          // plus aucun rayon après « Je n'aime pas » (dropCard)
    load();
    return { el: el, nav: '#/', refresh: load, destroy: function () { clearTimeout(timer); shelves.stop(); } };
  });

  // --- Bibliothèque ----------------------------------------------------------------
  // « Genre : Tous » : le nom d'un bouton de filtre en gras, sa valeur en poids normal ; la largeur du bouton est fixée par le CSS (.flt), la valeur trop longue est coupée
  // Les compteurs d'une bibliothèque, séparés par « · » : chacun dans son `span` (insécable, voir style.css), la ligne ne se coupe qu'entre deux.
  function paintCount(count, parts) {
    H.clear(count);
    parts.forEach(function (text, i) { if (i) count.appendChild(document.createTextNode(' · ')); count.appendChild(h('span', null, text)); });
  }
  // « 2 artistes · 1 album · 2 EP · 1 single » : EP et singles ne s'affichent que s'il y en a ; artistes et albums, toujours. Un ancien serveur (sans `eps` ni `singles`) garde « artistes · albums ».
  // « EP » est invariable en français (« 2 EP ») mais pas en anglais (« 2 EPs ») : H.plural ne sait pas dire un pluriel identique au singulier dans une langue seulement.
  function musicCounts(f) {
    var parts = [H.plural(f.artists, 'artiste'), H.plural(f.albums, 'album')];
    if (f.eps) parts.push(H.lang === 'fr' ? f.eps + ' EP' : H.plural(f.eps, 'EP', 'EPs'));
    if (f.singles) parts.push(H.plural(f.singles, 'single'));
    return parts;
  }
  function filterLabel(btn, name, value) { btn.title = H.t('{name} :', { name: name }) + ' ' + value; H.clear(btn).appendChild(h('span', { class: 'lbl' }, [h('b', null, H.t('{name} :', { name: name })), ' ', h('span', { class: 'val' }, value)])); }
  var SORTS = {
    movies: [['title', H.t('Titre')], ['added', H.t('Ajouts récents')], ['year', H.t('Plus récents')], ['year-asc', H.t('Plus anciens')], ['rating', H.t('Mieux notés')], ['my-rating', H.t('Mon avis')]],      // films et séries ; « Mieux notés » : la note TMDB ; « Mon avis » : celui du compte (J'aime, Bof, sans avis, Je n'aime pas)
    books: [['title', H.t('Collection')], ['added', H.t('Ajouts récents')], ['year', H.t('Plus récents')], ['year-asc', H.t('Plus anciens')]]
  };
  // Les choix du statut, accordés d'après le GENRE de la bibliothèque (jamais d'après son nom, qu'on peut changer) : les films au masculin, les séries et les livres (comptés en « séries ») au féminin. [valeur de `filter=`, libellé]
  function stateChoices(kind) {
    var fem = kind === 'shows' || kind === 'books';
    return [['', fem ? H.t('Toutes') : H.t('Tous')], ['todo', H.t('À voir')], ['progress', H.t('En cours')], ['done', fem ? H.t('Terminées') : H.t('Terminés')]];
  }
  H.route(/^\/l\/([\w-]+)$/, function (m, params) {
    var libId = m[1], lib = H.lib(libId);
    var kind = lib ? lib.kind : 'movies';
    var sorts = SORTS[kind] || SORTS.movies;
    var st = { q: '', genre: (params && params.genre) || '', sort: kind === 'music' ? 'title' : (H.pref('sort.' + libId) || 'title'), state: '', section: kind === 'books' ? (H.pref('section.' + libId) || '') : '' };
    var items = [], total = 0, loading = false, token = 0, lastGrp = null, facets = { genres: [], sections: [] };
    function artistMode() { return kind === 'music' && H.setting('music_view') === 'artists'; }      // le choix du compte : les albums sous leurs artistes, ou un artiste par carte
    function sagaMode() { return kind === 'movies' && H.setting('movie_view') !== 'films'; }         // le choix du compte : les films d'une saga sur une carte, ou chacun sur la sienne
    function viewMode() { return artistMode() + '/' + sagaMode(); }
    var shownMode = viewMode();
    var grid = h('div', { class: 'grid' + (kind === 'music' ? ' sq' : '') });
    var sentinel = h('div', { class: 'sentinel' });
    var count = h('span', { class: 'count' });
    var search = h('input', { class: 'f field', type: 'search', placeholder: H.t('Filtrer {name}', { name: lib ? (H.libRenamed(lib) ? H.libLabel(lib) : H.libLabel(lib).toLowerCase()) : '' }), 'aria-label': H.t('Filtrer'),
      oninput: function () { clearTimeout(search.t); search.t = setTimeout(function () { st.q = search.value; reset(); }, 280); },
      onkeydown: function (e) { if (e.key === 'Enter') { e.preventDefault(); search.blur(); H.focus.set(grid.querySelector('.f')); } } });
    var bGenre = H.button('', null, { class: 'flt wide', onclick: pickGenre }), bSort = H.button('', null, { class: 'flt wide', onclick: pickSort });
    // Où en est le compte : une liste comme « Genre » et « Tri » ; sa valeur est celle de `filter=` de l'API, vide pour tout montrer.
    var bState = H.button('', null, { class: 'flt wide', onclick: pickState });
    var bLists = H.button(H.t('Playlists'), 'queue', { onclick: function () { H.go('#/playlists'); } });
    var tools = h('div', { class: 'tools' }, search, bGenre, kind === 'music' ? null : bSort, kind === 'music' ? null : bState, kind === 'music' ? bLists : null, count);
    var quick = h('div', { class: 'chips quick', role: 'group', 'aria-label': H.t('Accès rapides'), hidden: true });
    var empty = h('div', { class: 'blank', hidden: true });
    var el = h('section', { class: 'library' }, h('h1', null, lib ? H.libLabel(lib) : H.t('Bibliothèque')), kind === 'books' ? quick : null, tools, grid, empty, sentinel);

    var label = filterLabel;
    function paint() {
      label(bGenre, H.t('Genre'), st.genre || H.t('Tous'));
      label(bSort, H.t('Tri'), (sorts.filter(function (s) { return s[0] === st.sort; })[0] || sorts[0])[1]);
      label(bState, H.t('Statut'), (stateChoices(kind).filter(function (c) { return c[0] === st.state; })[0] || stateChoices(kind)[0])[1]);
      bGenre.hidden = !facets.genres.length;
      var word = { movies: 'film', shows: 'série', music: artistMode() ? 'artiste' : 'album', books: 'série' }[kind] || 'élément';
      var music = kind === 'music' && typeof facets.albums === 'number';          // la musique compte toujours les artistes et les albums de ce que les filtres laissent, quelle que soit la vue
      paintCount(count, !total ? [] : music ? musicCounts(facets) : sagaMode() && facets.sagas ? [H.plural(facets.films, 'film'), H.plural(facets.sagas, 'saga')] : [H.plural(total, word)]);
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
      [['', H.t('Tout'), all]].concat(list.map(function (x) { return [x[0], H.tr(x[0]), x[1]]; })).forEach(function (x) {
        var on = st.section === x[0];
        quick.appendChild(h('button', { class: 'chip f' + (on ? ' on' : ''), type: 'button', 'data-section': x[0], 'aria-pressed': on ? 'true' : 'false',
          onclick: function () { st.section = x[0]; H.pref('section.' + libId, x[0] || null); reset(); } }, x[1], h('small', null, ' ' + H.number(x[2]))));
      });
      if (focus !== null) { var again = quick.querySelector('[data-section="' + focus.replace(/"/g, '\\"') + '"]'); if (again) H.focus.set(again, true); }
    }
    function pickGenre() {
      var list = [{ label: H.t('Tous les genres'), value: '' }].concat(facets.genres.map(function (g) { return { label: g[0], detail: String(g[1]), value: g[0] }; }));
      H.menu(H.t('Genre'), list, st.genre, function (v) { st.genre = v; reset(); });
    }
    function pickSort() {
      H.menu(H.t('Trier par'), sorts.map(function (s) { return { label: s[1], value: s[0] }; }), st.sort, function (v) { st.sort = v; H.pref('sort.' + libId, v); reset(); });
    }
    function pickState() {
      H.menu(H.t('Statut'), stateChoices(kind).map(function (c) { return { label: c[1], value: c[0] }; }), st.state, function (v) { st.state = v; paint(); reset(); });
    }
    function query(offset) {
      var q = ['offset=' + offset, 'limit=240', 'sort=' + encodeURIComponent(st.sort)];
      if (st.q) q.push('q=' + encodeURIComponent(st.q));
      if (st.genre) q.push('genre=' + encodeURIComponent(st.genre));
      if (st.section) q.push('section=' + encodeURIComponent(st.section));
      if (st.state) q.push('filter=' + st.state);
      if (sagaMode()) q.push('sagas=1');
      if (artistMode()) q.push('view=artists');
      return '/api/library/' + libId + '?' + q.join('&');
    }
    function append(list) {
      var frag = document.createDocumentFragment();
      list.forEach(function (it) {
        if ((kind === 'music' || kind === 'books') && st.sort === 'title' && !artistMode() && it.grp !== lastGrp) {
          lastGrp = it.grp;
          var heading = it.grp ? H.tr(it.grp) : (kind === 'music' ? H.t('Artiste inconnu') : H.t('Sans collection'));
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
        if (first && st.genre && !res.genres.some(function (g) { return g[0] === st.genre; })) { st.genre = ''; loading = false; reset(); return; }     // un genre venu de l'adresse (#/l/films?genre=…) que la bibliothèque n'a pas : on revient à « Tous »
        total = res.total;
        if (first) { facets = res; paintQuick(); }
        append(res.items);
        loading = false;
        paint();
        empty.hidden = items.length > 0;
        if (!items.length) showEmpty();
        if (first && !H.overlay.isOpen() && document.body.contains(el) && el.style.display !== 'none' && document.activeElement !== search && !bState.contains(document.activeElement) && (document.activeElement === document.body || el.contains(document.activeElement))) H.focus.set(grid.querySelector('.card') || search, true);
        if (items.length < total && sentinel.getBoundingClientRect().top < window.innerHeight + 600) fetchMore(false);
      }).catch(function (e) { loading = false; empty.hidden = false; H.clear(empty).appendChild(h('p', null, e.message)); });
    }
    function showEmpty() {
      var filtered = st.q || st.genre || st.state || st.section;
      H.clear(empty).appendChild(h('div', null,
        h('h2', null, filtered ? H.t('Rien ne correspond') : H.t('Cette bibliothèque est vide')),
        h('p', null, filtered ? H.t('Change ou retire un filtre.') : H.t('Vérifie que le dossier est bien monté, puis lance une mise à jour.')),
        filtered ? h('button', { class: 'btn f', onclick: function () { st.q = st.genre = st.section = st.state = ''; search.value = ''; H.pref('section.' + libId, null); reset(); } }, H.t('Retirer les filtres')) : H.isAdmin() ? h('a', { class: 'btn f', href: '#/admin' }, H.t('Ouvrir la gestion')) : null));
    }
    function reset() {
      token++; items = []; total = 0; lastGrp = null; loading = false;
      H.clear(grid);
      fetchMore(true);
    }
    if ('IntersectionObserver' in window) new IntersectionObserver(function (e) { if (e[0].isIntersecting) fetchMore(false); }, { rootMargin: '800px' }).observe(sentinel);
    window.addEventListener('scroll', onScroll, { passive: true });
    function onScroll() { if (document.body.contains(el) && el.style.display !== 'none' && sentinel.getBoundingClientRect().top < window.innerHeight + 800) fetchMore(false); }
    grid.addEventListener('focusin', function (e) {
      var cards = grid.querySelectorAll('.card'), idx = Array.prototype.indexOf.call(cards, e.target);
      if (idx >= 0 && idx > cards.length - 40) fetchMore(false);
    });
    reset();
    paint();
    return {
      el: el, lib: libId, nav: '#/l/' + libId,
      refresh: function () {      // au retour d'une fiche : les pastilles « vu » ont pu changer
        if (viewMode() !== shownMode) { shownMode = viewMode(); reset(); return; }          // le choix de la vue a changé dans les paramètres : on repart de zéro
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
  var SHOW_STATUS = { 'Ended': H.t('Terminée'), 'Returning Series': H.t('En cours'), 'Canceled': H.t('Annulée'), 'In Production': H.t('En production'), 'Planned': H.t('Prévue'), 'Pilot': H.t('Pilote') };
  function entryProgress(e) { return e.progress && !e.progress.done && e.progress.position >= (e.kind === 'book' ? 1 : 20) ? e.progress : null; }
  function epLabel(e) { return 'S' + (e.season === null || e.season === undefined ? 1 : e.season) + ' · E' + (e.number === null || e.number === undefined ? '?' : e.number); }

  H.route(/^\/i\/(\w+)$/, function (m) {
    var id = m[1], el = h('section', { class: 'detail' }), season = null, data = null;
    function load() {
      return H.api('/api/item/' + id).then(function (d) { data = d; render(); }).catch(function (e) {
        H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, H.t('Introuvable')), h('p', null, e.message), h('a', { class: 'btn f', href: '#/', 'data-autofocus': '1' }, H.t('Accueil'))));
      });
    }
    function setSeen(done, seasonNo) { H.api('/api/item/' + id + '/seen', { done: done, season: seasonNo === undefined ? null : seasonNo }).then(load); }

    // Ce que la fiche tire de ses fichiers : où reprendre, la position en cours, et si tout est vu.
    function viewing(d) {
      var entries = d.entries, resume = entries.filter(function (e) { return e.id === d.resume; })[0] || entries[0];
      return { resume: resume, prog: resume ? entryProgress(resume) : null, allDone: entries.length > 0 && entries.every(function (e) { return e.progress && e.progress.done; }) };
    }
    function totalDuration(entries) { return entries.reduce(function (s, e) { return s + (e.duration || 0); }, 0); }

    // Les types de sortie MusicBrainz (RELEASETYPE), dits en français ; un type inconnu s'affiche tel quel, avec une majuscule.
    var RELEASE_TYPES = { album: 'Album', single: 'Single', ep: 'EP', compilation: 'Compilation', live: 'Live', soundtrack: H.t('Bande originale'), remix: 'Remix' };
    function releaseTypes(d) {
      return (d.release_types || []).filter(function (t) { return typeof t === 'string' && t; }).map(function (t) { return RELEASE_TYPES[t] || t.charAt(0).toUpperCase() + t.slice(1); }).join(', ');
    }

    // La ligne grise sous le titre, selon ce qu'est la fiche : l'année, la durée, les saisons, les pistes, les volumes.
    var META = {
      movie: function (d) {
        var meta = [];
        if (d.year) meta.push(d.year);
        var secs = totalDuration(d.entries);
        if (secs) meta.push(H.dur(secs));
        return meta;
      },
      show: function (d) {
        var meta = [];
        if (d.year) meta.push(d.year);
        if (d.seasons && d.seasons.length) meta.push(H.plural(d.seasons.filter(function (s) { return s > 0; }).length || 1, 'saison'));
        meta.push(H.plural(d.entries.length, 'épisode'));
        if (d.status) meta.push(SHOW_STATUS[d.status] || d.status);
        return meta;
      },
      album: function (d) {
        var meta = [];
        if (d.artist && !d.artist_id) meta.push(d.artist);
        if (d.year) meta.push(d.year);
        meta.push(H.plural(d.entries.length, 'piste'));
        var t = totalDuration(d.entries);
        if (t) meta.push(H.dur(t));
        if (d.formats && d.formats.length) meta.push(d.formats.join(' / '));
        meta.push(releaseTypes(d));
        return meta;
      },
      books: function (d) {
        var meta = [d.grp ? H.tr(d.grp) : H.t('Livres'), H.plural(d.entries.length, d.unit || 'volume', d.units)];
        if (d.year) meta.push(d.year);
        return meta;
      }
    };
    function metaLine(d) { return (META[d.type] || META.books)(d).filter(Boolean).join(' · '); }

    // Les badges d'un fichier ou d'une saison (voir docs/api.md) : « 4K HDR · Audio FR EN · Sous-titres EN ». Ce que l'API ne donne pas (fichier pas encore sondé) n'apparaît pas,
    // une liste vide masque son groupe ; rien du tout : null. Ce ne sont que des étiquettes : ni lien, ni bouton, ni focus.
    function tagGroup(label, values) {
      return values.length ? h('span', { class: 'tgrp' }, label ? h('span', { class: 'tlab' }, label) : null, values.map(function (v) { return h('span', { class: 'tag' }, v); })) : null;
    }
    function langCodes(list) { return (Array.isArray(list) ? list : []).filter(function (c) { return typeof c === 'string' && c; }).map(function (c) { return c.toUpperCase(); }); }
    function qualityTags(b) {
      if (!b) return null;
      var groups = [tagGroup('', H.pictureTags(b)), tagGroup(H.t('Audio'), langCodes(b.audio)), tagGroup(H.t('Sous-titres'), langCodes(b.subs))].filter(Boolean);
      if (!groups.length) return null;
      var row = h('div', { class: 'tags' });
      groups.forEach(function (g, i) {
        if (i) row.appendChild(h('span', { class: 'sep', 'aria-hidden': 'true' }, '·'));
        row.appendChild(g);
      });
      return row;
    }
    // Ceux d'un film : le premier de ses fichiers que le serveur a sondé (une version en plusieurs parties a les mêmes).
    function movieTags(d) { return d.type === 'movie' ? qualityTags(d.entries.filter(function (e) { return e.quality || e.audio || e.subs; })[0]) : null; }

    // Un nom de la fiche (réalisateur, acteur) qui ouvre la page de la personne. `kind` distingue le même nom cité deux fois sur la fiche (clé de focus).
    function personLink(name, kind, children) {
      return h('a', { class: 'f who', href: personHref(name), 'data-key': 'who-' + kind + '-' + name }, children || name);
    }
    // « De {names} » : les noms sont des liens, le reste de la phrase (traduit) les entoure.
    function directedBy(d) {
      var slot = '\u0001', around = H.t('De {names}', { names: slot }).split(slot), line = h('p', { class: 'credit' }, around[0]);
      d.directors.forEach(function (name, i) {
        if (i) line.appendChild(document.createTextNode(', '));
        line.appendChild(personLink(name, 'd'));
      });
      line.appendChild(document.createTextNode(around[1] || ''));
      return line;
    }
    // Les lignes « De », « Par » et l'artiste d'un album, sous les genres.
    function credits(d) {
      var out = [];
      if (d.directors && d.directors.length) out.push(directedBy(d));
      if (d.authors && d.authors.length) out.push(h('p', { class: 'credit' }, H.t('Par {names}', { names: d.authors.join(', ') })));
      if (d.type === 'album' && d.artist && d.artist_id) out.push(h('p', { class: 'credit' }, h('a', { class: 'f head', href: '#/artist/' + d.library.id + '/' + d.artist_id }, d.artist)));
      return out;
    }
    function doubtNote(d) {
      return h('p', { class: 'warn doubt', role: 'note' }, H.t('Fiche TMDB à vérifier : {doubts}.', { doubts: d.doubts.map(function (x) { return H.tr(x); }).join(' ; ') }) + (H.device === 'tv' ? '' : ' ' + H.t('Ce n’est pas la bonne série ? « Changer la fiche TMDB ».')));
    }

    // Les boutons de lecture d'un album.
    function albumButtons(d) {
      if (!H.actions.canPlay(d.library && d.library.id)) return [H.button(H.t('Playlist'), 'plus', { 'data-autofocus': '1', onclick: function () { H.music.addToPlaylist(trackList().map(function (t) { return t.id; }), d.title); } })];
      return [
        H.button(H.t('Lire l’album'), 'play', { class: 'primary', 'data-autofocus': '1', onclick: function () { H.audio.play(trackList(), 0); } }),
        H.button(H.t('Aléatoire'), 'shuffle', { onclick: function () { H.audio.play(trackList(), 0, true); } }),
        H.button(H.t('Jouer ensuite'), null, { onclick: function () { H.music.queueTracks(trackList(), 'next', d.title); } }),
        H.button(H.t('À la file'), 'queue', { onclick: function () { H.music.queueTracks(trackList(), 'end', d.title); } }),
        H.button(H.t('Playlist'), 'plus', { onclick: function () { H.music.addToPlaylist(trackList().map(function (t) { return t.id; }), d.title); } })
      ];
    }
    // Les boutons de lecture d'un film, d'une série ou de tomes : lire ou reprendre là où on s'est arrêté, et repartir du début.
    function playButtons(d, view) {
      var resume = view.resume, prog = view.prog;
      if (!resume || !H.actions.canPlay(d.library && d.library.id)) return [];
      var what = d.type === 'show' ? epLabel(resume) : (d.type === 'series' ? H.t('tome {n}', { n: resume.label || resume.number || '' }) : '');
      var main;
      if (prog) {
        var at = resume.kind === 'book' ? Math.floor(prog.position) + 1 : H.time(prog.position);
        if (resume.kind === 'book') main = what ? H.t('Reprendre {what} (page {at})', { what: what, at: at }) : H.t('Reprendre (page {at})', { at: at });
        else main = what ? H.t('Reprendre {what} à {at}', { what: what, at: at }) : H.t('Reprendre à {at}', { at: at });
      } else main = d.type === 'movie' && view.allDone ? H.t('Revoir') : (what ? H.t('Lire {what}', { what: what }) : H.t('Lire'));
      var out = [H.button(main, 'play', { class: 'primary', 'data-autofocus': '1', onclick: function () { H.go(playHref(resume)); } })];
      if (prog) out.push(H.button(H.t('Depuis le début'), 'rew', { onclick: function () { H.go(playHref(resume, 0)); } }));
      if (resume.kind !== 'book' && H.actions.canVlc(d.library && d.library.id)) out.push(H.button(H.t('Ouvrir dans VLC'), 'play', { 'data-key': 'vlc', onclick: function () { H.actions.openInVlc(resume.id); } }));      // PC seulement : ce que « Lire » ou « Reprendre » ouvrirait
      return out;
    }
    // Les autres boutons : vu / non vu, saga, télécharger, affiche, fiche TMDB, liens.
    function otherButtons(d, view) {
      var out = [], entries = d.entries;
      if (d.type !== 'album' && entries.length) out.push(H.button(H.actions.seenLabel(d.type, view.allDone), 'check', { onclick: function () { setSeen(!view.allDone); } }));
      if (d.type === 'movie' && d.tmdb && H.isAdmin() && H.sagas) out.push(H.button(H.t('Ajouter à une saga'), 'plus', { 'data-key': 'saga-add', onclick: function () { H.sagas.addTo(d, load); } }));      // sans identifiant TMDB, un film ne peut pas entrer dans une saga
      if (entries.length && H.actions.canDownload(d.library && d.library.id)) out.push(H.button(d.type === 'album' ? H.t('Télécharger l’album') : H.t('Télécharger'), 'download', { 'data-key': 'download', onclick: function () { H.actions.downloadItem(d); } }));
      out.push(H.button(H.t('Signaler un problème'), 'flag', { 'data-key': 'report', onclick: function () { H.actions.report({ item: d.id, title: d.title }); } }));
      return out.concat(H.actions.posterTools(d, load), H.actions.tmdbTools(d, load),
        (d.links || []).map(function (l) { return h('a', { class: 'btn f', href: l.url, target: '_blank', rel: 'noopener noreferrer' }, H.tr(l.label)); }));
    }
    function actionsRow(d, view) {
      var actions = h('div', { class: 'actions' });
      (d.type === 'album' ? albumButtons(d) : playButtons(d, view)).concat(otherButtons(d, view)).forEach(function (node) { actions.appendChild(node); });
      return actions;
    }

    // « Mon avis » (films et séries entières) : trois boutons à bascule, et « Effacer » quand il y en a un. Un clic sur l'avis déjà choisi l'efface.
    // L'avis est privé : le serveur ne le donne qu'au compte qui l'a posé. Une fois posé, la fiche est relue (le serveur fait foi) et le focus reste sur le bouton.
    function ratingRow(d) {
      if (!H.actions.ratable(d)) return null;
      function rate(value) {
        var was = d.rating;
        H.actions.rate(d, value).then(load).then(function () { H.focus.key(el, 'rate-' + (value || was), true); }).catch(function (e) { H.toast(e && e.message ? e.message : H.t('Impossible pour le moment.'), 5000); });
      }
      var row = h('div', { class: 'myrating', role: 'group', 'aria-label': H.t('Mon avis') }, h('span', { class: 'lab', 'aria-hidden': 'true' }, H.t('Mon avis')));
      H.ratings.forEach(function (r) {
        var on = d.rating === r.value;
        row.appendChild(H.button(r.label, r.icon, { 'data-key': 'rate-' + r.value, 'aria-pressed': on ? 'true' : 'false', onclick: function () { rate(on ? null : r.value); } }));
      });
      if (d.rating) row.appendChild(H.button(H.t('Effacer'), 'close', { 'data-key': 'rate-clear', onclick: function () { rate(null); } }));
      return row;
    }

    // Sous la ligne grise, comme sur TMDB : le disque de la note, et à côté ce qu'il mesure et sur combien de votes.
    function scoreRow(d) {
      return h('div', { class: 'rate' }, H.scoreRing(d, true),
        h('span', { class: 'what', 'aria-hidden': 'true' }, h('b', null, H.t('Note des utilisateurs')), h('small', null, H.t('{n} votes sur TMDB', { n: H.number(d.tmdb_votes) }))));
    }

    // Le texte à droite de l'image : titre, ligne grise, note TMDB, genres, crédits, résumé, doutes TMDB, boutons.
    function infoBlock(d, view) {
      var genres = H.genreLine(d.library.id, d.genres);
      var info = h('div', { class: 'info' }, h('h1', null, d.title), h('p', { class: 'meta' }, metaLine(d)), movieTags(d));
      if (d.tmdb_rating) info.appendChild(scoreRow(d));
      if (genres) info.appendChild(genres);
      credits(d).forEach(function (node) { info.appendChild(node); });
      if (d.summary) info.appendChild(h('p', { class: 'synopsis' }, d.summary));
      if (d.doubts && d.doubts.length && H.isAdmin()) info.appendChild(doubtNote(d));
      info.appendChild(actionsRow(d, view));
      var mine = ratingRow(d);
      if (mine) info.appendChild(mine);
      return info;
    }
    function heroBlock(d, info) {
      return h('div', { class: 'hero' }, h('div', { class: 'hero-bg', style: 'background-image:url(/img/item/' + d.id + '?w=300)' }),
        h('div', { class: 'hero-in' }, H.picture('/img/item/' + d.id + '?w=500', d.title, d.type === 'album' ? 'sq big' : 'big'), info));
    }

    // Sous l'en-tête : la liste propre au type (épisodes, pistes, tomes, parties), la distribution, les sagas.
    function mainSection(d) {
      if (d.type === 'show') return episodes(d);
      if (d.type === 'album') return tracks(d);
      if (d.type === 'series') return volumes(d);
      return d.entries.length > 1 ? parts(d) : null;
    }
    function castSection(d) {
      if (!(d.actors && d.actors.length)) return null;
      return h('section', { class: 'cast' }, h('h2', null, H.t('Distribution')), h('ul', null, d.actors.slice(0, 14).map(function (a) { return h('li', null, personLink(a.name, 'a', [h('b', null, a.name), a.role ? h('span', null, a.role) : null])); })));
    }
    function sagaBoxes(d) {
      return (d.sagas || []).map(function (saga, k) {
        var cards = sagaCells(saga.films, saga.missing, function (s) {
          var c = H.card(s, false, true);
          c.setAttribute('data-key', k + ':' + s.id);                     // le même film peut figurer dans plusieurs sagas de la page
          if (s.id === d.id) c.classList.add('now');
          return c;
        });
        var absent = lacking(saga.missing);
        return h('section', { class: 'sagabox' },
          h('h2', null, h('a', { class: 'f head', href: sagaHref(d.library.id, saga.name) }, saga.name), h('span', { class: 'n' }, H.plural(saga.films.length, 'film') + (absent ? ' · ' + H.plural(absent, 'manquant') : ''))),
          h('div', { class: 'grid small' }, cards));
      });
    }
    function bodyBlock(d) {
      var body = h('div', { class: 'body' });
      [mainSection(d), castSection(d)].concat(sagaBoxes(d)).forEach(function (node) { if (node) body.appendChild(node); });
      return body;
    }

    function render() {
      var d = data, focusKey = document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
      H.clear(el);
      el.setAttribute('data-lib', d.library.id);
      el.appendChild(heroBlock(d, infoBlock(d, viewing(d))));
      el.appendChild(bodyBlock(d));
      if (!H.focus.shown(el)) return;          // une réponse tardive : la page a été quittée
      if (focusKey) H.focus.key(el, focusKey); else H.focus.first();
    }

    function trackList() {
      return data.entries.map(function (e) { return { id: e.id, title: e.title, artist: e.artist || data.artist || '', album: data.title, item: data.id, duration: e.duration }; });
    }
    // « Disque 2 » ou, quand MusicBrainz titre le disque, « Disque 2 · Bonus » (le titre vient du scan, `disc_titles`).
    function discHeading(d, disc) {
      var title = d.disc_titles && d.disc_titles[disc || 1];
      return H.t('Disque {n}', { n: disc || 1 }) + (typeof title === 'string' && title ? ' · ' + title : '');
    }
    function tracks(d) {
      var list = h('div', { class: 'tracks' }), disc = null, multi = (d.discs || 1) > 1;
      d.entries.forEach(function (e, i) {
        if (multi && e.disc !== disc) { disc = e.disc; list.appendChild(h('h3', { class: 'grp' }, discHeading(d, disc))); }
        var track = trackList()[i], shown = H.music.credit(e.title, e.artist, d.artist || d.grp, e.artists, d.artist_mbids);
        list.appendChild(h('div', { class: 'trkwrap' },
          h('button', { class: 'trk f', 'data-key': e.id, onclick: function () { if (H.actions.canPlay(d.library && d.library.id)) H.audio.play(trackList(), i); else H.actions.denyPlay(); } },
            h('span', { class: 'no' }, e.number || i + 1), h('span', { class: 'ti' }, shown.title, shown.line ? h('small', null, shown.line) : null), h('span', { class: 'du' }, e.duration ? H.time(e.duration) : '')),
          H.button('', 'more', { class: 'trkmore', 'aria-label': H.t('Actions de la piste'), 'data-key': 'm' + e.id, onclick: function () { H.music.trackMenu(track, null, d.library && d.library.id); } })));
      });
      var sec = h('section', null, list);
      H.audio.mark(list);
      return sec;
    }
    function episodeNumber(e) { return e.number === null || e.number === undefined ? '' : e.number; }
    // La vignette d'un épisode : l'image du fichier sur une lettre, avec une barre quand il est commencé.
    function episodeThumb(d, e) {
      var p = entryProgress(e), ratio = p && e.duration ? Math.min(1, p.position / e.duration) : 0;
      var thumb = h('div', { class: 'poster wide' }, h('span', { class: 'ph', style: '--h:' + H.hue(d.title + e.number) }, episodeNumber(e)),
        h('img', { src: '/img/entry/' + e.id + '?w=400', alt: '', loading: 'lazy', onload: function () { this.classList.add('in'); }, onerror: function () { if (this.parentNode) this.parentNode.removeChild(this); } }));
      if (ratio) thumb.appendChild(h('div', { class: 'pbar' }, h('i', { style: 'width:' + Math.round(ratio * 100) + '%' })));
      return thumb;
    }
    // Une ligne de la liste : la vignette, le titre, le résumé, la durée, « vu », et le bouton « … » de l'épisode.
    function episodeRow(d, e) {
      var done = e.progress && e.progress.done, no = episodeNumber(e);
      return h('div', { class: 'epwrap' }, h('button', { class: 'ep f', 'data-key': e.id, onclick: function () { if (H.actions.canPlay(d.library && d.library.id)) H.go(playHref(e)); else H.actions.denyPlay(); } }, h('div', { class: 'thumb' }, episodeThumb(d, e)),
        h('div', { class: 'txt' }, h('div', { class: 'ti' }, h('b', null, no === '' ? '' : no + '.'), ' ', e.title || H.t('Épisode')),
          e.summary ? h('div', { class: 'su' }, e.summary) : null,
          h('div', { class: 'du' }, [e.duration ? H.dur(e.duration) : '', e.date || ''].filter(Boolean).join(' · '))),
        done ? h('span', { class: 'seen' }, H.icon('check')) : null),
        H.button('', 'more', { class: 'epmore', 'aria-label': H.t('Actions de l’épisode'), 'data-key': 'm' + e.id, onclick: function () { H.actions.episodeMenu(e, load, d.library && d.library.id, d.id); } }));
    }
    // La saison ouverte au début : celle de l'épisode où reprendre, sinon la première.
    function firstSeason(d, seasons) {
      var r = d.entries.filter(function (e) { return e.id === d.resume; })[0];
      return r && seasons.indexOf(r.season) >= 0 ? r.season : seasons[0];
    }
    function episodes(d) {
      var sec = h('section', { class: 'episodes' });
      var seasons = d.seasons && d.seasons.length ? d.seasons : [1];
      if (season === null || seasons.indexOf(season) < 0) season = firstSeason(d, seasons);
      var list = h('div', { class: 'eps' }), blurb = h('p', { class: 'seasonsum' }), tags = h('div', { class: 'stags' }), bSeason = null;
      function seasonDone() { return d.entries.filter(function (e) { return e.season === season; }).every(function (e) { return e.progress && e.progress.done; }); }
      function fill() {
        if (bSeason) {          // le bouton annonce ce qu'il va faire de la saison affichée
          var done = seasonDone();
          bSeason.classList.toggle('done', done);
          H.clear(bSeason).appendChild(h('span', null, H.icon(done ? 'close' : 'check'), ' ', done ? H.t('Marquer la saison non vue') : H.t('Marquer la saison vue')));
        }
        H.clear(list);
        var text = (d.season_summaries || {})[String(season)] || '';
        blurb.textContent = text; blurb.hidden = !text;
        var row = qualityTags((d.season_badges || {})[String(season)]);           // ceux de la saison affichée : rien tant qu'aucun de ses épisodes n'est sondé
        H.clear(tags).hidden = !row;
        if (row) tags.appendChild(row);
        d.entries.filter(function (e) { return (e.season === null || e.season === undefined ? 1 : e.season) === season; }).forEach(function (e) { list.appendChild(episodeRow(d, e)); });
      }
      var chips = h('div', { class: 'chips' });
      if (seasons.length > 1 || seasons[0] !== 1) seasons.forEach(function (s) {
        chips.appendChild(h('button', { class: 'chip f' + (s === season ? ' on' : ''), 'data-key': 's' + s, 'aria-pressed': s === season ? 'true' : 'false', onclick: function () {
          season = s; Array.prototype.forEach.call(chips.querySelectorAll('.chip'), function (c) { var on = c.getAttribute('data-key') === 's' + s; c.classList.toggle('on', on); c.setAttribute('aria-pressed', on ? 'true' : 'false'); }); fill();
        } }, s === 0 ? H.t('Spéciaux') : H.t('Saison {n}', { n: s })));
      });
      if (seasons.length > 1) {
        bSeason = h('button', { class: 'chip f ghost', 'data-key': 'sv', onclick: function () { setSeen(!seasonDone(), season); } });
        chips.appendChild(bSeason);
      }
      sec.appendChild(chips); sec.appendChild(tags); sec.appendChild(blurb); sec.appendChild(list);
      fill();
      return sec;
    }
    function volumes(d) {
      var sec = h('section', { class: 'volumes' }, h('h2', null, d.units ? H.t(d.units).charAt(0).toUpperCase() + H.t(d.units).slice(1) : 'Volumes'));
      var grid = h('div', { class: 'grid small' });
      d.entries.forEach(function (e) {
        var p = entryProgress(e), done = e.progress && e.progress.done;
        var pic = H.picture('/img/entry/' + e.id + '?w=300', e.title || d.title);
        if (H.actions.canDownload(d.library && d.library.id)) pic.appendChild(H.actions.entryTools(e, load, d.library.id, d.id));
        if (done) pic.appendChild(h('span', { class: 'badge done' }, H.icon('check')));
        else if (p) pic.appendChild(h('div', { class: 'pbar' }, h('i', { style: 'width:' + Math.round(Math.min(1, p.position / Math.max(1, p.duration || 1)) * 100) + '%' })));
        grid.appendChild(h('a', { class: 'card f', href: H.actions.canPlay(d.library && d.library.id) ? playHref(e) : '#/i/' + d.id, onclick: H.actions.canPlay(d.library && d.library.id) ? null : function (ev) { ev.preventDefault(); H.actions.denyPlay(); }, 'data-key': e.id }, pic, h('div', { class: 't' }, e.label ? H.t('N° {n}', { n: e.label }) : (e.title || '')), h('div', { class: 's' }, [(e.format || '').toUpperCase(), e.pages ? H.t('{n} p.', { n: e.pages }) : ''].filter(Boolean).join(' · '))));
      });
      sec.appendChild(grid);
      if (d.missing && d.missing.length) {
        sec.appendChild(h('p', { class: 'missing' }, H.t('Manquants : {list}', { list: d.missing.map(function (x) { return (x.label ? x.label + ' ' : '') + x.numbers.join(', '); }).join(' ; ') })));
      }
      return sec;
    }
    function parts(d) {
      var sec = h('section', { class: 'eps' }, h('h2', null, H.t('Parties')));
      d.entries.forEach(function (e) {
        sec.appendChild(h('button', { class: 'ep f', onclick: function () { if (H.actions.canPlay(d.library && d.library.id)) H.go(playHref(e)); else H.actions.denyPlay(); } }, h('div', { class: 'txt' }, h('div', { class: 'ti' }, e.title), h('div', { class: 'du' }, H.dur(e.duration)))));
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
      if (!name) { H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, H.t('Introuvable')), h('p', null, H.t('Aucune saga n’est indiquée.')), h('a', { class: 'btn f', href: '#/l/' + libId, 'data-autofocus': '1' }, H.t('Retour')))); return Promise.resolve(); }
      return H.api('/api/library/' + libId + '?group=' + encodeURIComponent(name) + '&sort=part&limit=600').then(function (d) { data = d; render(); }).catch(function (e) {
        H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, H.t('Introuvable')), h('p', null, e.message), h('a', { class: 'btn f', href: '#/l/' + libId, 'data-autofocus': '1' }, H.t('Retour'))));
      });
    }
    function render() {
      var focusKey = document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
      var films = data.items, absent = data.missing || [];
      H.clear(el);
      if (!films.length) { el.appendChild(h('div', { class: 'blank' }, h('h1', null, name), h('p', null, H.t('Cette saga ne contient plus de film.')), h('a', { class: 'btn f', href: '#/l/' + libId, 'data-autofocus': '1' }, H.t('Retour')))); return; }
      var done = films.filter(function (f) { return f.seen >= Math.max(1, f.n); }).length;
      var next = films.filter(function (f) { return !(f.seen >= Math.max(1, f.n)); })[0];
      var years = films.map(function (f) { return f.year; }).filter(Boolean).sort();
      var meta = [H.plural(films.length, 'film')];
      if (lacking(absent)) meta.push(H.plural(lacking(absent), 'manquant'));
      if (years.length) meta.push(years[0] === years[years.length - 1] ? years[0] : years[0] + '–' + years[years.length - 1]);
      if (done) meta.push(done === films.length ? H.t('Tous vus') : H.t('{done}/{n} vus', { done: done, n: films.length }));
      var info = h('div', { class: 'info' }, h('h1', null, name), h('p', { class: 'meta' }, meta.join(' · ')));
      var actions = h('div', { class: 'actions' });
      var target = next || films[0];
      actions.appendChild(H.button(next ? (done ? H.t('Continuer : {title}', { title: next.title }) : H.t('Commencer : {title}', { title: next.title })) : H.t('Revoir : {title}', { title: films[0].title }), 'play', { class: 'primary', 'data-autofocus': '1', onclick: function () { H.go('#/i/' + target.id); } }));
      var all = done === films.length;
      actions.appendChild(H.button(all ? H.t('Marquer la saga non vue') : H.t('Marquer la saga vue'), all ? 'close' : 'check', { 'data-key': 'seen', onclick: function () { setSeen(!all); } }));
      info.appendChild(actions);
      el.appendChild(h('div', { class: 'hero' }, h('div', { class: 'hero-bg', style: 'background-image:url(/img/item/' + films[0].id + '?w=300)' }),
        h('div', { class: 'hero-in' }, h('div', { class: 'stack-big' }, H.stack(films.map(function (f) { return itemCover(f.id); }), name, 500)), info)));
      el.appendChild(h('div', { class: 'body' }, h('section', { class: 'sagaall' }, h('h2', null, H.t('Les films de la saga')),
        h('div', { class: 'grid' }, sagaCells(films, absent, function (f) { return H.card(f, false, true); }, { hide: H.isAdmin() ? hide : null })))));
      if (!H.focus.shown(el)) return;          // une réponse tardive : la page a été quittée
      if (focusKey) H.focus.key(el, focusKey); else H.focus.first();
    }
    // Marque tous les films de la saga vus (ou non vus) : un appel par film, comme le menu « … » d'une carte de saga.
    function setSeen(done) {
      Promise.all(data.items.map(function (f) { return H.api('/api/item/' + f.id + '/seen', { done: done }); }))
        .then(function () { H.toast(done ? H.t('Saga marquée comme vue') : H.t('Saga marquée comme non vue')); return load(); })
        .catch(function (e) { H.toast(e && e.message ? e.message : H.t('Impossible pour le moment.'), 5000); return load(); });
    }
    // Cache un film que la collection TMDB compte et pas nous : il ne sera plus montré comme manquant (Administration > Médiathèque > Sagas le rétablit).
    function hide(m) {
      H.api('/api/sagas/ignore', { tmdb: m.tmdb }).then(function () {
        H.toast(H.t('« {title} » ne sera plus montré comme manquant (Administration > Médiathèque > Sagas pour le rétablir)', { title: m.title }), 5000);
        return load();
      }).catch(function (e) { H.toast(e.message, 5000); });
    }
    load();
    return { el: el, nav: '#/l/' + libId, lib: libId, refresh: load };
  });

  // --- Recherche -------------------------------------------------------------------
  // Un rayon par bibliothèque qui a des résultats (la recherche et la page d'une personne).
  function shelves(results) {
    return results.map(function (g) {
      return h('section', { class: 'shelf', 'data-lib': g.id }, h('h2', null, h('a', { class: 'f head', href: '#/l/' + g.id }, H.libLabel(g))), h('div', { class: 'grid' + (g.kind === 'music' ? ' sq' : '') }, g.items.map(function (it) { return H.card(it, g.kind === 'music'); })));
    });
  }
  // La rangée « Personnes » de la recherche : une pastille par personne, vers sa page.
  function peopleRow(people) {
    return h('section', { class: 'people' }, h('h2', null, H.t('Personnes')),
      h('div', { class: 'chips' }, people.map(function (p) { return h('a', { class: 'chip f', href: personHref(p.name) }, p.name, h('small', null, ' · ' + H.plural(p.titles, 'titre'))); })));
  }
  H.route(/^\/search$/, function () {
    var out = h('div', { class: 'results' }), last = '', timer = null;
    var input = h('input', { class: 'f field big', type: 'search', placeholder: H.t('Titre, artiste, collection…'), 'aria-label': H.t('Rechercher'), 'data-autofocus': '1', autocomplete: 'off',
      oninput: function () { clearTimeout(timer); timer = setTimeout(run, 300); },
      onkeydown: function (e) { if (e.key === 'Enter') { e.preventDefault(); run(); var c = out.querySelector('.f'); if (c) { input.blur(); H.focus.set(c); } } } });
    function run() {
      var q = input.value.trim();
      if (q === last) return;
      last = q;
      if (q.length < 2) { H.clear(out).appendChild(h('p', { class: 'muted' }, H.t('Tape au moins deux lettres.'))); return; }
      H.api('/api/search?q=' + encodeURIComponent(q)).then(function (res) {
        if (q !== last) return;
        H.clear(out);
        var people = res.people || [];
        if (people.length) out.appendChild(peopleRow(people));
        if (!res.results.length && !people.length) {
          out.appendChild(h('p', { class: 'muted' }, H.t('Aucun résultat pour « {q} ».', { q: q })));
          if (H.canSearchRequests()) out.appendChild(h('div', { class: 'notfound' }, h('a', { class: 'btn f', href: '#/request?q=' + encodeURIComponent(q), 'data-key': 'req-notfound' }, H.icon('plus'), h('span', { class: 'lbl' }, H.t('Pas trouvé ? Demander « {q} »', { q: q })))));      // ce texte, cherché dans TMDB pour le demander
          return;
        }
        shelves(res.results).forEach(function (sec) { out.appendChild(sec); });
      }).catch(function (e) { H.clear(out).appendChild(h('p', { class: 'muted' }, e.message)); });
    }
    var el = h('section', { class: 'search' }, h('h1', null, H.t('Recherche')), input, out);
    return { el: el, nav: '#/search' };
  });

  // --- Une personne ----------------------------------------------------------------
  // #/person?n=<nom> : ce qu'un réalisateur ou un acteur a fait dans les bibliothèques que ce compte voit.
  H.route(/^\/person$/, function (m, query) {
    var name = (query.n || '').trim(), out = h('div', { class: 'results' });
    var title = h('h1', null, name), roles = h('p', { class: 'roles', hidden: true }), actions = h('p', { class: 'actions', hidden: true });
    var el = h('section', { class: 'person' }, title, roles, actions, out);
    var ROLE_LABELS = [['director', H.t('Réalisateur')], ['actor', H.t('Acteur')]];
    var ROLE_NAMES = {};
    ROLE_LABELS.forEach(function (r) { ROLE_NAMES[r[0]] = r[1]; });
    var shown = name, asking = false, gone = false, timer = null;
    // Le focus va sur la première carte (ou sur le lien de secours d'une page vide), sauf si l'utilisateur est déjà ailleurs (l'ancien focus, sur la page cachée, ne compte pas).
    // `key` : la carte qui avait le focus avant que la page soit redessinée.
    function settle(key) {
      var active = document.activeElement;
      var free = !active || active === document.body || el.contains(active) || active.getClientRects().length === 0;
      if (!free || active === button || !H.focus.shown(el) || H.overlay.isOpen()) return;          // sur le bouton « Chercher… », on y reste
      if (key && H.focus.key(out, key, true)) return;
      H.focus.set(out.querySelector('.card') || out.querySelector('.f'), true);
    }
    // Une page sans carte n'aurait rien à atteindre à la télécommande : un lien vers la recherche.
    function say(text) {
      H.clear(out).appendChild(h('p', { class: 'muted' }, text));
      out.appendChild(h('p', null, h('a', { class: 'btn f', href: '#/search', 'data-autofocus': '1' }, H.t('Rechercher'))));
      settle();
    }
    // Les films de la personne qu'on n'a pas (`missing` : voir docs/api.md) : des cases grisées, qui se demandent (sinon ni focus ni clic, comme un album manquant) ; son rôle sous le titre, pas de rang.
    function missingShelf(list) {
      var cards = list.map(function (m) { return missingCard({ tmdb: m.tmdb, title: m.title, year: m.year, cover: m.cover, note: ROLE_NAMES[m.role] || '', request: m.request }); });
      return h('section', { class: 'shelf missing' }, h('h2', null, H.t('Films qui manquent') + ' · ' + list.length), h('div', { class: 'grid' }, cards));
    }
    // Demande à TMDB les films de la personne (une tâche de fond), attend la fin de CETTE tâche (d'autres, un scan par exemple, peuvent passer devant), puis redessine la page. Le bouton ne fait rien tant que la demande court.
    // `wait` rend la tâche terminée (ou null : on est parti, ou elle n'est plus dans la liste des 30 dernières).
    function wait(id) {
      return H.api('/api/jobs').then(function (r) {
        var job = r.jobs.filter(function (j) { return j.id === id; })[0] || null;
        if (gone || !job || job.status === 'done' || job.status === 'failed') return job;
        return new Promise(function (ok) { timer = setTimeout(ok, 1500); }).then(function () { return wait(id); });
      });
    }
    // Une tâche en échec (clé refusée, TMDB qui ne répond pas pour cette personne) ne se présente jamais comme « aucun film ne manque ».
    function failed(job) { return !!job && (job.status === 'failed' || !!(job.result && job.result.failed)); }
    function finish(job) {
      if (gone) return null;
      if (failed(job)) { H.toast(H.t('La recherche a échoué : voir les tâches dans Administration.'), 5000); return null; }
      return load().then(function (res) { if (res && !gone) H.toast(res.missing ? H.t('Terminé') : H.t('Aucun film ne manque.')); });
    }
    function ask() {
      if (asking) return;
      asking = true;
      button.setAttribute('aria-busy', 'true');
      H.api('/api/enrich/person', { name: shown }).then(function (r) {
        if (!gone) H.toast(H.t('Recherche lancée : {name}', { name: shown }));
        return wait(r.job.id);
      }).then(finish).catch(function (e) { if (!gone) H.toast(e.message, 5000); }).then(function () { asking = false; button.removeAttribute('aria-busy'); });
    }
    // Le bouton est le même d'un dessin à l'autre : un redessin ne lui prend pas le focus de la télécommande. Il n'existe que pour l'administrateur, quand TMDB peut répondre pour cette personne.
    var button = H.button(H.t('Chercher ses films manquants'), null, { 'data-key': 'person-enrich', title: H.t('Demande à TMDB les films de cette personne, pour voir ceux qui ne sont pas dans ta médiathèque.'), onclick: ask });
    function showButton(on) {
      if (on && !button.parentNode) actions.appendChild(button);
      else if (!on && button.parentNode) button.parentNode.removeChild(button);
      actions.hidden = !on;
    }
    function show(res) {
      if (gone) return;
      shown = res.name || name;
      var focus = document.activeElement && out.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
      title.textContent = shown;
      showButton(!!res.enrichable && H.isAdmin());
      var counts = ROLE_LABELS.filter(function (r) { return res.roles && res.roles[r[0]] > 0; }).map(function (r) { return r[1] + ' · ' + H.plural(res.roles[r[0]], 'titre'); });
      roles.textContent = counts.join(' · ');
      roles.hidden = !counts.length;
      if (!res.results.length) { say(H.t('Aucun titre pour {name} dans ta médiathèque.', { name: shown })); return; }
      H.clear(out);
      shelves(res.results).forEach(function (sec) { out.appendChild(sec); });
      if (res.missing && res.missing.length) out.appendChild(missingShelf(res.missing));
      settle(focus);
    }
    function load() { return H.api('/api/person?name=' + encodeURIComponent(name)).then(function (res) { show(res); return res; }).catch(function (e) { say(e.message); }); }
    if (!name) say(H.t('Aucun nom n’est indiqué.')); else load();
    return { el: el, nav: '#/search', refresh: name ? load : null, destroy: function () { gone = true; clearTimeout(timer); } };      // au retour d'une fiche : les pastilles « vu » ont pu changer
  });

  // --- Demandes : « Mes demandes » et l'onglet de l'administrateur ----------------------------------
  var REQUEST_STATUS = { pending: H.t('En attente'), added: H.t('Ajouté'), declined: H.t('Refusé') };
  // Le titre d'une demande, avec son année : « Film intermédiaire (2014) ». Le titre vient du serveur (TMDB, ou ce que le compte a envoyé) : toujours en texte.
  function requestTitle(r) { return (r.title || (r.kind === 'show' ? H.t('Série TMDB {n}', { n: r.tmdb }) : H.t('Film TMDB {n}', { n: r.tmdb }))) + (r.year ? ' (' + r.year + ')' : ''); }
  // Le titre affiché d'une demande ou d'un résultat : un lien vers sa fiche TMDB (`url`, donnée par le serveur), dans un nouvel onglet. Sur la télé, ou sans adresse, un simple texte : rien à y atteindre.
  // `data-aside` : le lien s'atteint aux flèches, mais le focus n'y arrive jamais de lui-même (voir H.focus.first) ; `data-key` le lui rend quand la liste se redessine.
  function requestHeading(r, text, cls) {
    var link = H.device === 'tv' || !r.url ? text : h('a', { class: 'rqlink f', href: r.url, target: '_blank', rel: 'noopener noreferrer', 'data-aside': '1', 'data-key': 'tmdb-' + r.kind + '-' + r.tmdb,
      'aria-label': H.t('Ouvrir « {title} » sur TMDB (nouvel onglet)', { title: text }) }, text);
    return h('b', { class: cls || null }, link);
  }
  // L'état en toutes lettres, avec le motif d'un refus : « Refusé : pas en France ».
  function requestStatus(r) {
    var said = REQUEST_STATUS[r.status] || r.status;
    return r.status === 'declined' && r.reason ? H.t('{status} : {reason}', { status: said, reason: r.reason }) : said;
  }
  function requestPicture(r) { return H.picture(r.cover ? r.cover + (r.cover.indexOf('?') < 0 ? '?' : '&') + 'w=160' : '', r.title || '?', 'rqpic'); }
  function openLink(r) { return h('a', { class: 'btn f', href: '#/i/' + r.item, 'data-key': 'open-' + r.id }, H.icon('play'), h('span', { class: 'lbl' }, H.t('Ouvrir'))); }
  // La clé de focus du bouton de la ligne qui suit (ou précède) `r` dans `list`, pour que le focus ne retombe pas en haut quand la ligne disparaît.
  function neighbourKey(list, r, prefix) {
    var i = list.indexOf(r), next = list[i + 1] || list[i - 1];
    return next ? prefix + next.id : null;
  }
  // Ce que l'onglet Demandes de la page Administration (admin.js) reprend d'ici.
  H.requests = { title: requestTitle, heading: requestHeading, status: requestStatus, picture: requestPicture, open: openLink, neighbour: neighbourKey, gone: goneAlready };

  // #/requests : les demandes du compte (GET /api/requests?mine=1), de la plus récente à la plus ancienne. Chacune se retire ; une demande arrivée mène à l'élément quand le compte le voit.
  // Ce que le compte voit : ses demandes et son message, jamais qui d'autre a demandé le même film.
  H.route(/^\/requests$/, function () {
    var out = h('div', { class: 'panel reqlist' });
    var intro = h('p', { class: 'intro muted' }, H.t('Les films et les séries que tu as demandés à l’administrateur. Le hub ne télécharge rien : l’administrateur les ajoute s’il le peut.'));
    var add = H.canSearchRequests() ? h('div', { class: 'tools' }, h('a', { class: 'btn f', href: '#/request', 'data-key': 'req-new' }, H.icon('plus'), h('span', { class: 'lbl' }, H.t('Nouvelle demande')))) : null;
    var el = h('section', { class: 'myrequests' }, h('h1', null, H.t('Mes demandes')), intro, add, out);
    var list = [];
    function row(r) {
      var what = h('div', { class: 'what' }, requestHeading(r, requestTitle(r)), h('span', { class: 'rqstatus' }, requestStatus(r)),
        r.status === 'added' && !r.item ? h('span', { class: 'rqhint' }, H.t('Pas encore visible pour toi : demande à l’administrateur où le trouver.')) : null,      // ajoutée sans élément qu'il voit (bibliothèque cachée, ou marquée avant le scan) : ne dit pas où, voir docs/decisions.md
        r.note ? h('span', { class: 'rqnote' }, H.t('Ton message : « {note} »', { note: r.note })) : null,
        r.asked ? h('span', null, H.t('Demandé le {date}', { date: H.date(r.asked) })) : null);
      return h('div', { class: 'line reqrow', 'data-request': r.id, 'data-status': r.status }, requestPicture(r), what,
        r.item ? openLink(r) : null,
        H.button(H.t('Retirer'), 'trash', { 'data-key': 'rm-' + r.id, 'aria-label': H.t('Retirer ta demande de « {title} »', { title: requestTitle(r) }), onclick: function () { withdraw(r); } }));
    }
    function withdraw(r) {
      H.confirm({ title: H.t('Retirer ta demande ?'), lead: [H.t('« {title} » ne sera plus dans tes demandes.', { title: requestTitle(r) })], ok: H.t('Retirer'), danger: true,
        submit: function () { return H.api('/api/requests/' + r.id + '/delete', {}).catch(goneAlready); },
        done: function () { H.toast(H.t('Demande retirée : {title}', { title: requestTitle(r) })); load(neighbourKey(list, r, 'rm-')); } });
    }
    function show(focusKey, keep) {
      var key = focusKey || (document.activeElement && out.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null);
      H.clear(out);
      if (!H.canRequest()) out.appendChild(h('p', { class: 'muted' }, H.t('L’administrateur t’a retiré le droit de demander des films ; tes demandes déjà faites restent ici.')));
      if (!list.length) {
        out.appendChild(h('div', { class: 'reqempty' }, h('p', null, H.t('Tu n’as aucune demande pour l’instant. Un film qui manque à une saga, ou à la filmographie d’une personne, se demande depuis sa case grisée : « Demander ».')),
          h('a', { class: 'btn f', href: '#/search', 'data-autofocus': '1', 'data-key': 'req-search' }, H.icon('search'), h('span', { class: 'lbl' }, H.t('Rechercher')))));
      }
      list.forEach(function (r) { out.appendChild(row(r)); });
      if (!H.focus.shown(el) || H.overlay.isOpen()) return;          // une réponse tardive : la page a été quittée
      var kept = keep && document.activeElement && el.contains(document.activeElement) && !out.contains(document.activeElement);          // au retour, « Nouvelle demande » garde le focus
      if (!(key && H.focus.key(out, key, true)) && !kept) H.focus.first();
    }
    function load(focusKey, keep) {
      return H.api('/api/requests?mine=1').then(function (res) { list = res.requests || []; show(focusKey, keep); }).catch(function (e) {
        H.clear(out).appendChild(h('p', { class: 'muted' }, e.message));
        out.appendChild(h('button', { class: 'btn f', 'data-autofocus': '1', onclick: function () { load(); } }, H.t('Réessayer')));
      });
    }
    load();
    return { el: el, nav: '#/settings', refresh: function () { load(null, true); } };
  });

  // --- Suggestions : « Mes suggestions » et l'onglet de l'administrateur ----------------------------------
  // Une idée ou une remarque sur le hub lui-même (pas un film : ce sont les demandes), entre son auteur et l'administrateur. Textes et réponses viennent des comptes : toujours en texte.
  var SUGGESTION_CATEGORIES = [['evolution', H.t('Évolution')], ['improvement', H.t('Amélioration')], ['bug', H.t('Bug')], ['other', H.t('Autre')]];
  var SUGGESTION_STATUSES = [['new', H.t('Nouvelle')], ['planned', H.t('Prévue')], ['done', H.t('Faite')], ['declined', H.t('Écartée')]];
  function named(pairs, value) { var hit = pairs.filter(function (p) { return p[0] === value; })[0]; return hit ? hit[1] : value; }
  // Les causes d'un signalement (POST /api/suggestions avec `item`) : la valeur est celle du serveur.
  var REPORT_CAUSES = [['image', H.t('Image')], ['sound', H.t('Son')], ['subtitles', H.t('Sous-titres')], ['playback', H.t('Ne se lit pas')], ['other', H.t('Autre')]];
  function suggestionCategory(s) { return named(SUGGESTION_CATEGORIES, s.category); }
  function reportCause(s) { return named(REPORT_CAUSES, s.cause); }
  function suggestionStatus(s) { return named(SUGGESTION_STATUSES, s.status); }
  // Une rangée de puces dont une seule est choisie (catégorie, état) : `current()` rend la valeur choisie. Atteignable aux flèches, chaque puce s'active par Entrée.
  function suggestionChoice(pairs, value, label, prefix) {
    var chosen = value, buttons = [];
    var row = h('div', { class: 'chips sgchoice', role: 'group', 'aria-label': label });
    function paint() { buttons.forEach(function (b) { var on = b.getAttribute('data-value') === chosen; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on ? 'true' : 'false'); }); }
    pairs.forEach(function (p) {
      var b = h('button', { class: 'chip f', type: 'button', 'data-key': prefix + p[0], 'data-value': p[0], onclick: function () { chosen = p[0]; paint(); } }, h('span', { class: 'mark', 'aria-hidden': 'true' }, '✓'), h('span', { class: 'lbl' }, p[1]));
      buttons.push(b); row.appendChild(b);
    });
    paint();
    return { el: h('div', { class: 'flab' }, label, row), current: function () { return chosen; } };
  }
  // Ce qu'une ligne de suggestion montre, pour son auteur comme pour l'administrateur : la catégorie, l'état, le texte, la réponse, puis `more` (date, auteur).
  // Un signalement (`cause` donnée) montre son titre (`subject`, tel que l'auteur le voyait : un lien vers la fiche seulement si le serveur donne `link`) et sa cause, à la place de la catégorie ; son message est facultatif.
  function suggestionHead(s) {
    if (!s.cause) return [h('b', null, suggestionCategory(s))];
    return [h('b', { class: 'sgsubject' }, s.link && s.link.item ? h('a', { class: 'f', href: '#/i/' + s.link.item }, s.subject) : s.subject), h('span', { class: 'sgcause' }, reportCause(s))];
  }
  function suggestionWhat(s, more) {
    return h('div', { class: 'what' }, suggestionHead(s), h('span', { class: 'sgstatus' }, suggestionStatus(s)),
      s.text || !s.cause ? h('p', { class: 'sgtext' }, s.text) : null,
      s.reply ? h('p', { class: 'sgreply' }, H.t('Réponse de l’administrateur : « {reply} »', { reply: s.reply })) : null,
      more);
  }
  // Ce que l'onglet Suggestions de la page Administration (admin.js) reprend d'ici.
  H.suggestions = { categories: SUGGESTION_CATEGORIES, statuses: SUGGESTION_STATUSES, causes: REPORT_CAUSES, cause: reportCause, category: suggestionCategory, status: suggestionStatus, choice: suggestionChoice, what: suggestionWhat };

  // #/suggestions : les suggestions du compte (GET /api/suggestions?mine=1), de la plus récente à la plus ancienne, avec l'état et la réponse de l'administrateur. Chacune se retire.
  H.route(/^\/suggestions$/, function () {
    var out = h('div', { class: 'panel sglist' });
    var intro = h('p', { class: 'intro muted' }, H.t('Une idée pour le hub, quelque chose à améliorer, un bug : l’administrateur lit tes suggestions et te répond ici. Personne d’autre ne les voit.'));
    var add = H.canSuggest() ? h('div', { class: 'tools' }, H.button(H.t('Proposer une amélioration'), 'plus', { 'data-key': 'sg-new', onclick: function () { propose(); } }))
      : h('p', { class: 'muted sgtv' }, H.t('Pour proposer une amélioration, utilise un ordinateur, une tablette ou un téléphone.'));          // sur télévision : lire, retirer, pas rédiger (H.canSuggest)
    var el = h('section', { class: 'mysuggestions' }, h('h1', null, H.t('Mes suggestions')), intro, add, out);
    var list = [];
    function row(s) {
      return h('div', { class: 'line sgrow', 'data-suggestion': s.id, 'data-status': s.status }, suggestionWhat(s, h('span', null, H.t('Proposée le {date}', { date: H.date(s.created) }))),
        H.button(H.t('Retirer'), 'trash', { 'data-key': 'sg-rm-' + s.id, 'aria-label': H.t('Retirer ta suggestion du {date}', { date: H.date(s.created) }), onclick: function () { withdraw(s); } }));
    }
    function propose() {
      var category = suggestionChoice(SUGGESTION_CATEGORIES, 'improvement', H.t('Catégorie'), 'sg-cat-');
      H.form({ title: H.t('Proposer une amélioration'), ok: H.t('Envoyer'),
        lead: [H.t('Ce qui manque au hub, ce qui pourrait marcher mieux, ce qui ne marche pas. Pour un film ou une série, c’est « Mes demandes ».')],
        fields: [{ key: 'text', label: H.t('Ta suggestion (1000 caractères au plus)'), type: 'textarea', maxlength: 1000 }],
        extra: [category.el],
        submit: function (v) {
          if (!v.text) return H.t('Écris ta suggestion.');
          return H.api('/api/suggestions', { category: category.current(), text: v.text });
        },
        done: function (s) { H.toast(H.t('Suggestion envoyée')); load('sg-rm-' + s.id); } });
    }
    function withdraw(s) {
      H.confirm({ title: H.t('Retirer ta suggestion ?'), lead: [H.t('Elle disparaît pour toi et pour l’administrateur, avec sa réponse. Cela ne s’annule pas.')], ok: H.t('Retirer'), danger: true,
        submit: function () { return H.api('/api/suggestions/' + s.id + '/delete', {}).catch(goneAlready); },
        done: function () {
          var i = list.indexOf(s), next = list[i + 1] || list[i - 1];
          H.toast(H.t('Suggestion retirée')); load(next ? 'sg-rm-' + next.id : 'sg-new');
        } });
    }
    function show(focusKey, keep) {
      var key = focusKey || (document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null);
      H.clear(out);
      if (!list.length) out.appendChild(h('p', { class: 'muted sgempty' }, H.t('Tu n’as envoyé aucune suggestion pour l’instant.')));
      list.forEach(function (s) { out.appendChild(row(s)); });
      if (!H.focus.shown(el) || H.overlay.isOpen()) return;          // une réponse tardive : la page a été quittée
      if (!(key && H.focus.key(el, key, true)) && !keep) H.focus.first();
    }
    function load(focusKey, keep) {
      return H.api('/api/suggestions?mine=1').then(function (res) { list = res.suggestions || []; show(focusKey, keep); }).catch(function (e) {
        H.clear(out).appendChild(h('p', { class: 'muted' }, e.message));
        out.appendChild(h('button', { class: 'btn f', 'data-autofocus': '1', onclick: function () { load(); } }, H.t('Réessayer')));
      });
    }
    load();
    return { el: el, nav: '#/settings', refresh: function () { load(null, true); } };          // au retour : l'administrateur a pu répondre
  });

  // #/request?q=<texte>&kind=movie|show : chercher dans TMDB (par le serveur : GET /api/requests/search) un film ou une série que la médiathèque n'a pas, pour le demander.
  // Chaque résultat dit ce qu'on peut en faire, comme une case grisée (ASK) : « Demander » (la même fenêtre), « Demandé » (retirer la sienne), « Demander aussi », « Refusé » ; « Ouvrir » quand il est déjà là.
  // La recherche part 400 ms après la dernière frappe, ou sur Entrée (qui mène alors au premier résultat) ; une réponse périmée (une recherche est partie depuis) est ignorée.
  // Une erreur (429, 502, 503…) s'affiche à la place de l'état, sans effacer les résultats d'avant. Titres et résumés viennent de TMDB : toujours en texte.
  var REQUEST_KINDS = [['', H.t('Les deux')], ['movie', H.t('Films')], ['show', H.t('Séries')]];
  function today() {
    var d = new Date();
    return d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
  }
  // Un résultat de la recherche : sa sorte, son titre, son année, son résumé sur deux lignes, et son bouton. Il se refait sur place quand on le demande ou qu'on retire sa demande ; le focus reste sur son bouton.
  function requestHit(r) {
    var row = h('div', { class: 'line rqhit', 'data-kind': r.kind });
    var others = r.request && !r.request.mine ? r.request : null;           // la demande d'un autre compte : ce que redevient le résultat quand on retire la sienne
    function paint(focus) {
      var state = r.item ? null : askState(r.request), look = state ? ASK[state] : null, act = look && look.act, key = 'rq-' + r.kind + '-' + r.tmdb, action = null;
      if (r.item) action = h('a', { class: 'btn f', href: '#/i/' + r.item, 'data-key': key, 'aria-label': H.t('Ouvrir « {title} »', { title: r.title }) }, H.icon('play'), h('span', { class: 'lbl' }, H.t('Ouvrir')));
      else if (act) action = H.button(look.pill, act === 'withdraw' ? 'check' : 'plus', { class: act === 'withdraw' ? 'on' : null, 'data-key': key, 'aria-label': askLabel(state, r.title), onclick: function () {
        if (act === 'withdraw') withdrawFrom(r, function () { H.toast(H.t('Demande retirée : {title}', { title: r.title })); r.request = others; paint(true); });
        else askFor(r, function (res) { H.toast(H.t('Demandé : {title}', { title: r.title })); r.request = { status: res.status, mine: true, id: res.id }; paint(true); });
      } });
      // La sorte : les mots de H.plural (« film », « série »), la majuscule vient du CSS ; une clé « Film » traduirait aussi un dossier nommé « Film » dans les messages du serveur (H.tr)
      var head = h('div', { class: 'rqhead' }, h('span', { class: 'rqkind' }, r.kind === 'show' ? H.t('série') : H.t('film')), requestHeading(r, r.title || '?', 'rqtitle'),
        r.year ? h('span', { class: 'rqyear' }, r.year) : null, r.date && r.date > today() ? h('span', { class: 'rqsoon' }, H.t('À venir')) : null);
      H.clear(row).appendChild(h('div', { class: 'what' }, head, look && look.line ? h('span', { class: 'rqstate' }, look.line) : null, r.overview ? h('p', { class: 'rqsum' }, r.overview) : null));
      if (action) row.appendChild(action);
      if (focus && action && document.body.contains(row)) H.focus.set(action, true);
    }
    paint(false);
    return row;
  }
  H.route(/^\/request$/, function (m, query) {
    var title = H.t('Demander un film ou une série');
    if (!H.canSearchRequests()) {          // sans le droit, ou sans clé TMDB sur le serveur : la page le dit et ramène aux demandes
      var why = H.canRequest() ? H.t('La recherche de titres à demander n’est pas configurée sur ce serveur. Un film qui manque à une saga, ou à la filmographie d’une personne, se demande depuis sa case grisée.')
        : H.t('L’administrateur t’a retiré le droit de demander des films et des séries.');
      var closed = h('section', { class: 'blank' }, h('h1', null, title), h('p', null, why), h('a', { class: 'btn f', href: '#/requests', 'data-autofocus': '1' }, H.t('Mes demandes')));
      return { el: closed, nav: '#/settings' };
    }
    var kind = query && (query.kind === 'movie' || query.kind === 'show') ? query.kind : '';
    var seq = 0, timer = null, asked = null, toResults = false;          // `seq` : la dernière recherche partie ; `asked` : ce qu'elle cherchait (sorte et texte), effacé après une erreur
    var say = h('p', { class: 'rqsay muted', role: 'status' }), list = h('div', { class: 'rqhits' });
    var input = h('input', { class: 'f field big', type: 'search', maxlength: '100', autocomplete: 'off', value: (query && query.q) || '',
      placeholder: H.t('Titre d’un film ou d’une série'), 'aria-label': H.t('Titre à chercher dans TMDB'), 'data-autofocus': H.device === 'tv' ? null : '1',      // sur la télé, le champ n'ouvre pas le clavier à l'arrivée
      oninput: function () { clearTimeout(timer); timer = setTimeout(run, 400); },
      onkeydown: function (e) {
        if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); toResults = true; run(); }
        else if (e.key === 'Escape') { e.preventDefault(); H.focus.set(pick, true); }          // Retour sort d'abord du champ
      } });
    var pick = H.button('', null, { class: 'flt wide', 'data-key': 'rq-kind', onclick: function () {
      H.menu(H.t('Type'), REQUEST_KINDS.map(function (k) { return { label: k[1], value: k[0] }; }), kind, function (v) { kind = v; paint(); run(); });
    } });
    var el = h('section', { class: 'reqsearch' }, h('h1', null, title),
      h('p', { class: 'intro muted' }, H.t('Cherche dans TMDB un film ou une série que la médiathèque n’a pas, puis demande-le à l’administrateur. Le hub ne télécharge rien : l’administrateur l’ajoute s’il le peut.')),
      h('div', { class: 'tools' }, input, pick), say, list);
    function paint() { filterLabel(pick, H.t('Type'), REQUEST_KINDS.filter(function (k) { return k[0] === kind; })[0][1]); }
    function tell(text, error) { say.textContent = text; say.classList.toggle('err', !!error); }
    function landOnResults() {            // après Entrée : le bouton du premier résultat, pour la télécommande
      toResults = false;
      var first = list.querySelector('.f:not([data-aside])');
      if (first && document.body.contains(el) && el.style.display !== 'none' && !H.overlay.isOpen() && (document.activeElement === input || document.activeElement === document.body)) H.focus.set(first);
    }
    function run() {
      var q = input.value.trim(), what = kind + '|' + q;
      if (q.length < 2) { seq++; asked = null; toResults = false; list.removeAttribute('aria-busy'); H.clear(list); tell(q ? H.t('Tape au moins deux lettres.') : ''); return; }
      if (what === asked) { if (toResults && !list.hasAttribute('aria-busy')) landOnResults(); return; }          // déjà cherché (ou en cours) : rien à redemander
      var mine = ++seq;
      asked = what;
      list.setAttribute('aria-busy', 'true');
      tell(H.t('Recherche…'));
      H.api('/api/requests/search?q=' + encodeURIComponent(q) + (kind ? '&kind=' + kind : '')).then(function (res) {
        if (mine !== seq) return;          // une recherche est partie depuis : celle-ci est périmée
        var results = (res && res.results) || [];
        list.removeAttribute('aria-busy');
        H.clear(list);
        results.forEach(function (r) { list.appendChild(requestHit(r)); });
        tell(results.length ? '' : H.t('Aucun titre trouvé dans TMDB pour « {q} ».', { q: q }));
        if (toResults) landOnResults();
      }, function (e) {
        if (mine !== seq) return;
        asked = null; toResults = false;          // la même recherche pourra être relancée (Entrée)
        list.removeAttribute('aria-busy');
        tell(e.message, true);
      });
    }
    paint();
    if (input.value.trim()) run();          // « Pas trouvé ? » : le texte de la recherche habituelle, cherché tout de suite
    return { el: el, nav: '#/settings',
      onBack: function () { if (document.activeElement !== input) return false; H.focus.set(pick, true); return true; },          // la touche Retour de la télécommande, pendant la saisie
      destroy: function () { clearTimeout(timer); seq++; } };
  });

  // --- Paramètres, et ce que la page Administration (admin.js) leur emprunte ---------------------------
  H.ago = function (ts) {
    if (!ts) return H.t('jamais');
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return H.t('à l’instant');
    if (s < 5400) return H.t('il y a {n} min', { n: Math.round(s / 60) });
    if (s < 129600) return H.t('il y a {n} h', { n: Math.round(s / 3600) });
    return H.t('il y a {n} j', { n: Math.round(s / 86400) });
  };
  H.roles = { admin: H.t('Administrateur'), user: H.t('Utilisateur') };
  // Les règles du mot de passe, vérifiées avant d'appeler le serveur (qui les vérifie aussi) : un message, ou rien.
  H.checkPassword = function (pw, again) {
    if (pw.length < 12) return H.t('Le mot de passe doit faire au moins 12 caractères.');
    if (pw.length > 200) return H.t('Le mot de passe est trop long (200 caractères au plus).');
    if (pw !== again) return H.t('Les deux mots de passe ne sont pas identiques.');
    return '';
  };
  // Changer son propre nom (le serveur exige le mot de passe actuel ; la session reste valable). `done(compte)` une fois la fenêtre fermée : à l'appelant de redessiner et de rendre le focus.
  H.changeMyName = function (done) {
    var me = H.session.user || {};
    H.form({ title: H.t('Changer mon nom'), ok: H.t('Changer'),
      fields: [{ key: 'name', label: H.t('Nouveau nom'), value: me.name, auto: 'username' }, { key: 'current', label: H.t('Mot de passe actuel'), type: 'password', auto: 'current-password' }],
      submit: function (v) {
        if (!v.name) return H.t('Saisis un nom.');
        if (!v.current) return H.t('Saisis ton mot de passe actuel.');
        return H.api('/api/me/name', { current: v.current, name: v.name });
      },
      done: function (user) {
        H.session.user = user;           // l'accueil (« Bonjour … ») et Mon compte lisent le nom ici
        H.toast(H.t('Nom changé. Utilise-le à ta prochaine connexion.'), 5000);
        if (done) done(user);
      } });
  };
  // Changer son propre mot de passe (le serveur exige l'actuel) ; `done` rend le focus à ce qui a ouvert la fenêtre.
  H.changeMyPassword = function (done) {
    H.form({ title: H.t('Changer mon mot de passe'), ok: H.t('Changer'),
      fields: [{ key: 'current', label: H.t('Mot de passe actuel'), type: 'password', auto: 'current-password' }, { key: 'next', label: H.t('Nouveau mot de passe (12 caractères au moins)'), type: 'password', auto: 'new-password' }, { key: 'again', label: H.t('Nouveau mot de passe, encore une fois'), type: 'password', auto: 'new-password' }],
      submit: function (v) {
        if (!v.current) return H.t('Saisis ton mot de passe actuel.');
        var bad = H.checkPassword(v.next, v.again);
        if (bad) return bad;
        return H.api('/api/me/password', { current: v.current, 'new': v.next });   // le serveur renouvelle le cookie : rien à garder ici
      },
      done: function () { H.toast(H.t('Mot de passe changé. Les autres appareils devront se reconnecter.'), 5000); if (done) done(); } });
  };
  // Serveur ouvert (aucun compte) : comment créer le premier compte. Dans « Mon compte » et dans Administration > Comptes.
  H.openServerNote = function () {
    return h('p', { class: 'muted' }, H.t('Aucun compte n’existe : le serveur est ouvert, et toute personne qui peut l’atteindre voit et gère tout. Pour le protéger, crée le premier compte (il sera administrateur) sur le serveur avec '),
      h('code', null, H.t('python -m hub user add <nom>')), H.t(', ou définis '), h('code', null, 'HUB_PASSWORD'), H.t(' avant de démarrer le serveur. Les comptes suivants se gèrent ensuite ici.'));
  };

  // #/settings : ce qui concerne le compte connecté (ses préférences, son compte) et « À propos ». L'administration est sur #/admin.
  var SETTINGS_TABS = [{ id: 'preferences', label: H.t('Préférences') }, { id: 'compte', label: H.t('Mon compte') }, { id: 'apropos', label: H.t('À propos') }];
  var OLD_SETTINGS_TABS = { lecture: 'preferences', interface: 'preferences' };          // les anciens onglets : les adresses et le choix mémorisé y mènent encore
  // Les onglets partis dans Administration : leur ancienne adresse y mène l'administrateur (« comptes » était aussi « Mon compte » : un autre compte y retrouve le sien).
  var MOVED_TABS = { gestion: 'mediatheque', taches: 'mediatheque', serveur: 'serveur', inbox: 'inbox', demandes: 'demandes', comptes: 'comptes' };
  H.route(/^\/settings$/, function (m, query) {
    if (query.tab && MOVED_TABS[query.tab] && H.isAdmin()) {
      var to = '#/admin?tab=' + MOVED_TABS[query.tab];
      return { el: h('section', { class: 'blank' }, h('h1', null, H.t('Administration'))), nav: '#/admin', shown: function () { H.go(to, true); } };
    }
    var el = h('section', { class: 'admin settings' }), state = null, lastPage = '', closed = false;
    var tab = query.tab === 'comptes' ? 'compte' : (query.tab || H.pref('settings.tab') || '');
    function qualityLabel(v) { return { auto: H.t('Qualité d’origine quand c’est possible'), '1080': '1080p · 8 Mb/s', '720': '720p · 4 Mb/s', '480': H.t('480p · 1,5 Mb/s') }[v] || v; }
    function activeTab() {          // un onglet inconnu (ou parti dans Administration) retombe sur le premier
      var wanted = OLD_SETTINGS_TABS[tab] || tab;
      return SETTINGS_TABS.some(function (t) { return t.id === wanted; }) ? wanted : SETTINGS_TABS[0].id;
    }
    function choose(id) { tab = id; H.pref('settings.tab', id); render(); H.focus.key(el, 'tab-' + id); }
    function tabBar() {
      var active = activeTab();
      return h('div', { class: 'tabs', role: 'tablist' }, SETTINGS_TABS.map(function (t) {
        return h('button', { class: 'chip tab f' + (t.id === active ? ' on' : ''), role: 'tab', 'aria-selected': t.id === active ? 'true' : 'false', 'data-key': 'tab-' + t.id,
          'data-autofocus': t.id === active ? '1' : null, onclick: function () { choose(t.id); } }, t.label);
      }));
    }
    function render() {
      var focus = document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
      H.clear(el);
      el.appendChild(h('h1', null, H.t('Paramètres')));
      var bar = tabBar();
      el.appendChild(bar);
      var body = h('div', { class: 'tabbody' }), active = activeTab();
      if (active === 'preferences') preferences(body);
      else if (active === 'compte') body.appendChild(accountPanel());
      else body.appendChild(aboutPanel());
      el.appendChild(body);
      var on = bar.querySelector('.on');
      if (on) bar.scrollLeft = Math.max(0, on.offsetLeft - 24);       // sur un téléphone, la rangée défile : l'onglet ouvert doit s'y voir
      if (H.overlay.isOpen() || !H.focus.shown(el)) return;             // une fenêtre de saisie est ouverte : on ne lui prend pas le focus ; ni quand la page a été quittée avant la réponse
      if (!(focus && H.focus.key(el, focus, true))) H.focus.first();   // sans défilement
    }
    // Les réglages qu'on touche une fois : l'apparence de ce navigateur, la lecture, l'affichage du compte.
    function preferences(body) {
      interfaceTab(body);
      reading(body);
      body.appendChild(languagesPanel());
      body.appendChild(displayPanel());
    }
    // Les langues du compte (gardées sur le serveur) : le son et les sous-titres que le lecteur prend d'office à l'ouverture d'une vidéo, par ordre de préférence.
    var SUB_MODES = [{ label: H.t('Forcés seulement'), detail: H.t('Les passages dans une autre langue que celle du film'), value: 'forced' },
      { label: H.t('Si le son est étranger'), detail: H.t('Complets quand le son n’est dans aucune de tes langues, sinon forcés'), value: 'foreign' },
      { label: H.t('Toujours'), detail: H.t('Complets, sinon forcés'), value: 'always' },
      { label: H.t('Jamais'), detail: H.t('Aucun sous-titre d’office'), value: 'never' }];
    var MAX_LANGS = 8;          // comme prefs.MAX_LANGS
    function langName(code) { return (H.spoken.filter(function (l) { return l[0] === code; })[0] || [code, code.toUpperCase()])[1]; }
    function saveLangs(name, list, focusKey) {
      H.saveSetting(name, list).then(function () { render(); H.focus.key(el, focusKey); }).catch(function (e) { H.toast(e.message, 5000); });
    }
    // Une liste ordonnée de langues : une ligne pour l'ajout, puis une par langue, avec « Monter » (sauf la première) et « Retirer ». Le focus suit la langue touchée.
    function langList(panel, name, title, empty) {
      var list = H.setting(name) || [], left = H.spoken.filter(function (l) { return list.indexOf(l[0]) < 0; });
      panel.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, title), h('span', null, list.length ? H.t('Dans cet ordre de préférence') : empty)),
        H.button(H.t('Ajouter une langue'), 'plus', { 'data-key': 'add-' + name, disabled: list.length >= MAX_LANGS || !left.length, onclick: function () {
          H.menu(title, left.map(function (l) { return { label: l[1], value: l[0] }; }), null, function (v) { saveLangs(name, list.concat([v]), 'rm-' + name + '-' + v); });
        } })));
      list.forEach(function (code, i) {
        var raised = list.slice(0, i - 1).concat([code, list[i - 1]], list.slice(i + 1));
        panel.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, (i + 1) + '. ' + langName(code))),
          i > 0 ? H.button(H.t('Monter'), null, { 'data-key': 'up-' + name + '-' + code, onclick: function () { saveLangs(name, raised, (i > 1 ? 'up-' : 'rm-') + name + '-' + code); } }) : null,
          H.button(H.t('Retirer'), 'trash', { 'data-key': 'rm-' + name + '-' + code, onclick: function () { saveLangs(name, list.filter(function (c) { return c !== code; }), 'add-' + name); } })));
      });
    }
    function languagesPanel() {
      var panel = h('section', { class: 'panel' }, h('h2', null, H.t('Langues')));
      langList(panel, 'audio_langs', H.t('Langues du son'), H.t('Piste par défaut du fichier'));
      langList(panel, 'sub_langs', H.t('Langues des sous-titres'), H.t('Celle de l’interface'));
      var shown = SUB_MODES.filter(function (c) { return c.value === H.setting('sub_mode'); })[0] || SUB_MODES[0];
      panel.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, H.t('Sous-titres d’office')), h('span', null, shown.label + ' — ' + shown.detail)),
        H.button(H.t('Changer'), 'edit', { 'data-key': 'sub_mode', onclick: function () {
          H.menu(H.t('Sous-titres d’office'), SUB_MODES, shown.value, function (v) {
            H.saveSetting('sub_mode', v).then(function () { render(); H.focus.key(el, 'sub_mode'); }).catch(function (e) { H.toast(e.message, 5000); });
          });
        } })));
      return panel;
    }
    function reading(body) {
      var play = h('section', { class: 'panel' }, h('h2', null, H.t('Lecture')));
      var q = H.pref('quality') || 'auto', surround = !!H.pref('surround');
      play.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, H.t('Qualité')), h('span', null, qualityLabel(q))),
        H.button(H.t('Changer'), 'quality', { 'data-key': 'quality', onclick: function () {
          H.menu(H.t('Qualité de lecture'), ['auto', '1080', '720', '480'].map(function (v) { return { label: qualityLabel(v), value: v }; }), q, function (v) { H.pref('quality', v); render(); H.focus.key(el, 'quality'); });
        } })));
      play.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, H.t('Son surround 5.1')), h('span', null, surround ? H.t('Activé : le 5.1 est envoyé tel quel si l’appareil le lit.') : H.t('Désactivé : le son est ramené en stéréo (le plus sûr).'))),
        H.button(surround ? H.t('Désactiver') : H.t('Activer'), 'audio', { 'data-key': 'surround', onclick: function () { H.pref('surround', !surround); render(); H.focus.key(el, 'surround'); } })));
      body.appendChild(play);
    }
    // L'apparence de l'interface sur ce navigateur : l'appareil, la taille du texte, la langue.
    function interfaceTab(body) {
      var ui = h('section', { class: 'panel' }, h('h2', null, H.t('Interface')));
      var chosen = H.pref('device') || 'auto', names = {};
      H.devices.forEach(function (d) { names[d[0]] = d[1]; });
      ui.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, H.t('Appareil')),
        h('span', null, chosen === 'auto' ? H.t('Détecté : {name} — taille du texte, effets de survol, envoi de photo', { name: names[H.device] }) : H.t('Choisi : {name} — taille du texte, effets de survol, envoi de photo', { name: names[H.device] })), h('span', { class: 'ua' }, navigator.userAgent)),
        H.button(H.t('Changer'), 'quality', { 'data-key': 'device', onclick: function () {
          H.menu(H.t('Type d’appareil'), H.devices.map(function (d) { return { label: d[1], detail: d[0] === 'auto' ? H.t('Deviné d’après le navigateur ({name})', { name: names[H.detectDevice()] }) : '', value: d[0] }; }), chosen, function (v) {
            H.pref('device', v === 'auto' ? null : v); H.applyDevice(); H.nav.build(); H.nav.mark('#/settings'); render(); H.focus.key(el, 'device');          // la barre du haut suit : pas d'entrée « Envoyer » sur une télévision
          });
        } })));
      var sizeNames = {};
      H.textSizes.forEach(function (s) { sizeNames[s[0]] = s[1]; });
      ui.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, H.t('Taille du texte')), h('span', null, H.t('{size} — s’ajoute à la taille propre à l’appareil', { size: sizeNames[H.textSize] }))),
        H.button(H.t('Changer'), 'quality', { 'data-key': 'textSize', onclick: function () {
          H.menu(H.t('Taille du texte'), H.textSizes.map(function (s) { return { label: s[1], detail: s[2], value: s[0] }; }), H.textSize, function (v) {
            H.pref('textSize', v === 'normal' ? null : v); H.applyTextSize(); render(); H.focus.key(el, 'textSize');
          });
        } })));
      var langNames = {};
      H.langs.forEach(function (l) { langNames[l[0]] = l[1]; });
      ui.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, H.t('Langue')), h('span', null, H.t('{lang} — la langue de l’interface, pour ce navigateur', { lang: langNames[H.lang] }))),
        H.button(H.t('Changer'), 'quality', { 'data-key': 'lang', onclick: function () {
          H.menu(H.t('Langue'), H.langs.map(function (l) { return { label: l[1], value: l[0] }; }), H.lang, function (v) { if (v !== H.lang) H.setLang(v); });
        } })));
      body.appendChild(ui);
    }
    // Les réglages d'affichage du compte : chacun se choisit dans une liste.
    var ON_OFF = [{ label: H.t('Affiché'), detail: H.t('Le rayon est sur la page d’accueil'), value: 'show' }, { label: H.t('Masqué'), detail: H.t('Le rayon n’est pas sur la page d’accueil'), value: 'hide' }];
    var DISPLAY = [{ key: 'music_view', title: H.t('Bibliothèque de musique'), note: H.t('Comment elle s’ouvre'),
      choices: [{ label: H.t('Albums'), detail: H.t('Tous les albums, regroupés sous le nom de leur artiste'), value: 'albums' }, { label: H.t('Artistes'), detail: H.t('Un artiste par carte, avec sa photo, sans ses albums'), value: 'artists' }] },
      { key: 'movie_view', title: H.t('Bibliothèque de films'), note: H.t('Comment elle s’ouvre'),
        choices: [{ label: H.t('Sagas'), detail: H.t('Les films d’une même saga sur une seule carte'), value: 'sagas' }, { label: H.t('Films'), detail: H.t('Tous les films, chacun sur sa carte'), value: 'films' }] },
      { key: 'home_shelf_rows', title: H.t('Rangées par rayon'), note: H.t('Sur la page d’accueil'),
        choices: [{ label: H.t('1 rangée'), detail: H.t('Peu'), value: '1' }, { label: H.t('2 rangées'), detail: H.t('Habituel'), value: '2' }, { label: H.t('3 rangées'), detail: H.t('Beaucoup'), value: '3' }] },
      { key: 'home_latest', title: H.t('Nouveautés'), note: H.t('Sur la page d’accueil'), choices: ON_OFF },
      { key: 'home_unseen', title: H.t('Pas encore vu'), note: H.t('Sur la page d’accueil'), choices: ON_OFF },
      { key: 'home_unseen_sort', title: H.t('Pas encore vu : trier par'), note: H.t('Sur la page d’accueil'),
        choices: [{ label: H.t('Au hasard'), detail: H.t('Un ordre qui change chaque jour'), value: 'random' }, { label: H.t('Note TMDB'), detail: H.t('Les mieux notés d’abord, ceux sans note à la fin'), value: 'rating' },
          { label: H.t('Date d’ajout'), detail: H.t('Les plus récemment ajoutés d’abord'), value: 'added' }] },
      { key: 'home_requests', title: H.t('Tes demandes sont arrivées'), note: H.t('Sur la page d’accueil'), choices: ON_OFF },
      { key: 'home_recommend', title: H.t('Parce que tu as aimé…'), note: H.t('Sur la page d’accueil'), choices: ON_OFF }];
    function displayPanel() {
      var panel = h('section', { class: 'panel' }, h('h2', null, H.t('Affichage')));
      DISPLAY.forEach(function (setting) {
        var current = H.setting(setting.key), shown = setting.choices.filter(function (c) { return c.value === current; })[0] || setting.choices[0];
        panel.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, setting.title), h('span', null, setting.note + ' : ' + shown.label)),
          H.button(H.t('Changer'), 'edit', { 'data-key': 'display-' + setting.key, onclick: function () {
            H.menu(setting.title, setting.choices, shown.value, function (v) {
              H.saveSetting(setting.key, v).then(function () { render(); H.focus.key(el, 'display-' + setting.key); }).catch(function (e) { H.toast(e.message, 5000); });
            });
          } })));
      });
      return panel;
    }
    // « Mes demandes » : la page des demandes du compte, depuis « Mon compte ».
    function myRequestsLine() {
      return h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, H.t('Mes demandes')),
        h('span', null, H.canRequest() ? H.t('Les films et les séries que tu as demandés à l’administrateur, et où ils en sont') : H.t('Tu ne peux plus demander de film ni de série ; tes demandes déjà faites restent visibles'))),
        h('a', { class: 'btn f', href: '#/requests', 'data-key': 'my-requests' }, H.icon('queue'), h('span', { class: 'lbl' }, H.t('Mes demandes'))));
    }
    // « Mes suggestions » : les idées et les remarques sur le hub que le compte a envoyées à l'administrateur, depuis « Mon compte ».
    function mySuggestionsLine() {
      return h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, H.t('Mes suggestions')),
        h('span', null, H.t('Une idée, une amélioration ou un bug du hub, à proposer à l’administrateur, et ce qu’il en a fait'))),
        h('a', { class: 'btn f', href: '#/suggestions', 'data-key': 'my-suggestions' }, H.icon('edit'), h('span', { class: 'lbl' }, H.t('Mes suggestions'))));
    }
    // Le compte connecté : nom, rôle, changement de mot de passe, déconnexion. Serveur ouvert : comment créer le premier compte.
    function accountPanel() {
      var panel = h('section', { class: 'panel' }, h('h2', null, H.t('Mon compte')));
      var me = H.session.user;
      if (!H.session.auth || !me) {
        panel.appendChild(H.openServerNote());
        panel.appendChild(myRequestsLine());
        panel.appendChild(mySuggestionsLine());
        return panel;
      }
      panel.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, me.name), h('span', null, H.roles[me.role] || me.role)),
        H.button(H.t('Changer mon nom'), 'edit', { 'data-key': 'myname', onclick: function () { H.changeMyName(function () { load().then(function () { H.focus.key(el, 'myname'); }); }); } }),
        H.button(H.t('Changer mon mot de passe'), 'edit', { 'data-key': 'mypw', onclick: function () { H.changeMyPassword(function () { H.focus.key(el, 'mypw'); }); } }),
        H.button(H.t('Déconnecter les autres appareils'), null, { 'data-key': 'signout-others', onclick: signOutOthers }),
        h('a', { class: 'btn f', href: '/api/logout', 'data-key': 'logout', onclick: function () { H.forgetAccount(); } }, H.t('Se déconnecter'))));
      panel.appendChild(myRequestsLine());
      panel.appendChild(mySuggestionsLine());
      return panel;
    }
    // Les autres appareils du compte (téléphone, télé, navigateur oublié) sont déconnectés ; celui-ci reste connecté : le serveur renouvelle son cookie.
    function signOutOthers() {
      H.form({ title: H.t('Déconnecter les autres appareils'), ok: H.t('Déconnecter'),
        lead: [H.t('Tous les autres appareils connectés à ce compte devront se reconnecter. Celui-ci reste connecté. Ton mot de passe ne change pas.')],
        fields: [{ key: 'current', label: H.t('Mot de passe actuel'), type: 'password', auto: 'current-password' }],
        submit: function (v) {
          if (!v.current) return H.t('Saisis ton mot de passe actuel.');
          return H.api('/api/me/signout-others', { current: v.current });
        },
        done: function () { H.toast(H.t('Les autres appareils sont déconnectés.'), 5000); H.focus.key(el, 'signout-others'); } });
    }
    // Les nouveautés (GET /api/changelog), chargées une fois par ouverture de la page : `news` vaut null tant qu'elles ne sont pas là, [] si la route échoue (rien ne s'affiche).
    var NEWS_SHOWN = 5, news = null, newsAsked = false, newsAll = false;
    function loadNews() {
      if (newsAsked) return;
      newsAsked = true;
      H.api('/api/changelog').then(function (d) { news = (d && d.versions) || []; }).catch(function () { news = []; }).then(function () {
        if (!closed && news.length && activeTab() === 'apropos') render();
      });
    }
    function newsSection(panel) {
      loadNews();
      if (!news || !news.length) return;
      panel.appendChild(h('h2', null, H.t('Nouveautés du hub')));
      (newsAll ? news : news.slice(0, NEWS_SHOWN)).forEach(function (v) { panel.appendChild(H.newsEntry(v)); });
      if (!newsAll && news.length > NEWS_SHOWN) panel.appendChild(h('div', { class: 'newsmore' }, H.button(H.t('Tout voir'), null, { 'data-key': 'news-all', onclick: function () {
        newsAll = true; render(); H.focus.key(el, 'news-' + news[NEWS_SHOWN].version);          // le bouton disparaît : le focus va à la première version dépliée
      } })));
    }
    // À propos : la version, les nouveautés, et la mention et le logo de TMDB (tmdb.svg, le logo court officiel) que demandent les conditions de son API, pour tous les comptes.
    function aboutPanel() {
      var panel = h('section', { class: 'panel about' }, h('h2', null, H.t('À propos')));
      panel.appendChild(h('dl', null, h('dt', null, 'Version'), h('dd', null, state.version)));
      newsSection(panel);
      panel.appendChild(h('p', { class: 'credit-tmdb' }, h('img', { src: '/tmdb.svg', alt: 'TMDB', width: '92', height: '12' }),
        h('span', null, H.t('Ce produit utilise l’API TMDB mais n’est ni approuvé ni certifié par TMDB.'))));
      return panel;
    }
    // L'état du serveur : la version, la session (un droit ou un rôle changé ailleurs se voit dans la barre), les noms des bibliothèques.
    function load() {
      return H.api('/api/state').then(function (s) {
        if (closed) return;
        H.setSession(s);
        H.nav.sync();
        state = s; H.setLibs(s.libraries);
        var page = JSON.stringify(s);
        if (page !== lastPage) render();
        lastPage = page;
      }).catch(function (e) { if (!closed) H.toast(e.message, 5000); });
    }
    load();
    return { el: el, nav: '#/settings', refresh: load, destroy: function () { closed = true; } };          // au retour (« Mes demandes ») : la session a pu changer
  });
})();
