/* La liseuse : pages de BD / manga (.cbz) et romans (.epub). Elle reprend à la dernière page lue. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;
  var THEMES = { night: { bg: '#15181c', fg: '#d9d5cc', label: H.t('Nuit') }, sepia: { bg: '#f1e6d0', fg: '#3b3226', label: H.t('Sépia') }, day: { bg: '#ffffff', fg: '#1b1b1b', label: H.t('Jour') } };

  H.route(/^\/read\/(\w+)$/, function (m, query) {
    var id = m[1];
    var stage = h('div', { class: 'stage' });
    var label = h('span', { class: 'pg' }), title = h('div', { class: 'ti' });
    var slider = h('div', { class: 'seekbar f', role: 'slider', tabindex: '0', 'aria-label': H.t('Avancement') }, h('div', { class: 'track' }, h('i'), h('b')));
    var tools = h('div', { class: 'btns' });
    var bBack = H.button('', 'back', { 'aria-label': H.t('Retour'), onclick: function () { H.back('#/'); } });
    var bar = h('div', { class: 'rbar' }, h('div', { class: 'top' }, bBack, h('div', { class: 'txt' }, title), label), h('div', { class: 'bottom' }, slider, tools));
    var msg = h('div', { class: 'err', hidden: true });
    var el = h('section', { class: 'reader' }, stage, bar, msg);
    var info = null, pos = 0, saveTimer = null, hideTimer = null, closing = false, lastSaved = -1, endArmed = false;
    var fit = H.pref('fit') || 'height', rtl = false, spread = !!H.pref('spread'), shifted = false, theme = H.pref('theme') || 'night', size = H.pref('fontSize') || 100;
    var view = null, frame = null;
    var count = 1, cache = {}, token = 0, bSpread = null, bShift = null;      // BD : pages de la vue, pages préchargées, demande en cours
    var wide = {}, grid = null;                                               // BD : les pages qu'on sait en paysage, les couples du volume (à refaire quand l'une d'elles se révèle)
    var want = 0, wantDouble = false, busy = false, queued = 0;                       // BD : la page demandée et le mode de sa vue, si elle se charge encore, les tours demandés pendant ce temps
    var zoom = 1, base = null, bZoom = null;                                          // BD : l'agrandissement de la vue, la taille de ses pages à 100 % (mesurée au premier agrandissement)
    var touch = null, lastTap = null, tapTimer = null, press = null;                  // BD : le geste du doigt en cours, l'appui qui attend peut-être un double appui, l'appui de la souris

    function showBar(on) {
      bar.hidden = !on;
      clearTimeout(hideTimer);
      if (on) { hideTimer = setTimeout(function () { showBar(H.overlay.isOpen()); }, 6000); }          // une fenêtre ouverte la garde, et le compte repart : elle se masquera après la fermeture
      else if (document.activeElement && bar.contains(document.activeElement)) document.activeElement.blur();
    }
    function total() { return info ? (info.format === 'cbz' ? info.pages : info.chapters.length) : 0; }
    function savedPos() {      // la première page de la vue ; si la vue montre la dernière page, celle-ci (le tome est alors lu, comme en page seule)
      return info.format === 'cbz' && pos + count >= info.pages ? info.pages - 1 : pos;
    }
    function save(force) {
      if (!info) return;
      var at = savedPos();
      if (!force && at === lastSaved) return;
      lastSaved = at;
      var duration = info.format === 'cbz' ? info.pages : info.chapters.length + 1;
      H.api('/api/progress', { entry: id, position: at, duration: duration }, { keepalive: true }).catch(function () {});
    }
    function later() { clearTimeout(saveTimer); saveTimer = setTimeout(save, 1500); }
    function fail(text) {
      var save = H.actions.canDownloadKind('books') ? h('a', { class: 'btn f primary', href: '/media/' + id + '/file?download=1', 'data-autofocus': '1' }, H.t('Télécharger le fichier')) : null;
      H.clear(msg).appendChild(h('div', null, h('h2', null, H.t('Lecture impossible ici')), h('p', null, text),
        h('div', { class: 'actions' }, save, H.button(H.t('Retour'), 'back', { 'data-autofocus': save ? null : '1', onclick: function () { H.back('#/'); } }))));
      msg.hidden = false; bar.hidden = true;
      H.focus.first(msg);
    }
    function openNext() {
      if (info && info.next) { save(true); H.go('#/read/' + info.next.id, true); } else H.toast(H.t('C’est la fin.'));
    }

    // --- BD / manga -----------------------------------------------------------------
    // Une « vue » est une page, ou deux pages côte à côte (double page). Elle est repérée par le numéro de sa première page : c'est celui qu'on enregistre.
    // En double page, les vues sont les couples du volume entier (`couples`), et non un couple compté à partir de la page demandée : on retombe sur les mêmes couples d'où qu'on vienne.
    function pageUrl(n) { return '/book/' + id + '/page/' + n; }
    function portrait() { return window.innerWidth < 1.2 * window.innerHeight; }
    function double() { return spread && !portrait(); }
    function preload(n) {
      if (n < 0 || n >= info.pages || cache[n]) return;
      var im = new Image();
      im.addEventListener('error', function () { if (cache[n] === im) delete cache[n]; });          // en erreur : le prochain passage la redemande
      im.addEventListener('load', function () { if (im.naturalWidth > im.naturalHeight && !wide[n]) { wide[n] = true; grid = null; } });          // inscrit avant celui de `loaded` : les couples sont à jour quand la vue se décide
      im.src = pageUrl(n);
      cache[n] = im;
    }
    function drop(n) {                   // retire la page du cache ; vider `src` coupe son téléchargement s'il court encore
      var im = cache[n];
      delete cache[n];
      if (im && !im.complete) im.removeAttribute('src');
    }
    function ready(n) { var im = cache[n]; return !!im && im.complete && im.naturalWidth > 0; }
    function loaded(list, fn) {          // appelle fn quand ces pages sont chargées, en erreur, ou au plus tard après le délai de garde : une image qui ne vient pas ne bloque pas la liseuse
      var left = list.filter(function (n) { return n >= 0 && n < info.pages; }).map(function (n) { preload(n); return cache[n]; }).filter(function (im) { return !im.complete; });
      var wait = left.length, timer = null;
      if (!wait) { fn(); return; }
      var finish = function () { if (wait < 0) return; wait = -1; clearTimeout(timer); fn(); };
      timer = setTimeout(finish, H.pageTimeout || 15000);          // `H.pageTimeout` : abaissé par les tests
      left.forEach(function (im) {
        var done = function () { if (wait > 0 && --wait === 0) finish(); };
        im.addEventListener('load', done); im.addEventListener('error', done);
      });
    }
    function missing(k) {                // ce qui remplace une page qui n'est pas venue : « Réessayer » la redemande
      var retry = H.button(H.t('Réessayer'), null, { onclick: function (e) { e.stopPropagation(); drop(k); queued = 0; page(want); } });
      return h('div', { class: 'page missing ' + fit, 'data-page': k }, h('p', null, H.t('Image indisponible')), retry);
    }
    function couples() {                 // les vues du volume : la couverture seule (sauf couplage décalé), puis deux par deux ; une page paysage connue reste seule et les couples reprennent après elle
      var out = [], k = shifted ? 0 : 1;
      if (!shifted) out.push([0]);
      while (k < info.pages) {
        if (wide[k] || k + 1 >= info.pages || wide[k + 1]) { out.push([k]); k += 1; } else { out.push([k, k + 1]); k += 2; }
      }
      return out;
    }
    function spreadAt(n) {               // le couple du volume qui contient la page n
      if (!grid) grid = couples();
      for (var i = 0; i < grid.length; i++) if (grid[i][grid[i].length - 1] >= n) return grid[i];
      return [n];
    }
    function cbz() {
      var saved = H.pref('rtl.' + info.item);               // le sens choisi pour cette série l'emporte sur celui du serveur
      rtl = saved === null ? !!info.rtl : !!saved;
      stage.classList.add('cbz');
      stage.classList.toggle('wide', fit === 'width');
      bindGestures();
      var start = query.p !== undefined ? parseInt(query.p, 10) : info.position;
      pos = Math.min(Math.max(0, Math.floor(start || 0)), info.pages - 1);          // la reprise tient avant même que la première vue soit chargée
      page(pos);
      function fitText() { return fit === 'height' ? H.t('Ajuster : hauteur') : H.t('Ajuster : largeur'); }
      function directionText() { return rtl ? H.t('Lecture : droite → gauche') : H.t('Lecture : gauche → droite'); }
      function spreadText() { return spread ? H.t('Double page : oui') : H.t('Double page : non'); }
      tool(fitText(), null, function (b) {
        unzoom();
        fit = fit === 'height' ? 'width' : 'height'; H.pref('fit', fit);
        stage.classList.toggle('wide', fit === 'width'); stage.scrollTop = 0; setLabel(b, fitText());
        Array.prototype.forEach.call(stage.querySelectorAll('.page'), function (im) { im.classList.remove('height', 'width'); im.classList.add(fit); });
      });
      tool(directionText(), null, function (b) {
        rtl = !rtl; H.pref('rtl.' + info.item, rtl); setLabel(b, directionText());
        if (view) view.classList.toggle('rtl', rtl);
      });
      bSpread = tool(spreadText(), null, function (b) {
        spread = !spread; H.pref('spread', spread); setLabel(b, spreadText()); syncTools(); relayout();
      });
      bShift = tool(H.t('Décaler d’une page'), null, shift);
      bZoom = tool(zoomText(), null, function () { zoomTo(level(1) || 1); });          // 150, 200, 300 %, puis de nouveau 100 % : l'accès à la télécommande
      if (info.next) tool(H.t('Volume suivant'), 'next', openNext);
      syncTools();
    }
    function syncTools() { bSpread.hidden = portrait(); bShift.hidden = !double(); }
    function relayout() { queued = 0; page(want); }          // la vue demandée, sous un autre mode
    function onResize() {
      if (!info || info.format !== 'cbz' || !bSpread) return;
      unzoom();          // la taille des pages à 100 % a changé avec la fenêtre
      syncTools();
      if (double() !== wantDouble) relayout();
    }
    function shift() {                  // le couplage des pages bouge d'une page (jusqu'à la fermeture de la liseuse), sans perdre la page lue
      shifted = !shifted; grid = null; queued = 0;
      page(want);
    }
    function page(n, before, since) {   // montre la vue qui contient la page n ; sans les pages à partir de `before` ni celles d'avant `since` : la vue qu'on quitte, si les couples ont bougé depuis
      var mine = ++token, dbl = double();
      want = n; wantDouble = dbl; busy = true;
      for (var k = n - 3; k <= n + (dbl ? 5 : 2); k++) preload(k);
      Object.keys(cache).forEach(function (key) { if (key < n - 6 || key > n + 7) drop(key); });
      (function settle() {
        var pages = dbl ? spreadAt(n) : [n];
        loaded(pages, function () {          // une vue ne s'affiche qu'une fois ses images chargées : on connaît alors leurs dimensions
          if (closing || mine !== token) return;
          if (dbl !== double()) { page(n, before, since); return; }           // la fenêtre a changé de forme pendant le chargement
          if (dbl && spreadAt(n) !== pages) { settle(); return; }            // une page s'est révélée en paysage : les couples ont bougé
          var shown = pages.filter(function (k) { return (before === undefined || k < before) && (since === undefined || k >= since); });
          if (!shown.length) shown = [n];
          if (before !== undefined || since !== undefined) want = shown[0];      // après un tour de page, la page lue est la première de la vue
          show(shown);
        });
      })();
    }
    function show(pages) {
      var back = pages[0] < pos, kept = zoom;          // on revient en arrière ? le zoom de la vue quittée, que la nouvelle reprend
      pos = pages[0]; count = pages.length; busy = false;
      unzoom();                                        // la nouvelle vue se mesure d'abord à 100 %
      var holder = h('div', { class: 'spread' + (count > 1 ? ' two' : '') + (rtl ? ' rtl' : '') });
      pages.forEach(function (k) { holder.appendChild(ready(k) ? h('img', { class: 'page ' + fit, alt: '', src: pageUrl(k) }) : missing(k)); });
      if (view) stage.replaceChild(holder, view); else stage.appendChild(holder);
      view = holder;
      var retry = holder.querySelector('.missing .btn');
      if (retry && !(bar.contains(document.activeElement) && !bar.hidden)) H.focus.set(retry, true);          // Entrée réessaie ; les flèches tournent toujours les pages
      stage.scrollTop = 0; stage.scrollLeft = 0;
      if (kept > 1) { zoomTo(kept); toCorner(back); }
      paint(); later();
      endArmed = false;
      if (queued) {          // les tours demandés pendant le chargement, un à un ; s'il n'en part aucun chargement (début ou fin du volume), le reste est caduc
        var d = queued > 0 ? 1 : -1;
        queued -= d; turn(d);
        if (!busy) queued = 0;
      }
    }
    function go(n) {
      if (n < 0) return;
      if (n >= info.pages) {
        if (!endArmed) { endArmed = true; save(true); H.toast(info.next ? H.t('Fin du volume · encore une fois pour passer au suivant') : H.t('Fin du volume')); return; }
        openNext(); return;
      }
      page(n);
    }
    function turn(delta) {              // delta : en vues (une touche) ; une touche maintenue saute plusieurs pages
      if (busy) { queued = Math.max(-4, Math.min(4, queued + delta)); return; }          // une vue se charge : ce tour viendra juste après
      if (!wantDouble) { go(pos + delta); return; }
      if (Math.abs(delta) > 1) { go(pos + 2 * delta); return; }
      if (delta < 0) { if (pos > 0) page(pos - 1, pos); return; }
      if (pos + count >= info.pages) go(pos + count); else page(pos + count, undefined, pos + count);
    }

    // Le zoom agrandit réellement la vue (une taille en pixels pour chaque page), pas par `transform` : la zone de lecture la fait défiler au doigt, à la molette et au clavier comme n'importe quelle page.
    // Il passe d'une vue à l'autre tant que la liseuse est ouverte, et revient à 100 % quand la fenêtre change de taille, quand l'ajustement change et à la réouverture. Il ne touche ni `pos` ni `count` : la page enregistrée reste la même.
    var LEVELS = [1, 1.5, 2, 3], SLOP = 12;          // les paliers du bouton et des touches (le pincement va de l'un à l'autre) ; au-delà de ce déplacement, en px, un appui devient un glissement
    var ARROWS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    function zoomText() { return H.t('Zoom : {n} %', { n: Math.round(zoom * 100) }); }
    function level(dir) {                 // le palier au-dessus (dir > 0) ou au-dessous (dir < 0) du zoom actuel ; 0 s'il n'y en a pas
      var out = 0;
      LEVELS.forEach(function (l) { if (dir > 0 ? !out && l > zoom + 0.01 : l < zoom - 0.01) out = l; });
      return out;
    }
    function rects() { return Array.prototype.map.call(view.children, function (c) { return c.getBoundingClientRect(); }); }
    function bounds() {                   // le rectangle des pages de la vue, dans la fenêtre (à 100 %, la vue elle-même est aussi large que l'écran)
      var all = rects(), pick = function (side) { return all.map(function (r) { return r[side]; }); };
      var left = Math.min.apply(null, pick('left')), top = Math.min.apply(null, pick('top'));
      return { left: left, top: top, width: Math.max.apply(null, pick('right')) - left, height: Math.max.apply(null, pick('bottom')) - top };
    }
    function zoomTo(z, ax, ay) {          // autour du point (ax, ay) de la fenêtre, qui ne bouge pas ; par défaut le milieu de la zone de lecture
      if (!view) return;
      var s = stage.getBoundingClientRect();
      if (ax === undefined) { ax = s.left + stage.clientWidth / 2; ay = s.top + stage.clientHeight / 2; }
      var old = bounds(), fx = (ax - old.left) / old.width, fy = (ay - old.top) / old.height;
      if (!base) base = rects().map(function (r) { return { w: r.width, h: r.height }; });
      zoom = Math.min(LEVELS[LEVELS.length - 1], Math.max(1, z));
      if (zoom < 1.02) zoom = 1;
      sizePages();
      var now = bounds();
      stage.scrollLeft += now.left + fx * now.width - ax;
      stage.scrollTop += now.top + fy * now.height - ay;
    }
    function sizePages() {                // la taille de chaque page de la vue au zoom actuel ; à 100 %, la feuille de style reprend la main
      var on = zoom > 1;
      stage.classList.toggle('zoomed', on);          // zoomé, la vue se centre par ses marges : centrée en flex, ce qui dépasse en haut et à gauche serait hors d'atteinte
      if (view) Array.prototype.forEach.call(view.children, function (c, i) { c.style.width = on ? base[i].w * zoom + 'px' : ''; c.style.height = on ? base[i].h * zoom + 'px' : ''; });
      if (!on) base = null;
      if (bZoom) setLabel(bZoom, zoomText());
    }
    function unzoom() { zoom = 1; sizePages(); }
    function toCorner(end) {              // zoomé, une vue s'ouvre où l'on commence à la lire (en haut, du côté où commence la lecture) ; en revenant en arrière, où l'on finit (en bas, de l'autre côté)
      var right = end !== rtl;
      stage.scrollLeft = right ? stage.scrollWidth : 0;
      stage.scrollTop = end ? stage.scrollHeight : 0;
    }
    function pan(d, repeat) {             // zoomé : la flèche déplace l'image ; déjà au bord, un nouvel appui tourne la page (pas la répétition d'une flèche maintenue pour y arriver)
      var x = d[0], y = d[1];
      var room = x > 0 ? stage.scrollWidth - stage.clientWidth - stage.scrollLeft : x < 0 ? stage.scrollLeft : y > 0 ? stage.scrollHeight - stage.clientHeight - stage.scrollTop : stage.scrollTop;
      if (room > 1) stage.scrollBy(Math.round(x * stage.clientWidth * 0.25), Math.round(y * stage.clientHeight * 0.25));
      else if (!repeat) turn(x ? ((x > 0) !== rtl ? 1 : -1) : y);
    }

    // Appuis et gestes sur la zone de lecture. Au doigt, l'appui se traite à la levée du doigt (le clic du navigateur est annulé) : il attend un instant un éventuel second appui, sinon un double appui tournerait d'abord la page.
    function tap(cx) {                    // un appui : sur un côté, la page tourne, sauf zoomé (on regarde l'image) ; au milieu, la barre
      var r = stage.getBoundingClientRect(), x = (cx - r.left) / r.width;
      if (zoom > 1 || (x >= 0.3 && x <= 0.7)) showBar(bar.hidden);
      else turn((x < 0.3) === !rtl ? -1 : 1);
    }
    function far(a, x, y, limit) { return Math.abs(a.x - x) + Math.abs(a.y - y) > limit; }
    function span(t) { return Math.sqrt(Math.pow(t[0].clientX - t[1].clientX, 2) + Math.pow(t[0].clientY - t[1].clientY, 2)) || 1; }
    function bindGestures() {
      stage.addEventListener('mousedown', function (e) { press = { x: e.clientX, y: e.clientY }; });
      stage.addEventListener('click', function (e) {          // la souris ; un glissement de la souris n'est pas un clic
        var moved = press && far(press, e.clientX, e.clientY, SLOP);
        press = null;
        if (!moved) tap(e.clientX);
      });
      stage.addEventListener('touchstart', onTouchStart, { passive: true });
      stage.addEventListener('touchmove', onTouchMove, { passive: false });          // le pincement retient le défilement ; un doigt seul défile comme d'habitude
      stage.addEventListener('touchend', onTouchEnd);
      stage.addEventListener('touchcancel', function () { touch = null; });
    }
    function onTouchStart(e) {
      var t = e.touches;
      if (e.target.closest && e.target.closest('.btn')) { touch = null; return; }          // « Réessayer » garde son propre appui
      if (!touch || t.length === 1) touch = { x: t[0].clientX, y: t[0].clientY, moved: false, pinch: null };
      if (t.length > 1) { touch.moved = true; touch.pinch = { d: span(t), z: zoom }; }          // un pincement n'est jamais un appui
    }
    function onTouchMove(e) {
      var t = e.touches;
      if (!touch) return;
      if (touch.pinch && t.length > 1) {
        if (!e.cancelable) return;          // le navigateur défile déjà (un doigt posé avant l'autre) : le geste reste un défilement, qu'on ne modifie pas en même temps que lui
        e.preventDefault();
        zoomTo(touch.pinch.z * span(t) / touch.pinch.d, (t[0].clientX + t[1].clientX) / 2, (t[0].clientY + t[1].clientY) / 2);
      } else if (far(touch, t[0].clientX, t[0].clientY, SLOP)) touch.moved = true;
    }
    function onTouchEnd(e) {
      if (!touch || e.touches.length) return;          // un doigt reste posé : le geste continue
      var g = touch, now = Date.now();
      touch = null;
      if (e.cancelable) e.preventDefault();
      if (g.moved) return;
      clearTimeout(tapTimer);
      if (lastTap && !far(lastTap, g.x, g.y, 40) && now - lastTap.at < 350) { lastTap = null; zoomTo(zoom > 1 ? 1 : 2, g.x, g.y); return; }          // double appui : 200 % sous le doigt, ou retour à 100 %
      lastTap = { x: g.x, y: g.y, at: now };
      tapTimer = setTimeout(function () { lastTap = null; tap(g.x); }, 350);
    }

    // --- Roman ---------------------------------------------------------------------
    function epub() {
      frame = h('iframe', { class: 'book', title: H.t('Livre'), sandbox: 'allow-same-origin' });
      stage.appendChild(frame);
      stage.classList.add('epub');
      frame.addEventListener('load', styleFrame);
      var start = info.position || 0;
      chapter(Math.min(Math.floor(start), info.chapters.length - 1), start - Math.floor(start));
      if (info.toc && info.toc.length) tool(H.t('Sommaire'), null, openToc);
      tool(H.t('Texte −'), null, function () { setSize(size - 10); });
      tool(H.t('Texte +'), null, function () { setSize(size + 10); });
      function themeText() { return H.t('Thème : {theme}', { theme: THEMES[theme].label }); }
      tool(themeText(), null, function (b) {
        var names = Object.keys(THEMES); theme = names[(names.indexOf(theme) + 1) % names.length]; H.pref('theme', theme); setLabel(b, themeText()); styleFrame();
      });
      tool(H.t('Chapitre précédent'), 'prev', function () { skipChapter(-1); });
      tool(H.t('Chapitre suivant'), 'next', function () { skipChapter(1); });
    }
    var chap = 0, wanted = 0, anchor = '';
    function chapter(n, fraction, hash) {          // `fraction` : où reprendre dans le chapitre ; `hash` : l'ancre à rejoindre (lien du livre)
      chap = n; wanted = fraction || 0; anchor = hash || ''; pos = n + wanted;
      var win = frame.contentWindow;
      if (win) win.location.replace(info.chapters[n]); else frame.src = info.chapters[n];          // `replace` : un chapitre n'ajoute rien à l'historique, Retour quitte le livre
      paint();
    }
    function plain(url) { try { return decodeURI(url.pathname); } catch (e) { return url.pathname; } }
    function chapterOf(url) {
      if (url.origin !== location.origin) return -1;
      for (var n = 0; n < info.chapters.length; n++) if (plain(new URL(info.chapters[n], location.href)) === plain(url)) return n;
      return -1;
    }
    function here(doc) { return plain(new URL(doc.URL)) === plain(new URL(info.chapters[chap], location.href)); }          // `doc` est le chapitre demandé (et pas celui qu'on quitte)
    function anchorIn(doc, name) { return name ? doc.getElementById(name) || doc.getElementsByName(name)[0] : null; }          // `id`, ou `<a name>` des livres EPUB 2
    function reach(doc, name) {          // l'élément d'ancre `name` du chapitre affiché
      try { name = decodeURIComponent(name); } catch (e) { /* ancre telle quelle */ }
      var target = anchorIn(doc, name);
      if (target) target.scrollIntoView();
    }
    function shownChapter() {            // le document du chapitre lu, s'il est arrivé
      var doc;
      try { doc = frame.contentDocument; } catch (e) { return null; }
      return doc && doc.documentElement && here(doc) ? doc : null;
    }
    function passedLine(doc) {           // une ancre est passée quand elle est au-dessus de cette ligne ; en bas du chapitre, tout ce qui est à l'écran l'est : la fin ne peut plus remonter
      var win = frame.contentWindow, atEnd = win.scrollY > 0 && win.scrollY + win.innerHeight >= doc.documentElement.scrollHeight - 4;
      return win.innerHeight * (atEnd ? 1 : 0.25);
    }
    function tocCurrent() {              // l'entrée en cours : parmi celles du chapitre lu, la dernière passée (à défaut la première), sinon la dernière d'un chapitre précédent ; -1 s'il n'y en a pas
      var doc = shownChapter(), line = doc ? passedLine(doc) : 0, before = -1, first = -1, passed = -1;
      info.toc.forEach(function (e, i) {
        if (e.chapter < chap) before = i;
        if (e.chapter !== chap) return;
        if (first < 0) first = i;
        var target = doc && anchorIn(doc, e.anchor);
        if (!e.anchor || (target && target.getBoundingClientRect().top <= line)) passed = i;
      });
      return passed >= 0 ? passed : first >= 0 ? first : before;
    }
    function tocJump(e) {                // la barre se retire avec le focus qu'elle a repris : les flèches tournent de nouveau les pages
      var doc = shownChapter(), hash = encodeURIComponent(e.anchor);
      showBar(false);
      if (e.chapter !== chap || !doc) chapter(e.chapter, 0, hash);
      else if (e.anchor) reach(doc, hash);
      else frame.contentWindow.scrollTo(0, 0);
    }
    function openToc() {
      var options = info.toc.map(function (e, i) { return { label: e.title, value: i, level: e.level }; });
      H.menu(H.t('Sommaire'), options, tocCurrent(), function (i) { tocJump(info.toc[i]); });
    }
    function followLink(link, doc) {          // un lien du livre se suit dans la liseuse ; ni un site extérieur ni un fichier du livre ne fait quitter la page
      var to;
      try { to = new URL(link.getAttribute('href') || link.getAttribute('xlink:href') || '', doc.URL); } catch (e) { return; }
      var n = chapterOf(to);
      if (n < 0) { H.toast(H.t('Ce lien ne mène pas à un chapitre du livre.')); return; }
      var hash = to.hash.slice(1);
      if (n === chap && here(doc) && hash) reach(doc, hash); else chapter(n, 0, hash);          // le chapitre lu, sans ancre : on le reprend au début
    }
    function skipChapter(d) {
      var n = chap + d;
      if (n < 0) return;
      if (n >= info.chapters.length) { pos = info.chapters.length; save(true); openNext(); return; }
      chapter(n, 0);
    }
    function setSize(v) { size = Math.max(70, Math.min(220, v)); H.pref('fontSize', size); styleFrame(); H.toast(H.t('Texte à {size} %', { size: size })); }
    function styleFrame() {
      var doc;
      try { doc = frame.contentDocument; } catch (e) { return; }
      if (!doc || !doc.documentElement) return;
      var t = THEMES[theme], old = doc.getElementById('hub-style');
      if (old && old.parentNode) old.parentNode.removeChild(old);
      var css = doc.createElement('style');
      css.id = 'hub-style';
      css.textContent = 'html{font-size:' + size + '% !important;background:' + t.bg + ' !important}body{background:' + t.bg + ' !important;color:' + t.fg + ' !important;max-width:38em;margin:0 auto !important;padding:6vh 6vw !important;line-height:1.65 !important;font-family:Georgia,"Times New Roman",serif}' +
        'body *{color:inherit !important;background-color:transparent !important}img,svg{max-width:100% !important;height:auto !important}a{text-decoration:underline}';
      (doc.head || doc.documentElement).appendChild(css);
      var win = frame.contentWindow;
      if (!doc.hubBound) {
        doc.hubBound = true;
        doc.addEventListener('keydown', function (e) { onKey(e); }, true);
        doc.addEventListener('click', function (e) {
          var link = e.target.closest && e.target.closest('a');
          if (link && (link.hasAttribute('href') || link.hasAttribute('xlink:href'))) {          // une ancre nommée (`<a id>`) n'est pas un lien : le clic compte comme ailleurs sur le texte
            e.preventDefault(); followLink(link, doc); return;
          }
          var r = frame.getBoundingClientRect(), x = e.clientX / r.width; if (x < 0.25) scroll(-1); else if (x > 0.75) scroll(1); else showBar(bar.hidden); });
        win.addEventListener('scroll', progressOf, { passive: true });
      }
      if (here(doc)) {          // sinon le chapitre demandé n'est pas encore arrivé (le thème a changé en route) : l'ancre et la reprise l'attendent
        if (anchor) { reach(doc, anchor); anchor = ''; }
        else if (wanted > 0) { var max = doc.documentElement.scrollHeight - win.innerHeight; win.scrollTo(0, Math.max(0, max * wanted)); wanted = 0; }
      }
      progressOf();
    }
    function progressOf() {
      var win = frame && frame.contentWindow, doc = win && win.document;
      if (!doc || !doc.documentElement) return;
      var max = Math.max(1, doc.documentElement.scrollHeight - win.innerHeight), last = chap === info.chapters.length - 1;
      pos = chap + Math.min(last ? 1 : 0.999, Math.max(0, win.scrollY / max));          // le bas d'un chapitre reste ce chapitre à la reprise ; seul celui du dernier termine le livre
      paint(); later();
    }
    function scroll(dir) {
      var win = frame.contentWindow, doc = win.document;
      var max = doc.documentElement.scrollHeight - win.innerHeight, y = win.scrollY;
      if (dir > 0 && y >= max - 4) { skipChapter(1); return; }
      if (dir < 0 && y <= 4) { if (chap > 0) { chapter(chap - 1, 0.999); } return; }
      win.scrollBy(0, dir * Math.round(win.innerHeight * 0.88));
    }

    // --- Commun ----------------------------------------------------------------------
    function tool(text, icon, fn) {
      var b = H.button(text, icon, {});
      b.addEventListener('click', function () { fn(b); showBar(true); });
      tools.appendChild(b);
      return b;
    }
    function setLabel(b, text) { var l = b.querySelector('.lbl'); if (l) l.textContent = text; }
    function paint() {
      var n = total(), last = pos + count;          // BD : `last` est le rang (à partir de 1) de la dernière page de la vue
      if (info.format !== 'cbz') label.textContent = H.t('Chapitre {n} / {total}', { n: Math.min(Math.floor(pos), chap) + 1, total: n });
      else label.textContent = count > 1 ? H.t('Pages {a}–{b} / {total}', { a: pos + 1, b: last, total: n }) : H.t('Page {n} / {total}', { n: pos + 1, total: n });
      var ratio = n ? (info.format === 'cbz' ? last / n : pos / n) : 0;
      slider.querySelector('i').style.width = Math.min(100, ratio * 100) + '%';
      slider.querySelector('b').style.left = Math.min(100, ratio * 100) + '%';
    }
    slider.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        var d = (e.key === 'ArrowRight' ? 1 : -1) * (info.format === 'cbz' && rtl ? -1 : 1);
        if (info.format === 'cbz') turn(d * (e.repeat ? 5 : 1)); else skipChapter(d);
        showBar(true); e.preventDefault();
      }
    });
    slider.addEventListener('click', function (e) {
      var r = slider.getBoundingClientRect(), x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
      if (info.format === 'cbz') { queued = 0; page(Math.min(info.pages - 1, Math.floor(x * info.pages))); } else chapter(Math.min(info.chapters.length - 1, Math.floor(x * info.chapters.length)), 0);
    });
    function onKey(e) {
      if (!document.body.contains(el) || el.style.display === 'none' || H.overlay.isOpen() || e.ctrlKey || e.metaKey || e.altKey || !info || !msg.hidden) return;
      var k = e.key;
      if (!bar.hidden && bar.contains(document.activeElement)) { showBar(true); return; }
      var eat = function () { e.preventDefault(); e.stopPropagation(); };
      if (info.format === 'cbz') {
        if (k === '+' || k === '=') { zoomTo(level(1) || zoom); eat(); }
        else if (k === '-') { zoomTo(level(-1) || 1); eat(); }
        else if (zoom > 1 && ARROWS[k]) { pan(ARROWS[k], e.repeat); eat(); }
        else if (k === 'ArrowRight') { turn(rtl ? -1 : 1); eat(); }
        else if (k === 'ArrowLeft') { turn(rtl ? 1 : -1); eat(); }
        else if (k === ' ' || k === 'PageDown') { if (fit === 'width' && stage.scrollTop + stage.clientHeight < stage.scrollHeight - 4) stage.scrollBy(0, stage.clientHeight * 0.85); else turn(1); eat(); }
        else if (k === 'PageUp') { turn(-1); eat(); }
        else if (k === 'ArrowDown') { if (fit === 'width') stage.scrollBy(0, 120); else { showBar(true); H.focus.set(slider, true); } eat(); }
        else if (k === 'ArrowUp') { if (fit === 'width' && stage.scrollTop > 0) stage.scrollBy(0, -120); else { showBar(true); H.focus.set(slider, true); } eat(); }
        else if (k === 'Enter') { if (stage.contains(document.activeElement)) return; showBar(true); H.focus.set(slider, true); eat(); }          // sur « Réessayer », Entrée l'active
      } else {
        if (k === 'ArrowRight' || k === ' ' || k === 'PageDown') { scroll(1); eat(); }
        else if (k === 'ArrowLeft' || k === 'PageUp') { scroll(-1); eat(); }
        else if (k === 'ArrowDown') { frame.contentWindow.scrollBy(0, 90); eat(); }
        else if (k === 'ArrowUp') { frame.contentWindow.scrollBy(0, -90); eat(); }
        else if (k === 'Enter') { if (!onLink()) { showBar(true); H.focus.set(slider, true); eat(); } }          // sur un lien (Tab), Entrée le suit : le navigateur envoie le clic
      }
    }
    function onLink() {          // le focus est sur un lien du chapitre
      var doc = frame && frame.contentDocument, on = doc && doc.activeElement;
      return !!on && on.localName === 'a' && (on.hasAttribute('href') || on.hasAttribute('xlink:href'));
    }
    window.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onResize);
    var onHide = function () { if (document.hidden) save(true); };
    document.addEventListener('visibilitychange', onHide);
    window.addEventListener('pagehide', onHide);

    H.api('/api/book/' + id).then(function (data) {
      if (closing) return;
      info = data;
      if (document.activeElement && bar.contains(document.activeElement)) document.activeElement.blur();   // les flèches tournent les pages tant qu'on n'est pas sur la barre
      showBar(true);
      title.textContent = data.title;
      if (data.format === 'cbz') cbz();
      else if (data.format === 'epub') epub();
      else fail(H.t('Le format {format} ne se lit pas dans le navigateur.', { format: (data.format || H.t('inconnu')).toUpperCase() }));
    }).catch(function (e) { fail(e.message); });

    return {
      el: el, full: true, nav: '#/read', lib: '',
      shown: function () { if (info) { paint(); } },
      onBack: function () {
        if (H.routed <= 1 && info) { location.hash = '#/i/' + info.item; return true; }
        return false;
      },
      destroy: function () {
        closing = true; token += 1;
        clearTimeout(saveTimer); clearTimeout(hideTimer); clearTimeout(tapTimer);
        save(true);
        window.removeEventListener('keydown', onKey, true);
        window.removeEventListener('resize', onResize);
        document.removeEventListener('visibilitychange', onHide);
        window.removeEventListener('pagehide', onHide);
      }
    };
  });
})();
