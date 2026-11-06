/* Le lecteur de musique : une file d'attente et une barre en bas de page, qui continue pendant qu'on feuillette.
   La file se retrouve au rechargement de la page (pas la position dans la piste). */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;
  var el, bar, queue = [], idx = -1, ui = {}, reported = null;

  function build() {
    el = document.getElementById('audio');
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
    ui.stop = H.button('', 'close', { 'aria-label': H.t('Fermer le lecteur'), onclick: stop });
    ui.fill = h('i');
    ui.seek = h('div', { class: 'seek f', role: 'slider', tabindex: '0', 'aria-label': H.t('Position'), onclick: function (e) {
      var r = ui.seek.getBoundingClientRect();
      if (el.duration) el.currentTime = el.duration * Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    }, onkeydown: function (e) {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { el.currentTime = Math.max(0, el.currentTime + (e.key === 'ArrowLeft' ? -10 : 10)); e.preventDefault(); }
    } }, ui.fill);
    ui.time = h('div', { class: 'tm' });
    ui.mute = H.button('', 'audio', { 'aria-label': H.t('Couper le son'), onclick: function () { el.muted = !el.muted; if (!el.muted && !el.volume) el.volume = 0.5; } });
    ui.vol = h('input', { type: 'range', class: 'vol f', min: '0', max: '1', step: '0.05', 'aria-label': H.t('Volume'), oninput: function () {
      el.volume = Number(ui.vol.value);
      el.muted = el.volume === 0;
    } });
    var saved = H.pref('volume');
    if (saved && typeof saved.v === 'number') { el.volume = Math.min(1, Math.max(0, saved.v)); el.muted = !!saved.m; }
    el.addEventListener('volumechange', paintVolume);
    bar.appendChild(ui.cover);
    bar.appendChild(h('div', { class: 'txt' }, ui.title, ui.sub));
    bar.appendChild(h('div', { class: 'ctl' }, ui.prev, ui.play, ui.next));
    bar.appendChild(h('div', { class: 'pos' }, ui.seek, ui.time));
    bar.appendChild(h('div', { class: 'vl' }, ui.mute, ui.vol));
    bar.appendChild(ui.queue);
    bar.appendChild(ui.stop);
    paintVolume();
    el.addEventListener('timeupdate', paintTime);
    el.addEventListener('play', paintPlay);
    el.addEventListener('pause', paintPlay);
    el.addEventListener('ended', function () { finish(); step(1, true); });
    el.addEventListener('error', function () {
      H.toast(H.t('Cette piste ne se lit pas : {title}', { title: queue[idx] ? queue[idx].title : '' }), 4500);
      step(1, true);
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
  function paintVolume() {
    var silent = el.muted || el.volume === 0;
    var icon = ui.mute.querySelector('.ic');
    var fresh = H.icon(silent ? 'mute' : 'audio');
    if (icon) ui.mute.replaceChild(fresh, icon);
    ui.mute.setAttribute('aria-label', silent ? H.t('Remettre le son') : H.t('Couper le son'));
    ui.vol.value = silent ? 0 : el.volume;
    H.pref('volume', { v: el.volume, m: el.muted });
  }
  function paintTime() {
    var d = el.duration || (queue[idx] && queue[idx].duration) || 0;
    ui.fill.style.width = d ? Math.min(100, el.currentTime / d * 100) + '%' : '0%';
    ui.time.textContent = H.time(el.currentTime) + ' / ' + H.time(d);
  }
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
  function load(i) {
    idx = i;
    var t = queue[idx];
    reported = null;
    el.src = '/media/' + t.id + '/file';
    save();
    var p = el.play();
    if (p && p.catch) p.catch(function () { paintPlay(); });
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
