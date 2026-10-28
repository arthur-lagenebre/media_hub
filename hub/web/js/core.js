/* Outils communs : éléments, appels au serveur, formats, navigation à la télécommande, fenêtres de choix, pile de pages.
   Écrit en JavaScript sobre (pas de ?. ni de ??) pour passer sur les navigateurs un peu anciens des télés. */
(function () {
  'use strict';
  var H = window.Hub = window.Hub || {};

  // --- Éléments ---------------------------------------------------------------
  function add(el, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(function (x) { add(el, x); });
    else if (c.nodeType) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
  H.h = function (tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === 'class') el.className = v;
      else if (k.indexOf('on') === 0) el.addEventListener(k.slice(2), v);
      else if (k === 'text') el.textContent = v;
      else if (k === 'html') el.innerHTML = v;
      else if (k === 'style') el.style.cssText = v;
      else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'hidden') el[k] = v;
      else el.setAttribute(k, v === true ? '' : v);
    });
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  };
  var h = H.h;
  H.clear = function (el) { while (el.firstChild) el.removeChild(el.firstChild); return el; };

  var ICONS = {
    play: 'M8 5v14l11-7z', pause: 'M6 5h4v14H6zM14 5h4v14h-4z',
    rew: 'M11 18V6l-8.5 6zm.5-6l8.5 6V6z', fwd: 'M4 18l8.5-6L4 6v12zm9-12v12l8.5-6L13 6z',
    next: 'M6 18l8.5-6L6 6v12zM16 6v12h2V6h-2z', prev: 'M6 6h2v12H6zm3.5 6l8.5 6V6z',
    audio: 'M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0 0 14 8v8a4.5 4.5 0 0 0 2.5-4z',
    subs: 'M19 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zM6 10h5v1.5H7.5v1H11V14H6zm7 0h5v1.5h-3.5v1H18V14h-5z',
    gear: 'M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z',
    search: 'M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z',
    check: 'M9 16.2L4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z', close: 'M19 6.4L17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12z',
    full: 'M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z',
    back: 'M20 11H7.8l5.6-5.6L12 4l-8 8 8 8 1.4-1.4L7.8 13H20v-2z',
    shuffle: 'M10.6 9.2L5.4 4 4 5.4l5.2 5.2 1.4-1.4zM14.5 4l2.04 2.04L4 18.6 5.4 20l12.56-12.56L20 9.5V4h-5.5zm.5 9.8l-1.4 1.4 3.36 3.36L15 20.5h5.5V15l-2.04 2.04L15 13.8z',
    quality: 'M4 6h16v2H4zm3 5h10v2H7zm3 5h4v2h-4z', refresh: 'M17.65 6.35A8 8 0 1 0 19.74 14h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z',
    note: 'M12 3v10.55A4 4 0 1 0 14 17V7h4V3z', home: 'M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z'
  };
  H.icon = function (name, cls) {
    return h('span', { class: 'ic' + (cls ? ' ' + cls : ''), 'aria-hidden': 'true', html: '<svg viewBox="0 0 24 24"><path d="' + (ICONS[name] || '') + '"/></svg>' });
  };
  H.button = function (label, icon, attrs) {
    attrs = attrs || {};
    attrs.class = 'btn f' + (attrs.class ? ' ' + attrs.class : '');
    return h('button', attrs, icon ? H.icon(icon) : null, label ? h('span', { class: 'lbl' }, label) : null);
  };

  // --- Serveur ----------------------------------------------------------------
  H.api = function (path, body, opts) {
    var init = { credentials: 'same-origin', headers: {} };
    if (body !== undefined && body !== null) {
      init.method = 'POST';
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    if (opts && opts.keepalive) init.keepalive = true;
    return fetch(path, init).then(function (r) {
      if (r.status === 401) { location.href = '/login'; throw new Error('connexion requise'); }
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = null; }
        if (!r.ok) {
          var err = new Error((data && data.error) || ('Erreur ' + r.status));
          err.status = r.status;
          throw err;
        }
        return data;
      });
    });
  };

  H.pref = function (key, value) {
    try {
      if (value === undefined) { var v = localStorage.getItem('hub.' + key); return v === null ? null : JSON.parse(v); }
      localStorage.setItem('hub.' + key, JSON.stringify(value));
    } catch (e) { return null; }
    return value;
  };

  // --- Formats ----------------------------------------------------------------
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  H.time = function (s) {
    s = Math.max(0, Math.floor(s || 0));
    var hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
    return hh ? hh + ':' + pad(mm) + ':' + pad(ss) : mm + ':' + pad(ss);
  };
  H.dur = function (s) {
    s = Math.round((s || 0) / 60);
    if (!s) return '';
    return s >= 60 ? Math.floor(s / 60) + ' h ' + pad(s % 60) : s + ' min';
  };
  H.plural = function (n, one, many) { return n + ' ' + (n > 1 ? (many || one + 's') : one); };
  H.number = function (n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' '); };
  H.hue = function (text) {
    var x = 0;
    for (var i = 0; i < text.length; i++) x = (x * 31 + text.charCodeAt(i)) % 360;
    return x;
  };
  H.toast = function (message, ms) {
    var box = document.getElementById('toast');
    box.textContent = message;
    box.classList.add('on');
    clearTimeout(H.toast.timer);
    H.toast.timer = setTimeout(function () { box.classList.remove('on'); }, ms || 3200);
  };

  // --- Fenêtres de choix --------------------------------------------------------
  // Chaque fenêtre ouverte ajoute une entrée à l'historique : le bouton Retour de la télécommande la ferme sans quitter la page.
  var overlays = [], skipPops = 0, afterPop = null;
  H.overlay = {
    open: function (el, onClose) {
      var back = h('div', { class: 'veil', onclick: function (e) { if (e.target === back) H.overlay.close(); } }, el);
      back.hubClose = onClose;
      back.opener = document.activeElement;
      document.body.appendChild(back);
      overlays.push(back);
      try { history.pushState({ ov: overlays.length }, ''); } catch (e) { /* tant pis : Échap fermera */ }
      H.focus.first(back);
      return back;
    },
    close: function (then) {
      var top = overlays.pop();
      if (!top) { if (then) then(); return; }
      cleanup(top);
      if (history.state && history.state.ov) { skipPops++; afterPop = then || null; history.back(); } else if (then) then();
    },
    closeAll: function () { while (overlays.length) cleanup(overlays.pop()); },
    isOpen: function () { return overlays.length > 0; },
    top: function () { return overlays[overlays.length - 1] || null; }
  };
  function cleanup(back) {
    if (back.parentNode) back.parentNode.removeChild(back);
    if (back.hubClose) back.hubClose();
    if (back.opener && document.body.contains(back.opener)) H.focus.set(back.opener, true);
  }
  window.addEventListener('popstate', function () {
    if (skipPops > 0) {
      skipPops--;
      var then = afterPop; afterPop = null;
      if (then) then();
      return;
    }
    if (overlays.length) cleanup(overlays.pop());
  });

  // Un menu de choix : [{label, detail, value, disabled}] ; `current` est la valeur cochée.
  H.menu = function (title, options, current, onPick) {
    var list = h('div', { class: 'menu-list' });
    var box = h('div', { class: 'menu', role: 'dialog', 'aria-label': title }, h('h3', null, title), list);
    options.forEach(function (o) {
      var on = o.value === current;
      var b = h('button', { class: 'opt f' + (on ? ' on' : ''), disabled: !!o.disabled, 'data-autofocus': on ? '1' : null,
        onclick: function () { H.overlay.close(function () { onPick(o.value); }); } },
        h('span', { class: 'mark' }, on ? H.icon('check') : null), h('span', { class: 'lbl' }, o.label), o.detail ? h('span', { class: 'det' }, o.detail) : null);
      list.appendChild(b);
    });
    if (!options.length) list.appendChild(h('p', { class: 'muted' }, 'Rien à choisir.'));
    H.overlay.open(box);
  };

  // --- Focus à la télécommande ----------------------------------------------------
  var TOL = 6;
  function visible(el) {
    if (el.disabled || el.hasAttribute('hidden')) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && el.getClientRects().length > 0;
  }
  function interval(a1, a2, b1, b2) { return Math.max(0, Math.max(a1, b1) - Math.min(a2, b2)); }
  function candidates() {
    var scope = H.overlay.top() || document;
    return Array.prototype.filter.call(scope.querySelectorAll('.f'), visible);
  }
  function score(c, r, dir) {
    var gap, cross, off, penalty = 0;
    if (dir === 'right' || dir === 'left') {
      if (dir === 'right' ? r.cx <= c.cx : r.cx >= c.cx) return null;
      gap = dir === 'right' ? r.left - c.right : c.left - r.right;
      if (gap < -TOL) { penalty = 400; gap = 0; }
      cross = interval(c.top, c.bottom, r.top, r.bottom); off = Math.abs(r.cy - c.cy);
    } else {
      if (dir === 'down' ? r.cy <= c.cy : r.cy >= c.cy) return null;
      gap = dir === 'down' ? r.top - c.bottom : c.top - r.bottom;
      if (gap < -TOL) { penalty = 400; gap = 0; }
      cross = interval(c.left, c.right, r.left, r.right); off = Math.abs(r.cx - c.cx);
    }
    return Math.max(0, gap) + penalty + cross * 3 + off * 0.6;
  }
  function box(el) {
    var r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, cx: (r.left + r.right) / 2, cy: (r.top + r.bottom) / 2 };
  }
  function reveal(el) {
    var nav = document.getElementById('nav');
    var navH = nav && nav.offsetHeight && !document.body.classList.contains('full') ? nav.offsetHeight : 0;
    var bar = document.getElementById('abar');
    var barH = bar && !bar.hidden && !document.body.classList.contains('full') ? bar.offsetHeight : 0;
    var row = el.closest ? el.closest('.row') : null;
    if (row) {
      var rr = row.getBoundingClientRect(), er = el.getBoundingClientRect();
      if (er.left < rr.left + 8 || er.right > rr.right - 8) row.scrollLeft += (er.left + er.right) / 2 - (rr.left + rr.right) / 2;
    }
    if (el.closest && el.closest('.veil, .full')) return;
    var r = el.getBoundingClientRect();
    if (r.top < navH + 24 || r.bottom > window.innerHeight - barH - 24) {
      var target = window.scrollY + (r.top + r.bottom) / 2 - (navH + (window.innerHeight - barH - navH) / 2);
      window.scrollTo(0, Math.max(0, target));
    }
  }
  H.focus = {
    set: function (el, noScroll) {
      if (!el) return false;
      try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); }
      if (!noScroll) reveal(el);
      return true;
    },
    first: function (root) {
      var scoped = root && root !== document;
      root = root || H.overlay.top() || document;
      var pick = function (scope) {
        var el = scope.querySelector('[data-autofocus]:not([hidden])');
        if (el && visible(el)) return el;
        return Array.prototype.filter.call(scope.querySelectorAll('.f'), function (x) { return x.tagName !== 'INPUT' && visible(x); })[0] || null;   // un champ de saisie ouvrirait le clavier à l'écran de la télé
      };
      var el = null;
      if (!scoped && !H.overlay.top()) {            // sans fenêtre ouverte : le contenu de la page, pas la barre du haut
        var views = document.querySelectorAll('#views > .view');
        for (var i = views.length - 1; i >= 0; i--) if (views[i].style.display !== 'none') { el = pick(views[i]); return el ? H.focus.set(el, true) : false; }
      }
      return H.focus.set(pick(root), true);
    },
    key: function (root, key) {
      var el = null;
      Array.prototype.forEach.call((root || document).querySelectorAll('[data-key]'), function (x) { if (!el && x.getAttribute('data-key') === key) el = x; });
      return el ? H.focus.set(el) : false;
    },
    move: function (dir) {
      var cur = document.activeElement;
      var all = candidates();
      if (!cur || cur === document.body || all.indexOf(cur) < 0) {
        if (H.overlay.top() && H.overlay.top().contains(cur)) return false;
        return H.focus.first();
      }
      var c = box(cur), best = null, bestScore = Infinity;
      all.forEach(function (el) {
        if (el === cur) return;
        var s = score(c, box(el), dir);
        if (s !== null && s < bestScore) { best = el; bestScore = s; }
      });
      if (best) return H.focus.set(best);
      if (dir === 'down' && H.audio && H.audio.focusBar && !H.overlay.top()) return H.audio.focusBar();
      return false;
    }
  };

  var ARROWS = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', 37: 'left', 39: 'right', 38: 'up', 40: 'down' };
  var BACKS = { Escape: 1, BrowserBack: 1, GoBack: 1, XF86Back: 1, 27: 1, 4: 1, 10009: 1, 461: 1, 166: 1 };
  document.addEventListener('keydown', function (e) {
    document.body.classList.remove('mouse');
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    var key = e.key, code = e.keyCode;
    var t = e.target, typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT');
    var dir = ARROWS[key] || ARROWS[code];
    if (dir) {
      if (typing && t.tagName === 'INPUT' && (dir === 'left' || dir === 'right')) {
        var atStart = t.selectionStart === 0 && t.selectionEnd === 0, atEnd = t.selectionStart === t.value.length;
        if (!((dir === 'left' && atStart) || (dir === 'right' && atEnd))) return;
      }
      if (typing && t.tagName === 'SELECT') return;
      if (H.focus.move(dir)) e.preventDefault(); else if (dir === 'left' || dir === 'right') e.preventDefault();
      return;
    }
    if (BACKS[key] || BACKS[code] || (key === 'Backspace' && !typing)) {
      if (typing && key === 'Escape') { t.blur(); e.preventDefault(); return; }
      e.preventDefault();
      H.back();
      return;
    }
    if ((key === 'Enter' || code === 13 || code === 23) && t && t.classList && t.classList.contains('f') && !/^(A|BUTTON|INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) {
      e.preventDefault();
      t.click();
    }
  });
  ['pointermove', 'mousedown', 'touchstart'].forEach(function (name) {
    document.addEventListener(name, function () { document.body.classList.add('mouse'); }, { passive: true });
  });

  // --- Pile de pages ----------------------------------------------------------------
  // Chaque page ouverte garde son DOM (filtres, défilement, focus) tant qu'elle est dans la pile : Retour la retrouve telle qu'elle était.
  var routes = [], stack = [], MAX = 10;
  H.route = function (pattern, factory) { routes.push({ pattern: pattern, factory: factory }); };
  H.routed = 0;
  H.replaceNext = false;

  function create(hash) {
    var path = hash.replace(/^#/, '') || '/';
    var query = {}, qi = path.indexOf('?');
    if (qi >= 0) {
      path.slice(qi + 1).split('&').forEach(function (p) { var kv = p.split('='); if (kv[0]) query[decodeURIComponent(kv[0])] = decodeURIComponent((kv[1] || '').replace(/\+/g, ' ')); });
      path = path.slice(0, qi);
    }
    for (var i = 0; i < routes.length; i++) {
      var m = routes[i].pattern.exec(path);
      if (m) {
        var view = routes[i].factory(m, query);
        view.el.classList.add('view');
        document.getElementById('views').appendChild(view.el);
        return { hash: hash, el: view.el, view: view, scroll: 0, focus: null };
      }
    }
    var el = h('section', { class: 'view empty' }, h('h1', null, 'Page introuvable'), h('a', { class: 'btn f', href: '#/', 'data-autofocus': '1' }, 'Retour à l’accueil'));
    document.getElementById('views').appendChild(el);
    return { hash: hash, el: el, view: { el: el }, scroll: 0, focus: null };
  }
  function park(entry) {
    entry.scroll = window.scrollY;
    entry.focus = document.activeElement && entry.el.contains(document.activeElement) ? document.activeElement : null;
    entry.el.style.display = 'none';
  }
  function destroy(entry) {
    try { if (entry.view.destroy) entry.view.destroy(); } catch (e) { console.error(e); }
    if (entry.el.parentNode) entry.el.parentNode.removeChild(entry.el);
  }
  function present(entry, fresh) {
    entry.el.style.display = '';
    document.body.classList.toggle('full', !!entry.view.full);
    document.body.setAttribute('data-lib', entry.view.lib || '');
    H.nav.mark(entry.view.nav || entry.hash);
    if (fresh) {
      window.scrollTo(0, 0);
      H.focus.first();
    } else {
      window.scrollTo(0, entry.scroll);
      if (entry.focus && document.body.contains(entry.focus)) H.focus.set(entry.focus, true); else H.focus.first();
      if (entry.view.refresh) entry.view.refresh();
    }
    if (entry.view.shown) entry.view.shown(fresh);
  }
  function onRoute() {
    H.overlay.closeAll();
    var hash = location.hash || '#/';
    var top = stack[stack.length - 1];
    if (top && top.hash === hash) return;
    H.routed++;
    var back = -1;
    for (var i = stack.length - 2; i >= 0; i--) if (stack[i].hash === hash) { back = i; break; }
    if (back >= 0 && !H.replaceNext) {
      while (stack.length - 1 > back) destroy(stack.pop());
      present(stack[back], false);
      return;
    }
    if (top) { if (H.replaceNext) destroy(stack.pop()); else park(top); }
    H.replaceNext = false;
    var entry = create(hash);
    stack.push(entry);
    while (stack.length > MAX) destroy(stack.shift());
    present(entry, true);
  }
  H.start = function () { window.addEventListener('hashchange', onRoute); onRoute(); };
  H.go = function (hash, replace) {
    if (replace) { H.replaceNext = true; location.replace(hash); } else location.hash = hash;
  };
  H.current = function () { return stack[stack.length - 1] || null; };
  // Retour : ferme la fenêtre ouverte, sinon remonte d'une page ; `fallback` sert quand la page a été ouverte directement.
  H.back = function (fallback) {
    if (H.overlay.isOpen()) { H.overlay.close(); return; }
    var top = H.current();
    if (top && top.view.onBack && top.view.onBack()) return;
    if (stack.length > 1 || H.routed > 1) history.back();
    else if (fallback && location.hash !== fallback) location.hash = fallback;
    else if (location.hash && location.hash !== '#/') location.hash = '#/';
  };
})();
