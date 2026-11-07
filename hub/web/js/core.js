/* Outils communs : éléments, appels au serveur, formats, navigation à la télécommande, pop-up (voir docs/popups.md), pile de pages.
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
    mute: 'M16.5 12A4.5 4.5 0 0 0 14 8v2.18l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.8 8.8 0 0 0 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 0 0 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z',
    subs: 'M19 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zM6 10h5v1.5H7.5v1H11V14H6zm7 0h5v1.5h-3.5v1H18V14h-5z',
    gear: 'M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.488.488 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z',
    search: 'M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z',
    check: 'M9 16.2L4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z', close: 'M19 6.4L17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12z',
    full: 'M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z',
    back: 'M20 11H7.8l5.6-5.6L12 4l-8 8 8 8 1.4-1.4L7.8 13H20v-2z',
    shuffle: 'M10.6 9.2L5.4 4 4 5.4l5.2 5.2 1.4-1.4zM14.5 4l2.04 2.04L4 18.6 5.4 20l12.56-12.56L20 9.5V4h-5.5zm.5 9.8l-1.4 1.4 3.36 3.36L15 20.5h5.5V15l-2.04 2.04L15 13.8z',
    quality: 'M4 6h16v2H4zm3 5h10v2H7zm3 5h4v2h-4z', refresh: 'M17.65 6.35A8 8 0 1 0 19.74 14h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z',
    note: 'M12 3v10.55A4 4 0 1 0 14 17V7h4V3z', home: 'M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z',
    queue: 'M15 6H3v2h12V6zm0 4H3v2h12v-2zM3 16h8v-2H3v2zM17 6v8.18c-.31-.11-.65-.18-1-.18-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3V8h3V6h-5z',
    plus: 'M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z',
    more: 'M6 10c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm12 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm-6 0c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z',
    download: 'M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z', up: 'M7.41 15.41L12 10.83l4.59 4.58L18 14l-6-6-6 6z', down: 'M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z',
    trash: 'M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z',
    edit: 'M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z'
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
    if (opts && opts.raw) {              // un fichier envoyé tel quel (Blob), pas du JSON
      init.method = 'POST';
      init.headers['Content-Type'] = body.type || 'application/octet-stream';
      init.body = body;
    } else if (body !== undefined && body !== null) {
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
          var err = new Error(data && data.error ? H.tr(data.error) : H.t('Erreur {status}', { status: r.status }));
          err.status = r.status;
          throw err;
        }
        return data;
      });
    });
  };

  // Qui est connecté : rempli à partir de /api/state (démarrage, page Gestion). Serveur ouvert (`auth` faux) : un seul profil, administrateur.
  // Masquer une action n'est qu'un confort : le serveur refuse (403) ce que le rôle ne permet pas.
  H.session = { auth: false, user: null, settings: {} };
  H.setSession = function (state) {
    H.session = { auth: !!(state && state.auth), user: (state && state.user) || null, settings: (state && state.settings) || {} };
  };
  // Les réglages du compte (gardés sur le serveur : ils le suivent d'un appareil à l'autre) ; `name` : « music_view »...
  H.setting = function (name) { return (H.session.settings || {})[name]; };
  H.saveSetting = function (name, value) {
    var change = {};
    change[name] = value;
    return H.api('/api/me/settings', change).then(function (res) { H.session.settings = res.settings; return res.settings; });
  };
  // Ce navigateur peut servir à plusieurs comptes : ce qui est propre à un compte (la file audio) ne doit pas passer de l'un à l'autre.
  // On retient l'id du dernier compte vu ; s'il change, on purge. Serveur ouvert : pas de compte, rien à purger. À appeler au démarrage, avant de relire la file.
  H.syncAccount = function () {
    var me = H.session.auth && H.session.user;
    if (!me) return;
    if (H.pref('account') !== me.id) H.pref('queue', null);
    H.pref('account', me.id);
  };
  H.forgetAccount = function () { H.pref('queue', null); H.pref('account', null); };     // déconnexion, compte supprimé
  H.isAdmin = function () { return !H.session.auth || !H.session.user || H.session.user.role === 'admin'; };

  H.pref = function (key, value) {
    try {
      if (value === undefined) { var v = localStorage.getItem('hub.' + key); return v === null ? null : JSON.parse(v); }
      localStorage.setItem('hub.' + key, JSON.stringify(value));
    } catch (e) { return null; }
    return value;
  };

  // --- Appareil : pc, phone, tablet ou tv ----------------------------------------------
  // Le navigateur ne dit pas « je suis une télé » : on le devine à son identifiant (Tizen, webOS, Fire TV…), puis au toucher. Approximatif, donc modifiable dans Gestion.
  H.devices = [['auto', H.t('Automatique')], ['pc', H.t('Ordinateur')], ['phone', H.t('Smartphone')], ['tablet', 'Tablette'], ['tv', H.t('Télévision')]];
  H.detectDevice = function () {
    var ua = navigator.userAgent || '', touch = (navigator.maxTouchPoints || 0) > 1;
    var tizenPhone = /Tizen/i.test(ua) && /Mobile/i.test(ua) && !/TV/i.test(ua);                        // Tizen est aussi celui de quelques téléphones Samsung ; un Fire TV, lui, s'annonce « Mobile »
    if (/SMART-?TV|HbbTV|Tizen|Web0S|webOS|NetCast|VIDAA|CrKey|AppleTV|Roku|Viera|PhilipsTV|BRAVIA|AFT[A-Z0-9]+\b|Android.*\b(TV|SHIELD)\b/i.test(ua) && !tizenPhone) return 'tv';
    if (/iPad|Tablet/i.test(ua) || (/Macintosh/.test(ua) && touch)) return 'tablet';             // un iPad se présente comme un Mac : seul le toucher le trahit
    if (/Android/i.test(ua)) return /Mobile/i.test(ua) ? 'phone' : 'tablet';
    if (/Mobi|iPhone|iPod/i.test(ua)) return 'phone';
    return 'pc';
  };
  H.applyDevice = function () {
    var chosen = H.pref('device');
    H.device = H.devices.some(function (d) { return d[0] === chosen && chosen !== 'auto'; }) ? chosen : H.detectDevice();
    document.documentElement.setAttribute('data-device', H.device);
    return H.device;
  };
  H.applyDevice();

  // --- Taille du texte : celle de l'appareil (par défaut) ou un cran plus petit, au choix, sur n'importe quel appareil ---
  H.textSizes = [['normal', H.t('Normale'), H.t('La taille prévue pour l’appareil')], ['small', H.t('Petite'), H.t('Un peu plus petite, sur tous les appareils')]];
  H.applyTextSize = function () {
    var chosen = H.pref('textSize');
    H.textSize = H.textSizes.some(function (s) { return s[0] === chosen; }) ? chosen : 'normal';
    if (H.textSize === 'normal') document.documentElement.removeAttribute('data-text'); else document.documentElement.setAttribute('data-text', H.textSize);
    return H.textSize;
  };
  H.applyTextSize();

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

  // --- Fenêtres : pile, voile, bouton Retour -----------------------------------------
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

  // --- Pop-up : un gabarit (`.menu`), quatre déclinaisons, pas une de plus (voir docs/popups.md) ---------------
  //   choice  : choisir dans une liste, un clic ferme                      → H.menu
  //   confirm : une question, un bouton d'action (rouge si on détruit), Annuler   → H.confirm
  //   form    : un ou plusieurs champs, ou un petit réglage, avec Valider / Annuler → H.form, H.prompt (un seul champ)
  //   wide    : contenu libre (liste à gérer, recherche, image à recadrer) avec un pied de boutons → H.box('wide …') puis H.overlay.open
  // Un nouveau pop-up part d'une de ces quatre ; il ne construit jamais son `div role="dialog"` lui-même (un test le vérifie).
  // `kind` : la déclinaison, suivie au besoin d'un modificateur (« wide queue ») ; les enfants suivants sont ajoutés tels quels.
  H.box = function (kind, title) {
    var box = h('div', { class: 'menu ' + kind, role: 'dialog', 'aria-label': title }, h('h3', null, title));
    for (var i = 2; i < arguments.length; i++) add(box, arguments[i]);
    box.addEventListener('focusin', function (e) { try { e.target.scrollIntoView({ block: 'nearest' }); } catch (x) { /* tant pis */ } });   // petit écran ou télé : la fenêtre défile jusqu'à l'élément atteint
    return box;
  };

  // choice : un menu de choix : [{label, detail, value, disabled}] ; `current` est la valeur cochée.
  H.menu = function (title, options, current, onPick) {
    var list = h('div', { class: 'menu-list' });
    var box = H.box('choice', title, list);
    options.forEach(function (o) {
      var on = o.value === current;
      var b = h('button', { class: 'opt f' + (on ? ' on' : ''), disabled: !!o.disabled, 'data-autofocus': on ? '1' : null,
        onclick: function () { H.overlay.close(function () { onPick(o.value); }); } },
        h('span', { class: 'mark' }, on ? H.icon('check') : null), h('span', { class: 'lbl' }, o.label), o.detail ? h('span', { class: 'det' }, o.detail) : null);
      list.appendChild(b);
    });
    if (!options.length) list.appendChild(h('p', { class: 'muted' }, H.t('Rien à choisir.')));
    H.overlay.open(box);
  };

  // form : une fenêtre à champs, ou à contenu propre : o = {title, lead: [phrases], fields: [{key, label, type: 'text' | 'password', auto, maxlength}], extra: [éléments sous les champs],
  // ok: libellé du bouton, danger, kind (« form » par défaut s'il y a des champs), cls (modificateur), submit(valeurs) → texte (refus affiché dans la fenêtre) ou promesse,
  // done(résultat) une fois la fenêtre fermée}. Une erreur du serveur reste affichée dans la fenêtre, qui reste ouverte. Entrée passe au champ suivant, puis valide.
  H.form = function (o) {
    var fields = o.fields || [], inputs = {}, list = [], busy = false, ok, err = h('p', { class: 'formerr', role: 'alert' });
    var box = H.box((o.kind || (fields.length ? 'form' : 'confirm')) + (fields.length > 1 ? ' stacked' : '') + (o.cls ? ' ' + o.cls : ''), o.title);
    (o.lead || []).forEach(function (t) { box.appendChild(h('p', { class: 'lead' }, t)); });
    function show(text) { err.textContent = text || ''; }
    function send() {
      if (busy) return;
      var values = {};
      fields.forEach(function (f) { var v = inputs[f.key].value; values[f.key] = f.type === 'password' ? v : v.replace(/\s+/g, ' ').trim(); });
      var r = o.submit ? o.submit(values) : Promise.resolve();
      if (typeof r === 'string') { show(r); return; }
      busy = true; show('');
      r.then(function (res) { H.overlay.close(function () { if (o.done) o.done(res); }); },
        function (e) { busy = false; show(e.message); });
    }
    fields.forEach(function (f, i) {
      var input = h('input', { class: 'f field big', type: f.type === 'password' ? 'password' : 'text', maxlength: String(f.maxlength || (f.type === 'password' ? 200 : 40)), autocomplete: f.auto || 'off',
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
    ok = H.button(o.ok || H.t('Valider'), o.danger ? 'trash' : 'check', { class: o.danger ? 'danger' : 'primary', onclick: send });
    box.appendChild(h('div', { class: 'btns' }, ok, H.button(H.t('Annuler'), null, { 'data-autofocus': fields.length ? null : '1', onclick: function () { H.overlay.close(); } })));   // sans champ, le focus part sur Annuler : Entrée ne détruit rien
    H.overlay.open(box);
    if (fields.length) setTimeout(function () { try { list[0].focus(); } catch (e) { /* tant pis */ } }, 30);
  };

  // confirm : « Supprimer ceci ? ». o = {title, lead, ok, danger, extra, submit, done} comme H.form, sans champ ; `done` est appelé une fois la fenêtre fermée.
  H.confirm = function (o) { o.fields = []; delete o.kind; H.form(o); };

  // Une question à une ligne de texte (nom d'une playlist) : `onOk(texte)` est appelé si le texte n'est pas vide.
  // `opts` (facultatif) : {maxlength} (80 par défaut : une adresse web en demande davantage), {placeholder}.
  H.prompt = function (title, initial, okLabel, onOk, opts) {
    opts = opts || {};
    var input = h('input', { class: 'f field big', type: 'text', value: initial || '', maxlength: String(opts.maxlength || 80), placeholder: opts.placeholder || null, 'aria-label': title, 'data-autofocus': '1', autocomplete: 'off' });
    function submit() {
      var text = input.value.replace(/\s+/g, ' ').trim();
      if (!text) { input.focus(); return; }
      H.overlay.close(function () { onOk(text); });
    }
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
    var box = H.box('form prompt', title, input,
      h('div', { class: 'btns' }, H.button(okLabel || H.t('Valider'), 'check', { class: 'primary', onclick: submit }), H.button(H.t('Annuler'), null, { onclick: function () { H.overlay.close(); } })));
    H.overlay.open(box);
    setTimeout(function () { try { input.focus(); input.select(); } catch (e) { /* tant pis */ } }, 30);
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
    key: function (root, key, noScroll) {
      var el = null;
      Array.prototype.forEach.call((root || document).querySelectorAll('[data-key]'), function (x) { if (!el && x.getAttribute('data-key') === key) el = x; });
      return el ? H.focus.set(el, noScroll) : false;
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
  var BACK_KEYS = { Escape: 1, BrowserBack: 1, GoBack: 1, XF86Back: 1 };
  var BACK_CODES = { 27: 1, 4: 1, 10009: 1, 461: 1, 166: 1 };      // deux tables : chercher la touche « 4 » (e.key === '4') parmi les codes fermait toute fenêtre à la frappe du chiffre 4
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
    if (BACK_KEYS[key] || BACK_CODES[code] || (key === 'Backspace' && !typing)) {
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
    var el = h('section', { class: 'view empty' }, h('h1', null, H.t('Page introuvable')), h('a', { class: 'btn f', href: '#/', 'data-autofocus': '1' }, H.t('Retour à l’accueil')));
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
