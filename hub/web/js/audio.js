/* Le lecteur de musique : une file d'attente et une barre en bas de page, qui continue pendant qu'on feuillette. */
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
    ui.prev = H.button('', 'prev', { 'aria-label': 'Piste précédente', onclick: function () { step(-1); } });
    ui.play = H.button('', 'play', { 'aria-label': 'Lecture / pause', onclick: toggle });
    ui.next = H.button('', 'next', { 'aria-label': 'Piste suivante', onclick: function () { step(1); } });
    ui.stop = H.button('', 'close', { 'aria-label': 'Fermer le lecteur', onclick: stop });
    ui.fill = h('i');
    ui.seek = h('div', { class: 'seek f', role: 'slider', tabindex: '0', 'aria-label': 'Position', onclick: function (e) {
      var r = ui.seek.getBoundingClientRect();
      if (el.duration) el.currentTime = el.duration * Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    }, onkeydown: function (e) {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { el.currentTime = Math.max(0, el.currentTime + (e.key === 'ArrowLeft' ? -10 : 10)); e.preventDefault(); }
    } }, ui.fill);
    ui.time = h('div', { class: 'tm' });
    bar.appendChild(ui.cover);
    bar.appendChild(h('div', { class: 'txt' }, ui.title, ui.sub));
    bar.appendChild(h('div', { class: 'ctl' }, ui.prev, ui.play, ui.next));
    bar.appendChild(h('div', { class: 'pos' }, ui.seek, ui.time));
    bar.appendChild(ui.stop);
    el.addEventListener('timeupdate', paintTime);
    el.addEventListener('play', paintPlay);
    el.addEventListener('pause', paintPlay);
    el.addEventListener('ended', function () { finish(); step(1, true); });
    el.addEventListener('error', function () {
      H.toast('Cette piste ne se lit pas : ' + (queue[idx] ? queue[idx].title : ''), 4500);
      step(1, true);
    });
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
  function show() {
    var t = queue[idx];
    if (!t) return;
    bar.hidden = false;
    document.body.classList.add('has-audio');
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
    Array.prototype.forEach.call(document.querySelectorAll('.trk'), function (row) { row.classList.toggle('now', row.getAttribute('data-key') === current); });
  }
  function load(i) {
    idx = i;
    var t = queue[idx];
    reported = null;
    el.src = '/media/' + t.id + '/file';
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
    if (n < 0) { el.currentTime = 0; return; }
    if (n >= queue.length) { if (auto) { el.pause(); H.toast('Fin de la liste'); } return; }
    load(n);
  }
  function toggle() { if (el.paused) el.play(); else el.pause(); }
  function stop() {
    var back = H.focus && document.activeElement && bar.contains(document.activeElement);
    el.pause(); el.removeAttribute('src'); el.load();
    queue = []; idx = -1; bar.hidden = true;
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
    mark: mark,
    focusBar: function () { if (bar.hidden) return false; return H.focus.set(ui.play, true); },
    active: function () { return idx >= 0; }
  };
})();
