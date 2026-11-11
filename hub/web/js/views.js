/* Les pages : accueil, bibliothèque, fiche, recherche, gestion. */
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
      nav.appendChild(h('a', { class: 'tool f', href: '#/settings', 'data-nav': '#/settings', 'aria-label': H.t('Paramètres') }, H.icon('gear')));
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
    else if (it.type === 'show') { if (it.year) parts.push(it.year); parts.push(H.t('{n} ép.', { n: it.n })); }
    else if (it.type === 'series') { parts.push(H.plural(it.n, 'volume')); if (it.missing) parts.push(H.plural(it.missing, 'manquant')); }
    else { if (it.year) parts.push(it.year); }
    return parts.join(' · ');
  }
  function sagaHref(libId, name) { return '#/saga/' + libId + '?g=' + encodeURIComponent(name); }
  function personHref(name) { return '#/person?n=' + encodeURIComponent(name); }
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
  // Un film qui manque à une saga : son affiche grisée, rien à ouvrir ni à lire (la case n'est donc pas dans la navigation à la télécommande, comme un album manquant).
  // `hide` (administrateur, film que seule la collection TMDB compte) : un bouton « Ne pas afficher » sous la case.
  // Sans `rank`, pas de pastille de rang (les films d'une personne ne se suivent pas) ; `note` remplace « Manquant » sous le titre (son rôle : « Acteur »).
  function missingCard(m, hide) {
    var pic = H.picture(m.cover ? m.cover + '?w=300' : '', m.title, '');
    if (m.rank) pic.appendChild(h('span', { class: 'part' }, m.rank));
    var state = m.coming ? H.t('pas encore sorti') : 'manquant';           // un film annoncé est grisé comme les autres, mais il ne manque à personne
    var card = h('div', { class: 'card wanted', title: m.title + ' (' + state + ')' }, pic, h('div', { class: 't' }, m.title), h('div', { class: 's' }, [m.year, m.note || (m.coming ? H.t('À venir') : H.t('Manquant'))].filter(Boolean).join(' · ')));
    if (!hide || !m.ignorable) return card;
    return h('div', { class: 'mcell' }, card, H.button(H.t('Ne pas afficher'), 'close', { class: 'mhide', 'data-key': 'hide-' + m.tmdb, 'aria-label': H.t('Ne pas afficher {title} comme manquant', { title: m.title }), onclick: function () { hide(m); } }));
  }
  // Combien de ces films manquent vraiment : ceux qui ne sont pas encore sortis ne comptent pas.
  function lacking(absent) { return (absent || []).filter(function (m) { return !m.coming; }).length; }
  // Les films qu'on a et ceux qui manquent, rangés ensemble par leur rang dans la saga ; `card(film)` fait la case d'un film qu'on a.
  function sagaCells(films, absent, card, hide) {
    var cells = films.map(function (f, i) { return { rank: f.part === undefined || f.part === null ? i + 1 : f.part, node: card(f) }; });
    (absent || []).forEach(function (m) { cells.push({ rank: m.rank, node: missingCard(m, hide) }); });
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
    var total = it.n || 1;
    if (it.seen >= total && it.seen > 0) pic.appendChild(h('span', { class: 'badge done' }, H.icon('check')));
    else if (it.seen || it.started) pic.appendChild(h('span', { class: 'badge' }, it.type === 'movie' ? H.t('En cours') : (it.seen || 0) + '/' + total));
    if (square && H.music) pic.appendChild(H.music.cardTools(it));
    if (it.type === 'movie' || it.type === 'show') { pic.appendChild(H.actions.tools(it)); H.actions.bind(a, it); }
    if (withPart && it.part !== undefined && it.part !== null) pic.appendChild(h('span', { class: 'part' }, it.part));
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
  // `n` : au plus ce nombre en tout. Le serveur a déjà fait ce qui est propre à une bibliothèque : en musique, l'arrivée d'un artiste n'est qu'une carte (voir recent_music).
  var LATEST = 24;          // quatre lignes pleines de six, trois de huit
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
  // « Pas encore vu » : un rayon par bibliothèque que le serveur dit avoir un `unseen` (films, séries, animations… et toute bibliothèque vidéo ajoutée plus tard), dans l'ordre des bibliothèques.
  // Chaque rayon : ce que le serveur propose, sans ce que `taken` (les nouveautés montrées) affiche déjà, `shown` au plus (le réglage du compte, 12 par défaut) ; une bibliothèque sans rien à proposer n'a pas de rayon.
  var UNSEEN_SHOWN = 12;
  H.unseen = function (libraries, taken, shown) {
    shown = shown > 0 ? shown : UNSEEN_SHOWN;
    var skip = {};
    taken.forEach(function (e) { skip[e.item.id] = true; });
    return libraries.map(function (lib) {
      return { lib: lib, items: (lib.unseen || []).filter(function (it) { return !skip[it.id]; }).slice(0, shown) };
    }).filter(function (shelf) { return shelf.items.length; });
  };
  function cardsSection(cls, title, entries) {
    return h('section', { class: 'shelf ' + cls }, h('h2', null, title), h('div', { class: 'grid' }, entries.map(latestCard)));
  }
  // Le rayon d'une bibliothèque : son titre est un lien vers elle, précédé du point de sa couleur (comme dans la recherche).
  function libraryShelf(cls, title, lib, items) {
    return h('section', { class: 'shelf ' + cls, 'data-lib': lib.id },
      h('h2', null, h('a', { class: 'f head', href: '#/l/' + lib.id }, title + ' · ' + H.libLabel(lib))),
      h('div', { class: 'grid' }, items.map(function (it) { return H.card(it, false); })));
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
            h('h1', null, H.t('Ta médiathèque est vide pour l’instant')),
            h('p', null, !H.isAdmin() ? H.t('Rien n’est encore indexé : l’administrateur doit lancer une mise à jour de la médiathèque.') : H.libs.length ? H.t('Les dossiers sont lus au premier démarrage. Si tu viens d’y ajouter des fichiers, lance une mise à jour.') : H.t('Aucun dossier de bibliothèque trouvé : monte tes dossiers sous /srv (Films, Series, Animations, Musique, Livres), ou décris-les avec HUB_LIBRARIES.')),
            H.isAdmin() ? h('a', { class: 'btn f primary', href: '#/settings?tab=gestion', 'data-autofocus': '1' }, H.t('Ouvrir les paramètres')) : null));
          clearTimeout(timer);
          timer = setTimeout(function () { if (document.body.contains(el) && !document.hidden) load(); }, 4000);
          return;
        }
        var me = H.session.auth && H.session.user && H.session.user.name;
        el.appendChild(h('h1', { class: 'greeting' }, me ? H.t('Bonjour {name}', { name: me }) : H.t('Bonjour')));
        if (data.continue.length) el.appendChild(row(H.t('Reprendre'), null, data.continue.map(function (c) { return resumeCard(c, dismissed); })));
        var latest = H.setting('home_latest') === 'hide' ? [] : H.latest(data.libraries, LATEST);          // chaque rayon se masque dans les réglages du compte
        if (latest.length) el.appendChild(cardsSection('latest', H.t('Nouveautés'), latest));
        var unseen = H.setting('home_unseen') === 'hide' ? [] : H.unseen(data.libraries, latest, parseInt(H.setting('home_unseen_count'), 10));
        unseen.forEach(function (shelf) {
          el.appendChild(libraryShelf('unseen', H.t('Pas encore vu'), shelf.lib, shelf.items));
        });
        if (!data.continue.length && !latest.length && !unseen.length) libraryTiles(data.libraries).forEach(function (node) { el.appendChild(node); });       // aucun rayon : les bibliothèques, pour ne pas laisser une page nue
        if (!document.body.contains(el) || el.style.display === 'none') return;       // une réponse tardive, la page a été quittée : `H.focus.first()` prendrait le focus de la page courante (la barre de la liseuse, dont les flèches ne tournent plus les pages)
        if (typeof focusKey === 'string' && H.focus.key(el, focusKey)) return;
        var a = document.activeElement;
        if (!a || a === document.body || (a.closest && a.closest('#nav'))) H.focus.first();
      }).catch(function (e) { H.clear(el).appendChild(h('div', { class: 'blank' }, h('h1', null, H.t('Le serveur ne répond pas')), h('p', null, e.message), h('button', { class: 'btn f', onclick: load, 'data-autofocus': '1' }, H.t('Réessayer')))); });
    }
    load();
    return { el: el, nav: '#/', refresh: load, destroy: function () { clearTimeout(timer); } };
  });

  // --- Bibliothèque ----------------------------------------------------------------
  var SORTS = {
    movies: [['title', H.t('Titre')], ['added', H.t('Ajouts récents')], ['year', H.t('Plus récents')], ['year-asc', H.t('Plus anciens')], ['rating', H.t('Mieux notés')]],      // films et séries ; « Mieux notés » : la note TMDB
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
    // Les films d'animation sont dans Films, avec le genre « Animation » : un raccourci depuis la page Animations, si le compte voit Films ; il se range à droite de la rangée des filtres
    var related = libId === 'animations' && H.lib('films') ? h('div', { class: 'related' }, h('a', { class: 'chip f', href: '#/l/films?genre=Animation' }, H.t('Films d’animation'))) : null;
    var tools = h('div', { class: 'tools' }, search, bGenre, kind === 'music' ? null : bSort, kind === 'music' ? null : bState, kind === 'music' ? bLists : null, related, count);
    var quick = h('div', { class: 'chips quick', role: 'group', 'aria-label': H.t('Accès rapides'), hidden: true });
    var empty = h('div', { class: 'blank', hidden: true });
    var el = h('section', { class: 'library' }, h('h1', null, lib ? H.libLabel(lib) : H.t('Bibliothèque')), kind === 'books' ? quick : null, tools, grid, empty, sentinel);

    // « Genre : Tous » : le nom du filtre en gras, sa valeur en poids normal ; la largeur du bouton est fixée par le CSS (.flt), la valeur trop longue est coupée
    function label(btn, name, value) { btn.title = H.t('{name} :', { name: name }) + ' ' + value; H.clear(btn).appendChild(h('span', { class: 'lbl' }, [h('b', null, H.t('{name} :', { name: name })), ' ', h('span', { class: 'val' }, value)])); }
    function paint() {
      label(bGenre, H.t('Genre'), st.genre || H.t('Tous'));
      label(bSort, H.t('Tri'), (sorts.filter(function (s) { return s[0] === st.sort; })[0] || sorts[0])[1]);
      label(bState, H.t('Statut'), (stateChoices(kind).filter(function (c) { return c[0] === st.state; })[0] || stateChoices(kind)[0])[1]);
      bGenre.hidden = !facets.genres.length;
      var word = { movies: 'film', shows: lib && lib.id === 'animations' ? 'animation' : 'série', music: artistMode() ? 'artiste' : 'album', books: 'série' }[kind] || 'élément';
      var music = kind === 'music' && typeof facets.albums === 'number';          // la musique compte toujours les artistes et les albums de ce que les filtres laissent, quelle que soit la vue
      count.textContent = !total ? '' : music ? H.plural(facets.artists, 'artiste') + ' · ' + H.plural(facets.albums, 'album') : sagaMode() && facets.sagas ? H.plural(facets.films, 'film') + ' · ' + H.plural(facets.sagas, 'saga') : H.plural(total, word);
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
      return out;
    }
    // Les autres boutons : vu / non vu, saga, télécharger, affiche, fiche TMDB, liens.
    function otherButtons(d, view) {
      var out = [], entries = d.entries;
      if (d.type !== 'album' && entries.length) out.push(H.button(H.actions.seenLabel(d.type, view.allDone), 'check', { onclick: function () { setSeen(!view.allDone); } }));
      if (d.type === 'movie' && d.tmdb && H.isAdmin() && H.sagas) out.push(H.button(H.t('Ajouter à une saga'), 'plus', { 'data-key': 'saga-add', onclick: function () { H.sagas.addTo(d, load); } }));      // sans identifiant TMDB, un film ne peut pas entrer dans une saga
      if (entries.length && H.actions.canDownload(d.library && d.library.id)) out.push(H.button(d.type === 'album' ? H.t('Télécharger l’album') : H.t('Télécharger'), 'download', { 'data-key': 'download', onclick: function () { H.actions.downloadItem(d); } }));
      return out.concat(H.actions.posterTools(d, load), H.actions.tmdbTools(d, load),
        (d.links || []).map(function (l) { return h('a', { class: 'btn f', href: l.url, target: '_blank', rel: 'noopener noreferrer' }, H.tr(l.label)); }));
    }
    function actionsRow(d, view) {
      var actions = h('div', { class: 'actions' });
      (d.type === 'album' ? albumButtons(d) : playButtons(d, view)).concat(otherButtons(d, view)).forEach(function (node) { actions.appendChild(node); });
      return actions;
    }

    // Sous la ligne grise, comme sur TMDB : le disque de la note, et à côté ce qu'il mesure et sur combien de votes.
    function scoreRow(d) {
      return h('div', { class: 'rate' }, H.scoreRing(d, true),
        h('span', { class: 'what', 'aria-hidden': 'true' }, h('b', null, H.t('Note des utilisateurs')), h('small', null, H.t('{n} votes sur TMDB', { n: H.number(d.tmdb_votes) }))));
    }

    // Le texte à droite de l'image : titre, ligne grise, note TMDB, genres, crédits, résumé, doutes TMDB, boutons.
    function infoBlock(d, view) {
      var genres = (d.genres || []).join(', ');
      var info = h('div', { class: 'info' }, h('h1', null, d.title), h('p', { class: 'meta' }, metaLine(d)));
      if (d.tmdb_rating) info.appendChild(scoreRow(d));
      if (genres) info.appendChild(h('p', { class: 'genres' }, genres));
      credits(d).forEach(function (node) { info.appendChild(node); });
      if (d.summary) info.appendChild(h('p', { class: 'synopsis' }, d.summary));
      if (d.doubts && d.doubts.length && H.isAdmin()) info.appendChild(doubtNote(d));
      info.appendChild(actionsRow(d, view));
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
        H.button('', 'more', { class: 'epmore', 'aria-label': H.t('Actions de l’épisode'), 'data-key': 'm' + e.id, onclick: function () { H.actions.episodeMenu(e, load, d.library && d.library.id); } }));
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
      var list = h('div', { class: 'eps' }), blurb = h('p', { class: 'seasonsum' }), bSeason = null;
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
      sec.appendChild(chips); sec.appendChild(blurb); sec.appendChild(list);
      fill();
      return sec;
    }
    function volumes(d) {
      var sec = h('section', { class: 'volumes' }, h('h2', null, d.units ? H.t(d.units).charAt(0).toUpperCase() + H.t(d.units).slice(1) : 'Volumes'));
      var grid = h('div', { class: 'grid small' });
      d.entries.forEach(function (e) {
        var p = entryProgress(e), done = e.progress && e.progress.done;
        var pic = H.picture('/img/entry/' + e.id + '?w=300', e.title || d.title);
        if (H.actions.canDownload(d.library && d.library.id)) pic.appendChild(H.actions.entryTools(e, load, d.library.id));
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
        h('div', { class: 'grid' }, sagaCells(films, absent, function (f) { return H.card(f, false, true); }, H.isAdmin() ? hide : null)))));
      if (focusKey) H.focus.key(el, focusKey); else H.focus.first();
    }
    // Marque tous les films de la saga vus (ou non vus) : un appel par film, comme le menu « … » d'une carte de saga.
    function setSeen(done) {
      Promise.all(data.items.map(function (f) { return H.api('/api/item/' + f.id + '/seen', { done: done }); }))
        .then(function () { H.toast(done ? H.t('Saga marquée comme vue') : H.t('Saga marquée comme non vue')); return load(); })
        .catch(function (e) { H.toast(e && e.message ? e.message : H.t('Impossible pour le moment.'), 5000); return load(); });
    }
    // Cache un film que la collection TMDB compte et pas nous : il ne sera plus montré comme manquant (Paramètres > Gestion > Sagas le rétablit).
    function hide(m) {
      H.api('/api/sagas/ignore', { tmdb: m.tmdb }).then(function () {
        H.toast(H.t('« {title} » ne sera plus montré comme manquant (Paramètres > Gestion > Sagas pour le rétablir)', { title: m.title }), 5000);
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
        if (!res.results.length && !people.length) { out.appendChild(h('p', { class: 'muted' }, H.t('Aucun résultat pour « {q} ».', { q: q }))); return; }
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
      if (!free || active === button || !document.body.contains(el) || el.style.display === 'none' || H.overlay.isOpen()) return;          // sur le bouton « Chercher… », on y reste
      if (key && H.focus.key(out, key, true)) return;
      H.focus.set(out.querySelector('.card') || out.querySelector('.f'), true);
    }
    // Une page sans carte n'aurait rien à atteindre à la télécommande : un lien vers la recherche.
    function say(text) {
      H.clear(out).appendChild(h('p', { class: 'muted' }, text));
      out.appendChild(h('p', null, h('a', { class: 'btn f', href: '#/search', 'data-autofocus': '1' }, H.t('Rechercher'))));
      settle();
    }
    // Les films de la personne qu'on n'a pas (`missing` : voir docs/api.md) : des cases grisées, ni focus ni clic (comme un album manquant) ; son rôle sous le titre, pas de rang.
    function missingShelf(list) {
      var cards = list.map(function (m) { return missingCard({ tmdb: m.tmdb, title: m.title, year: m.year, cover: m.cover, note: ROLE_NAMES[m.role] || '' }); });
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
      if (failed(job)) { H.toast(H.t('La recherche a échoué : voir les tâches dans Gestion.'), 5000); return null; }
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

  // --- Gestion ---------------------------------------------------------------------
  function ago(ts) {
    if (!ts) return H.t('jamais');
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return H.t('à l’instant');
    if (s < 5400) return H.t('il y a {n} min', { n: Math.round(s / 60) });
    if (s < 129600) return H.t('il y a {n} h', { n: Math.round(s / 3600) });
    return H.t('il y a {n} j', { n: Math.round(s / 86400) });
  }
  var ROLES = { admin: H.t('Administrateur'), user: H.t('Utilisateur') };
  function checkPassword(pw, again) {
    if (pw.length < 12) return H.t('Le mot de passe doit faire au moins 12 caractères.');
    if (pw.length > 200) return H.t('Le mot de passe est trop long (200 caractères au plus).');
    if (pw !== again) return H.t('Les deux mots de passe ne sont pas identiques.');
    return '';
  }

  // `#/admin` est l'ancienne adresse de la page : les signets et les liens déjà donnés continuent d'y mener.
  H.route(/^\/(?:admin|settings)$/, function (m, query) {
    var el = h('section', { class: 'admin' }), timer = null, wasBusy = false, wasAdmin = null, busy = false, state = null, jobs = [], users = null, usersError = '', logPanel = null, lastPage = '', lastTasks = '', libsOpen = false, libsPanelNode = null, closed = false;
    var tab = query.tab || H.pref('settings.tab') || '', openJobs = {};
    function qualityLabel(v) { return { auto: H.t('Qualité d’origine quand c’est possible'), '1080': '1080p · 8 Mb/s', '720': '720p · 4 Mb/s', '480': H.t('480p · 1,5 Mb/s') }[v] || v; }
    function start(path, body, text) {
      return H.api(path, body).then(function () { H.toast(text); poll(); }).catch(function (e) { H.toast(e.message, 5000); });
    }
    var TABS = [{ id: 'gestion', label: H.t('Gestion'), admin: true }, { id: 'inbox', label: H.t('Boîte de réception'), admin: true }, { id: 'preferences', label: H.t('Préférences') }, { id: 'comptes', label: H.t('Comptes') }];
    var OLD_TABS = { lecture: 'preferences', interface: 'preferences', taches: 'gestion', serveur: 'gestion' };          // les anciens onglets : les adresses et le choix mémorisé y mènent encore
    function visibleTabs() { return TABS.filter(function (t) { return !t.admin || H.isAdmin(); }); }
    function activeTab() {          // un onglet réservé à l'administrateur ne s'ouvre pas pour un autre compte : on retombe sur le premier
        var ids = visibleTabs().map(function (t) { return t.id; });
        var wanted = OLD_TABS[tab] || tab;
        return ids.indexOf(wanted) >= 0 ? wanted : ids[0];
    }
    function choose(id) { tab = id; H.pref('settings.tab', id); render(); H.focus.key(el, 'tab-' + id); }
    function tabBar() {
      var active = activeTab();
      return h('div', { class: 'tabs', role: 'tablist' }, visibleTabs().map(function (t) {
        return h('button', { class: 'chip tab f' + (t.id === active ? ' on' : '') + (t.id === 'gestion' && busy ? ' busy' : ''), role: 'tab', 'aria-selected': t.id === active ? 'true' : 'false', 'data-key': 'tab-' + t.id,
          'data-autofocus': t.id === active ? '1' : null, onclick: function () { choose(t.id); } }, t.label);
      }));
    }
    function render() {
      var focus = document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
      var kept = logStates(logPanel);
      H.clear(el);
      logPanel = null; libsPanelNode = null;
      el.appendChild(h('h1', null, H.t('Paramètres')));
      var bar = tabBar();
      el.appendChild(bar);
      var body = h('div', { class: 'tabbody' }), active = activeTab();
      if (active === 'gestion') gestion(body);
      else if (active === 'inbox') body.appendChild(H.upload.adminPanel());
      else if (active === 'preferences') preferences(body);
      else accounts(body);
      el.appendChild(body);
      if (logPanel) logRestore(logPanel, kept);          // un journal lu à mi-chemin le reste quand la page est refaite (une tâche qui se termine)
      var on = bar.querySelector('.on');
      if (on) bar.scrollLeft = Math.max(0, on.offsetLeft - 24);       // sur un téléphone, la rangée défile : l'onglet ouvert doit s'y voir (après le contenu : lire offsetLeft sur une page à moitié reconstruite la ramenait en haut)
      wasAdmin = H.isAdmin();
      if (H.overlay.isOpen()) return;                                   // une fenêtre de saisie est ouverte : on ne lui prend pas le focus
      if (!(focus && H.focus.key(el, focus, true))) H.focus.first();   // sans défilement
    }
    function refreshCovers(l) {
      H.api('/api/images/refresh', { lib: l.id }).then(function (r) { H.toast(r.removed > 1 ? H.t('Covers à relire : {name} ({n} vignettes oubliées)', { name: H.libLabel(l), n: r.removed }) : H.t('Covers à relire : {name} ({n} vignette oubliée)', { name: H.libLabel(l), n: r.removed })); })
        .catch(function (e) { H.toast(e.message, 5000); });
    }
    // Les films des personnes d'une bibliothèque de films ou de séries (animations comprises) : une tâche de fond par bibliothèque, qui n'a de sens qu'avec une clé TMDB.
    // Pour des films, c'est « Films manquants » ; pour des séries, les identifiants des acteurs viennent de « Enrichir (TMDB) ».
    function categoryButton(l) {
      if (!state.tmdb || (l.kind !== 'movies' && l.kind !== 'shows')) return null;
      var shows = l.kind === 'shows';
      return H.button(shows ? H.t('Films des personnes (TMDB)') : H.t('Films manquants'), null, { 'data-key': 'people-' + l.id, title: shows ? H.t('Demande à TMDB les films des acteurs de cette bibliothèque, pour voir sur la page de chacun ceux qui ne sont pas dans ta médiathèque. Les identifiants viennent de « Enrichir (TMDB) » : lance-le d’abord une fois.') : H.t('Demande à TMDB les films des réalisateurs et des acteurs de cette bibliothèque, pour voir sur la page de chacun ceux qui ne sont pas dans ta médiathèque. Compte une heure ou plus la première fois (un appel par personne, puis les affiches) ; une personne déjà connue depuis moins d’un mois n’est pas redemandée.'),
        onclick: function () { start('/api/enrich/people', { lib: l.id }, H.t('Recherche des films manquants lancée : {name}', { name: H.libLabel(l) })); } });
    }
    // Les cinq bibliothèques tiennent sur une ligne tant qu'on ne les déplie pas : « Tout mettre à jour » suffit presque toujours.
    function libsPanel() {
      var libs = h('section', { class: 'panel' }, h('div', { class: 'phead' }, h('h2', null, H.t('Bibliothèques')),
        H.button(H.t('Tout mettre à jour'), 'refresh', { class: 'primary', 'data-key': 'scan-all', title: H.t('Relit les cinq bibliothèques ; seuls les fichiers modifiés sont relus.'), onclick: function () { start('/api/scan', {}, H.t('Mise à jour lancée')); } })));
      libs.appendChild(h('button', { class: 'jhead f', 'aria-expanded': libsOpen ? 'true' : 'false', 'data-key': 'libs-toggle',
        onclick: function () { libsOpen = !libsOpen; showLibs('libs-toggle'); } }, h('span', { class: 'jt' }, h('b', null, H.t('Détail par bibliothèque')), h('span', null, H.plural(state.libraries.length, 'bibliothèque')))));
      if (!libsOpen) return libs;
      state.libraries.forEach(function (l) {
        libs.appendChild(h('div', { class: 'line', 'data-lib': l.id },
          h('div', { class: 'what' }, h('b', null, H.libLabel(l)), h('span', null, l.exists ? H.plural(l.count, 'élément') + ' · ' + H.t('mis à jour {when}', { when: ago(l.scanned) }) : H.t('dossier introuvable'))),
          l.kind === 'shows' ? H.button(H.t('Enrichir (TMDB)'), null, { 'data-key': 'enrich-' + l.id, onclick: function () { start('/api/enrich', { lib: l.id }, H.t('Enrichissement lancé : {name}', { name: H.libLabel(l) })); } }) : null,
          l.kind === 'movies' ? H.button(H.t('Sagas (TMDB)'), null, { 'data-key': 'enrich-' + l.id, title: H.t('Cherche chez TMDB les films qui manquent à tes sagas, et la note de chaque film.'), onclick: function () { start('/api/enrich', { lib: l.id }, H.t('Recherche des films manquants lancée : {name}', { name: H.libLabel(l) })); } }) : null,
          categoryButton(l),
          l.kind === 'music' ? H.button(H.t('Fiches artistes'), null, { 'data-key': 'enrich-' + l.id, onclick: function () { start('/api/enrich', { lib: l.id }, H.t('Fiches artistes demandées : {name}', { name: H.libLabel(l) })); } }) : null,
          l.kind === 'movies' ? H.button(H.t('Relire les covers'), null, { 'data-key': 'covers-' + l.id, title: H.t('Vide les vignettes gardées : chacune se refait depuis folder.jpg ou la jaquette du .mkv. Les affiches choisies à la main restent.'), onclick: function () { refreshCovers(l); } }) : null,
          H.button(H.t('Mettre à jour'), 'refresh', { 'data-key': 'scan-' + l.id, disabled: false, onclick: function () { start('/api/scan', { lib: l.id }, H.t('Mise à jour lancée : {name}', { name: H.libLabel(l) })); } }),
          H.isAdmin() ? H.button(H.t('Renommer'), 'edit', { 'data-key': 'rename-' + l.id, 'aria-label': H.t('Renommer la bibliothèque {name}', { name: H.libLabel(l) }), onclick: function () { renameLibrary(l); } }) : null));
      });
      return libs;
    }
    // Change le nom AFFICHÉ d'une bibliothèque (jamais son identifiant) ; un champ vide, ou « Rétablir », rend le nom d'origine. Le serveur nettoie le nom et refuse un doublon : son message s'affiche dans la fenêtre, qui reste ouverte.
    function renameLibrary(l) {
      var current = l.label, renamed = H.libRenamed(l);
      function save(text) { return H.api('/api/library/' + encodeURIComponent(l.id) + '/label', { label: text }); }
      H.form({ title: H.t('Renommer la bibliothèque « {name} »', { name: H.libLabel(l) }), ok: H.t('Enregistrer'),
        lead: [H.t('Seul le nom affiché change : les droits, la progression et les fichiers restent attachés à la bibliothèque.')],
        fields: [{ key: 'label', label: H.t('Nom affiché (40 caractères au plus)'), type: 'text', maxlength: 40, value: current }],
        alt: renamed ? { label: H.t('Rétablir « {name} »', { name: H.tr(l.default_label) }), submit: function () { return save(''); } } : null,
        submit: function (v) { return v.label === current || (!v.label && !renamed) ? Promise.resolve(null) : save(v.label); },          // rien à changer : on ferme sans rien demander au serveur
        done: function (res) {
          if (!res) return;
          H.toast(H.libRenamed(res) ? H.t('Bibliothèque renommée : {name}', { name: res.label }) : H.t('Nom d’origine rétabli : {name}', { name: H.libLabel(res) }));
          poll().then(function () { H.focus.key(el, 'rename-' + l.id); });          // relit /api/state : la barre, les pages et la grille des droits prennent le nouveau nom (H.setLibs)
        } });
    }
    // Remplace le panneau des bibliothèques sur place (la page garde son défilement), en rendant le focus à ce qui l'avait.
    function showLibs(key) {
      var fresh = libsPanel();
      libsPanelNode.parentNode.replaceChild(fresh, libsPanelNode);
      libsPanelNode = fresh;
      H.focus.key(fresh, key, true);
    }
    // Gestion : les bibliothèques, les sagas, l'entretien, puis ce qui se lit plus qu'il ne se règle : les tâches que les boutons ci-dessus lancent, et le serveur.
    function gestion(body) {
      libsPanelNode = libsPanel();
      body.appendChild(libsPanelNode);
      body.appendChild(h('section', { class: 'panel' }, h('h2', null, H.t('Sagas')), h('div', { class: 'line' },
        h('div', { class: 'what' }, h('b', null, H.t('Sagas composées à la main')), h('span', null, H.t('Une trilogie que TMDB ne réunit pas, l’ordre du MCU… : des listes de films dans l’ordre de ton choix, en plus de la saga de l’étiquette.'))),
        h('a', { class: 'btn f', href: '#/sagas', 'data-key': 'sagas' }, H.icon('edit'), h('span', { class: 'lbl' }, H.t('Composer'))))));

      var tools = h('section', { class: 'panel' }, h('h2', null, H.t('Entretien')));
      tools.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, H.t('Fichiers parasites')), h('span', null, H.t('Thumbs.db, desktop.ini, .DS_Store et les AlbumArt_{…}_Large / _Small.jpg semés par Windows et macOS ; en musique, toute image autre que cover.jpg (ou .png…), la seule pochette d’un album, y compris folder.jpg.'))),
        H.button(H.t('Supprimer'), null, { 'data-key': 'junk-del', onclick: confirmCleanup })));
      body.appendChild(tools);
      tasks(body);
      serverFacts(body);
    }
    // Les réglages qu'on touche une fois : l'apparence de ce navigateur, la lecture, l'affichage du compte.
    function preferences(body) {
      interfaceTab(body);
      reading(body);
      body.appendChild(displayPanel());
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
      { key: 'home_latest', title: H.t('Nouveautés'), note: H.t('Sur la page d’accueil'), choices: ON_OFF },
      { key: 'home_unseen', title: H.t('Pas encore vu'), note: H.t('Sur la page d’accueil'), choices: ON_OFF },
      { key: 'home_unseen_count', title: H.t('Pas encore vu : cartes par rayon'), note: H.t('Sur la page d’accueil'),
        choices: [['6', H.t('Peu')], ['12', H.t('Habituel')], ['18', H.t('Beaucoup')], ['24', H.t('Le maximum')]].map(function (c) { return { label: H.t('{n} cartes', { n: c[0] }), detail: c[1], value: c[0] }; }) },
      { key: 'home_unseen_sort', title: H.t('Pas encore vu : trier par'), note: H.t('Sur la page d’accueil'),
        choices: [{ label: H.t('Au hasard'), detail: H.t('Un ordre qui change chaque jour'), value: 'random' }, { label: H.t('Note TMDB'), detail: H.t('Les mieux notés d’abord, ceux sans note à la fin'), value: 'rating' },
          { label: H.t('Date d’ajout'), detail: H.t('Les plus récemment ajoutés d’abord'), value: 'added' }] }];
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
    function accounts(body) {
      body.appendChild(accountPanel());
      if (H.session.auth && H.isAdmin()) body.appendChild(usersPanel());
    }
    function tasks(body) {
      logPanel = jobsPanel();
      body.appendChild(logPanel);
    }
    function serverFacts(body) {
      var server = h('section', { class: 'panel' }, h('h2', null, H.t('Serveur')));
      var facts = [['Version', state.version], ['ffmpeg', state.ffmpeg ? H.t('présent') : H.t('absent : lecture directe uniquement')], ['TMDB', state.tmdb ? H.t('clé configurée') : H.t('pas de clé (TMDB_KEY)')],
        [H.t('Conversions'), H.t('{n} en cours sur {max}', { n: state.streams, max: state.max_streams })], [H.t('Accès'), state.auth ? H.t('protégé par mot de passe') : H.t('sans mot de passe')]];
      server.appendChild(h('dl', null, facts.map(function (f) { return [h('dt', null, f[0]), h('dd', null, f[1])]; })));
      // La mention et le logo de TMDB (tmdb.svg, le logo court officiel), que demandent les conditions de son API.
      server.appendChild(h('p', { class: 'credit-tmdb' }, h('img', { src: '/tmdb.svg', alt: 'TMDB', width: '92', height: '12' }),
        h('span', null, H.t('Ce produit utilise l’API TMDB mais n’est ni approuvé ni certifié par TMDB.'))));
      body.appendChild(server);
    }
    // Supprimer les fichiers parasites ne s'annule pas : on demande, en rappelant ce que la dernière simulation en a compté.
    // « Compter » relance une simulation depuis la fenêtre et en met le nombre à jour sans la fermer.
    function confirmCleanup() {
      var sim = jobs.filter(function (j) { return j.status === 'done' && j.result && j.label.indexOf('simulation') >= 0; })[0];
      var counting = false;
      function said(found) { return found === 1 ? H.t('La dernière simulation en a compté 1.') : H.t('La dernière simulation en a compté {n}.', { n: found }); }
      var count = h('p', { class: 'lead', role: 'status' }, sim ? said(sim.result.found) : H.t('Aucune simulation faite : lance « Compter » pour savoir combien il y en a.'));
      var sample = h('ul', { class: 'sample', 'aria-label': H.t('Les premiers fichiers qui seraient supprimés') });
      function listed(result) {          // les premiers fichiers que la simulation a nommés, et combien d'autres
        H.clear(sample);
        var names = (result && result.sample) || [];
        names.forEach(function (name) { sample.appendChild(h('li', null, name)); });
        if (result && result.found > names.length) sample.appendChild(h('li', { class: 'more' }, H.t('… et {n} autre(s)', { n: result.found - names.length })));
      }
      listed(sim && sim.result);
      var recount = H.button(H.t('Compter'), null, { 'data-key': 'junk-recount', 'aria-label': H.t('Compter de nouveau les fichiers parasites'), onclick: function () {
        if (counting) return;
        counting = true; count.textContent = H.t('Comptage en cours…'); listed(null);
        function wait(id) {
          return H.api('/api/jobs').then(function (r) {
            var job = r.jobs.filter(function (j) { return j.id === id; })[0];
            if (job && (job.status === 'waiting' || job.status === 'running')) return new Promise(function (go) { setTimeout(function () { go(wait(id)); }, 700); });
            return job;
          });
        }
        H.api('/api/cleanup', {}).then(function (r) { return wait(r.job.id); }).then(function (job) {
          count.textContent = job && job.status === 'done' && job.result ? said(job.result.found) : H.t('Le comptage a échoué : voir les Tâches, en bas de Gestion.');
          if (job && job.status === 'done') listed(job.result);
        }, function (e) { count.textContent = e.message; }).then(function () { counting = false; poll(); });
      } });
      H.confirm({ title: H.t('Supprimer les fichiers parasites ?'), ok: H.t('Supprimer définitivement'), danger: true,
        lead: [H.t('Les fichiers parasites seront supprimés de la médiathèque, et cela ne s’annule pas.')], extra: [count, sample, recount],
        submit: function () { return H.api('/api/cleanup', { apply: true }); },
        done: function () { H.toast(H.t('Nettoyage lancé')); poll(); } });
    }
    // Le compte connecté : nom, rôle, changement de mot de passe, déconnexion. Serveur ouvert : comment créer le premier compte.
    function accountPanel() {
      var panel = h('section', { class: 'panel' }, h('h2', null, H.t('Mon compte')));
      var me = H.session.user;
      if (!H.session.auth || !me) {
        panel.appendChild(h('p', { class: 'muted' }, H.t('Aucun compte n’existe : le serveur est ouvert, et toute personne qui peut l’atteindre voit et gère tout. Pour le protéger, crée le premier compte (il sera administrateur) sur le serveur avec '),
          h('code', null, H.t('python -m hub user add <nom>')), H.t(', ou définis '), h('code', null, 'HUB_PASSWORD'), H.t(' avant de démarrer le serveur. Les comptes suivants se gèrent ensuite ici.')));
        return panel;
      }
      panel.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, me.name), h('span', null, ROLES[me.role] || me.role)),
        H.button(H.t('Changer mon mot de passe'), 'edit', { 'data-key': 'mypw', onclick: changeMine }),
        H.button(H.t('Déconnecter les autres appareils'), null, { 'data-key': 'signout-others', onclick: signOutOthers }),
        h('a', { class: 'btn f', href: '/api/logout', 'data-key': 'logout', onclick: function () { H.forgetAccount(); } }, H.t('Se déconnecter'))));
      return panel;
    }
    function changeMine() {
      H.form({ title: H.t('Changer mon mot de passe'), ok: H.t('Changer'),
        fields: [{ key: 'current', label: H.t('Mot de passe actuel'), type: 'password', auto: 'current-password' }, { key: 'next', label: H.t('Nouveau mot de passe (12 caractères au moins)'), type: 'password', auto: 'new-password' }, { key: 'again', label: H.t('Nouveau mot de passe, encore une fois'), type: 'password', auto: 'new-password' }],
        submit: function (v) {
          if (!v.current) return H.t('Saisis ton mot de passe actuel.');
          var bad = checkPassword(v.next, v.again);
          if (bad) return bad;
          return H.api('/api/me/password', { current: v.current, 'new': v.next });   // le serveur renouvelle le cookie : rien à garder ici
        },
        done: function () { H.toast(H.t('Mot de passe changé. Les autres appareils devront se reconnecter.'), 5000); H.focus.key(el, 'mypw'); } });
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
    // Gestion des comptes (administrateur, serveur avec comptes).
    function usersPanel() {
      var panel = h('section', { class: 'panel' }, h('div', { class: 'phead' }, h('h2', null, H.t('Comptes')),
        H.button(H.t('Ajouter un compte'), 'plus', { 'data-key': 'user-add', onclick: addUser })));
      if (usersError) panel.appendChild(h('p', { class: 'muted' }, usersError));
      else if (!users) panel.appendChild(h('p', { class: 'muted' }, H.t('Chargement…')));
      (users || []).forEach(function (u) {
        var mine = H.session.user && u.id === H.session.user.id;
        panel.appendChild(h('div', { class: 'line', 'data-user': u.id }, h('div', { class: 'what' }, h('b', null, u.name + (mine ? ' ' + H.t('(toi)') : '')),
          h('span', null, (ROLES[u.role] || u.role) + ' · ' + H.t('créé le {date}', { date: H.date(u.created) })), u.role === 'admin' ? h('span', { 'data-upload': u.id }, H.t('Peut envoyer des fichiers : toujours')) : h('span', { 'data-rights': u.id }, rightsSummary(u))),
          u.role === 'admin' ? null : H.button(H.t('Droits'), 'edit', { 'data-key': 'rights-' + u.id, 'aria-label': H.t('Droits de {name} : consulter, lecture, télécharger, envoyer', { name: u.name }), onclick: function () { rightsOf(u); } }),
          H.button(H.t('Mot de passe'), 'edit', { 'data-key': 'pw-' + u.id, 'aria-label': H.t('Changer le mot de passe de {name}', { name: u.name }), onclick: function () { passwordOf(u, mine); } }),
          H.button(H.t('Supprimer'), 'trash', { 'data-key': 'del-' + u.id, 'aria-label': H.t('Supprimer le compte de {name}', { name: u.name }), onclick: function () { deleteUser(u, mine); } })));
      });
      return panel;
    }
    function addUser() {
      var role = 'user', toggle = H.button(H.t('Rôle : {role}', { role: ROLES.user }), null, { 'data-key': 'new-role', 'aria-label': H.t('Rôle du nouveau compte, à changer'), onclick: function () {
        role = role === 'user' ? 'admin' : 'user';
        toggle.querySelector('.lbl').textContent = H.t('Rôle : {role}', { role: ROLES[role] });
      } });
      H.form({ title: H.t('Ajouter un compte'), ok: H.t('Créer le compte'),
        lead: [H.t('Un administrateur gère la médiathèque et les comptes ; un utilisateur lit, avec sa propre progression et ses propres playlists.')],
        fields: [{ key: 'name', label: H.t('Nom (40 caractères au plus)'), type: 'text' }, { key: 'pw', label: H.t('Mot de passe (12 caractères au moins)'), type: 'password', auto: 'new-password' }, { key: 'again', label: H.t('Mot de passe, encore une fois'), type: 'password', auto: 'new-password' }],
        extra: [toggle],
        submit: function (v) {
          if (!v.name) return H.t('Donne un nom au compte.');
          var bad = checkPassword(v.pw, v.again);
          if (bad) return bad;
          return H.api('/api/users', { name: v.name, password: v.pw, role: role });
        },
        done: function (u) { H.toast(H.t('Compte créé : {name}', { name: u.name })); refreshUsers('user-add'); } });
    }
    // Ce qu'un compte peut consulter, lire et télécharger, puis s'il peut envoyer des fichiers, en une phrase pour la liste des comptes.
    function rightsSummary(u) {
      var libs = (state && state.libraries) || [], rights = u.rights || {}, parts = [];
      function who(perm) {
        var list = libs.filter(function (l) { return !rights[l.id] || rights[l.id][perm]; });
        return list.length === libs.length ? H.t('tout') : list.length ? list.map(function (l) { return H.libLabel(l); }).join(', ') : H.t('rien');
      }
      if (libs.length) parts.push(H.t('Consulter : {view} · Lecture : {play} · Télécharger : {download}', { view: who('view'), play: who('play'), download: who('download') }));
      parts.push(H.t('Envoyer des fichiers : {upload}', { upload: u.can_upload ? H.t('oui') : H.t('non') }));
      return parts.join(' · ');
    }
    // Pour chaque bibliothèque : consulter, lecture, télécharger. Retirer « consulter » retire aussi les deux autres. Au-dessous, le droit d'envoyer des fichiers (Oui / Non), qui ne tient pas à une bibliothèque.
    // Les droits valent tout de suite, sans que la personne se reconnecte.
    function rightsOf(u) {
      var draft = {}, rows = h('div', { class: 'rights' }), upload = !!u.can_upload;
      (state.libraries || []).forEach(function (l) {
        var r = (u.rights || {})[l.id] || { view: true, play: true, download: true };
        draft[l.id] = { view: !!r.view, play: !!(r.view && r.play), download: !!(r.view && r.download) };
      });
      var saved = { rights: JSON.stringify(draft), upload: upload, touched: false };      // ce que le serveur a déjà : un second essai n'envoie que le reste
      function toggle(l, which) {
        var button = h('button', { class: 'chip f', type: 'button', 'data-key': 'r-' + which + '-' + l.id, onclick: function () {
          var d = draft[l.id];
          if (which === 'view') { d.view = !d.view; if (!d.view) { d.play = false; d.download = false; } }
          else if (d.view) d[which] = !d[which];
          paint();
        } }, h('span', { class: 'mark', 'aria-hidden': 'true' }, '✓'), h('span', { class: 'lbl' }));        // la coche a toujours sa place (visible ou non) : le bouton garde la même largeur
        return button;
      }
      function choice(value, label) {
        return h('button', { class: 'chip f', type: 'button', 'data-key': 'r-upload-' + (value ? 'yes' : 'no'), onclick: function () { upload = value; paint(); } },
          h('span', { class: 'mark', 'aria-hidden': 'true' }, '✓'), h('span', { class: 'lbl' }, label));
      }
      var buttons = {}, yes = choice(true, H.t('Oui')), no = choice(false, H.t('Non'));
      (state.libraries || []).forEach(function (l) {
        buttons[l.id] = { view: toggle(l, 'view'), play: toggle(l, 'play'), download: toggle(l, 'download') };
        rows.appendChild(h('div', { class: 'rrow', 'data-lib': l.id }, h('b', null, H.libLabel(l)), buttons[l.id].view, buttons[l.id].play, buttons[l.id].download));
      });
      rows.appendChild(h('div', { class: 'rrow', 'data-upload-right': '1' }, h('b', null, H.t('Envoyer des fichiers')), yes, no));
      function paint() {
        (state.libraries || []).forEach(function (l) {
          var d = draft[l.id], b = buttons[l.id];
          b.view.classList.toggle('on', d.view); b.view.setAttribute('aria-pressed', d.view ? 'true' : 'false');
          b.view.querySelector('.lbl').textContent = H.t('Consulter');
          b.play.classList.toggle('on', d.play); b.play.setAttribute('aria-pressed', d.play ? 'true' : 'false');
          b.play.querySelector('.lbl').textContent = H.t('Lecture');
          b.play.disabled = !d.view;
          b.download.classList.toggle('on', d.download); b.download.setAttribute('aria-pressed', d.download ? 'true' : 'false');
          b.download.querySelector('.lbl').textContent = H.t('Télécharger');
          b.download.disabled = !d.view;
        });
        yes.classList.toggle('on', upload); yes.setAttribute('aria-pressed', upload ? 'true' : 'false');
        no.classList.toggle('on', !upload); no.setAttribute('aria-pressed', upload ? 'false' : 'true');
      }
      paint();
      H.form({ title: H.t('Droits de {name}', { name: u.name }), ok: H.t('Enregistrer'), kind: 'form', cls: 'rightsbox',
        lead: [H.t('Consulter : la liste, les fiches, les images. Lecture : vidéo, musique et livres en ligne. Sans « Consulter », la bibliothèque disparaît pour ce compte.')],
        extra: [rows],
        submit: function () { return saveAccount(u, draft, upload, saved); },
        done: function () { H.toast(H.t('Droits enregistrés : {name}', { name: u.name })); refreshUsers('rights-' + u.id); },
        cancel: function () { if (saved.touched) refreshUsers('rights-' + u.id); } });       // fermée après un enregistrement à moitié réussi : la liste montre ce qui l'est
    }
    // Enregistre les droits de bibliothèque et le droit d'envoi, chacun par son appel et seulement s'il a changé. Refuse (avec ce qui a réussi et ce qui a échoué) si l'un des deux échoue.
    function saveAccount(u, draft, upload, saved) {
      var base = '/api/user/' + encodeURIComponent(u.id), now = JSON.stringify(draft), failed = {}, applied = {};
      function run(part, changed, path, body, keep) {
        if (!changed) return Promise.resolve();
        return H.api(path, body).then(function () { keep(); applied[part] = true; saved.touched = true; }, function (e) { failed[part] = e.message; });
      }
      return Promise.all([
        run('rights', now !== saved.rights, base + '/rights', { rights: draft }, function () { saved.rights = now; }),
        run('upload', upload !== saved.upload, base + '/upload', { upload: upload }, function () { saved.upload = upload; })
      ]).then(function () {
        var bad = saveFailure(failed, applied);
        if (bad) throw new Error(bad);
      });
    }
    // Dit ce qui a échoué, et seulement ce qui est enregistré s'il l'est : l'un n'est jamais présenté comme raté parce que l'autre l'est.
    function saveFailure(failed, applied) {
      if (failed.rights && failed.upload) return H.t('Rien n’a été enregistré. Droits de bibliothèque : {rights}. Envoyer des fichiers : {upload}', { rights: failed.rights, upload: failed.upload });
      if (failed.rights) return applied.upload ? H.t('« Envoyer des fichiers » est enregistré, mais pas les droits de bibliothèque : {error}', { error: failed.rights }) : H.t('Les droits de bibliothèque n’ont pas été enregistrés : {error}', { error: failed.rights });
      if (failed.upload) return applied.rights ? H.t('Les droits de bibliothèque sont enregistrés, mais pas « Envoyer des fichiers » : {error}', { error: failed.upload }) : H.t('« Envoyer des fichiers » n’a pas été enregistré : {error}', { error: failed.upload });
      return '';
    }
    function passwordOf(u, mine) {
      if (mine) { changeMine(); return; }                              // son propre compte : même formulaire que « Changer mon mot de passe » (le serveur exige le mot de passe actuel)
      H.form({ title: H.t('Mot de passe de {name}', { name: u.name }), ok: H.t('Changer'),
        lead: [H.t('Les sessions ouvertes de ce compte seront fermées ; la personne se reconnectera avec le nouveau mot de passe.')],
        fields: [{ key: 'pw', label: H.t('Nouveau mot de passe (12 caractères au moins)'), type: 'password', auto: 'new-password' }, { key: 'again', label: H.t('Encore une fois'), type: 'password', auto: 'new-password' }],
        submit: function (v) {
          var bad = checkPassword(v.pw, v.again);
          if (bad) return bad;
          return H.api('/api/user/' + encodeURIComponent(u.id) + '/password', { password: v.pw });
        },
        done: function () { H.toast(H.t('Mot de passe changé : {name}', { name: u.name })); H.focus.key(el, 'pw-' + u.id); } });
    }
    function deleteUser(u, mine) {
      var lead = [H.t('Supprimer le compte « {name} » ? Sa progression de lecture, ses playlists et ses éléments retirés de « Reprendre » seront perdus, et cela ne s’annule pas. Les fichiers de la médiathèque ne sont pas touchés.', { name: u.name })];
      if (mine) lead.push(H.t('C’est le compte avec lequel tu es connecté : tu seras déconnecté. Il doit rester au moins un autre administrateur.'));
      H.confirm({ title: H.t('Supprimer le compte de {name}', { name: u.name }), ok: H.t('Supprimer définitivement'), danger: true, lead: lead,
        submit: function () { return H.api('/api/user/' + encodeURIComponent(u.id) + '/delete', {}); },
        done: function () {
          if (mine) { H.forgetAccount(); location.href = '/login'; return; }
          H.toast(H.t('Compte supprimé : {name}', { name: u.name }));
          refreshUsers('user-add');
        } });
    }
    function refreshUsers(key) {
      poll().then(function () { if (!(key && H.focus.key(el, key))) H.focus.first(); });
    }
    function jobsPanel() {
      var all = jobs.length > 0 && jobs.every(function (j) { return openJobs[j.id]; });
      var log = h('section', { class: 'panel' }, h('div', { class: 'phead' }, h('h2', null, H.t('Tâches'), busy ? h('span', { class: 'busy' }, '…') : null),
        jobs.length ? H.button(all ? H.t('Tout replier') : H.t('Tout déplier'), null, { 'data-key': 'jobs-all', onclick: function () { jobs.forEach(function (j) { openJobs[j.id] = !all; }); showJobs('jobs-all'); } }) : null));
      if (!jobs.length) log.appendChild(h('p', { class: 'muted' }, H.t('Aucune tâche pour l’instant.')));
      jobs.forEach(function (j) {
        var status = { waiting: H.t('en attente'), running: H.t('en cours'), done: H.t('terminée'), failed: H.t('échec') }[j.status], open = !!openJobs[j.id];
        var node = h('div', { class: 'job ' + j.status, 'data-job': j.id }, h('button', { class: 'jhead f', 'aria-expanded': open ? 'true' : 'false', 'data-key': 'job-' + j.id,
          onclick: function () { openJobs[j.id] = !open; showJobs('job-' + j.id); } }, h('span', { class: 'jt' }, h('b', null, H.tr(j.label)), h('span', null, status))));
        if (open) node.appendChild(logText(j) ? h('pre', null, logText(j)) : h('p', { class: 'muted' }, H.t('Rien n’a été noté.')));
        log.appendChild(node);
      });
      return log;
    }
    function logText(j) { return (j.log || []).map(function (line) { return H.tr(line); }).join('\n'); }
    // Le défilement d'un journal : où il en est, et s'il était lu jusqu'en bas (alors il suit la fin).
    function logState(pre) { return { top: pre.scrollTop, end: pre.scrollHeight - pre.scrollTop - pre.clientHeight < 4 }; }
    function logKeep(pre, was) { pre.scrollTop = was.end ? pre.scrollHeight : was.top; }
    function logStates(panel) {          // le défilement de chaque journal ouvert du panneau, par tâche : à rendre au panneau qui le remplace
      var kept = {};
      if (panel) Array.prototype.forEach.call(panel.querySelectorAll('.job pre'), function (pre) { kept[pre.parentNode.getAttribute('data-job')] = logState(pre); });
      return kept;
    }
    function logRestore(panel, kept) {
      Array.prototype.forEach.call(panel.querySelectorAll('.job pre'), function (pre) { var was = kept[pre.parentNode.getAttribute('data-job')]; if (was) logKeep(pre, was); });
    }
    // Les journaux grossissent toutes les 1,5 s : on ne change que le texte de ceux qui ont changé, sans toucher au reste (focus, défilement, sélection).
    // Faux (rien n'a été modifié) si autre chose que des journaux a changé : une tâche ajoutée ou finie, un journal qui apparaît.
    function patchLogs() {
      var nodes = logPanel.querySelectorAll('.job'), todo = [];
      if (nodes.length !== jobs.length) return false;
      for (var i = 0; i < jobs.length; i++) {
        var j = jobs[i], node = nodes[i], pre = node.querySelector('pre'), text = logText(j);
        if (node.getAttribute('data-job') !== String(j.id) || node.className !== 'job ' + j.status) return false;
        if (openJobs[j.id] && !!pre !== !!text) return false;
        if (pre && pre.textContent !== text) todo.push([pre, text]);
      }
      todo.forEach(function (t) { var was = logState(t[0]); grow(t[0], t[1]); logKeep(t[0], was); });
      return true;
    }
    function grow(pre, text) {          // un journal ne fait que s'allonger : on ajoute la fin, et ce qu'on y a sélectionné reste sélectionné (sinon le texte est remplacé)
      var old = pre.textContent;
      if (pre.childNodes.length === 1 && pre.firstChild.nodeType === 3 && text.slice(0, old.length) === old) pre.firstChild.appendData(text.slice(old.length));
      else pre.textContent = text;
    }
    // Remplace le panneau des tâches sur place (la page garde son défilement), en rendant le focus à ce qui l'avait.
    function showJobs(key) {
      var focus = key || (document.activeElement && logPanel && logPanel.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null);
      var kept = logStates(logPanel), fresh = jobsPanel();
      logPanel.parentNode.replaceChild(fresh, logPanel);
      logPanel = fresh;
      logRestore(fresh, kept);
      if (focus) H.focus.key(fresh, focus, true);
    }
    function poll() {
      if (closed) return Promise.resolve();          // la vue est détruite : une réponse tardive ne réarme pas le sondage
      clearTimeout(timer);
      // Un non-administrateur n'interroge que l'état « occupé » (sans journal). Si son rôle a changé depuis le démarrage, le 403 se rattrape par un appel sans journal.
      var jobsCall = H.isAdmin() ? H.api('/api/jobs?log=1').catch(function (e) { if (e.status === 403) return H.api('/api/jobs'); throw e; }) : H.api('/api/jobs');
      return Promise.all([H.api('/api/state'), jobsCall]).then(function (r) {
        H.setSession(r[0]);
        if (H.canUpload() !== !!document.querySelector('#nav [data-nav="#/upload"]')) { H.nav.build(); H.nav.mark(location.hash || '#/'); }       // un droit « Envoyer » donné ou retiré se voit sans recharger
        if (!(r[0].auth && H.isAdmin())) { users = null; usersError = ''; return r; }
        return H.api('/api/users').then(function (res) { users = res.users; usersError = ''; return r; }, function (e) { users = null; usersError = e.message; return r; });
      }).then(function (r) {
        if (closed) return;
        var page = JSON.stringify([r[0], users, usersError]), tasks = JSON.stringify(r[1].jobs);
        state = r[0]; jobs = r[1].jobs; H.setLibs(state.libraries);
        busy = r[1].busy;
        if (wasBusy && !busy) H.toast(H.t('Terminé'));
        wasBusy = busy;
        // Pendant une tâche on revient ici toutes les 1,5 s : reconstruire la page entière coupait le défilement (molette, tactile). On ne refait que ce qui a changé.
        if (page !== lastPage || H.isAdmin() !== wasAdmin) render();
        else {
          var mark = el.querySelector('[data-key="tab-gestion"]');          // un point sur l'onglet de gestion tant qu'il y en a une en cours
          if (mark) mark.classList.toggle('busy', !!busy);
          if (logPanel && tasks !== lastTasks && !patchLogs()) showJobs();
        }
        lastPage = page; lastTasks = tasks;
        if (busy) timer = setTimeout(poll, 1500);
      }).catch(function (e) { if (!closed) H.toast(e.message, 5000); });
    }
    poll();
    return { el: el, nav: '#/settings', refresh: poll, destroy: function () { closed = true; clearTimeout(timer); } };
  });
})();
