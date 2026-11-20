/* Le lecteur vidéo : choisit avec le serveur comment lire (direct, audio converti, tout converti), puis repart où l'on s'était arrêté.
   Les sous-titres sont affichés ici (WebVTT lu et dessiné par la page, ou PGS décodé par pgs.js sur un canvas) : ils restent justes même quand le film est relancé depuis un autre point. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;

  var QUALITIES = { auto: { k: 0, h: 0, label: H.t('Qualité d’origine') }, '1080': { k: 8000, h: 1080, label: '1080p · 8 Mb/s' }, '720': { k: 4000, h: 720, label: '720p · 4 Mb/s' }, '480': { k: 1500, h: 480, label: H.t('480p · 1,5 Mb/s') } };
  var MODES = { direct: H.t('Lecture directe'), remux: H.t('Son converti'), transcode: H.t('Image convertie') };
  var SKIPS = { intro: H.t('Passer l’introduction'), credits: H.t('Passer le générique') };          // ce que le bouton propose, selon le `skip` du chapitre en cours
  // La vitesse passe à l'épisode que « Suivant » (ou la fin de l'épisode) enchaîne, et à lui seul : toute autre ouverture du lecteur, le film suivant d'une saga compris, repart à 1×.
  var carried = null;

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
    var subBox = h('div', { class: 'cue' }), subPic = h('canvas', { class: 'pgs', hidden: true });
    var spin = h('div', { class: 'spin', hidden: true }, h('i'));
    var osd = h('div', { class: 'osd', hidden: true });
    var title = h('div', { class: 'ti' }), sub = h('div', { class: 'su' }), pill = h('span', { class: 'pill' });
    var fill = h('i'), thumb = h('b'), tCur = h('span'), tEnd = h('span');
    var seekbar = h('div', { class: 'seekbar f', role: 'slider', tabindex: '0', 'data-autofocus': '1', 'aria-label': H.t('Position dans le film') }, h('div', { class: 'track' }, fill, thumb));
    var playBtn = H.button('', 'pause', { 'aria-label': H.t('Lecture / pause'), onclick: toggle });
    // Sur un écran étroit, ces boutons n'affichent que leur icône (style.css) : leur nom passe par aria-label.
    var bAudio = H.button(H.t('Audio'), 'audio', { 'aria-label': H.t('Audio'), onclick: pickAudio }), bSubs = H.button(H.t('Sous-titres'), 'subs', { 'aria-label': H.t('Sous-titres'), onclick: pickSubs }), bQual = H.button(H.t('Qualité'), 'quality', { 'aria-label': H.t('Qualité'), onclick: pickQuality });
    var bChapters = H.button(H.t('Chapitres'), 'chapters', { 'aria-label': H.t('Chapitres'), onclick: pickChapter, hidden: true });
    var bNext = H.button(H.t('Suivant'), 'next', { 'aria-label': H.t('Suivant'), onclick: goNext, hidden: true });
    var bSkip = H.button(SKIPS.intro, 'next', { class: 'skip', onclick: skipChapter, hidden: true });          // hors de `ctl` : il reste là quand les commandes s'effacent
    var bRew = H.button('10 s', 'rew', { 'aria-label': H.t('Reculer de 10 s'), onclick: function () { nudge(-1, 10); } }), bFwd = H.button('10 s', 'fwd', { 'aria-label': H.t('Avancer de 10 s'), onclick: function () { nudge(1, 10); } });
    var bSpeed = H.button(H.t('Vitesse'), 'speed', { class: 'spd', 'aria-label': H.t('Vitesse de lecture : {rate}', { rate: H.speedLabel(1) }), onclick: pickSpeed });
    var bFull = H.button('', 'full', { 'aria-label': H.t('Plein écran'), onclick: fullscreen });
    var bBack = H.button('', 'back', { 'aria-label': H.t('Retour'), onclick: function () { H.back('#/'); } });
    // Le volume sur ordinateur et tablette ; sur téléphone et télé, les boutons de l'appareil ou de la télécommande s'en chargent. Il tient d'une source à l'autre (piste, qualité, saut) : c'est l'élément qui le garde.
    var volume = H.device === 'pc' || H.device === 'tablet' ? H.volume(video, 'videoVolume') : null;
    var ctl = h('div', { class: 'ctl' },
      h('div', { class: 'top' }, bBack, h('div', { class: 'txt' }, title, sub), pill),
      h('div', { class: 'bottom' }, h('div', { class: 'times' }, tCur, tEnd), seekbar,
        h('div', { class: 'btns' }, playBtn, bRew, bFwd, volume && volume.el, h('span', { class: 'sp' }), bSpeed, bChapters, bAudio, bSubs, bQual, bNext, bFull)));
    var err = h('div', { class: 'err', hidden: true });
    var el = h('section', { class: 'player idle' }, video, subPic, subBox, spin, osd, ctl, bSkip, err);

    var plan = null, base = 0, level = 0, firstPlan = true, closing = false, pending = null, pendingTimer = null, hideTimer = null, tickTimer = null, reportTimer = null, lastReport = 0;
    var steps = 0, stepsTimer = null, audioIdx = null, subs = { cues: [], sel: '', pgs: null, shown: null }, subText = '';
    var asleep = false, kept = null, hold = false;          // mis de côté (voir `parked`) : la vidéo n'a plus de source, `kept` garde la position ; `hold` : la prochaine source s'ouvre en pause
    var aimed = null, stale = false;          // `aimed` : la position qu'un départ encore en route doit atteindre ; `stale` : rouvert par Retour, il n'a pas rejoué, sa position n'est plus à écrire
    var speed = 1, offered = false;          // `offered` : le film est fini et le panneau propose le suivant de sa saga
    var skipNow = null, skipShown = '', skipped = {};          // le chapitre que le bouton propose, ce qu'il affiche, et les chapitres déjà passés (par début : un nouveau plan refait les objets)
    if (carried && carried.id === id) setSpeed(carried.rate);
    carried = null;

    function q() { return QUALITIES[H.pref('quality')] || QUALITIES.auto; }
    function cur() { if (kept !== null) return kept; var t = video.currentTime || 0; return plan && !plan.seekable ? base + t : t; }
    function dur() { return (plan && plan.duration) || video.duration || 0; }
    function here() { return pending !== null ? pending : cur(); }          // la position affichée : celle qu'on est en train de choisir, sinon celle du film
    function chapters() { return (plan && plan.chapters) || []; }

    // --- Ouverture -----------------------------------------------------------
    function open(at) {
      spin.hidden = false; err.hidden = true;
      var body = { caps: capabilities(level), audio: audioIdx, max_kbps: q().k, max_height: q().h, device: H.device };
      return H.api('/api/play/' + id, body).then(function (p) {
        if (closing) return;
        plan = p;
        if (audioIdx === null) { var chosen = p.audio.filter(function (a) { return a.selected; })[0]; audioIdx = chosen ? chosen.index : null; }
        title.textContent = p.show || p.title || '';
        sub.textContent = p.show ? [(p.season !== null && p.season !== undefined ? 'S' + p.season : ''), (p.number !== null && p.number !== undefined ? 'E' + p.number : ''), p.title].filter(Boolean).join(' · ') : '';
        pill.textContent = MODES[p.mode]; pill.title = p.why.map(function (w) { return H.tr(w); }).join('\n'); pill.setAttribute('data-mode', p.mode);
        bNext.hidden = !p.next;
        bAudio.hidden = p.audio.length < 2;
        bSubs.hidden = !p.subs.length;
        bChapters.hidden = !chapters().length;
        if (p.warning) H.toast(H.tr(p.warning), 7000);
        var start = at !== undefined ? at : (query.t !== undefined ? parseFloat(query.t) || 0 : p.resume);
        if (firstPlan) { firstPlan = false; chooseDefaultSub(); if (start >= 20 && query.t === undefined) H.toast(H.t('Reprise à {time}', { time: H.time(start) })); }
        attach(start);
      }).catch(function (e) { spin.hidden = true; showError(e.message); });
    }
    // En remux l'image est recopiée : le flux ne peut commencer que sur une image clé, parfois bien avant l'instant voulu. Le serveur dit laquelle ; c'est de là que l'horloge et les sous-titres comptent.
    var attaching = 0;
    function attach(at) {
      at = Math.max(0, Math.min(at || 0, Math.max(0, dur() - 2)));
      aimed = at;
      var turn = ++attaching;
      if (plan.seekable || plan.mode !== 'remux' || at <= 0) { begin(at); return; }
      spin.hidden = false;
      H.api('/api/play/' + id + '/start?t=' + at.toFixed(3)).then(function (r) { return r.start; }, function () { return at; }).then(function (start) {
        if (turn === attaching && !closing) begin(start);
      });
    }
    function begin(at) {
      if (asleep) { kept = at; return; }          // le plan est arrivé après le départ : on retient seulement où commencer
      kept = null; aimed = null;
      base = plan.seekable ? 0 : at;
      video.src = plan.seekable ? plan.url : plan.url + '&start=' + at.toFixed(3);
      if (plan.seekable && at > 0) {
        var once = function () { video.removeEventListener('loadedmetadata', once); try { video.currentTime = at; } catch (e) { /* tant pis */ } };
        video.addEventListener('loadedmetadata', once);
      }
      if (hold) hold = false;
      else { var p = video.play(); if (p && p.catch) p.catch(function () { show(true); H.focus.set(playBtn, true); }); }
      paintTime();
    }
    // Le panneau posé sur l'image : tant qu'il est là, les touches du lecteur ne répondent plus (voir onKey), le focus est sur `first`.
    function panel(heading, text, first, second, third) {
      first.setAttribute('data-autofocus', '1');
      H.clear(err).appendChild(h('div', null, h('h2', null, heading), h('p', null, text), h('div', { class: 'actions' }, first, second, third)));
      err.hidden = false; show(true);
      H.focus.first(err);
    }
    // L'élément du fichier : celui du plan ou, quand le plan n'est pas venu, celui de la fiche gardée juste derrière le lecteur (voir toSheet), mais seulement si cette fiche contient ce fichier :
    // un lien direct ou un retour arrière peuvent laisser la fiche d'un autre titre. Sans preuve, on ne sait pas quoi signaler.
    function itemOfFile() {
      var below = H.below(), sheet = below && below.view.itemOf;
      return plan ? plan.item : sheet ? sheet(id) : '';
    }
    function showError(message) {
      var item = itemOfFile();
      panel(H.t('Lecture impossible'), message, H.button(H.t('Réessayer'), 'refresh', { onclick: function () { level = 0; open(cur()); } }), H.button(H.t('Retour'), 'back', { onclick: function () { H.back('#/'); } }),
        item ? H.button(H.t('Signaler un problème'), 'flag', { onclick: function () { H.actions.report({ item: item, entry: id, title: title.textContent }); } }) : null);
    }
    // Un film ne s'enchaîne pas comme un épisode : on propose le suivant de sa saga. Les commandes disparaissent, sinon les flèches iraient sur des boutons cachés par le panneau.
    function offerNext(next) {
      offered = true;
      panel(H.t('Film suivant de « {saga} »', { saga: next.saga }), next.title, H.button(H.t('Regarder'), 'play', { onclick: goNext }), H.button(H.t('Retour à la fiche'), 'back', { onclick: toSheet }));
    }
    // La fiche du film : celle d'où l'on vient si elle est juste derrière le lecteur ; sinon elle prend la place du lecteur. Sans page gardée derrière lui, elle s'ajoute à l'historique : la mettre à sa place ferait sortir de l'application ou doublerait une entrée.
    function toSheet() {
      var sheet = '#/i/' + plan.item, below = H.below();
      if (below && below.hash === sheet) history.back(); else if (below) H.go(sheet, true); else location.hash = sheet;
    }
    // Si le lecteur refuse ce que le serveur lui donne, on descend d'un cran : conteneur converti, puis image convertie.
    video.addEventListener('error', function () {
      if (closing || !plan || asleep) return;
      var at = cur();
      if (level < 2 && plan.mode !== 'transcode') {
        level++;
        H.toast(level === 1 ? H.t('Lecture directe impossible : conversion du conteneur.') : H.t('Ce lecteur ne lit pas cette image : conversion complète.'), 4500);
        open(at);
        return;
      }
      spin.hidden = true;
      H.api('/api/state').then(function (s) {
        showError(s.streams >= s.max_streams ? H.t('Le serveur convertit déjà {n} flux (maximum {max}). Réessaie dans un instant.', { n: s.streams, max: s.max_streams }) : (plan.why.map(function (w) { return H.tr(w); }).join(' · ') || H.t('Le fichier n’a pas pu être lu.')));
      }, function () { showError(H.t('Le serveur ne répond plus.')); });
    });
    video.addEventListener('waiting', function () { spin.hidden = false; });
    video.addEventListener('playing', function () { stale = false; spin.hidden = true; paintPlay(); schedule(); });
    video.addEventListener('canplay', function () { spin.hidden = true; });
    video.addEventListener('pause', function () { paintPlay(); show(true); report(); });
    video.addEventListener('timeupdate', paintTime);
    video.addEventListener('ended', function () {
      report(true);
      var next = plan && plan.next;
      if (next && next.saga) offerNext(next);
      else if (next) { carry(); H.go('#/play/' + next.id, true); }
      else H.back(plan ? '#/i/' + plan.item : '#/');
    });

    // --- Position ------------------------------------------------------------------
    function paintTime() {
      var t = here(), d = dur();
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
      stale = false;          // une position choisie, même en pause, est à retenir
      Object.keys(skipped).forEach(function (start) { if (t < +start) delete skipped[start]; });          // on demande à revenir avant un chapitre passé : il sera proposé de nouveau
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
      if (!plan || stale) return;
      var t = cur();
      if (!done && t < 20) return;
      lastReport = Date.now();
      H.api('/api/progress', { entry: id, position: t, duration: dur(), done: !!done }, { keepalive: true }).catch(function () {});
    }

    // --- Commandes visibles -----------------------------------------------------------
    function show(keep) {
      ctl.hidden = offered; el.classList.remove('idle');
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
    // Un menu de pistes (Audio, Sous-titres, Qualité) à la fois : un bouton activé derrière un menu déjà ouvert (la télécommande, un double appui) ferme d'abord celui-ci.
    // La fermeture passe par l'historique, donc s'achève plus tard : `wanted` garde le dernier menu demandé entre-temps, et lui seul s'ouvre.
    var wanted = null;
    function trackMenu(title, options, current, onPick) {
      var show = function () { H.menu(title, options, current, onPick); };
      if (wanted) { wanted = show; return; }
      var top = H.overlay.top(), box = top && top.querySelector('.menu.choice');
      var label = box && box.getAttribute('aria-label');
      if (label !== H.t('Audio') && label !== H.t('Sous-titres') && label !== H.t('Qualité') && label !== H.t('Vitesse') && label !== H.t('Chapitres')) { show(); return; }
      wanted = show;
      H.overlay.close(function () { var next = wanted; wanted = null; if (next && document.body.contains(el)) next(); });
    }
    function pickAudio() {
      trackMenu(H.t('Audio'), plan.audio.map(function (a) { return { label: H.tr(a.label) + (a.commentary ? ' ' + H.t('(commentaires)') : ''), value: a.index }; }), audioIdx, function (v) {
        if (v === audioIdx) return;
        audioIdx = v; open(cur());
      });
    }
    function pickSubs() {
      var opts = [{ label: H.t('Désactivés'), value: '' }].concat(plan.subs.map(function (s) {
        return { label: H.tr(s.label), detail: s.text ? H.tr(s.where) : s.image ? H.t('image · {where}', { where: H.tr(s.where) }) : H.t('image, non prise en charge'), value: s.id, disabled: !s.text && !s.image };
      }));
      trackMenu(H.t('Sous-titres'), opts, subs.sel, setSub);          // pour cette lecture seulement : les préférences durables sont celles du compte (Paramètres)
    }
    function pickQuality() {
      var now = H.pref('quality') || 'auto';
      var opts = Object.keys(QUALITIES).map(function (k) { return { label: QUALITIES[k].label, detail: k === now ? MODES[plan.mode] : '', value: k }; });
      trackMenu(H.t('Qualité'), opts, now, function (v) { H.pref('quality', v); level = 0; open(cur()); });
    }
    // La vitesse : le bouton l'affiche dès qu'elle n'est plus 1×. Un saut en son ou image convertis relance le flux (nouvelle source) : `defaultPlaybackRate` la garde, et `cur()` lit `currentTime`, déjà en temps du film.
    function setSpeed(rate) { speed = H.setSpeed(video, rate, bSpeed, H.t('Vitesse')); }
    function pickSpeed() {
      H.speedMenu(speed, function (v) { if (v === speed) return; setSpeed(v); flash(H.speedLabel(speed)); }, trackMenu);
    }
    function carry() { if (plan && plan.next && !plan.next.saga && speed !== 1) carried = { id: plan.next.id, rate: speed }; }

    // --- Chapitres ------------------------------------------------------------------
    // L'indice du chapitre à l'instant `t`, -1 hors de tout chapitre. S'ils se chevauchent, celui qui a commencé le dernier.
    function chapterAt(t) {
      var list = chapters(), found = -1;
      for (var i = 0; i < list.length && list[i].start <= t; i++) if (t < list[i].end) found = i;
      return found;
    }
    function pickChapter() {
      var list = chapters();
      var opts = list.map(function (c, i) { return { label: c.title || H.t('Chapitre {n}', { n: i + 1 }), detail: H.time(c.start), value: i }; });
      trackMenu(H.t('Chapitres'), opts, chapterAt(here()), function (i) {
        delete skipped[list[i].start];          // choisi dans la liste : on veut le revoir, son bouton revient avec lui
        commit(list[i].start);
      });
    }
    // Le bouton ne saute jamais tout seul. Le chapitre passé est retenu : en remux, le flux repart d'une image clé, parfois avant sa fin, voire avant son début, et le bouton reviendrait.
    function skipChapter() {
      var c = skipNow;
      if (!c) return;
      commit(c.end);
      skipped[c.start] = true;          // après `commit` : tout près de la fin du film, il ramène `end` avant le début du chapitre
      offerSkip(null);
    }
    // Propose `c` (null : rien). Le DOM n'est touché que si l'affichage change. Le bouton qui part avec le focus le rend aux commandes si elles sont là, sinon le lâche : Entrée remet alors en pause.
    function offerSkip(c) {
      skipNow = c;
      var key = c ? c.skip + '@' + c.start : '';
      if (key === skipShown) return;
      skipShown = key;
      el.classList.toggle('skipping', !!c);          // les sous-titres montent au-dessus du bouton (style.css)
      if (c) { bSkip.querySelector('.lbl').textContent = SKIPS[c.skip]; bSkip.hidden = false; return; }
      var focused = document.activeElement === bSkip;
      if (focused) bSkip.blur();
      bSkip.hidden = true;
      if (focused && !ctl.hidden) H.focus.set(playBtn, true);
    }
    function paintSkip() {
      if (!plan || !err.hidden) { offerSkip(null); return; }          // un panneau occupe l'écran
      if (video.readyState < 1 || video.seeking) return;              // le film se place : `currentTime` ne dit pas encore où il sera (0 à l'ouverture d'une reprise), on attend
      var c = chapters()[chapterAt(here())];
      offerSkip(c && SKIPS[c.skip] && !skipped[c.start] ? c : null);
    }
    // Le sous-titre du premier plan : celui que le serveur a choisi d'après les langues du compte (au plus un `selected` ; jamais une piste image, dont l'extraction peut durer).
    // Les plans suivants (autre piste audio, autre qualité) gardent le sous-titre en cours : c'est peut-être celui que l'utilisateur vient de choisir.
    function chooseDefaultSub() {
      var pick = plan.subs.filter(function (s) { return s.selected; })[0];
      setSub(pick ? pick.id : '');
    }
    function setSub(sid) {
      subs.sel = sid; subs.cues = []; subs.pgs = null; subs.shown = undefined; subText = ''; H.clear(subBox); subPic.hidden = true;
      var track = plan.subs.filter(function (s) { return s.id === sid; })[0];
      if (!track) return;
      if (track.image) {
        H.toast(H.t('Préparation des sous-titres image… la première fois, cela peut durer.'), 5000);
        fetch(track.url, { credentials: 'same-origin' }).then(function (r) { if (!r.ok) throw new Error(H.t('sous-titres indisponibles')); return r.arrayBuffer(); }).then(function (data) {
          if (subs.sel !== sid) return;
          subs.pgs = H.pgs.parse(data); subPic.hidden = false;
        }).catch(function (e) { H.toast(e.message); });
        return;
      }
      fetch(track.url, { credentials: 'same-origin' }).then(function (r) { if (!r.ok) throw new Error(H.t('sous-titres indisponibles')); return r.text(); }).then(function (text) {
        if (subs.sel === sid) subs.cues = parseVtt(text);
      }).catch(function (e) { H.toast(e.message); });
    }
    tickTimer = setInterval(function () {
      paintSkip();          // avant tout : la suite s'arrête tôt selon les sous-titres
      if (subs.pgs) {
        var set = H.pgs.at(subs.pgs, cur());
        if (set !== subs.shown) { subs.shown = set; H.pgs.draw(subPic, subs.pgs, set); }
        return;
      }
      if (!subs.cues.length) return;
      var t = cur(), lines = [];
      for (var i = 0; i < subs.cues.length; i++) { var c = subs.cues[i]; if (c.s > t) break; if (t < c.e) lines.push(c.t); }
      var text = lines.join('\n');
      if (text !== subText) { subText = text; H.clear(subBox); if (text) subBox.appendChild(cueNodes(text)); }
    }, 200);
    reportTimer = setInterval(function () { if (!video.paused && Date.now() - lastReport > 9000) report(); }, 10000);
    // Mis de côté (quitté vers la fiche quand rien n'était derrière lui, Retour y revient) : la position est enregistrée, la vidéo se tait et lâche sa source, ce qui ferme le flux converti côté serveur.
    // Un film fini, ou un panneau posé sur l'image (le suivant d'une saga, « Lecture impossible ») : rien ne se lit, il est gardé tel quel et Retour y retrouve le panneau.
    function parked() {
      if (closing || asleep || !err.hidden || video.ended) return;
      clearTimeout(pendingTimer);
      if (plan) kept = pending !== null ? pending : aimed !== null ? aimed : cur();          // un départ en remux encore attendu : c'est sa cible qu'on garde, pas 0
      pending = null; aimed = null; asleep = true; attaching++;          // `attaching` : ce départ-là ne s'ouvrira pas
      report();
      stale = true;          // c'est écrit ; rien d'autre ne s'écrira avant que le film rejoue (ou qu'on choisisse une position)
      video.pause(); video.removeAttribute('src'); video.load();
      paintPlay();          // `load()` a retiré l'événement `pause` encore en file : l'icône ne suivrait pas
    }
    // Rouvert par Retour : au même endroit, en pause, la lecture reprend quand on la demande. Rouvert par « Lecture » sur la fiche (`ahead`) : il repart de là, en lecture.
    function wake(ahead) {
      if (!asleep) return;
      asleep = false;
      H.audio.pause();
      show(true); paintPlay();
      if (!ahead) H.focus.set(playBtn, true);
      hold = !ahead;          // aussi pour un plan encore attendu : il s'ouvrira de la même façon
      if (kept !== null && plan) attach(kept);
    }
    // Passer à l'épisode suivant vaut « vu » ; quitter un film pour le suivant de sa saga ne dit pas qu'on l'a fini : il reste à reprendre.
    function goNext() { if (plan && plan.next) { report(!plan.next.saga); carry(); H.go('#/play/' + plan.next.id, true); } }

    // --- Clavier et souris ----------------------------------------------------------------
    function eat(e) { e.preventDefault(); e.stopPropagation(); }
    function onKey(e) {
      if (!document.body.contains(el) || el.style.display === 'none' || H.overlay.isOpen() || e.ctrlKey || e.metaKey || e.altKey) return;
      var k = e.key;
      if (!err.hidden) return;
      if (k === 'MediaPlayPause') { toggle(); eat(e); return; }
      if (k === 'MediaTrackNext') { goNext(); eat(e); return; }
      if (k === 'f' || k === 'F') { fullscreen(); eat(e); return; }
      if ((k === 'm' || k === 'M') && volume) { volume.toggle(); show(); eat(e); return; }
      var inControls = !ctl.hidden && ctl.contains(document.activeElement), onSkip = document.activeElement === bSkip;
      if (!inControls) {                     // personne n'est sur une commande : les flèches pilotent directement la lecture, et haut ou bas va d'abord au bouton de saut s'il est affiché
        if (k === 'ArrowLeft' || k === 'ArrowRight') { seekBy(k === 'ArrowLeft' ? -1 : 1); eat(e); }
        else if ((k === 'ArrowUp' || k === 'ArrowDown') && !bSkip.hidden && !onSkip) { H.focus.set(bSkip, true); eat(e); }
        else if (k === 'ArrowUp' || k === 'ArrowDown') { show(); H.focus.set(k === 'ArrowUp' ? seekbar : playBtn, true); eat(e); }
        else if ((k === 'Enter' || k === ' ') && onSkip) return;          // le bouton s'active lui-même
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
      shown: function (fresh, ahead) { if (!fresh) { setSpeed(1); wake(ahead); } show(); },          // un lecteur laissé en réserve puis rouvert (Lecture depuis la fiche) : c'est une nouvelle ouverture, elle repart à 1×
      parked: parked,
      onBack: function () {
        if (offered) { toSheet(); return true; }
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
