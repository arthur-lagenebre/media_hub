/* Les envois de fichiers : la page « Envoyer » (un compte qui a le droit d'envoyer) et le panneau « Boîte de réception » de Paramètres (administrateur).
   Rien n'entre dans une bibliothèque : le serveur range les fichiers dans une boîte de réception, et l'administrateur les range lui-même (docs/decisions.md).
   Les fichiers partent un par un, par XMLHttpRequest (fetch ne rend pas la progression d'un envoi) : le serveur en accepte deux à la fois au plus et refuse le suivant en 429.
   Un dossier (choisi ou glissé) part de même, fichier par fichier, avec `dir` : son chemin relatif, nom du dossier choisi en tête ; une seule ligne l'annonce, dépliable. Avant le départ, ce que le serveur refuserait (type, caché, trop profond, vide) est écarté, et la personne confirme.
   Pas de page « Envoyer » sur une télévision : saisir et choisir des fichiers à la télécommande ne marche pas. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h;
  var U = H.upload = { retry: 5000 };        // `retry` : le délai (ms) avant de réessayer après un 429 ; un test le raccourcit

  // --- Formats ----------------------------------------------------------------
  var UNITS = [['Go', 1073741824, 'GB'], ['Mo', 1048576, 'MB'], ['Ko', 1024, 'KB']];
  // « 1,5 Go » en français, « 1.5 GB » en anglais ; `french` impose le français (les refus que l'on écrit comme le serveur, avant de les traduire).
  function sizeText(count, french) {
    var fr = french || H.lang === 'fr';
    for (var i = 0; i < UNITS.length; i++) {
      if (count >= UNITS[i][1]) {
        var text = (count / UNITS[i][1]).toFixed(1);
        if (/\.0$/.test(text)) text = text.slice(0, -2);
        return (fr ? text.replace('.', ',') : text) + ' ' + UNITS[i][fr ? 0 : 2];
      }
    }
    return count + ' ' + (fr ? 'octets' : 'bytes');
  }
  // Un texte que le serveur a écrit en français : traduit, et ses tailles (« 1,5 Go ») écrites à la manière de la langue.
  function said(text) {
    var out = H.tr(text);
    if (H.lang === 'fr' || typeof out !== 'string') return out;
    return out.replace(/(\d+)(?:,(\d))? (Go|Mo|Ko|octets)/g, function (m, whole, tenth, unit) {
      var en = unit === 'octets' ? 'bytes' : UNITS.filter(function (u) { return u[0] === unit; })[0][2];
      return whole + (tenth ? '.' + tenth : '') + ' ' + en;
    });
  }
  function when(seconds) {
    var d = new Date(seconds * 1000);
    return d.toLocaleDateString(H.locale) + ' ' + d.toLocaleTimeString(H.locale, { hour: '2-digit', minute: '2-digit' });
  }
  function fileUrl(f) { return '/api/inbox/' + encodeURIComponent(f.account) + '/' + encodeURIComponent(f.name) + (f.dir ? '?dir=' + encodeURIComponent(f.dir) : ''); }
  function pathOf(f) { return f.dir ? f.dir + '/' + f.name : f.name; }
  function meter(percent) {
    var bar = h('i');
    var box = h('div', { class: 'meter', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(percent) }, bar);
    bar.style.width = percent + '%';
    return { el: box, set: function (p) { bar.style.width = p + '%'; box.setAttribute('aria-valuenow', String(p)); } };
  }

  // --- Un dossier : ce qui part, ce qu'on écarte ----------------------------------------------
  var DEPTH = 4;                // niveaux de dossiers au plus, celui que l'on a choisi compris (le serveur refuse au-delà : 400)
  var SHOWN = 100;              // lignes de la liste des fichiers écartés
  function hidden(name) { return name.charAt(0) === '.' || /^(thumbs\.db|desktop\.ini|node_modules)$/i.test(name); }      // `node_modules` : jamais de la médiathèque, et trop de fichiers à lire pour rien
  function extension(name) {
    var clean = name.replace(/[ .]+$/, ''), at = clean.lastIndexOf('.');      // comme le serveur : ni point ni espace en bout, une extension se lit au dernier point
    return at > 0 ? clean.slice(at).toLowerCase() : '';
  }
  // `found` : [{path: 'Film/Bonus/a.mkv', file}] (le dossier choisi en tête ; `file` vaut null si le navigateur n'a pas pu le lire ; `unread` : un dossier dont la lecture a échoué ; `pruned` : un dossier caché ou trop profond, écarté sans y entrer) ; `types` : les extensions acceptées {'.mkv': true}, ou null (le serveur tranchera).
  // Rend ce qui part (`take`, dans l'ordre des noms) et ce qui est écarté (`skip`, avec la raison).
  function sift(found, types) {
    var take = [], skip = [];
    found.forEach(function (f) {
      var parts = f.path.split('/'), name = parts[parts.length - 1], why = '';
      if (f.unread || f.pruned) why =parts.slice(1).some(hidden) ? 'hidden' : parts.length > DEPTH ? 'deep' : 'unread';      // un dossier dont la lecture a échoué (ses fichiers, eux, n'ont pas été vus)
      else if (!f.file) why = 'unreadable';
      else if (parts.slice(1).some(hidden)) why = 'hidden';                    // le dossier choisi n'est pas jugé : la personne l'a désigné
      else if (parts.length - 1 > DEPTH) why = 'deep';
      else if (types && !types[extension(name)]) why = 'type';
      else if (!f.file.size) why = 'empty';                                       // le serveur refuse un fichier vide
      if (why) skip.push({ path: f.path, why: why });
      else take.push({ file: f.file, dir: parts.slice(0, -1).join('/'), sub: parts.slice(1, -1).join('/') });
    });
    take.sort(function (a, b) { return byName(a.dir + '/' + a.file.name, b.dir + '/' + b.file.name); });
    skip.sort(function (a, b) { return byName(a.path, b.path); });
    return { take: take, skip: skip };
  }
  function byName(a, b) { return a.localeCompare(b, undefined, { numeric: true }); }     // « 2 » avant « 10 »
  // Un dossier glissé : l'API de fichiers du navigateur. `readEntries` rend ses résultats par paquets (100 au plus) : on le rappelle jusqu'à un paquet vide.
  // Une lecture qui échoue ne rend pas la liste partielle pour complète : `done(all, true)` le dit.
  function readEntries(reader, done) {
    var all = [];
    (function more() {
      reader.readEntries(function (batch) {
        if (!batch.length) { done(all, false); return; }
        all = all.concat(Array.prototype.slice.call(batch));
        more();
      }, function () { done(all, true); });
    })();
  }
  function walk(entry, path, found, done) {
    if (entry.isFile) {
      entry.file(function (file) { found.push({ path: path, file: file }); done(); }, function () { found.push({ path: path, file: null }); done(); });
      return;
    }
    // Un dossier caché (`.git`, `node_modules` : des milliers d'entrées) ou d'un niveau de plus que le serveur n'en accepte (ses fichiers seraient tous « trop profonds ») est écarté sans y entrer : une seule ligne « ignoré ». Le dossier choisi (niveau 1) n'est pas jugé.
    var depth = path.split('/').length;
    if (depth > DEPTH || (depth > 1 && hidden(entry.name))) { found.push({ path: path, file: null, pruned: true }); done(); return; }
    readEntries(entry.createReader(), function (children, failed) {
      if (failed) found.push({ path: path, file: null, unread: true });          // ce dossier est peut-être incomplet : sift le signale, la confirmation le dit
      var left = children.length;
      if (!left) { done(); return; }
      children.forEach(function (child) { walk(child, path + '/' + child.name, found, function () { if (--left === 0) done(); }); });
    });
  }
  // Un bouton qui montre ou cache `panel` (caché au départ) : un bouton, pas un <details>, pour que la télécommande l'actionne comme le reste.
  function disclose(show, hide, panel, attrs) {
    panel.hidden = true;
    var button = H.button(show, null, attrs || {});
    button.setAttribute('aria-expanded', 'false');
    button.addEventListener('click', function () {
      panel.hidden = !panel.hidden;
      button.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
      button.querySelector('.lbl').textContent = panel.hidden ? show : hide;
    });
    return button;
  }

  // --- La page « Envoyer » ----------------------------------------------------------
  H.route(/^\/upload$/, function () {
    var me = H.session.user;
    if (H.device === 'tv' || !me || !me.can_upload) {          // pas de page : retour à l'accueil, avec la raison
      var reason = H.device === 'tv' ? H.t('Envoyer des fichiers n’est pas possible sur une télévision.') : H.t('Tu n’as pas le droit d’envoyer des fichiers.');
      return { el: h('section', { class: 'blank' }, h('h1', null, H.t('Envoyer')), h('p', null, reason)), nav: '#/upload', shown: function () { H.toast(reason, 5000); H.go('#/', true); } };
    }
    var el = h('section', { class: 'upload' });
    var items = [], jobs = [], seq = 0, current = null, stopped = false, alive = true, over = 0, types = null, offers = [], offering = false, loadTimer = null;
    // `items` : les lignes de la page (un fichier, ou un dossier) ; `jobs` : tous les fichiers à envoyer, ceux d'un dossier compris, dans l'ordre de la file.
    var list = h('div', { class: 'sends' });
    var sendPanel = h('section', { class: 'panel', hidden: true }, h('h2', null, H.t('Envois')), list);
    var expired = h('div', { class: 'panel', hidden: true, role: 'alert' }, h('p', { class: 'warn' }, H.t('Ta session a expiré : les envois sont arrêtés. Reconnecte-toi, puis recommence.')),
      h('a', { class: 'btn f', href: '/login', 'data-key': 'relogin' }, H.t('Se reconnecter')));
    var mineBox = h('div', { class: 'mine' }), space = h('div', { class: 'used' });
    var input = h('input', { type: 'file', multiple: true, style: 'display:none', tabindex: '-1', 'aria-hidden': 'true', onchange: function () {
      var files = Array.prototype.slice.call(input.files); input.value = ''; addFiles(files);
    } });
    var choose = H.button(H.t('Choisir des fichiers'), 'upload', { class: 'primary', 'data-autofocus': '1', 'data-key': 'choose', onclick: function () { input.click(); } });
    var canFolder = 'webkitdirectory' in document.createElement('input');          // un navigateur qui ne sait pas choisir un dossier n'a pas le bouton, et ne dit rien
    var dirInput = canFolder ? h('input', { type: 'file', webkitdirectory: true, style: 'display:none', tabindex: '-1', 'aria-hidden': 'true', onchange: function () {
      var picked = Array.prototype.slice.call(dirInput.files); dirInput.value = ''; addPicked(picked);
    } }) : null;
    var chooseDir = canFolder ? H.button(H.t('Envoyer un dossier'), 'upload', { 'data-key': 'choose-dir', onclick: function () { dirInput.click(); } }) : null;
    var zone = h('div', { class: 'drop panel' },
      h('p', { class: 'dropline' }, H.device === 'phone' ? H.t('Choisis les fichiers à envoyer depuis cet appareil.') : H.t('Glisse des fichiers ici, ou choisis-les sur cet appareil.')),
      choose, chooseDir, input, dirInput,
      h('p', { class: 'muted' }, H.t('Films, séries, musique, livres, sous-titres et archives .zip. Les autres types de fichiers sont refusés.')), space);

    function active(item) { return item.state === 'wait' || item.state === 'send'; }
    function busy() { return jobs.some(active); }
    // --- La file : un envoi à la fois ---
    function newJob(file, extra) {
      var item = { id: ++seq, file: file, name: file.name, total: file.size, state: 'wait', sent: 0, began: 0, kept: '', error: '', note: '', xhr: null, hold: null, button: null, mode: '', dir: '', sub: '', group: null };
      Object.keys(extra || {}).forEach(function (k) { item[k] = extra[k]; });
      if (stopped) { item.state = 'error'; item.error = H.t('Arrêté : la session a expiré.'); }
      jobs.push(item);
      return item;
    }
    function addFiles(files) {
      files.forEach(function (file) {
        var item = newJob(file);
        items.push(item);
        list.appendChild(build(item));
        paint(item);
      });
      sendPanel.hidden = !items.length;
      pump();
    }
    function pump() {
      if (current || stopped || !alive) return;
      var next = jobs.filter(function (i) { return i.state === 'wait'; })[0];
      if (next && !next.hold) start(next);
    }
    function start(item) {
      var xhr = new XMLHttpRequest();
      current = item; item.xhr = xhr; item.state = 'send'; item.sent = 0; item.note = ''; item.began = Date.now();
      xhr.open('POST', '/api/inbox?name=' + encodeURIComponent(item.name) + (item.dir ? '&dir=' + encodeURIComponent(item.dir) : ''));
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.onprogress = function (e) { if (e.lengthComputable) item.total = e.total; item.sent = e.loaded; paint(item); };
      xhr.onload = function () { answered(item, xhr); };
      // Le serveur refuse un envoi avant d'en lire le corps, puis coupe la connexion, mais laisse la réponse arriver (il lit et jette le corps encore envoyé, dans des bornes de temps et d'octets) : cette erreur n'est plus que le cas d'une vraie coupure, ou d'un refus dont le corps dépasse ces bornes.
      xhr.onerror = function () { fail(item, H.t('La connexion a été coupée pendant l’envoi : le serveur est peut-être occupé (réessaie plus tard), ou le fichier est refusé (type, espace).')); };
      xhr.onabort = function () { item.state = 'cancel'; release(item); };
      paint(item);
      xhr.send(item.file);
    }
    function release(item) {
      if (current === item) current = null;
      item.xhr = null;
      paint(item);
      pump();
    }
    function fail(item, text) { item.state = 'error'; item.error = text; release(item); }
    function answered(item, xhr) {
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      if (xhr.status === 201 && data) {
        item.state = 'done'; item.kept = data.name; item.sent = item.total;
        release(item);
        refresh();
      } else if (xhr.status === 401) {
        expire(item);
      } else if (xhr.status === 429) {                                    // d'autres envois occupent le serveur : le fichier reprend sa place dans la file, on réessaie plus tard
        item.state = 'wait'; item.note = H.t('Le serveur est occupé : nouvel essai dans quelques secondes.');
        item.hold = setTimeout(function () { item.hold = null; pump(); }, U.retry);
        release(item);
      } else {                                                           // 400 (chemin refusé), 409 (trop de sous-dossiers, nom pris), 415, 507… : le message du serveur, sur ce fichier seul
        fail(item, data && data.error ? said(data.error) : H.t('Erreur {status}', { status: xhr.status }));
      }
    }
    function expire(item) {            // la session a pris fin : rien ne passera plus, la file s'arrête (les fichiers restent listés pour qu'on sache ce qui n'est pas parti)
      stopped = true;
      jobs.forEach(function (i) {
        if (i === item || i.state === 'wait') { clearTimeout(i.hold); i.hold = null; i.state = 'error'; i.error = H.t('Arrêté : la session a expiré.'); i.xhr = null; paint(i); }
      });
      current = null;
      expired.hidden = false;
    }
    function cancel(item, quiet) {
      if (item.state === 'send' && item.xhr) {
        if (item.xhr.readyState === 4) { item.state = 'cancel'; release(item); } else item.xhr.abort();       // fini (on cherche pourquoi il a échoué) : rien à interrompre ; sinon onabort fait le reste, et le serveur efface le .part
        return;
      }
      if (item.state === 'wait') { clearTimeout(item.hold); item.hold = null; item.state = 'cancel'; paint(item); if (!quiet) pump(); }
    }
    function cancelThese(some) {       // d'abord ceux qui attendent : l'annulation de l'envoi en cours ne doit pas lancer le suivant
      some.forEach(function (i) { if (i.state === 'wait') cancel(i, true); });
      some.forEach(function (i) { if (i.state === 'send') cancel(i); });
    }
    function cancelAll() { cancelThese(jobs.slice()); }
    function retry(some) {             // les fichiers en erreur repartent, en queue de file
      if (stopped) return;
      some.forEach(function (i) { if (i.state === 'error') { i.state = 'wait'; i.error = ''; i.note = ''; i.sent = 0; paint(i); } });
      pump();
    }
    function dismiss(item) {
      var at = items.indexOf(item), next = items[at + 1] || items[at - 1];
      var hadFocus = item.node.contains(document.activeElement);
      items.splice(at, 1);
      (item.files || [item]).forEach(function (j) { jobs.splice(jobs.indexOf(j), 1); });
      list.removeChild(item.node);
      sendPanel.hidden = !items.length;
      if (hadFocus) H.focus.set(next && next.button ? next.button : choose, true);
    }

    // --- Un dossier : choisi ou glissé, confirmé, puis une ligne dépliable ---
    function addPicked(picked) {       // un dossier choisi : chaque fichier porte son chemin (`webkitRelativePath`), dont le premier élément est le dossier
      var byRoot = {}, order = [], loose = [];
      picked.forEach(function (file) {
        var path = file.webkitRelativePath || '', at = path.indexOf('/');
        if (at < 0) { loose.push(file); return; }
        var root = path.slice(0, at);
        if (!byRoot[root]) { byRoot[root] = []; order.push(root); }
        byRoot[root].push({ path: path, file: file });
      });
      order.forEach(function (root) { offer(root, byRoot[root]); });
      if (loose.length) addFiles(loose);
    }
    function offer(root, found) {      // les dossiers se confirment l'un après l'autre
      offers.push({ root: root, found: found });
      if (!offering) nextOffer();
    }
    function nextOffer() {
      var o = offers.shift();
      if (!o || !alive) { offering = false; return; }
      offering = true;
      (types ? Promise.resolve() : load()).then(function () {
        if (!alive || !document.body.contains(el) || el.style.display === 'none') { offering = false; offers = []; return; }       // une réponse tardive, la page a été quittée : la confirmation s'ouvrirait sur une autre page
        confirmFolder(o.root, o.found, nextOffer);
      });
    }
    function why(kind) {               // la raison d'un fichier écarté, au pluriel (le résumé) ou au singulier (sa ligne)
      var WHY = { type: [H.t('types non acceptés'), H.t('type non accepté')], hidden: [H.t('fichiers cachés'), H.t('fichier caché')], deep: [H.t('dossiers trop profonds'), H.t('trop profond (4 niveaux au plus)')],
        empty: [H.t('fichiers vides'), H.t('fichier vide')], unreadable: [H.t('fichiers illisibles'), H.t('illisible')],
        unread: [H.t('dossiers illisibles'), H.t('lecture du dossier échouée')] };
      return WHY[kind];
    }
    function summary(skip) {           // « types non acceptés, fichiers cachés » : les raisons présentes, dans un ordre fixe
      var seen = {};
      skip.forEach(function (s) { seen[s.why] = true; });
      return ['type', 'hidden', 'deep', 'empty', 'unreadable', 'unread'].filter(function (k) { return seen[k]; }).map(function (k) { return why(k)[0]; }).join(', ');
    }
    function skippedList(skip) {
      var ul = h('ul', { class: 'skipped' });
      skip.slice(0, SHOWN).forEach(function (s) { ul.appendChild(h('li', null, s.path + ' — ' + why(s.why)[1])); });
      if (skip.length > SHOWN) ul.appendChild(h('li', null, H.t('… et {n} autres', { n: skip.length - SHOWN })));
      return ul;
    }
    function confirmFolder(root, found, next) {
      var s = sift(found, types), reason = summary(s.skip), extra = [];
      var unread = s.skip.filter(function (k) { return k.why === 'unread'; });
      if (s.skip.length) {
        var ul = skippedList(s.skip);
        extra = [disclose(H.t('Voir les fichiers ignorés'), H.t('Masquer les fichiers ignorés'), ul, { 'data-key': 'skipped' }), ul];
      }
      if (!s.take.length && unread.length) {            // rien n'a pu être lu : ce n'est pas « rien à envoyer », le dossier n'est peut-être pas vide
        H.confirm({ title: H.t('Dossier illisible'), ok: H.t('Compris'), cls: 'sendfolder', extra: extra, cancel: next, done: next,
          lead: [H.t('La lecture du dossier « {name} » a échoué : aucun fichier n’a pu être lu.', { name: root })] });
        return;
      }
      if (!s.take.length) {            // rien d'envoyable : on le dit, sans rien lancer
        H.confirm({ title: H.t('Rien à envoyer'), ok: H.t('Compris'), cls: 'sendfolder', extra: extra, cancel: next, done: next,
          lead: [s.skip.length ? H.t('Aucun fichier du dossier « {name} » ne peut être envoyé ({why}) : il n’y a rien à envoyer.', { name: root, why: reason }) : H.t('Le dossier « {name} » est vide : il n’y a rien à envoyer.', { name: root })] });
        return;
      }
      var size = s.take.reduce(function (sum, t) { return sum + t.file.size; }, 0), files = H.plural(s.take.length, 'fichier');
      var lead = [s.skip.length ? H.t('Envoyer le dossier « {name} » : {files} ({size}), {skipped} ({why}).', { name: root, files: files, size: sizeText(size), skipped: H.plural(s.skip.length, 'ignoré', 'ignorés'), why: reason })
                                : H.t('Envoyer le dossier « {name} » : {files} ({size}).', { name: root, files: files, size: sizeText(size) })];
      if (unread.length)               // un dossier lu à moitié : la personne sait qu'il peut manquer des fichiers, et peut annuler ou envoyer quand même
        lead.push(unread.length === 1 ? H.t('La lecture du dossier « {name} » a échoué : il peut manquer des fichiers.', { name: unread[0].path })
                                      : H.t('La lecture de {n} dossiers a échoué : il peut manquer des fichiers.', { n: unread.length }));
      H.confirm({ title: H.t('Envoyer ce dossier ?'), ok: H.t('Envoyer'), cls: 'sendfolder', extra: extra, cancel: next, lead: lead,
        done: function () { if (alive) addFolder(root, s.take); next(); } });
    }
    function addFolder(name, take) {
      var group = { id: ++seq, kind: 'folder', name: name, files: [], state: 'wait', mode: '', button: null, total: 0 };
      take.forEach(function (t) {
        var item = newJob(t.file, { dir: t.dir, sub: t.sub ? t.sub + '/' : '', group: group });
        group.files.push(item); group.total += t.file.size;
      });
      items.push(group);
      list.appendChild(buildFolder(group));
      group.files.forEach(function (item) { group.details.appendChild(build(item)); paint(item); });
      paintFolder(group);
      sendPanel.hidden = false;
      pump();
    }

    // --- L'affichage d'un envoi ---
    function build(item) {
      item.title = h('b'); item.status = h('span'); item.meter = meter(0); item.meter.el.hidden = true;
      item.node = h('div', { class: 'line ' + (item.group ? 'sendfile' : 'send'), 'data-send': String(item.id) }, h('div', { class: 'what' }, item.title, item.status, item.meter.el));
      return item.node;
    }
    function buildFolder(group) {
      group.title = h('b', null, group.name); group.status = h('span'); group.meter = meter(0); group.meter.el.hidden = true;
      group.details = h('div', { class: 'sendfiles' });
      var toggle = disclose(H.t('Voir les fichiers'), H.t('Masquer les fichiers'), group.details, { 'data-key': 'files-' + group.id });
      group.retry = H.button(H.t('Réessayer'), 'refresh', { 'data-key': 'retry-' + group.id, 'aria-label': H.t('Réessayer les fichiers en erreur de {name}', { name: group.name }),
        onclick: function () { retry(group.files); if (group.button) H.focus.set(group.button, true); } });
      group.node = h('div', { class: 'line send folder', 'data-send': String(group.id), 'data-folder': group.name }, h('div', { class: 'what' }, group.title, group.status, group.meter.el), toggle, group.retry, group.details);
      return group.node;
    }
    function progress(item) {
      var sent = Math.min(item.sent, item.total), percent = item.total ? Math.floor(sent / item.total * 100) : 0, seconds = (Date.now() - item.began) / 1000;
      var speed = seconds > 0.3 ? sent / seconds : 0, parts = [percent + (H.lang === 'fr' ? ' %' : '%')];
      parts.push(H.t('{sent} sur {total}', { sent: sizeText(sent), total: sizeText(item.total) }));
      if (speed > 0) {
        parts.push(sizeText(speed) + '/s');
        if (sent < item.total) parts.push(H.t('reste {time}', { time: H.time((item.total - sent) / speed) }));
      }
      return { percent: percent, text: parts.join(' · ') };
    }
    function act(item, mode) {         // le bouton d'action d'une ligne, remplacé quand son rôle change (le focus le suit)
      if (mode === item.mode) return;
      var had = item.button && document.activeElement === item.button, name = item.name, button = null;
      if (mode === 'cancel') button = H.button(H.t('Annuler'), 'close', { 'data-key': 'cancel-' + item.id, 'aria-label': H.t('Annuler l’envoi de {name}', { name: name }), onclick: function () { if (item.files) cancelThese(item.files.slice()); else cancel(item); } });
      else if (mode === 'drop') button = H.button(H.t('Retirer de la liste'), null, { 'data-key': 'drop-' + item.id, 'aria-label': H.t('Retirer {name} de la liste', { name: name }), onclick: function () { dismiss(item); } });
      else if (mode === 'retry') button = H.button(H.t('Réessayer'), 'refresh', { 'data-key': 'retry-' + item.id, 'aria-label': H.t('Réessayer l’envoi de {name}', { name: item.sub + name }), onclick: function () { retry([item]); } });
      if (item.button && button) item.node.replaceChild(button, item.button);
      else if (item.button) item.node.removeChild(item.button);
      else if (button) item.node.insertBefore(button, item.details || item.retry || null);
      item.button = button; item.mode = mode;
      if (had) H.focus.set(button || (item.group && item.group.button) || choose, true);
    }
    function paint(item) {
      var st = item.state, text = '', percent = 0;
      item.node.className = 'line ' + (item.group ? 'sendfile ' : 'send ') + st;
      item.node.setAttribute('data-state', st);
      item.title.textContent = item.sub + (st === 'done' && item.kept ? item.kept : item.name);
      if (st === 'wait') text = item.note || H.t('En attente');
      else if (st === 'send') { var p = progress(item); text = p.text; percent = p.percent; }
      else if (st === 'done') text = H.t('Envoyé · {size}', { size: sizeText(item.total) }) + (item.kept !== item.name ? ' · ' + H.t('enregistré sous ce nom (tu l’avais appelé « {name} »)', { name: item.name }) : '');
      else if (st === 'error') text = item.error;
      else text = H.t('Annulé');
      item.status.textContent = text;
      item.status.className = st === 'error' ? 'warn' : '';
      item.meter.el.hidden = st !== 'send';
      item.meter.set(percent);
      // « Annuler » tant que l'envoi est à faire ou en cours ; ensuite « Retirer de la liste » (un fichier seul) ou « Réessayer » (un fichier en erreur d'un dossier)
      act(item, active(item) ? 'cancel' : item.group ? (st === 'error' && !stopped ? 'retry' : '') : 'drop');
      if (item.group) paintFolder(item.group);
    }
    function paintFolder(group) {
      var done = 0, failed = 0, cancelled = 0, live = 0, sending = 0, sent = 0;
      group.files.forEach(function (i) {
        if (i.state === 'done') { done++; sent += i.total; }
        else if (i.state === 'error') failed++;
        else if (i.state === 'cancel') cancelled++;
        else { live++; if (i.state === 'send') { sending++; sent += Math.min(i.sent, i.total); } }
      });
      var st = live ? (sending ? 'send' : 'wait') : failed ? 'error' : cancelled ? 'cancel' : 'done';
      var percent = group.total ? Math.floor(sent / group.total * 100) : 0;
      var parts = [done + ' / ' + H.plural(group.files.length, 'fichier'), sizeText(group.total)];
      if (live) parts.push(percent + (H.lang === 'fr' ? ' %' : '%'));
      else if (st === 'done') parts.push(H.t('Envoyé'));
      else if (cancelled) parts.push(H.t('Annulé'));
      if (failed) parts.push(H.t('{n} en erreur', { n: failed }));
      group.state = st;
      group.node.className = 'line send folder ' + st;
      group.node.setAttribute('data-state', st);
      group.status.textContent = parts.join(' · ');
      group.status.className = st === 'error' ? 'warn' : '';
      group.meter.el.hidden = !live;
      group.meter.set(percent);
      group.retry.hidden = !!live || !failed || stopped;
      act(group, live ? 'cancel' : 'drop');
    }

    // --- Mes envois : ce qui est déjà sur le serveur ---
    function paintMine(res) {
      H.clear(mineBox); H.clear(space);
      var files = res.files;           // le serveur les rend du plus récent au plus ancien
      space.appendChild(h('p', { class: 'usedline' }, H.t('Espace utilisé : {used}', { used: sizeText(res.used) })));
      if (!files.length) mineBox.appendChild(h('p', { class: 'muted empty' }, H.t('Tu n’as encore rien envoyé.')));
      files.forEach(function (f, i) {
        mineBox.appendChild(h('div', { class: 'line mine-file', 'data-file': f.name, 'data-dir': f.dir },
          h('div', { class: 'what' }, h('b', null, f.name), h('span', null, (f.dir ? f.dir + ' · ' : '') + sizeText(f.size) + ' · ' + when(f.date))),
          H.button(H.t('Supprimer'), 'trash', { 'data-key': 'del-' + i, 'aria-label': H.t('Supprimer {name}', { name: pathOf(f) }), onclick: function () { remove(f, i); } })));
      });
    }
    function load() {
      return H.api('/api/inbox?mine=1').then(function (res) {
        if (!alive) return;
        if (res.extensions) { types = {}; res.extensions.forEach(function (x) { types[x] = true; }); }       // les types que le serveur accepte : l'interface écarte les autres avant le départ
        paintMine(res);
      }, function (e) {
        if (!alive) return;
        if (e.status === 403) { H.toast(H.t('Tu n’as pas le droit d’envoyer des fichiers.'), 5000); H.go('#/', true); } else H.toast(e.message, 5000);
      });
    }
    function refresh() {               // un fichier est arrivé : « Mes envois » le montre, sans relire toute la liste à chacun des fichiers d'un dossier
      if (busy()) {
        if (!loadTimer) loadTimer = setTimeout(function () { loadTimer = null; load(); }, 1500);
      } else {
        clearTimeout(loadTimer); loadTimer = null;
        load();
      }
    }
    function remove(f, i) {
      H.confirm({ title: H.t('Supprimer ce fichier ?'), ok: H.t('Supprimer'), danger: true,
        lead: [H.t('« {name} » sera supprimé du serveur, et cela ne s’annule pas.', { name: pathOf(f) })],
        submit: function () { return H.api(fileUrl(f), undefined, { method: 'DELETE' }); },
        done: function () {
          H.toast(H.t('Fichier supprimé : {name}', { name: pathOf(f) }));
          load().then(function () {
            var rows = mineBox.querySelectorAll('.mine-file .btn');
            H.focus.set(rows[Math.min(i, rows.length - 1)] || choose, true);
          });
        } });
    }

    // --- Glisser-déposer : toute la page reçoit les fichiers ---
    function hasFiles(e) { return !!(e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0); }
    el.addEventListener('dragenter', function (e) { if (hasFiles(e)) { e.preventDefault(); over++; zone.classList.add('over'); } });
    el.addEventListener('dragover', function (e) { if (hasFiles(e)) { e.preventDefault(); try { e.dataTransfer.dropEffect = 'copy'; } catch (x) { /* tant pis */ } } });
    el.addEventListener('dragleave', function (e) { if (hasFiles(e) && --over <= 0) { over = 0; zone.classList.remove('over'); } });
    el.addEventListener('drop', function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault(); over = 0; zone.classList.remove('over');
      var dt = e.dataTransfer, files = [], folders = [];
      for (var i = 0; i < dt.files.length; i++) {            // les entrées se lisent pendant l'événement : elles ne valent plus après
        var entry = dt.items && dt.items[i] && dt.items[i].webkitGetAsEntry ? dt.items[i].webkitGetAsEntry() : null;
        if (entry && entry.isDirectory) folders.push(entry); else files.push(dt.files[i]);
      }
      addFiles(files);
      folders.forEach(function (entry) {
        var found = [];
        walk(entry, entry.name, found, function () { offer(entry.name, found); });
      });
    });

    // --- Quitter la page pendant un envoi : on demande (le navigateur ici, le routeur de core.js pour les autres pages) ---
    function beforeUnload(e) {
      if (!busy()) return undefined;
      e.preventDefault();
      e.returnValue = '';
      return '';
    }
    window.addEventListener('beforeunload', beforeUnload);
    // Un fichier lâché à côté de la zone (la barre du haut) ferait ouvrir le fichier par le navigateur, et quitter la page : on l'en empêche tant que cette page est affichée.
    function stray(e) { if (el.style.display !== 'none' && hasFiles(e)) e.preventDefault(); }
    window.addEventListener('dragover', stray);
    window.addEventListener('drop', stray);

    el.appendChild(h('h1', null, H.t('Envoyer')));
    el.appendChild(h('p', { class: 'intro muted' }, H.t('Dépose ici des films, séries, musique ou livres pour l’administrateur. Ils vont dans une boîte de réception : rien n’entre dans la médiathèque avant qu’il les range.')));
    el.appendChild(zone);
    el.appendChild(expired);
    el.appendChild(sendPanel);
    el.appendChild(h('section', { class: 'panel' }, h('h2', null, H.t('Mes envois')), mineBox));
    load();
    return {
      el: el, nav: '#/upload', refresh: load,
      leave: function () {
        if (!busy()) return null;
        return { title: H.t('Quitter la page ?'), lead: [H.t('Des envois sont en cours ou en attente : si tu quittes cette page, ils seront annulés.')], ok: H.t('Quitter et annuler'), accept: cancelAll };
      },
      destroy: function () { alive = false; offers = []; clearTimeout(loadTimer); cancelAll(); window.removeEventListener('beforeunload', beforeUnload); window.removeEventListener('dragover', stray); window.removeEventListener('drop', stray); }
    };
  });

  // --- Paramètres > Boîte de réception (administrateur) --------------------------------
  // Tous les envois, du plus récent au plus ancien : on les télécharge ou on les supprime. Aucun bouton ne les range dans une bibliothèque (décision : docs/decisions.md).
  U.adminPanel = function () {
    var panel = h('section', { class: 'panel inbox' }), body = h('div');
    panel.appendChild(h('div', { class: 'phead' }, h('h2', null, H.t('Boîte de réception'))));
    panel.appendChild(h('p', { class: 'muted' }, H.t('Les fichiers que les comptes ont envoyés. Rien n’y est rangé dans une bibliothèque : télécharge-les, puis range-les toi-même.')));
    panel.appendChild(body);
    body.appendChild(h('p', { class: 'muted' }, H.t('Chargement…')));
    function show(files) {
      H.clear(body);
      if (!files.length) { body.appendChild(h('p', { class: 'muted empty' }, H.t('La boîte de réception est vide.'))); return; }
      var total = files.reduce(function (sum, f) { return sum + f.size; }, 0);
      body.appendChild(h('p', { class: 'muted total' }, H.plural(files.length, 'fichier') + ' · ' + sizeText(total)));
      files.forEach(function (f, i) {
        body.appendChild(h('div', { class: 'line inbox-file', 'data-file': f.name, 'data-account': f.account, 'data-dir': f.dir },
          h('div', { class: 'what' }, h('b', null, f.name), h('span', null, (f.deleted ? f.user + ' ' + H.t('(compte supprimé)') : f.user) + ' · ' + (f.dir ? f.dir + ' · ' : '') + sizeText(f.size) + ' · ' + when(f.date))),
          h('a', { class: 'btn f', href: fileUrl(f), 'data-key': 'inbox-get-' + i, 'aria-label': H.t('Télécharger {name}', { name: pathOf(f) }) }, H.icon('download'), h('span', { class: 'lbl' }, H.t('Télécharger'))),
          H.button(H.t('Supprimer'), 'trash', { 'data-key': 'inbox-del-' + i, 'aria-label': H.t('Supprimer {name}', { name: pathOf(f) }), onclick: function () { remove(f, i); } })));
      });
    }
    function load() {
      return H.api('/api/inbox').then(function (res) { show(res.files); }, function (e) {
        H.clear(body);
        body.appendChild(h('p', { class: 'muted', role: 'alert' }, e.message));
      });
    }
    function remove(f, i) {
      H.confirm({ title: H.t('Supprimer ce fichier ?'), ok: H.t('Supprimer'), danger: true,
        lead: [H.t('« {name} », envoyé par {user}, sera supprimé du serveur, et cela ne s’annule pas.', { name: pathOf(f), user: f.user })],
        submit: function () { return H.api(fileUrl(f), undefined, { method: 'DELETE' }); },
        done: function () {
          H.toast(H.t('Fichier supprimé : {name}', { name: pathOf(f) }));
          load().then(function () {          // le focus reste sur la ligne qui prend la place, sinon sur l'onglet
            var rest = body.querySelectorAll('.inbox-file button');
            if (rest.length) H.focus.set(rest[Math.min(i, rest.length - 1)], true); else H.focus.key(document, 'tab-inbox', true);
          });
        } });
    }
    load();
    return panel;
  };
})();
