/* Le lecteur vidéo : choisit avec le serveur comment lire (direct, audio converti, tout converti), puis repart où l'on s'était arrêté.
   Les sous-titres sont affichés ici (WebVTT lu et dessiné par la page) : ils restent justes même quand le film est relancé depuis un autre point. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;

  var QUALITIES = { auto: { k: 0, h: 0, label: 'Qualité d’origine' }, '1080': { k: 8000, h: 1080, label: '1080p · 8 Mb/s' }, '720': { k: 4000, h: 720, label: '720p · 4 Mb/s' }, '480': { k: 1500, h: 480, label: '480p · 1,5 Mb/s' } };
  var MODES = { direct: 'Lecture directe', remux: 'Son converti', transcode: 'Image convertie' };

  // Ce que ce navigateur sait lire, d'après lui-même.
  function capabilities(level) {
    var v = document.createElement('video');
    function can(t) { var r = v.canPlayType(t); return r === 'probably' || r === 'maybe'; }
    var surround = !!H.pref('surround');
    var video = [], audio = [], containers = ['mp4'];
    if (level < 2) {
      if (can('video/mp4; codecs="avc1.640029"')) video.push('h264');
      if (can('video/mp4; codecs="hvc1.1.6.L120.90"')) video.push('hevc');
      if (can('video/mp4; codecs="hvc1.2.4.L120.90"')) video.push('hevc10');
      if (can('video/webm; codecs="vp9"')) video.push('vp9');
      if (can('video/webm; codecs="vp09.02.10.10"')) video.push('vp910');
      if (can('video/mp4; codecs="av01.0.08M.08"')) video.push('av1');
    }
    if (can('audio/mp4; codecs="mp4a.40.2"')) audio.push('aac');
    if (can('audio/mpeg')) audio.push('mp3');
    if (can('audio/webm; codecs="opus"')) audio.push('opus');
    if (can('audio/webm; codecs="vorbis"')) audio.push('vorbis');
    if (can('audio/flac')) audio.push('flac');
    if (surround && can('audio/mp4; codecs="ac-3"')) audio.push('ac3');
    if (surround && can('audio/mp4; codecs="ec-3"')) audio.push('eac3');
    if (level < 1) {
      if (can('video/webm')) containers.push('webm');
      if (can('video/x-matroska')) containers.push('mkv');
    }
    var hdr = false;
    try { hdr = window.matchMedia('(dynamic-range: high)').matches; } catch (e) { hdr = false; }
    return { video: video, audio: audio, containers: containers, channels: surround ? 6 : 2, hdr: hdr };
  }

  function parseVtt(text) {
    var cues = [];
    String(text).replace(/\r/g, '').split(/\n\n+/).forEach(function (block) {
      var lines = block.split('\n'), i = 0;
      while (i < lines.length && lines[i].indexOf('-->') < 0) i++;
      if (i >= lines.length) return;
      var m = /(?:(\d+):)?(\d+):(\d+)[.,](\d+)\s*-->\s*(?:(\d+):)?(\d+):(\d+)[.,](\d+)/.exec(lines[i]);
      if (!m) return;
      var s = (+m[1] || 0) * 3600 + (+m[2]) * 60 + (+m[3]) + (+('0.' + m[4])), e = (+m[5] || 0) * 3600 + (+m[6]) * 60 + (+m[7]) + (+('0.' + m[8]));
      var t = lines.slice(i + 1).join('\n').replace(/\{[^}]*\}/g, '').trim();
      if (t) cues.push({ s: s, e: e, t: t });
    });
    cues.sort(function (a, b) { return a.s - b.s; });
    return cues;
  }
  // Texte d'un sous-titre -> nœuds : <i>, <b>, <u> gardés, tout le reste est du texte brut.
  function cueNodes(text) {
    var frag = document.createDocumentFragment(), node = frag, stack = [];
    text.replace(/<(?!\/?[ibu]>)[^>]*>/gi, '').split(/(<\/?[ibu]>)/i).forEach(function (part) {
      var tag = /^<(\/?)([ibu])>$/i.exec(part);
      if (tag) {
        if (tag[1]) { if (stack.length) { stack.pop(); node = stack.length ? stack[stack.length - 1] : frag; } }
        else { var n = document.createElement(tag[2].toLowerCase()); node.appendChild(n); stack.push(n); node = n; }
      } else {
        part.split('\n').forEach(function (line, i) { if (i) node.appendChild(document.createElement('br')); node.appendChild(document.createTextNode(line)); });
      }
    });
    return frag;
  }

  H.route(/^\/play\/(\w+)$/, function (m, query) {
    var id = m[1];
    H.audio.pause();
    var video = h('video', { playsinline: '', preload: 'auto' });
    var subBox = h('div', { class: 'cue' });
    var spin = h('div', { class: 'spin', hidden: true }, h('i'));
    var osd = h('div', { class: 'osd', hidden: true });
    var title = h('div', { class: 'ti' }), sub = h('div', { class: 'su' }), pill = h('span', { class: 'pill' });
    var fill = h('i'), thumb = h('b'), tCur = h('span'), tEnd = h('span');
    var seekbar = h('div', { class: 'seekbar f', role: 'slider', tabindex: '0', 'data-autofocus': '1', 'aria-label': 'Position dans le film' }, h('div', { class: 'track' }, fill, thumb));
    var playBtn = H.button('', 'pause', { 'aria-label': 'Lecture / pause', onclick: toggle });
    var bAudio = H.button('Audio', 'audio', { onclick: pickAudio }), bSubs = H.button('Sous-titres', 'subs', { onclick: pickSubs }), bQual = H.button('Qualité', 'quality', { onclick: pickQuality });
    var bNext = H.button('Suivant', 'next', { onclick: goNext, hidden: true });
    var bFull = H.button('', 'full', { 'aria-label': 'Plein écran', onclick: fullscreen });
    var bBack = H.button('', 'back', { 'aria-label': 'Retour', onclick: function () { H.back('#/'); } });
    var ctl = h('div', { class: 'ctl' },
      h('div', { class: 'top' }, bBack, h('div', { class: 'txt' }, title, sub), pill),
      h('div', { class: 'bottom' }, h('div', { class: 'times' }, tCur, tEnd), seekbar,
        h('div', { class: 'btns' }, playBtn, H.button('10 s', 'rew', { onclick: function () { nudge(-1, 10); } }), H.button('10 s', 'fwd', { onclick: function () { nudge(1, 10); } }), h('span', { class: 'sp' }), bAudio, bSubs, bQual, bNext, bFull)));
    var err = h('div', { class: 'err', hidden: true });
    var el = h('section', { class: 'player idle' }, video, subBox, spin, osd, ctl, err);

    var plan = null, base = 0, level = 0, firstPlan = true, closing = false, pending = null, pendingTimer = null, hideTimer = null, tickTimer = null, reportTimer = null, lastReport = 0;
    var steps = 0, stepsTimer = null, audioIdx = null, subs = { cues: [], sel: '' }, subText = '';

    function q() { return QUALITIES[H.pref('quality')] || QUALITIES.auto; }
    function cur() { var t = video.currentTime || 0; return plan && !plan.seekable ? base + t : t; }
    function dur() { return (plan && plan.duration) || video.duration || 0; }

    // --- Ouverture -----------------------------------------------------------
    function open(at) {
      spin.hidden = false; err.hidden = true;
      var body = { caps: capabilities(level), audio: audioIdx, max_kbps: q().k, max_height: q().h };
      return H.api('/api/play/' + id, body).then(function (p) {
        if (closing) return;
        plan = p;
        if (audioIdx === null) { var chosen = p.audio.filter(function (a) { return a.selected; })[0]; audioIdx = chosen ? chosen.index : null; }
        title.textContent = p.show || p.title || '';
        sub.textContent = p.show ? [(p.season !== null && p.season !== undefined ? 'S' + p.season : ''), (p.number !== null && p.number !== undefined ? 'E' + p.number : ''), p.title].filter(Boolean).join(' · ') : '';
        pill.textContent = MODES[p.mode]; pill.title = p.why.join('\n'); pill.setAttribute('data-mode', p.mode);
        bNext.hidden = !p.next;
        bAudio.hidden = p.audio.length < 2;
        bSubs.hidden = !p.subs.length;
        if (p.warning) H.toast(p.warning, 7000);
        var start = at !== undefined ? at : (query.t !== undefined ? parseFloat(query.t) || 0 : p.resume);
        if (firstPlan) { firstPlan = false; chooseDefaultSub(); if (start >= 20 && query.t === undefined) H.toast('Reprise à ' + H.time(start)); }
        attach(start);
      }).catch(function (e) { spin.hidden = true; showError(e.message); });
    }
    function attach(at) {
      at = Math.max(0, Math.min(at || 0, Math.max(0, dur() - 2)));
      base = plan.seekable ? 0 : at;
      video.src = plan.seekable ? plan.url : plan.url + '&start=' + at.toFixed(1);
      if (plan.seekable && at > 0) {
        var once = function () { video.removeEventListener('loadedmetadata', once); try { video.currentTime = at; } catch (e) { /* tant pis */ } };
        video.addEventListener('loadedmetadata', once);
      }
      var p = video.play();
      if (p && p.catch) p.catch(function () { show(true); H.focus.set(playBtn, true); });
      paintTime();
    }
    function showError(message) {
      H.clear(err).appendChild(h('div', null, h('h2', null, 'Lecture impossible'), h('p', null, message),
        h('div', { class: 'actions' }, H.button('Réessayer', 'refresh', { 'data-autofocus': '1', onclick: function () { level = 0; open(cur()); } }), H.button('Retour', 'back', { onclick: function () { H.back('#/'); } }))));
      err.hidden = false; show(true);
      H.focus.first(err);
    }
    // Si le lecteur refuse ce que le serveur lui donne, on descend d'un cran : conteneur converti, puis image convertie.
    video.addEventListener('error', function () {
      if (closing || !plan) return;
      var at = cur();
      if (level < 2 && plan.mode !== 'transcode') {
        level++;
        H.toast(level === 1 ? 'Lecture directe impossible : conversion du conteneur.' : 'Ce lecteur ne lit pas cette image : conversion complète.', 4500);
        open(at);
        return;
      }
      spin.hidden = true;
      H.api('/api/state').then(function (s) {
        showError(s.streams >= s.max_streams ? 'Le serveur convertit déjà ' + s.streams + ' flux (maximum ' + s.max_streams + '). Réessaie dans un instant.' : (plan.why.join(' · ') || 'Le fichier n’a pas pu être lu.'));
      }, function () { showError('Le serveur ne répond plus.'); });
    });
    video.addEventListener('waiting', function () { spin.hidden = false; });
    video.addEventListener('playing', function () { spin.hidden = true; paintPlay(); schedule(); });
    video.addEventListener('canplay', function () { spin.hidden = true; });
    video.addEventListener('pause', function () { paintPlay(); show(true); report(); });
    video.addEventListener('timeupdate', paintTime);
    video.addEventListener('ended', function () {
      report(true);
      if (plan && plan.next) { H.go('#/play/' + plan.next.id, true); } else H.back(plan ? '#/i/' + plan.item : '#/');
    });

    // --- Position ------------------------------------------------------------------
    function paintTime() {
      var t = pending !== null ? pending : cur(), d = dur();
      fill.style.width = d ? Math.min(100, t / d * 100) + '%' : '0%';
      thumb.style.left = d ? Math.min(100, t / d * 100) + '%' : '0%';
      tCur.textContent = H.time(t); tEnd.textContent = d ? '-' + H.time(Math.max(0, d - t)) : '';
      seekbar.setAttribute('aria-valuenow', String(Math.round(t)));
    }
    function paintPlay() {
      var icon = playBtn.querySelector('.ic');
      if (icon) playBtn.replaceChild(H.icon(video.paused ? 'play' : 'pause'), icon);
    }
    function commit(t) {
      pending = null;
      t = Math.max(0, Math.min(t, Math.max(0, dur() - 1)));
      spin.hidden = false;
      if (plan.seekable) { try { video.currentTime = t; } catch (e) { /* tant pis */ } paintTime(); }
      else attach(t);
    }
    function nudge(sign, amount) {
      var target = (pending !== null ? pending : cur()) + sign * amount;
      pending = Math.max(0, Math.min(target, Math.max(0, dur() - 1)));
      flash((sign < 0 ? '−' : '+') + Math.round(Math.abs(pending - cur())) + ' s · ' + H.time(pending));
      paintTime();
      clearTimeout(pendingTimer);
      pendingTimer = setTimeout(function () { if (pending !== null) commit(pending); }, plan && plan.seekable ? 250 : 700);
      show();
    }
    function seekBy(sign) {
      steps++;
      clearTimeout(stepsTimer); stepsTimer = setTimeout(function () { steps = 0; }, 900);
      nudge(sign, [10, 10, 10, 30, 30, 60, 60, 120][Math.min(steps - 1, 7)]);
    }
    seekbar.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { seekBy(e.key === 'ArrowLeft' ? -1 : 1); e.preventDefault(); }
      else if (e.key === 'Enter') { toggle(); e.preventDefault(); }
    });
    seekbar.addEventListener('click', function (e) {
      var r = seekbar.getBoundingClientRect();
      commit(dur() * Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)));
    });
    function toggle() { if (video.paused) { var p = video.play(); if (p && p.catch) p.catch(function () {}); } else video.pause(); }
    function flash(text) {
      osd.textContent = text; osd.hidden = false;
      clearTimeout(flash.t); flash.t = setTimeout(function () { osd.hidden = true; }, 1100);
    }
    function report(done) {
      if (!plan) return;
      var t = cur();
      if (!done && t < 20) return;
      lastReport = Date.now();
      H.api('/api/progress', { entry: id, position: t, duration: dur(), done: !!done }, { keepalive: true }).catch(function () {});
    }

    // --- Commandes visibles -----------------------------------------------------------
    function show(keep) {
      ctl.hidden = false; el.classList.remove('idle');
      clearTimeout(hideTimer);
      if (!keep) schedule();
    }
    function schedule() {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(function () {
        if (video.paused || H.overlay.isOpen() || !err.hidden) return;
        ctl.hidden = true; el.classList.add('idle');
        if (document.activeElement && ctl.contains(document.activeElement)) document.activeElement.blur();
      }, 4200);
    }
    function fullscreen() {
      var d = document;
      if (d.fullscreenElement) { d.exitFullscreen(); return; }
      var f = el.requestFullscreen || el.webkitRequestFullscreen;
      if (f) { try { var p = f.call(el); if (p && p.catch) p.catch(function () {}); } catch (e) { /* refusé */ } }
    }

    // --- Pistes ---------------------------------------------------------------------
    function pickAudio() {
      H.menu('Audio', plan.audio.map(function (a) { return { label: a.label + (a.commentary ? ' (commentaires)' : ''), value: a.index }; }), audioIdx, function (v) {
        if (v === audioIdx) return;
        audioIdx = v; open(cur());
      });
    }
    function pickSubs() {
      var opts = [{ label: 'Désactivés', value: '' }].concat(plan.subs.map(function (s) {
        return { label: s.label, detail: s.text ? s.where : 'image, non prise en charge', value: s.id, disabled: !s.text };
      }));
      H.menu('Sous-titres', opts, subs.sel, function (v) {
        setSub(v);
        var track = plan.subs.filter(function (s) { return s.id === v; })[0];
        H.pref('subLang', v ? (track && track.lang) || 'any' : 'off');
      });
    }
    function pickQuality() {
      var now = H.pref('quality') || 'auto';
      var opts = Object.keys(QUALITIES).map(function (k) { return { label: QUALITIES[k].label, detail: k === now ? MODES[plan.mode] : '', value: k }; });
      H.menu('Qualité', opts, now, function (v) { H.pref('quality', v); level = 0; open(cur()); });
    }
    function chooseDefaultSub() {
      var want = H.pref('subLang'), list = plan.subs.filter(function (s) { return s.text; }), pick = null;
      if (want === 'off') pick = null;
      else if (want && want !== 'any') pick = list.filter(function (s) { return s.lang === want && !s.forced; })[0] || list.filter(function (s) { return s.lang === want; })[0];
      else if (!want) pick = list.filter(function (s) { return s.forced && /^(fr|fre|fra)$/.test(s.lang); })[0] || list.filter(function (s) { return s.default; })[0];
      setSub(pick ? pick.id : '');
    }
    function setSub(sid) {
      subs.sel = sid; subs.cues = []; subText = ''; H.clear(subBox);
      var track = plan.subs.filter(function (s) { return s.id === sid; })[0];
      if (!track) return;
      fetch(track.url, { credentials: 'same-origin' }).then(function (r) { if (!r.ok) throw new Error('sous-titres indisponibles'); return r.text(); }).then(function (text) {
        if (subs.sel === sid) subs.cues = parseVtt(text);
      }).catch(function (e) { H.toast(e.message); });
    }
    tickTimer = setInterval(function () {
      if (!subs.cues.length) return;
      var t = cur(), lines = [];
      for (var i = 0; i < subs.cues.length; i++) { var c = subs.cues[i]; if (c.s > t) break; if (t < c.e) lines.push(c.t); }
      var text = lines.join('\n');
      if (text !== subText) { subText = text; H.clear(subBox); if (text) subBox.appendChild(cueNodes(text)); }
    }, 200);
    reportTimer = setInterval(function () { if (!video.paused && Date.now() - lastReport > 9000) report(); }, 10000);
    function goNext() { if (plan && plan.next) { report(true); H.go('#/play/' + plan.next.id, true); } }

    // --- Clavier et souris ----------------------------------------------------------------
    function eat(e) { e.preventDefault(); e.stopPropagation(); }
    function onKey(e) {
      if (!el.isConnected || el.style.display === 'none' || H.overlay.isOpen() || e.ctrlKey || e.metaKey || e.altKey) return;
      var k = e.key;
      if (!err.hidden) return;
      if (k === 'MediaPlayPause') { toggle(); eat(e); return; }
      if (k === 'MediaTrackNext') { goNext(); eat(e); return; }
      if (k === 'f' || k === 'F') { fullscreen(); eat(e); return; }
      var inControls = !ctl.hidden && ctl.contains(document.activeElement);
      if (!inControls) {                     // personne n'est sur un bouton : les flèches pilotent directement la lecture
        if (k === 'ArrowLeft' || k === 'ArrowRight') { seekBy(k === 'ArrowLeft' ? -1 : 1); eat(e); }
        else if (k === 'ArrowUp' || k === 'ArrowDown') { show(); H.focus.set(k === 'ArrowUp' ? seekbar : playBtn, true); eat(e); }
        else if (k === 'Enter' || k === ' ') { toggle(); show(); eat(e); }
        else if (k !== 'Escape' && k !== 'Backspace' && k !== 'BrowserBack' && k !== 'GoBack') show();
      } else {
        show();
        if (k === ' ' && !(e.target && e.target.tagName === 'BUTTON')) { toggle(); eat(e); }
      }
    }
    window.addEventListener('keydown', onKey, true);
    el.addEventListener('mousemove', function () { show(); });
    video.addEventListener('click', function () { toggle(); show(); });
    video.addEventListener('dblclick', fullscreen);
    var onFull = function () { var on = !!document.fullscreenElement; bFull.classList.toggle('on', on); };
    document.addEventListener('fullscreenchange', onFull);
    var onHide = function () { if (document.hidden) report(); };
    document.addEventListener('visibilitychange', onHide);
    window.addEventListener('pagehide', onHide);

    open();
    return {
      el: el, full: true, nav: '#/play',
      lib: '',
      shown: function () { show(); },
      onBack: function () {
        if (H.routed <= 1 && plan) { location.hash = '#/i/' + plan.item; return true; }
        return false;
      },
      destroy: function () {
        closing = true;
        report(video.ended);
        [pendingTimer, hideTimer, tickTimer, reportTimer, stepsTimer].forEach(clearTimeout);
        clearInterval(tickTimer); clearInterval(reportTimer);
        window.removeEventListener('keydown', onKey, true);
        document.removeEventListener('fullscreenchange', onFull);
        document.removeEventListener('visibilitychange', onHide);
        window.removeEventListener('pagehide', onHide);
        video.pause(); video.removeAttribute('src'); video.load();
        if (document.fullscreenElement) { try { document.exitFullscreen(); } catch (e) { /* déjà sorti */ } }
      }
    };
  });
})();
