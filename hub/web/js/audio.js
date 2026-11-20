/* Le lecteur de musique : une file d'attente et une barre en bas de page, qui continue pendant qu'on feuillette.
   La file se retrouve au rechargement de la page (pas la position dans la piste).
   Deux éléments <audio>, jamais plus : celui qui joue (`el`, toujours `id="audio"`) et `spare` (`id="audio-next"`), qui charge la piste suivante pendant les dernières
   secondes de la piste en cours, sans la jouer ; à la fin, les deux s'échangent. Les écouteurs sont posés sur les deux mais n'écoutent que celui qui joue. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;
  var el, spare, bar, queue = [], idx = -1, ui = {}, reported = null;
  var PRELOAD_SECONDS = 15;          // la piste suivante se charge quand il reste ce temps à la piste en cours
  var nextId = null;                 // la piste que `spare` charge, ou null

  // Un écouteur qui ne réagit qu'à l'élément qui joue : celui qui précharge ne doit ni peindre la barre, ni finir une piste, ni signaler une erreur.
  function mine(fn) { return function (e) { if (e.target === el) fn(e); }; }
  // Ce que règle le volume (voir H.volume) : il lit l'élément qui joue et écrit dans les deux, si bien que l'échange ne change pas le son.
  function sound() {
    var both = {
      addEventListener: function (type, fn) { [el, spare].forEach(function (a) { a.addEventListener(type, mine(fn)); }); }
    };
    ['volume', 'muted'].forEach(function (key) {
      Object.defineProperty(both, key, { get: function () { return el[key]; }, set: function (v) { el[key] = v; spare[key] = v; } });
    });
    return both;
  }

  function build() {
    el = document.getElementById('audio');
    spare = h('audio', { id: 'audio-next', preload: 'none' });
    el.parentNode.insertBefore(spare, el.nextSibling);
    bar = document.getElementById('abar');
    ui.cover = h('div', { class: 'cov' });
    ui.title = h('div', { class: 'ti' });
    ui.sub = h('div', { class: 'su' });
    ui.prev = H.button('', 'prev', { 'aria-label': H.t('Piste précédente'), onclick: function () { step(-1); } });
    ui.play = H.button('', 'play', { 'aria-label': H.t('Lecture / pause'), onclick: toggle });
    ui.next = H.button('', 'next', { 'aria-label': H.t('Piste suivante'), onclick: function () { step(1); } });
    ui.queue = H.button('', 'queue', { 'aria-label': H.t('File d’attente'), onclick: function () { if (H.music) H.music.openQueue(); } });
    ui.count = h('span', { class: 'qn' });
    ui.queue.appendChild(ui.count);
    ui.speed = H.button(H.speedLabel(1), '', { class: 'spd', 'aria-label': H.t('Vitesse de lecture : {rate}', { rate: H.speedLabel(1) }), onclick: function () {
      H.speedMenu(el.playbackRate, setSpeed);
    } });
    ui.stop = H.button('', 'close', { 'aria-label': H.t('Fermer le lecteur'), onclick: stop });
    ui.fill = h('i');
    ui.seek = h('div', { class: 'seek f', role: 'slider', tabindex: '0', 'aria-label': H.t('Position'), onclick: function (e) {
      var r = ui.seek.getBoundingClientRect();
      if (el.duration) el.currentTime = el.duration * Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    }, onkeydown: function (e) {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { el.currentTime = Math.max(0, el.currentTime + (e.key === 'ArrowLeft' ? -10 : 10)); e.preventDefault(); }
    } }, ui.fill);
    ui.time = h('div', { class: 'tm' });
    var volume = H.volume(sound(), 'volume');
    bar.appendChild(ui.cover);
    bar.appendChild(h('div', { class: 'txt' }, ui.title, ui.sub));
    bar.appendChild(h('div', { class: 'ctl' }, ui.prev, ui.play, ui.next));
    bar.appendChild(h('div', { class: 'pos' }, ui.seek, ui.time));
    bar.appendChild(volume.el);
    bar.appendChild(ui.speed);
    bar.appendChild(ui.queue);
    bar.appendChild(ui.stop);
    [el, spare].forEach(function (a) {
      a.addEventListener('timeupdate', mine(function () { paintTime(); ahead(); }));
      a.addEventListener('play', mine(paintPlay));
      a.addEventListener('pause', mine(paintPlay));
      a.addEventListener('ended', mine(function () { finish(); step(1, true); }));
      a.addEventListener('error', mine(function () {
        H.toast(H.t('Cette piste ne se lit pas : {title}', { title: queue[idx] ? queue[idx].title : '' }), 4500);
        step(1, true);
      }));
    });
    restore();
    if ('mediaSession' in navigator) {
      var set = function (name, fn) { try { navigator.mediaSession.setActionHandler(name, fn); } catch (e) { /* non géré */ } };
      set('play', function () { el.play(); }); set('pause', function () { el.pause(); });
      set('previoustrack', function () { step(-1); }); set('nexttrack', function () { step(1); });
      set('seekto', function (d) { if (d.seekTime !== undefined) el.currentTime = d.seekTime; });
    }
  }

  function paintPlay() {
    var icon = ui.play.querySelector('.ic');
    var fresh = H.icon(el.paused ? 'play' : 'pause');
    if (icon) ui.play.replaceChild(fresh, icon);
  }
  function paintTime() {
    var d = el.duration || (queue[idx] && queue[idx].duration) || 0;
    ui.fill.style.width = d ? Math.min(100, el.currentTime / d * 100) + '%' : '0%';
    ui.time.textContent = H.time(el.currentTime) + ' / ' + H.time(d);
  }
  // La vitesse vaut pour la barre tant qu'elle reste ouverte (d'une piste à l'autre : `defaultPlaybackRate`) ; la fermer la remet à 1×. Rien n'est retenu.
  function setSpeed(rate) { H.setSpeed(el, rate, ui.speed); spare.defaultPlaybackRate = spare.playbackRate = el.playbackRate; }
  function show() {
    var t = queue[idx];
    if (!t) return;
    bar.hidden = false;
    document.body.classList.add('has-audio');
    ui.count.textContent = queue.length > 1 ? (idx + 1) + '/' + queue.length : '';
    ui.title.textContent = t.title;
    ui.sub.textContent = [t.artist, t.album].filter(Boolean).join(' · ');
    H.clear(ui.cover).appendChild(H.picture('/img/item/' + t.item + '?w=120', t.album || t.title, 'sq'));
    paintTime(); paintPlay();
    if ('mediaSession' in navigator && window.MediaMetadata) {
      navigator.mediaSession.metadata = new window.MediaMetadata({ title: t.title, artist: t.artist || '', album: t.album || '', artwork: [{ src: '/img/item/' + t.item + '?w=300', sizes: '300x300', type: 'image/jpeg' }] });
    }
    mark();
  }
  function mark() {
    var current = queue[idx] ? queue[idx].id : null;
    Array.prototype.forEach.call(document.querySelectorAll('.trk'), function (row) { row.classList.toggle('now', (row.getAttribute('data-track') || row.getAttribute('data-key')) === current); });
  }
  function save() {
    if (nextId && !(queue[idx + 1] && queue[idx + 1].id === nextId)) cancel();        // la file a changé (retrait, ajout juste après, déplacement, arrêt) : la piste préchargée n'est plus la suivante
    H.pref('queue', queue.length ? { q: queue.slice(0, 2000), i: idx } : null);
  }
  function restore() {
    var saved = H.pref('queue');
    if (!saved || !Array.isArray(saved.q) || !saved.q.length) return;
    queue = saved.q.filter(function (t) { return t && t.id; });
    idx = Math.min(Math.max(0, saved.i || 0), queue.length - 1);
    if (queue.length) show();
    else { queue = []; idx = -1; }
  }
  function url(t) { return '/media/' + t.id + '/file'; }
  // Les dernières secondes : `spare` charge la piste suivante (une fois), sans la jouer.
  function ahead() {
    var next = queue[idx + 1];
    if (!next || nextId === next.id || !el.duration || el.duration - el.currentTime > PRELOAD_SECONDS) return;
    nextId = next.id;
    spare.volume = el.volume; spare.muted = el.muted; spare.defaultPlaybackRate = el.playbackRate;
    spare.preload = 'auto';
    spare.src = url(next);
  }
  // Plus de préchargement : `spare` lâche son fichier (la requête s'arrête), comme `stop` pour l'élément qui joue.
  function cancel() {
    nextId = null;
    spare.preload = 'none';
    if (spare.getAttribute('src')) { spare.removeAttribute('src'); spare.load(); }
  }
  // La piste préchargée devient celle qui joue ; l'ancien élément se vide et servira au prochain préchargement.
  function exchange() {
    var old = el;
    el = spare; spare = old;
    old.id = 'audio-next'; el.id = 'audio';
  }
  function swap() {
    exchange();
    spare.pause();
    cancel();
  }
  // Safari (iPhone, iPad) n'autorise la lecture sans geste que sur un élément déjà joué : s'il refuse l'élément préchargé, la piste repart dans l'ancien, comme avant le préchargement.
  function refused(t, player) {
    return function (e) {
      if (!e || e.name !== 'NotAllowedError' || el !== player || queue[idx] !== t) { paintPlay(); return; }
      exchange();
      cancel();
      el.src = url(t);
      var p = el.play();
      if (p && p.catch) p.catch(function () { paintPlay(); });
    };
  }
  function load(i) {
    idx = i;
    var t = queue[idx], swapped = nextId === t.id && !spare.error;
    reported = null;
    if (swapped) swap();
    else { cancel(); el.src = url(t); }
    save();
    var p = el.play();
    if (p && p.catch) p.catch(swapped ? refused(t, el) : function () { paintPlay(); });
    show();
  }
  function finish() {
    var t = queue[idx];
    if (!t || reported === t.id) return;
    reported = t.id;
    H.api('/api/progress', { entry: t.id, position: el.duration || t.duration || 1, duration: el.duration || t.duration || 1, done: true }, { keepalive: true }).catch(function () {});
  }
  function step(delta, auto) {
    var n = idx + delta;
    if (n < 0) { if (el.getAttribute('src')) el.currentTime = 0; return; }
    if (n >= queue.length) { if (auto) { el.pause(); H.toast(H.t('Fin de la liste')); } return; }
    load(n);
  }
  function toggle() {
    if (!el.getAttribute('src')) { if (idx >= 0) load(idx); return; }          // file retrouvée au rechargement : rien n'est encore chargé
    if (el.paused) el.play(); else el.pause();
  }
  function stop() {
    var back = H.focus && document.activeElement && bar.contains(document.activeElement);
    el.pause(); el.removeAttribute('src'); el.load();
    setSpeed(1);
    queue = []; idx = -1; bar.hidden = true;
    save();
    document.body.classList.remove('has-audio');
    mark();
    if (back) H.focus.first();
  }

  H.audio = {
    init: build,
    play: function (list, index, shuffle) {
      queue = list.slice();
      if (shuffle) {
        for (var i = queue.length - 1; i > 0; i--) { var j = Math.floor(Math.random() * (i + 1)); var x = queue[i]; queue[i] = queue[j]; queue[j] = x; }
        index = 0;
      }
      load(index || 0);
    },
    pause: function () { if (el && !el.paused) el.pause(); },
    // La file d'attente : `where` = 'next' (juste après la piste en cours) ou 'end' (tout à la fin). Si rien ne joue, la lecture démarre.
    enqueue: function (list, where) {
      if (!list.length) return 0;
      if (idx < 0 || !queue.length) { H.audio.play(list, 0); return list.length; }
      var at = where === 'next' ? idx + 1 : queue.length;
      queue.splice.apply(queue, [at, 0].concat(list));
      save(); show();
      return list.length;
    },
    state: function () { return { queue: queue.slice(), idx: idx }; },
    jump: function (i) { if (i >= 0 && i < queue.length) load(i); },
    remove: function (i) {
      if (i < 0 || i >= queue.length) return;
      queue.splice(i, 1);
      if (!queue.length) { stop(); return; }
      if (i < idx) idx--;
      else if (i === idx) { idx = Math.min(idx, queue.length - 1); load(idx); return; }
      save(); show();
    },
    move: function (i, to) {
      if (i < 0 || to < 0 || i >= queue.length || to >= queue.length || i === to) return;
      var t = queue.splice(i, 1)[0];
      queue.splice(to, 0, t);
      if (idx === i) idx = to; else if (i < idx && to >= idx) idx--; else if (i > idx && to <= idx) idx++;
      save(); show();
    },
    clear: function () { stop(); },
    clearNext: function () { if (idx >= 0) { queue.length = idx + 1; save(); show(); } },
    mark: mark,
    focusBar: function () { if (bar.hidden) return false; return H.focus.set(ui.play, true); },
    active: function () { return idx >= 0; }
  };
})();
