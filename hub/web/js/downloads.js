/* La file des téléchargements : plusieurs fichiers (une saison, une série, un album) partent un par un, le suivant quand le serveur a fini d'envoyer le précédent.
   Le navigateur ne dit pas quand un téléchargement lancé par un lien se termine : le serveur, si (GET /api/downloads, voir docs/api.md). Chaque lien porte une tentative tirée au hasard (`dl`).
   La file est gardée dans ce navigateur, par compte (localStorage) : elle reprend au rechargement. Un seul onglet la mène (un verrou, rafraîchi à chaque pas) ; les autres la montrent.
   Un fichier interrompu ne bloque pas la file : il est noté et proposé de nouveau dans le bilan. Un lien que le serveur n'a jamais vu (le navigateur l'a bloqué, ou demande une autorisation) met la file en pause.
   Un seul fichier : un seul lien, suivi sans rien afficher. L'avancement d'une file est une barre, en bas, au-dessus de la musique (docs/popups.md : les barres ne sont pas des pop-ups). */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;
  var POLL = 2500;              // l'interrogation de /api/downloads, en millisecondes
  var NEVER_TAKEN = 30000;      // un lien que le serveur n'a toujours pas vu au bout de ce temps : le navigateur ne l'a pas suivi
  var LOCK_STALE = 90000;       // le verrou d'un onglet qui ne le rafraîchit plus : un onglet en arrière-plan ne se réveille qu'une fois par minute
  var ALNUM = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  var BOOT = Date.now();        // une tentative lancée avant le chargement de cette page et que le serveur ne connaît pas est relancée
  var memo = null;              // la file, quand le stockage du navigateur est fermé : elle ne survit pas au rechargement, mais marche
  var timer = null, bar = null, ui = {};
  var D = H.downloads = {};

  // --- Le stockage ---------------------------------------------------------------------
  // Une file : `items` ({id, title, state, dl, at, seen, bytes, size}), `show` (plus d'un fichier : la barre se montre), `paused`, `end` (le bilan attend qu'on le ferme).
  // `state` : `wait` (pas encore lancé), `sent` (le lien est parti), `done`, `failed`.
  function rand() {
    var bytes = new Uint8Array(16), out = '';
    try { window.crypto.getRandomValues(bytes); } catch (e) { for (var j = 0; j < 16; j++) bytes[j] = Math.floor(Math.random() * 256); }
    for (var i = 0; i < 16; i++) out += ALNUM.charAt(bytes[i] % ALNUM.length);
    return out;
  }
  var TAB = (function () {       // l'onglet : gardé pour la session, il survit au rechargement (le verrou reste le sien)
    try {
      var t = sessionStorage.getItem('hub.dltab');
      if (!t) { t = rand(); sessionStorage.setItem('hub.dltab', t); }
      return t;
    } catch (e) { return rand(); }
  })();
  function key() { var me = H.session.auth && H.session.user; return 'hub.dlq.' + (me ? me.id : 'open'); }
  function load() {
    try { var v = localStorage.getItem(key()); return v ? JSON.parse(v) : null; } catch (e) { return memo; }
  }
  function save(q) {
    memo = q;
    try { if (q) localStorage.setItem(key(), JSON.stringify(q)); else localStorage.removeItem(key()); } catch (e) { /* stockage fermé : la file reste en mémoire */ }
  }
  // Vrai quand cet onglet mène la file : le verrou est libre, déjà le sien, ou abandonné. Le prendre le rafraîchit.
  function lead() {
    var k = key() + '.lock', now = Date.now();
    try {
      var l = JSON.parse(localStorage.getItem(k) || 'null');
      if (l && l.tab !== TAB && now - l.at < LOCK_STALE) return false;
      localStorage.setItem(k, JSON.stringify({ tab: TAB, at: now }));
    } catch (e) { /* stockage fermé : personne d'autre ne voit la file */ }
    return true;
  }
  function unlock() {
    try {
      var k = key() + '.lock', l = JSON.parse(localStorage.getItem(k) || 'null');
      if (l && l.tab === TAB) localStorage.removeItem(k);
    } catch (e) { /* rien à rendre */ }
  }

  // --- La file -------------------------------------------------------------------------
  function first(q, state) { return q.items.filter(function (it) { return it.state === state; })[0] || null; }
  function byDl(q, dl) { return q.items.filter(function (it) { return it.dl === dl; })[0] || null; }

  function launch(q, it) {
    it.dl = rand(); it.state = 'sent'; it.at = Date.now(); it.seen = false; it.bytes = 0;
    q.paused = false;
    save(q);
    var link = document.createElement('a');
    link.href = '/media/' + it.id + '/file?download=1&dl=' + it.dl;
    link.setAttribute('download', '');
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }
  // Le fichier suivant, ou la fin : le bilan d'une file, rien pour un fichier seul (sauf s'il a échoué).
  function advance(q) {
    var next = first(q, 'wait');
    if (next) launch(q, next);
    else if (q.show) { q.end = true; save(q); }
    else {
      if (first(q, 'failed')) H.toast(H.t('Le téléchargement de « {title} » s’est interrompu.', { title: q.items[0].title }), 6000);
      save(null);
      unlock();
    }
    render(load());
  }
  // Ce que le serveur dit de la tentative en cours. La file a pu changer entre la question et la réponse (arrêtée, menée par un autre onglet) : on relit avant d'écrire.
  // En pause, la question continue : si la personne autorise le téléchargement après coup, le serveur le voit et la pause se lève d'elle-même (« Reprendre » relancerait le même fichier).
  function settle(dl, answer) {
    var q = load(), it = q && byDl(q, dl);
    if (!it || it.state !== 'sent') { render(q); return; }
    var state = (answer && answer.state) || 'unknown';
    if (state !== 'unknown') q.paused = false;
    if (state === 'done' || state === 'stopped' || (state === 'unknown' && it.seen)) {        // connue puis oubliée : le serveur a redémarré, l'envoi est perdu
      it.state = state === 'done' ? 'done' : 'failed';
      advance(q);
      return;
    }
    if (state === 'running') { it.seen = true; it.bytes = answer.bytes; it.size = answer.size; save(q); }
    else if (q.paused) render(q);                                                               // toujours rien : on attend « Reprendre », sans relancer seul
    else if (it.at < BOOT) launch(q, it);                                                       // lancée avant le rechargement, jamais vue : le lien s'est perdu avec la page
    else if (Date.now() - it.at > NEVER_TAKEN) { q.paused = true; save(q); }
    render(load());
  }
  function step() {
    var q = load();
    if (!q) { stopTimer(); render(null); return; }
    if (!lead() || q.end) { render(q); return; }
    var cur = first(q, 'sent');
    if (!cur) { if (q.paused) render(q); else advance(q); return; }
    H.api('/api/downloads?dl=' + cur.dl).then(function (r) { settle(cur.dl, r.downloads[cur.dl]); }, function () { render(load()); });        // le serveur ne répond pas : au prochain pas
  }
  function schedule() { if (!timer) timer = setInterval(step, POLL); }
  function stopTimer() { clearInterval(timer); timer = null; }

  // `files` : [{id, title}]. Ajoutés à la fin de la file, sauf ceux qui y attendent déjà ; le premier part tout de suite (le clic qui le demande vaut autorisation pour le navigateur).
  // `show` : la barre se montre même pour un seul fichier (on réessaie ceux qui ont échoué : le bilan doit suivre).
  D.add = function (files, show) {
    var q = load();
    if (q && q.end) q = null;                     // le bilan d'une file finie laisse la place
    q = q || { items: [], show: false };
    var waiting = {};
    q.items.forEach(function (it) { if (it.state === 'wait') waiting[it.id] = true; });
    files.forEach(function (f) {
      if (waiting[f.id]) return;
      waiting[f.id] = true;
      q.items.push({ id: f.id, title: f.title || '', state: 'wait' });
    });
    q.show = !!(q.show || show || q.items.length > 1);
    save(q);
    if (lead() && !q.paused && !first(q, 'sent')) advance(q);
    else render(q);
    schedule();
  };
  D.stop = function () {
    save(null);
    unlock();
    stopTimer();
    render(null);
    H.toast(H.t('File des téléchargements arrêtée'), 4000);
  };
  D.resume = function () {
    var q = load(), it = q && (first(q, 'sent') || first(q, 'wait'));
    if (!it) return;
    lead();
    launch(q, it);
    render(load());
    schedule();
  };
  D.retry = function () {
    var q = load(), failed = q ? q.items.filter(function (it) { return it.state === 'failed'; }) : [];
    save(null);
    D.add(failed.map(function (it) { return { id: it.id, title: it.title }; }), true);
  };
  D.close = function () { save(null); unlock(); stopTimer(); render(null); };

  // --- La barre --------------------------------------------------------------------------
  // Construite une fois ; ses boutons ne changent qu'avec son mode (en cours, en pause, bilan) : un bouton qui a le focus le garde d'un pas à l'autre.
  function barEl() {
    if (bar) return bar;
    ui.text = h('div', { class: 'ti', 'aria-live': 'polite' });
    ui.fill = h('i');
    ui.meter = h('div', { class: 'meter', 'aria-hidden': 'true' }, ui.fill);
    ui.btns = h('div', { class: 'ctl' });
    bar = h('div', { id: 'dlbar', role: 'region', 'aria-label': H.t('Téléchargements'), hidden: true }, h('div', { class: 'txt' }, ui.text, ui.meter), ui.btns);
    document.body.appendChild(bar);
    return bar;
  }
  function buttons(mode, q) {
    if (ui.mode === mode) return;
    ui.mode = mode;
    H.clear(ui.btns);
    var stop = H.button(H.t('Arrêter la file'), '', { 'data-key': 'dl-stop', onclick: D.stop });
    if (mode === 'paused') ui.btns.appendChild(H.button(H.t('Reprendre'), 'download', { 'data-key': 'dl-resume', onclick: D.resume }));
    if (mode === 'failed') ui.btns.appendChild(H.button(H.t('Réessayer ceux-là'), '', { 'data-key': 'dl-retry', onclick: D.retry }));
    ui.btns.appendChild(mode === 'done' || mode === 'failed' ? H.button(H.t('Fermer'), '', { 'data-key': 'dl-close', onclick: D.close }) : stop);
  }
  function summary(q) {
    var failed = q.items.filter(function (it) { return it.state === 'failed'; });
    if (!failed.length) return { mode: 'done', text: H.t('Téléchargements finis : {files}', { files: H.plural(q.items.length, 'fichier') }) };
    var names = failed.map(function (it) { return it.title; }).join(', ');
    return { mode: 'failed', text: failed.length > 1 ? H.t('Ces téléchargements ont échoué : {names}', { names: names }) : H.t('Ce téléchargement a échoué : {names}', { names: names }) };
  }
  function progress(q) {
    var it = first(q, 'sent') || first(q, 'wait') || q.items[q.items.length - 1];
    var shown = { mode: 'running', percent: it.size ? Math.min(100, Math.round(it.bytes / it.size * 100)) : 0,
      text: H.t('Téléchargement {i} / {n} : {title}', { i: q.items.indexOf(it) + 1, n: q.items.length, title: it.title }) };
    if (q.paused) {
      shown.mode = 'paused';
      shown.text = H.t('Le navigateur n’a pas lancé « {title} ». S’il le demande, autorise les téléchargements multiples pour ce site, puis reprends.', { title: it.title });
    }
    return shown;
  }
  function render(q) {
    var el = barEl(), visible = !!(q && (q.show || q.paused));
    el.hidden = !visible;
    document.body.classList.toggle('has-dl', visible);
    if (!visible) { ui.mode = null; return; }
    var shown = q.end ? summary(q) : progress(q);
    ui.text.textContent = shown.text;
    ui.meter.hidden = shown.mode !== 'running';
    ui.fill.style.width = (shown.percent || 0) + '%';
    buttons(shown.mode, q);
  }

  // Au démarrage, une fois le compte connu : la file d'avant le rechargement reprend (l'onglet qui la menait, ou celui-ci si le verrou est abandonné).
  D.init = function () {
    var q = load();
    render(q);
    if (!q) return;
    schedule();
    step();
  };
  // Un autre onglet a changé la file : elle se montre ici aussi, et cet onglet la reprendra si l'autre se ferme.
  window.addEventListener('storage', function (e) {
    if (e.key !== key()) return;
    var q = load();
    render(q);
    if (q) schedule();
  });
  window.addEventListener('pagehide', unlock);
})();
