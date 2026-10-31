/* La liseuse : pages de BD / manga (.cbz) et romans (.epub). Elle reprend à la dernière page lue. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;
  var THEMES = { night: { bg: '#15181c', fg: '#d9d5cc', label: 'Nuit' }, sepia: { bg: '#f1e6d0', fg: '#3b3226', label: 'Sépia' }, day: { bg: '#ffffff', fg: '#1b1b1b', label: 'Jour' } };

  H.route(/^\/read\/(\w+)$/, function (m, query) {
    var id = m[1];
    var stage = h('div', { class: 'stage' });
    var label = h('span', { class: 'pg' }), title = h('div', { class: 'ti' });
    var slider = h('div', { class: 'seekbar f', role: 'slider', tabindex: '0', 'aria-label': 'Avancement' }, h('div', { class: 'track' }, h('i'), h('b')));
    var tools = h('div', { class: 'btns' });
    var bBack = H.button('', 'back', { 'aria-label': 'Retour', onclick: function () { H.back('#/'); } });
    var bar = h('div', { class: 'rbar' }, h('div', { class: 'top' }, bBack, h('div', { class: 'txt' }, title), label), h('div', { class: 'bottom' }, slider, tools));
    var msg = h('div', { class: 'err', hidden: true });
    var el = h('section', { class: 'reader' }, stage, bar, msg);
    var info = null, pos = 0, saveTimer = null, hideTimer = null, closing = false, lastSaved = -1, endArmed = false;
    var fit = H.pref('fit') || 'height', rtl = !!H.pref('rtl'), theme = H.pref('theme') || 'night', size = H.pref('fontSize') || 100;
    var img = null, frame = null;

    function showBar(on) {
      bar.hidden = !on;
      clearTimeout(hideTimer);
      if (on) { hideTimer = setTimeout(function () { if (!H.overlay.isOpen()) showBar(false); }, 6000); }
      else if (document.activeElement && bar.contains(document.activeElement)) document.activeElement.blur();
    }
    function total() { return info ? (info.format === 'cbz' ? info.pages : info.chapters.length) : 0; }
    function save(force) {
      if (!info || (!force && pos === lastSaved)) return;
      lastSaved = pos;
      var duration = info.format === 'cbz' ? info.pages : info.chapters.length + 1;
      H.api('/api/progress', { entry: id, position: pos, duration: duration }, { keepalive: true }).catch(function () {});
    }
    function later() { clearTimeout(saveTimer); saveTimer = setTimeout(save, 1500); }
    function fail(text) {
      H.clear(msg).appendChild(h('div', null, h('h2', null, 'Lecture impossible ici'), h('p', null, text),
        h('div', { class: 'actions' }, h('a', { class: 'btn f primary', href: '/media/' + id + '/file?download=1', 'data-autofocus': '1' }, 'Télécharger le fichier'), H.button('Retour', 'back', { onclick: function () { H.back('#/'); } }))));
      msg.hidden = false; bar.hidden = true;
      H.focus.first(msg);
    }
    function openNext() {
      if (info && info.next) { save(true); H.go('#/read/' + info.next.id, true); } else H.toast('C’est la fin.');
    }

    // --- BD / manga -----------------------------------------------------------------
    function cbz() {
      img = h('img', { class: 'page ' + fit, alt: '' });
      stage.appendChild(img);
      stage.classList.add('cbz');
      stage.classList.toggle('wide', fit === 'width');
      stage.addEventListener('click', function (e) {
        var r = stage.getBoundingClientRect(), x = (e.clientX - r.left) / r.width;
        if (x < 0.3) turn(rtl ? 1 : -1); else if (x > 0.7) turn(rtl ? -1 : 1); else showBar(bar.hidden);
      });
      var start = query.p !== undefined ? parseInt(query.p, 10) : info.position;
      page(Math.min(Math.max(0, Math.floor(start || 0)), info.pages - 1));
      tool('Ajuster : ' + (fit === 'height' ? 'hauteur' : 'largeur'), null, function (b) {
        fit = fit === 'height' ? 'width' : 'height'; H.pref('fit', fit); img.className = 'page ' + fit; stage.classList.toggle('wide', fit === 'width'); stage.scrollTop = 0; setLabel(b, 'Ajuster : ' + (fit === 'height' ? 'hauteur' : 'largeur'));
      });
      tool('Lecture : ' + (rtl ? 'droite → gauche' : 'gauche → droite'), null, function (b) {
        rtl = !rtl; H.pref('rtl', rtl); setLabel(b, 'Lecture : ' + (rtl ? 'droite → gauche' : 'gauche → droite'));
      });
      if (info.next) tool('Volume suivant', 'next', openNext);
    }
    function page(n) {
      pos = n;
      img.src = '/book/' + id + '/page/' + n;
      stage.scrollTop = 0;
      [1, 2].forEach(function (k) { if (n + k < info.pages) { var pre = new Image(); pre.src = '/book/' + id + '/page/' + (n + k); } });
      paint(); later();
      endArmed = false;
    }
    function turn(delta) {
      var n = pos + delta;
      if (n < 0) return;
      if (n >= info.pages) {
        if (!endArmed) { endArmed = true; save(true); H.toast(info.next ? 'Fin du volume · encore une fois pour passer au suivant' : 'Fin du volume'); return; }
        openNext(); return;
      }
      page(n);
    }

    // --- Roman ---------------------------------------------------------------------
    function epub() {
      frame = h('iframe', { class: 'book', title: 'Livre', sandbox: 'allow-same-origin' });
      stage.appendChild(frame);
      stage.classList.add('epub');
      frame.addEventListener('load', styleFrame);
      var start = info.position || 0;
      chapter(Math.min(Math.floor(start), info.chapters.length - 1), start - Math.floor(start));
      tool('Texte −', null, function () { setSize(size - 10); });
      tool('Texte +', null, function () { setSize(size + 10); });
      tool('Thème : ' + THEMES[theme].label, null, function (b) {
        var names = Object.keys(THEMES); theme = names[(names.indexOf(theme) + 1) % names.length]; H.pref('theme', theme); setLabel(b, 'Thème : ' + THEMES[theme].label); styleFrame();
      });
      tool('Chapitre précédent', 'prev', function () { skipChapter(-1); });
      tool('Chapitre suivant', 'next', function () { skipChapter(1); });
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
    function setSize(v) { size = Math.max(70, Math.min(220, v)); H.pref('fontSize', size); styleFrame(); H.toast('Texte à ' + size + ' %'); }
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
      var n = total(), shown = info.format === 'cbz' ? pos + 1 : Math.floor(pos) + 1;
      label.textContent = info.format === 'cbz' ? 'Page ' + shown + ' / ' + n : 'Chapitre ' + shown + ' / ' + n;
      var ratio = n ? (info.format === 'cbz' ? (pos + 1) / n : pos / n) : 0;
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
      if (info.format === 'cbz') page(Math.min(info.pages - 1, Math.floor(x * info.pages))); else chapter(Math.min(info.chapters.length - 1, Math.floor(x * info.chapters.length)), 0);
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
      else fail('Le format ' + (data.format || 'inconnu').toUpperCase() + ' ne se lit pas dans le navigateur.');
    }).catch(function (e) { fail(e.message); });

    return {
      el: el, full: true, nav: '#/read', lib: '',
      shown: function () { if (info) { paint(); } },
      onBack: function () {
        if (H.routed <= 1 && info) { location.hash = '#/i/' + info.item; return true; }
        return false;
      },
      destroy: function () {
        closing = true;
        clearTimeout(saveTimer); clearTimeout(hideTimer);
        save(true);
        window.removeEventListener('keydown', onKey, true);
        document.removeEventListener('visibilitychange', onHide);
        window.removeEventListener('pagehide', onHide);
      }
    };
  });
})();
