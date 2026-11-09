/* Les envois de fichiers : la page « Envoyer » (un compte qui a le droit d'envoyer) et le panneau « Boîte de réception » de Paramètres (administrateur).
   Rien n'entre dans une bibliothèque : le serveur range les fichiers dans une boîte de réception, et l'administrateur les range lui-même (docs/decisions.md).
   Les fichiers partent un par un, par XMLHttpRequest (fetch ne rend pas la progression d'un envoi) : le serveur en accepte deux à la fois au plus et refuse le suivant en 429.
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
  function fileUrl(f) { return '/api/inbox/' + encodeURIComponent(f.account) + '/' + encodeURIComponent(f.name); }
  function meter(percent) {
    var bar = h('i');
    var box = h('div', { class: 'meter', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(percent) }, bar);
    bar.style.width = percent + '%';
    return { el: box, set: function (p) { bar.style.width = p + '%'; box.setAttribute('aria-valuenow', String(p)); } };
  }

  // --- La page « Envoyer » ----------------------------------------------------------
  H.route(/^\/upload$/, function () {
    var me = H.session.user;
    if (H.device === 'tv' || !me || !me.can_upload) {          // pas de page : retour à l'accueil, avec la raison
      var reason = H.device === 'tv' ? H.t('Envoyer des fichiers n’est pas possible sur une télévision.') : H.t('Tu n’as pas le droit d’envoyer des fichiers.');
      return { el: h('section', { class: 'blank' }, h('h1', null, H.t('Envoyer')), h('p', null, reason)), nav: '#/upload', shown: function () { H.toast(reason, 5000); H.go('#/', true); } };
    }
    var el = h('section', { class: 'upload' });
    var items = [], seq = 0, current = null, stopped = false, alive = true, over = 0;
    var list = h('div', { class: 'sends' });
    var sendPanel = h('section', { class: 'panel', hidden: true }, h('h2', null, H.t('Envois')), list);
    var expired = h('div', { class: 'panel', hidden: true, role: 'alert' }, h('p', { class: 'warn' }, H.t('Ta session a expiré : les envois sont arrêtés. Reconnecte-toi, puis recommence.')),
      h('a', { class: 'btn f', href: '/login', 'data-key': 'relogin' }, H.t('Se reconnecter')));
    var mineBox = h('div', { class: 'mine' }), space = h('div', { class: 'used' });
    var input = h('input', { type: 'file', multiple: true, style: 'display:none', tabindex: '-1', 'aria-hidden': 'true', onchange: function () {
      var files = Array.prototype.slice.call(input.files); input.value = ''; addFiles(files, 0);
    } });
    var choose = H.button(H.t('Choisir des fichiers'), 'upload', { class: 'primary', 'data-autofocus': '1', 'data-key': 'choose', onclick: function () { input.click(); } });
    var zone = h('div', { class: 'drop panel' },
      h('p', { class: 'dropline' }, H.device === 'phone' ? H.t('Choisis les fichiers à envoyer depuis cet appareil.') : H.t('Glisse des fichiers ici, ou choisis-les sur cet appareil.')),
      choose, input,
      h('p', { class: 'muted' }, H.t('Films, séries, musique, livres, sous-titres et archives .zip. Les autres types de fichiers sont refusés.')), space);

    function active(item) { return item.state === 'wait' || item.state === 'send'; }
    function busy() { return items.some(active); }
    // --- La file : un envoi à la fois ---
    function addFiles(files, folders) {
      if (folders) H.toast(H.t('Un dossier ne s’envoie pas : choisis les fichiers qu’il contient.'), 5000);
      files.forEach(function (file) {
        var item = { id: ++seq, file: file, name: file.name, total: file.size, state: 'wait', sent: 0, began: 0, kept: '', error: '', note: '', xhr: null, hold: null, button: null, mode: '' };
        if (stopped) { item.state = 'error'; item.error = H.t('Arrêté : la session a expiré.'); }
        items.push(item);
        list.appendChild(build(item));
        paint(item);
      });
      sendPanel.hidden = !items.length;
      pump();
    }
    function pump() {
      if (current || stopped || !alive) return;
      var next = items.filter(function (i) { return i.state === 'wait'; })[0];
      if (next && !next.hold) start(next);
    }
    function start(item) {
      var xhr = new XMLHttpRequest();
      current = item; item.xhr = xhr; item.state = 'send'; item.sent = 0; item.note = ''; item.began = Date.now();
      xhr.open('POST', '/api/inbox?name=' + encodeURIComponent(item.name));
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
        load();
      } else if (xhr.status === 401) {
        expire(item);
      } else if (xhr.status === 429) {                                    // d'autres envois occupent le serveur : le fichier reprend sa place dans la file, on réessaie plus tard
        item.state = 'wait'; item.note = H.t('Le serveur est occupé : nouvel essai dans quelques secondes.');
        item.hold = setTimeout(function () { item.hold = null; pump(); }, U.retry);
        release(item);
      } else {
        fail(item, data && data.error ? said(data.error) : H.t('Erreur {status}', { status: xhr.status }));
      }
    }
    function expire(item) {            // la session a pris fin : rien ne passera plus, la file s'arrête (les fichiers restent listés pour qu'on sache ce qui n'est pas parti)
      stopped = true;
      items.forEach(function (i) {
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
    function cancelAll() {          // d'abord ceux qui attendent : l'annulation de l'envoi en cours ne doit pas lancer le suivant
      items.forEach(function (i) { if (i.state === 'wait') cancel(i, true); });
      items.forEach(function (i) { if (i.state === 'send') cancel(i); });
    }
    function dismiss(item) {
      var at = items.indexOf(item), next = items[at + 1] || items[at - 1];
      var hadFocus = item.node.contains(document.activeElement);
      items.splice(at, 1);
      list.removeChild(item.node);
      sendPanel.hidden = !items.length;
      if (hadFocus) H.focus.set(next && next.button ? next.button : choose, true);
    }

    // --- L'affichage d'un envoi ---
    function build(item) {
      item.title = h('b'); item.status = h('span'); item.meter = meter(0); item.meter.el.hidden = true;
      item.node = h('div', { class: 'line send', 'data-send': String(item.id) }, h('div', { class: 'what' }, item.title, item.status, item.meter.el));
      return item.node;
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
    function paint(item) {
      var st = item.state, text = '', percent = 0;
      item.node.className = 'line send ' + st;
      item.node.setAttribute('data-state', st);
      item.title.textContent = st === 'done' && item.kept ? item.kept : item.name;
      if (st === 'wait') text = item.note || H.t('En attente');
      else if (st === 'send') { var p = progress(item); text = p.text; percent = p.percent; }
      else if (st === 'done') text = H.t('Envoyé · {size}', { size: sizeText(item.total) }) + (item.kept !== item.name ? ' · ' + H.t('enregistré sous ce nom (tu l’avais appelé « {name} »)', { name: item.name }) : '');
      else if (st === 'error') text = item.error;
      else text = H.t('Annulé');
      item.status.textContent = text;
      item.status.className = st === 'error' ? 'warn' : '';
      item.meter.el.hidden = st !== 'send';
      item.meter.set(percent);
      var mode = active(item) ? 'cancel' : 'drop';
      if (mode !== item.mode) {          // « Annuler » tant que l'envoi est à faire ou en cours, « Retirer de la liste » ensuite
        var had = item.button && item.node.contains(document.activeElement) && document.activeElement === item.button;
        var button = mode === 'cancel'
          ? H.button(H.t('Annuler'), 'close', { 'data-key': 'cancel-' + item.id, 'aria-label': H.t('Annuler l’envoi de {name}', { name: item.name }), onclick: function () { cancel(item); } })
          : H.button(H.t('Retirer de la liste'), null, { 'data-key': 'drop-' + item.id, 'aria-label': H.t('Retirer {name} de la liste', { name: item.name }), onclick: function () { dismiss(item); } });
        if (item.button) item.node.replaceChild(button, item.button); else item.node.appendChild(button);
        item.button = button; item.mode = mode;
        if (had) H.focus.set(button, true);
      }
    }

    // --- Mes envois : ce qui est déjà sur le serveur ---
    function paintMine(res) {
      H.clear(mineBox); H.clear(space);
      var files = res.files;           // le serveur les rend du plus récent au plus ancien
      space.appendChild(h('p', { class: 'usedline' }, H.t('Espace utilisé : {used}', { used: sizeText(res.used) })));
      if (!files.length) mineBox.appendChild(h('p', { class: 'muted empty' }, H.t('Tu n’as encore rien envoyé.')));
      files.forEach(function (f, i) {
        mineBox.appendChild(h('div', { class: 'line mine-file', 'data-file': f.name },
          h('div', { class: 'what' }, h('b', null, f.name), h('span', null, sizeText(f.size) + ' · ' + when(f.date))),
          H.button(H.t('Supprimer'), 'trash', { 'data-key': 'del-' + i, 'aria-label': H.t('Supprimer {name}', { name: f.name }), onclick: function () { remove(f, i); } })));
      });
    }
    function load() {
      return H.api('/api/inbox?mine=1').then(function (res) {
        if (!alive) return;
        paintMine(res);
      }, function (e) {
        if (!alive) return;
        if (e.status === 403) { H.toast(H.t('Tu n’as pas le droit d’envoyer des fichiers.'), 5000); H.go('#/', true); } else H.toast(e.message, 5000);
      });
    }
    function remove(f, i) {
      H.confirm({ title: H.t('Supprimer ce fichier ?'), ok: H.t('Supprimer'), danger: true,
        lead: [H.t('« {name} » sera supprimé du serveur, et cela ne s’annule pas.', { name: f.name })],
        submit: function () { return H.api(fileUrl(f), undefined, { method: 'DELETE' }); },
        done: function () {
          H.toast(H.t('Fichier supprimé : {name}', { name: f.name }));
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
      var dt = e.dataTransfer, files = [], folders = 0;
      for (var i = 0; i < dt.files.length; i++) {
        var entry = dt.items && dt.items[i] && dt.items[i].webkitGetAsEntry ? dt.items[i].webkitGetAsEntry() : null;
        if (entry && entry.isDirectory) folders++; else files.push(dt.files[i]);
      }
      addFiles(files, folders);
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
      destroy: function () { alive = false; cancelAll(); window.removeEventListener('beforeunload', beforeUnload); window.removeEventListener('dragover', stray); window.removeEventListener('drop', stray); }
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
        body.appendChild(h('div', { class: 'line inbox-file', 'data-file': f.name, 'data-account': f.account },
          h('div', { class: 'what' }, h('b', null, f.name), h('span', null, f.user + ' · ' + sizeText(f.size) + ' · ' + when(f.date))),
          h('a', { class: 'btn f', href: fileUrl(f), 'data-key': 'inbox-get-' + i, 'aria-label': H.t('Télécharger {name}', { name: f.name }) }, H.icon('download'), h('span', { class: 'lbl' }, H.t('Télécharger'))),
          H.button(H.t('Supprimer'), 'trash', { 'data-key': 'inbox-del-' + i, 'aria-label': H.t('Supprimer {name}', { name: f.name }), onclick: function () { remove(f, i); } })));
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
        lead: [H.t('« {name} », envoyé par {user}, sera supprimé du serveur, et cela ne s’annule pas.', { name: f.name, user: f.user })],
        submit: function () { return H.api(fileUrl(f), undefined, { method: 'DELETE' }); },
        done: function () {
          H.toast(H.t('Fichier supprimé : {name}', { name: f.name }));
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
