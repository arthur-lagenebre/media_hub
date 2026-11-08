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
    var count = 1, trail = [], cache = {}, token = 0, bSpread = null, bShift = null;      // BD : pages de la vue, débuts des vues déjà passées, pages préchargées, demande en cours
    var want = 0, wantDouble = false, busy = false, queued = 0;                       // BD : la vue demandée (début, mode), si elle se charge encore, les tours demandés pendant ce temps

    function showBar(on) {
      bar.hidden = !on;
      clearTimeout(hideTimer);
      if (on) { hideTimer = setTimeout(function () { if (!H.overlay.isOpen()) showBar(false); }, 6000); }
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
    function pageUrl(n) { return '/book/' + id + '/page/' + n; }
    function portrait() { return window.innerWidth < 1.2 * window.innerHeight; }
    function double() { return spread && !portrait(); }
    function preload(n) {
      if (n < 0 || n >= info.pages || cache[n]) return;
      var im = new Image();
      im.src = pageUrl(n);
      cache[n] = im;
    }
    function loaded(list, fn) {          // appelle fn quand ces pages sont chargées (ou en erreur : on ne reste pas bloqué)
      var left = list.filter(function (n) { return n >= 0 && n < info.pages; }).map(function (n) { preload(n); return cache[n]; }).filter(function (im) { return !im.complete; });
      var wait = left.length;
      if (!wait) { fn(); return; }
      left.forEach(function (im) {
        var done = function () { wait -= 1; if (wait === 0) fn(); };
        im.addEventListener('load', done); im.addEventListener('error', done);
      });
    }
    function landscape(n) { var im = cache[n]; return !!im && im.complete && im.naturalWidth > im.naturalHeight; }
    function spreadAt(n) {               // les pages de la vue qui commence en n : la couverture seule (sauf couplage décalé), une page paysage seule, sinon deux pages
      if (n >= info.pages - 1 || landscape(n) || landscape(n + 1) || (n === 0 && !shifted)) return [n];
      return [n, n + 1];
    }
    function cbz() {
      var saved = H.pref('rtl.' + info.item);               // le sens choisi pour cette série l'emporte sur celui du serveur
      rtl = saved === null ? !!info.rtl : !!saved;
      stage.classList.add('cbz');
      stage.classList.toggle('wide', fit === 'width');
      stage.addEventListener('click', function (e) {
        var r = stage.getBoundingClientRect(), x = (e.clientX - r.left) / r.width;
        if (x < 0.3) turn(rtl ? 1 : -1); else if (x > 0.7) turn(rtl ? -1 : 1); else showBar(bar.hidden);
      });
      var start = query.p !== undefined ? parseInt(query.p, 10) : info.position;
      pos = Math.min(Math.max(0, Math.floor(start || 0)), info.pages - 1);          // la reprise tient avant même que la première vue soit chargée
      page(pos);
      function fitText() { return fit === 'height' ? H.t('Ajuster : hauteur') : H.t('Ajuster : largeur'); }
      function directionText() { return rtl ? H.t('Lecture : droite → gauche') : H.t('Lecture : gauche → droite'); }
      function spreadText() { return spread ? H.t('Double page : oui') : H.t('Double page : non'); }
      tool(fitText(), null, function (b) {
        fit = fit === 'height' ? 'width' : 'height'; H.pref('fit', fit);
        stage.classList.toggle('wide', fit === 'width'); stage.scrollTop = 0; setLabel(b, fitText());
        Array.prototype.forEach.call(stage.querySelectorAll('.page'), function (im) { im.className = 'page ' + fit; });
      });
      tool(directionText(), null, function (b) {
        rtl = !rtl; H.pref('rtl.' + info.item, rtl); setLabel(b, directionText());
        if (view) view.classList.toggle('rtl', rtl);
      });
      bSpread = tool(spreadText(), null, function (b) {
        spread = !spread; H.pref('spread', spread); setLabel(b, spreadText()); syncTools(); relayout();
      });
      bShift = tool(H.t('Décaler d’une page'), null, shift);
      if (info.next) tool(H.t('Volume suivant'), 'next', openNext);
      syncTools();
    }
    function syncTools() { bSpread.hidden = portrait(); bShift.hidden = !double(); }
    function relayout() { trail = []; queued = 0; page(want); }          // la vue demandée, sous un autre mode
    function onResize() {
      if (!info || info.format !== 'cbz' || !bSpread) return;
      syncTools();
      if (double() !== wantDouble) relayout();
    }
    function shift() {                  // le couplage des pages bouge d'une page (jusqu'à la fermeture de la liseuse), sans perdre la page lue
      shifted = !shifted; trail = []; queued = 0;
      page(want > 0 ? (shifted ? want - 1 : Math.min(want + 1, info.pages - 1)) : 0);
    }
    function page(n, before, from) {    // montre la vue qui commence en n ; `before` : sans aller jusqu'à cette page (retour en arrière) ; `from` : le début de la vue qu'on quitte en avançant
      var mine = ++token, dbl = double();
      want = n; wantDouble = dbl; busy = true;
      for (var k = n - 3; k <= n + (dbl ? 5 : 2); k++) preload(k);
      Object.keys(cache).forEach(function (key) { if (key < n - 6 || key > n + 7) delete cache[key]; });
      loaded(dbl ? [n, n + 1] : [n], function () {          // une vue ne s'affiche qu'une fois ses images chargées : on connaît alors leurs dimensions
        if (closing || mine !== token) return;
        if (dbl !== double()) { page(n, before, from); return; }           // la fenêtre a changé de forme pendant le chargement
        var pages = dbl ? spreadAt(n).filter(function (k) { return before === undefined || k < before; }) : [n];
        if (!pages.length) pages = [n];
        if (from !== undefined && from < n) trail.push(from);
        show(n, pages);
      });
    }
    function show(n, pages) {
      pos = n; count = pages.length; busy = false;
      var holder = h('div', { class: 'spread' + (count > 1 ? ' two' : '') + (rtl ? ' rtl' : '') });
      pages.forEach(function (k) { holder.appendChild(h('img', { class: 'page ' + fit, alt: '', src: pageUrl(k) })); });
      if (view) stage.replaceChild(holder, view); else stage.appendChild(holder);
      view = holder;
      stage.scrollTop = 0;
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
      if (Math.abs(delta) > 1) { trail = []; go(pos + 2 * delta); return; }
      if (delta < 0) { back(); return; }
      if (pos + count >= info.pages) go(pos + count); else page(pos + count, undefined, pos);
    }
    function back() {
      var from = pos;
      if (from <= 0) return;
      while (trail.length && trail[trail.length - 1] >= from) trail.pop();
      if (trail.length) { page(trail.pop(), from); return; }
      var mine = ++token;
      busy = true;
      loaded([from - 2, from - 1], function () {          // pas d'historique (reprise, saut) : on cherche le couplage qui finit juste avant
        if (closing || mine !== token) return;            // une demande plus récente (saut, décalage...) a pris la main
        var before = from - 2;
        page(before >= 0 && spreadAt(before).length === 2 ? before : from - 1, from);
      });
    }

    // --- Roman ---------------------------------------------------------------------
    function epub() {
      frame = h('iframe', { class: 'book', title: H.t('Livre'), sandbox: 'allow-same-origin' });
      stage.appendChild(frame);
      stage.classList.add('epub');
      frame.addEventListener('load', styleFrame);
      var start = info.position || 0;
      chapter(Math.min(Math.floor(start), info.chapters.length - 1), start - Math.floor(start));
      tool(H.t('Texte −'), null, function () { setSize(size - 10); });
      tool(H.t('Texte +'), null, function () { setSize(size + 10); });
      function themeText() { return H.t('Thème : {theme}', { theme: THEMES[theme].label }); }
      tool(themeText(), null, function (b) {
        var names = Object.keys(THEMES); theme = names[(names.indexOf(theme) + 1) % names.length]; H.pref('theme', theme); setLabel(b, themeText()); styleFrame();
      });
      tool(H.t('Chapitre précédent'), 'prev', function () { skipChapter(-1); });
      tool(H.t('Chapitre suivant'), 'next', function () { skipChapter(1); });
    }
    var chap = 0, wanted = 0;
    function chapter(n, fraction) {
      chap = n; wanted = fraction || 0; pos = n + wanted;
      frame.src = info.chapters[n];
      paint();
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
        doc.addEventListener('click', function (e) { var r = frame.getBoundingClientRect(), x = e.clientX / r.width; if (x < 0.25) scroll(-1); else if (x > 0.75) scroll(1); else showBar(bar.hidden); });
        win.addEventListener('scroll', progressOf, { passive: true });
      }
      if (wanted > 0) { var max = doc.documentElement.scrollHeight - win.innerHeight; win.scrollTo(0, Math.max(0, max * wanted)); wanted = 0; }
      progressOf();
    }
    function progressOf() {
      var win = frame && frame.contentWindow, doc = win && win.document;
      if (!doc || !doc.documentElement) return;
      var max = Math.max(1, doc.documentElement.scrollHeight - win.innerHeight);
      pos = chap + Math.min(1, Math.max(0, win.scrollY / max));
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
      if (info.format !== 'cbz') label.textContent = H.t('Chapitre {n} / {total}', { n: Math.floor(pos) + 1, total: n });
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
      if (info.format === 'cbz') { trail = []; queued = 0; page(Math.min(info.pages - 1, Math.floor(x * info.pages))); } else chapter(Math.min(info.chapters.length - 1, Math.floor(x * info.chapters.length)), 0);
    });
    function onKey(e) {
      if (!document.body.contains(el) || el.style.display === 'none' || H.overlay.isOpen() || e.ctrlKey || e.metaKey || e.altKey || !info || !msg.hidden) return;
      var k = e.key;
      if (!bar.hidden && bar.contains(document.activeElement)) { showBar(true); return; }
      var eat = function () { e.preventDefault(); e.stopPropagation(); };
      if (info.format === 'cbz') {
        if (k === 'ArrowRight') { turn(rtl ? -1 : 1); eat(); }
        else if (k === 'ArrowLeft') { turn(rtl ? 1 : -1); eat(); }
        else if (k === ' ' || k === 'PageDown') { if (fit === 'width' && stage.scrollTop + stage.clientHeight < stage.scrollHeight - 4) stage.scrollBy(0, stage.clientHeight * 0.85); else turn(1); eat(); }
        else if (k === 'PageUp') { turn(-1); eat(); }
        else if (k === 'ArrowDown') { if (fit === 'width') stage.scrollBy(0, 120); else { showBar(true); H.focus.set(slider, true); } eat(); }
        else if (k === 'ArrowUp') { if (fit === 'width' && stage.scrollTop > 0) stage.scrollBy(0, -120); else { showBar(true); H.focus.set(slider, true); } eat(); }
        else if (k === 'Enter') { showBar(true); H.focus.set(slider, true); eat(); }
      } else {
        if (k === 'ArrowRight' || k === ' ' || k === 'PageDown') { scroll(1); eat(); }
        else if (k === 'ArrowLeft' || k === 'PageUp') { scroll(-1); eat(); }
        else if (k === 'ArrowDown') { frame.contentWindow.scrollBy(0, 90); eat(); }
        else if (k === 'ArrowUp') { frame.contentWindow.scrollBy(0, -90); eat(); }
        else if (k === 'Enter') { showBar(true); H.focus.set(slider, true); eat(); }
      }
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
        clearTimeout(saveTimer); clearTimeout(hideTimer);
        save(true);
        window.removeEventListener('keydown', onKey, true);
        window.removeEventListener('resize', onResize);
        document.removeEventListener('visibilitychange', onHide);
        window.removeEventListener('pagehide', onHide);
      }
    };
  });
})();
