/* La page Administration (#/admin), réservée à l'administrateur : la médiathèque (bibliothèques, sagas, entretien, tâches), les demandes, les suggestions, la boîte de réception, les comptes et le serveur.
   Paramètres (#/settings, views.js) ne garde que ce qui concerne le compte connecté. Les routes sont protégées côté serveur par le rôle : cette page ne fait que les rassembler. */
(function () {
  'use strict';
  var H = window.Hub, h = H.h, R = H.requests, S = H.suggestions;

  var TABS = [{ id: 'mediatheque', label: H.t('Médiathèque') }, { id: 'demandes', label: H.t('Demandes') }, { id: 'suggestions', label: H.t('Suggestions') }, { id: 'inbox', label: H.t('Boîte de réception') },
    { id: 'comptes', label: H.t('Comptes') }, { id: 'serveur', label: H.t('Serveur') }];
  var OLD_TABS = { gestion: 'mediatheque', taches: 'mediatheque' };          // les anciens noms d'onglets : les adresses et le choix mémorisé y mènent encore

  H.route(/^\/admin$/, function (m, query) {
    // Un autre compte n'a rien à faire ici : il retombe sur ses paramètres (l'ancienne adresse `#/admin` y menait, ses signets aussi).
    if (!H.isAdmin()) return { el: h('section', { class: 'blank' }, h('h1', null, H.t('Administration'))), nav: '#/settings', shown: function () { H.go('#/settings', true); } };
    var el = h('section', { class: 'admin' }), timer = null, wasBusy = false, busy = false, state = null, jobs = [], users = null, usersError = '', logPanel = null, lastPage = '', lastTasks = '', closed = false;
    var tab = query.tab || H.pref('admin.tab') || '', openJobs = {};
    function start(path, body, text) {
      return H.api(path, body).then(function () { H.toast(text); poll(); }).catch(function (e) { H.toast(e.message, 5000); });
    }
    function activeTab() {
      var wanted = OLD_TABS[tab] || tab;
      return TABS.some(function (t) { return t.id === wanted; }) ? wanted : TABS[0].id;
    }
    function choose(id) {
      tab = id; H.pref('admin.tab', id);
      if (id === 'demandes' && !reqLoading) { reqList = null; reqError = ''; }          // l'onglet se relit à chaque fois qu'on y entre : pas de liste périmée
      if (id === 'suggestions' && !sgLoading) { sgList = null; sgError = ''; }
      render(); H.focus.key(el, 'tab-' + id);
    }
    function tabBar() {
      var active = activeTab();
      return h('div', { class: 'tabs', role: 'tablist' }, TABS.map(function (t) {
        return h('button', { class: 'chip tab f' + (t.id === active ? ' on' : '') + (t.id === 'mediatheque' && busy ? ' busy' : ''), role: 'tab', 'aria-selected': t.id === active ? 'true' : 'false', 'data-key': 'tab-' + t.id,
          'data-autofocus': t.id === active ? '1' : null, onclick: function () { choose(t.id); } }, t.label,
          t.id === 'demandes' && H.session.pending > 0 ? h('span', { class: 'pend', 'aria-label': H.t('{n} en attente', { n: H.plural(H.session.pending, 'demande') }) }, String(H.session.pending)) : null,
          t.id === 'suggestions' && H.session.suggestions > 0 ? h('span', { class: 'pend', 'aria-label': H.t('{n} en attente', { n: H.plural(H.session.suggestions, 'suggestion') }) }, String(H.session.suggestions)) : null);
      }));
    }
    function render() {
      var focus = document.activeElement && el.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null;
      var kept = logStates(logPanel);
      H.clear(el);
      logPanel = null;
      el.appendChild(h('h1', null, H.t('Administration')));
      var bar = tabBar();
      el.appendChild(bar);
      var body = h('div', { class: 'tabbody' }), active = activeTab();
      if (active === 'mediatheque') mediatheque(body);
      else if (active === 'inbox') body.appendChild(H.upload.adminPanel());
      else if (active === 'demandes') requestsTab(body);
      else if (active === 'suggestions') suggestionsTab(body);
      else if (active === 'comptes') body.appendChild(usersPanel());
      else serverFacts(body);
      el.appendChild(body);
      if (logPanel) logRestore(logPanel, kept);          // un journal lu à mi-chemin le reste quand la page est refaite (une tâche qui se termine)
      var on = bar.querySelector('.on');
      if (on) bar.scrollLeft = Math.max(0, on.offsetLeft - 24);       // sur un téléphone, la rangée défile : l'onglet ouvert doit s'y voir (après le contenu : lire offsetLeft sur une page à moitié reconstruite la ramenait en haut)
      if (H.overlay.isOpen()) return;                                   // une fenêtre de saisie est ouverte : on ne lui prend pas le focus
      if (!(focus && H.focus.key(el, focus, true))) H.focus.first();   // sans défilement
    }

    // --- Médiathèque : les bibliothèques, les sagas, l'entretien, puis les tâches que ces boutons lancent ---------------
    function refreshCovers(l) {
      H.api('/api/images/refresh', { lib: l.id }).then(function (r) { H.toast(r.removed > 1 ? H.t('Covers à relire : {name} ({n} vignettes oubliées)', { name: H.libLabel(l), n: r.removed }) : H.t('Covers à relire : {name} ({n} vignette oubliée)', { name: H.libLabel(l), n: r.removed })); })
        .catch(function (e) { H.toast(e.message, 5000); });
    }
    // Les films des personnes d'une bibliothèque de films ou de séries : une tâche de fond par bibliothèque, qui n'a de sens qu'avec une clé TMDB.
    // Pour des films, c'est « Films manquants » ; pour des séries, les identifiants des acteurs viennent de « Enrichir (TMDB) ».
    function categoryButton(l) {
      if (!state.tmdb || (l.kind !== 'movies' && l.kind !== 'shows')) return null;
      var shows = l.kind === 'shows';
      return H.button(shows ? H.t('Films des personnes (TMDB)') : H.t('Films manquants'), null, { 'data-key': 'people-' + l.id, title: shows ? H.t('Demande à TMDB les films des acteurs de cette bibliothèque, pour voir sur la page de chacun ceux qui ne sont pas dans ta médiathèque. Les identifiants viennent de « Enrichir (TMDB) » : lance-le d’abord une fois.') : H.t('Demande à TMDB les films des réalisateurs et des acteurs de cette bibliothèque, pour voir sur la page de chacun ceux qui ne sont pas dans ta médiathèque. Compte une heure ou plus la première fois (un appel par personne, puis les affiches) ; une personne déjà connue depuis moins d’un mois n’est pas redemandée.'),
        onclick: function () { start('/api/enrich/people', { lib: l.id }, H.t('Recherche des films manquants lancée : {name}', { name: H.libLabel(l) })); } });
    }
    // Les bibliothèques, chacune sur sa ligne avec ses boutons : toujours affichées (plus de panneau à déplier), « Tout mettre à jour » en tête.
    function libsPanel() {
      var libs = h('section', { class: 'panel' }, h('div', { class: 'phead' }, h('h2', null, H.t('Bibliothèques')),
        H.button(H.t('Tout mettre à jour'), 'refresh', { class: 'primary', 'data-key': 'scan-all', title: H.t('Relit les cinq bibliothèques ; seuls les fichiers modifiés sont relus.'), onclick: function () { start('/api/scan', {}, H.t('Mise à jour lancée')); } })));
      state.libraries.forEach(function (l) {
        libs.appendChild(h('div', { class: 'line', 'data-lib': l.id },
          h('div', { class: 'what' }, h('b', null, H.libLabel(l)), h('span', null, l.exists ? H.plural(l.count, 'élément') + ' · ' + H.t('mis à jour {when}', { when: H.ago(l.scanned) }) : H.t('dossier introuvable'))),
          l.kind === 'shows' ? H.button(H.t('Enrichir (TMDB)'), null, { 'data-key': 'enrich-' + l.id, onclick: function () { start('/api/enrich', { lib: l.id }, H.t('Enrichissement lancé : {name}', { name: H.libLabel(l) })); } }) : null,
          l.kind === 'movies' ? H.button(H.t('Sagas (TMDB)'), null, { 'data-key': 'enrich-' + l.id, title: H.t('Cherche chez TMDB les films qui manquent à tes sagas, et la note de chaque film.'), onclick: function () { start('/api/enrich', { lib: l.id }, H.t('Recherche des films manquants lancée : {name}', { name: H.libLabel(l) })); } }) : null,
          categoryButton(l),
          l.kind === 'music' ? H.button(H.t('Fiches artistes'), null, { 'data-key': 'enrich-' + l.id, onclick: function () { start('/api/enrich', { lib: l.id }, H.t('Fiches artistes demandées : {name}', { name: H.libLabel(l) })); } }) : null,
          l.kind === 'movies' ? H.button(H.t('Relire les covers'), null, { 'data-key': 'covers-' + l.id, title: H.t('Vide les vignettes gardées : chacune se refait depuis folder.jpg ou la jaquette du .mkv. Les affiches choisies à la main restent.'), onclick: function () { refreshCovers(l); } }) : null,
          H.button(H.t('Mettre à jour'), 'refresh', { 'data-key': 'scan-' + l.id, disabled: false, onclick: function () { start('/api/scan', { lib: l.id }, H.t('Mise à jour lancée : {name}', { name: H.libLabel(l) })); } }),
          H.button(H.t('Renommer'), 'edit', { 'data-key': 'rename-' + l.id, 'aria-label': H.t('Renommer la bibliothèque {name}', { name: H.libLabel(l) }), onclick: function () { renameLibrary(l); } })));
      });
      return libs;
    }
    // Change le nom AFFICHÉ d'une bibliothèque (jamais son identifiant) ; un champ vide, ou « Rétablir », rend le nom d'origine. Le serveur nettoie le nom et refuse un doublon : son message s'affiche dans la fenêtre, qui reste ouverte.
    function renameLibrary(l) {
      var current = l.label, renamed = H.libRenamed(l);
      function save(text) { return H.api('/api/library/' + encodeURIComponent(l.id) + '/label', { label: text }); }
      H.form({ title: H.t('Renommer la bibliothèque « {name} »', { name: H.libLabel(l) }), ok: H.t('Enregistrer'),
        lead: [H.t('Seul le nom affiché change : les droits, la progression et les fichiers restent attachés à la bibliothèque.')],
        fields: [{ key: 'label', label: H.t('Nom affiché (40 caractères au plus)'), type: 'text', maxlength: 40, value: current }],
        alt: renamed ? { label: H.t('Rétablir « {name} »', { name: H.tr(l.default_label) }), submit: function () { return save(''); } } : null,
        submit: function (v) { return v.label === current || (!v.label && !renamed) ? Promise.resolve(null) : save(v.label); },          // rien à changer : on ferme sans rien demander au serveur
        done: function (res) {
          if (!res) return;
          H.toast(H.libRenamed(res) ? H.t('Bibliothèque renommée : {name}', { name: res.label }) : H.t('Nom d’origine rétabli : {name}', { name: H.libLabel(res) }));
          poll().then(function () { H.focus.key(el, 'rename-' + l.id); });          // relit /api/state : la barre, les pages et la grille des droits prennent le nouveau nom (H.setLibs)
        } });
    }
    function mediatheque(body) {
      body.appendChild(libsPanel());
      body.appendChild(h('section', { class: 'panel' }, h('h2', null, H.t('Sagas')), h('div', { class: 'line' },
        h('div', { class: 'what' }, h('b', null, H.t('Sagas composées à la main')), h('span', null, H.t('Une trilogie que TMDB ne réunit pas, l’ordre du MCU… : des listes de films dans l’ordre de ton choix, en plus de la saga de l’étiquette.'))),
        h('a', { class: 'btn f', href: '#/sagas', 'data-key': 'sagas' }, H.icon('edit'), h('span', { class: 'lbl' }, H.t('Composer'))))));

      var tools = h('section', { class: 'panel' }, h('h2', null, H.t('Entretien')));
      tools.appendChild(h('div', { class: 'line' }, h('div', { class: 'what' }, h('b', null, H.t('Fichiers parasites')), h('span', null, H.t('Thumbs.db, desktop.ini, .DS_Store et les AlbumArt_{…}_Large / _Small.jpg semés par Windows et macOS ; en musique, toute image autre que cover.jpg (ou .png…), la seule pochette d’un album, y compris folder.jpg.'))),
        H.button(H.t('Supprimer'), null, { 'data-key': 'junk-del', onclick: confirmCleanup })));
      body.appendChild(tools);
      logPanel = jobsPanel();
      body.appendChild(logPanel);
    }
    // Supprimer les fichiers parasites ne s'annule pas : on demande d'abord.
    // « Compter » lance un comptage depuis la fenêtre et en affiche le nombre sans la fermer ; rien n'est affiché avant.
    function confirmCleanup() {
      var counting = false;
      function said(found) { return H.t('Fichiers trouvés : {n}.', { n: found }); }
      var count = h('p', { class: 'lead', role: 'status', hidden: true });
      var sample = h('ul', { class: 'sample', 'aria-label': H.t('Les premiers fichiers qui seraient supprimés') });
      function listed(result) {          // les premiers fichiers que la simulation a nommés, et combien d'autres
        H.clear(sample);
        var names = (result && result.sample) || [];
        names.forEach(function (name) { sample.appendChild(h('li', null, name)); });
        if (result && result.found > names.length) sample.appendChild(h('li', { class: 'more' }, H.t('… et {n} autre(s)', { n: result.found - names.length })));
      }
      var recount = H.button(H.t('Compter'), null, { 'data-key': 'junk-recount', 'aria-label': H.t('Compter de nouveau les fichiers parasites'), onclick: function () {
        if (counting) return;
        counting = true; count.hidden = false; count.textContent = H.t('Comptage en cours…'); listed(null);
        function wait(id) {
          return H.api('/api/jobs').then(function (r) {
            var job = r.jobs.filter(function (j) { return j.id === id; })[0];
            if (job && (job.status === 'waiting' || job.status === 'running')) return new Promise(function (go) { setTimeout(function () { go(wait(id)); }, 700); });
            return job;
          });
        }
        H.api('/api/cleanup', {}).then(function (r) { return wait(r.job.id); }).then(function (job) {
          count.textContent = job && job.status === 'done' && job.result ? said(job.result.found) : H.t('Le comptage a échoué : voir les Tâches, en bas de l’onglet Médiathèque.');
          if (job && job.status === 'done') listed(job.result);
        }, function (e) { count.textContent = e.message; }).then(function () { counting = false; poll(); });
      } });
      H.confirm({ title: H.t('Supprimer les fichiers parasites ?'), ok: H.t('Supprimer définitivement'), danger: true,
        lead: [H.t('Les fichiers parasites seront supprimés de la médiathèque, et cela ne s’annule pas.')], extra: [count, sample, recount],
        submit: function () { return H.api('/api/cleanup', { apply: true }); },
        done: function () { H.toast(H.t('Nettoyage lancé')); poll(); } });
    }

    // --- Tâches : le journal des tâches de fond, relu toutes les 1,5 s tant qu'une tâche tourne ---------------------
    function jobsPanel() {
      var all = jobs.length > 0 && jobs.every(function (j) { return openJobs[j.id]; });
      var log = h('section', { class: 'panel' }, h('div', { class: 'phead' }, h('h2', null, H.t('Tâches'), busy ? h('span', { class: 'busy' }, '…') : null),
        jobs.length ? H.button(all ? H.t('Tout replier') : H.t('Tout déplier'), null, { 'data-key': 'jobs-all', onclick: function () { jobs.forEach(function (j) { openJobs[j.id] = !all; }); showJobs('jobs-all'); } }) : null));
      if (!jobs.length) log.appendChild(h('p', { class: 'muted' }, H.t('Aucune tâche pour l’instant.')));
      jobs.forEach(function (j) {
        var status = { waiting: H.t('en attente'), running: H.t('en cours'), done: H.t('terminée'), failed: H.t('échec') }[j.status], open = !!openJobs[j.id];
        var node = h('div', { class: 'job ' + j.status, 'data-job': j.id }, h('button', { class: 'jhead f', 'aria-expanded': open ? 'true' : 'false', 'data-key': 'job-' + j.id,
          onclick: function () { openJobs[j.id] = !open; showJobs('job-' + j.id); } }, h('span', { class: 'jt' }, h('b', null, H.tr(j.label)), h('span', null, status))));
        if (open) node.appendChild(logText(j) ? h('pre', null, logText(j)) : h('p', { class: 'muted' }, H.t('Rien n’a été noté.')));
        log.appendChild(node);
      });
      return log;
    }
    function logText(j) { return (j.log || []).map(function (line) { return H.tr(line); }).join('\n'); }
    // Le défilement d'un journal : où il en est, et s'il était lu jusqu'en bas (alors il suit la fin).
    function logState(pre) { return { top: pre.scrollTop, end: pre.scrollHeight - pre.scrollTop - pre.clientHeight < 4 }; }
    function logKeep(pre, was) { pre.scrollTop = was.end ? pre.scrollHeight : was.top; }
    function logStates(panel) {          // le défilement de chaque journal ouvert du panneau, par tâche : à rendre au panneau qui le remplace
      var kept = {};
      if (panel) Array.prototype.forEach.call(panel.querySelectorAll('.job pre'), function (pre) { kept[pre.parentNode.getAttribute('data-job')] = logState(pre); });
      return kept;
    }
    function logRestore(panel, kept) {
      Array.prototype.forEach.call(panel.querySelectorAll('.job pre'), function (pre) { var was = kept[pre.parentNode.getAttribute('data-job')]; if (was) logKeep(pre, was); });
    }
    // Les journaux grossissent toutes les 1,5 s : on ne change que le texte de ceux qui ont changé, sans toucher au reste (focus, défilement, sélection).
    // Faux (rien n'a été modifié) si autre chose que des journaux a changé : une tâche ajoutée ou finie, un journal qui apparaît.
    function patchLogs() {
      var nodes = logPanel.querySelectorAll('.job'), todo = [];
      if (nodes.length !== jobs.length) return false;
      for (var i = 0; i < jobs.length; i++) {
        var j = jobs[i], node = nodes[i], pre = node.querySelector('pre'), text = logText(j);
        if (node.getAttribute('data-job') !== String(j.id) || node.className !== 'job ' + j.status) return false;
        if (openJobs[j.id] && !!pre !== !!text) return false;
        if (pre && pre.textContent !== text) todo.push([pre, text]);
      }
      todo.forEach(function (t) { var was = logState(t[0]); grow(t[0], t[1]); logKeep(t[0], was); });
      return true;
    }
    function grow(pre, text) {          // un journal ne fait que s'allonger : on ajoute la fin, et ce qu'on y a sélectionné reste sélectionné (sinon le texte est remplacé)
      var old = pre.textContent;
      if (pre.childNodes.length === 1 && pre.firstChild.nodeType === 3 && text.slice(0, old.length) === old) pre.firstChild.appendData(text.slice(old.length));
      else pre.textContent = text;
    }
    // Remplace le panneau des tâches sur place (la page garde son défilement), en rendant le focus à ce qui l'avait.
    function showJobs(key) {
      var focus = key || (document.activeElement && logPanel && logPanel.contains(document.activeElement) ? document.activeElement.getAttribute('data-key') : null);
      var kept = logStates(logPanel), fresh = jobsPanel();
      logPanel.parentNode.replaceChild(fresh, logPanel);
      logPanel = fresh;
      logRestore(fresh, kept);
      if (focus) H.focus.key(fresh, focus, true);
    }

    // --- Demandes : toutes les demandes, filtrées par état, avec leurs demandeurs et leurs messages ----------------------
    // Lues à l'ouverture de l'onglet et après chaque action ; le compteur de la barre et de l'onglet suit (/api/state relu par poll).
    var reqList = null, reqError = '', reqFilter = 'all', reqLoading = false, reqToken = 0;
    var REQ_FILTERS = [['all', H.t('Toutes')], ['pending', H.t('En attente')], ['added', H.t('Ajoutées')], ['declined', H.t('Refusées')]];
    // Relit la liste, puis met le focus sur `key`, à défaut sur `fallback` (une ligne voisine, quand celle qu'on vient de traiter sort du filtre), à défaut sur le filtre.
    // Une réponse arrivée après une autre lecture (un filtre changé entre-temps) est ignorée.
    function loadRequests(key, fallback) {
      var mine = ++reqToken;
      reqLoading = true;
      return H.api('/api/requests' + (reqFilter !== 'all' ? '?status=' + reqFilter : '')).then(function (res) { return { list: res.requests || [], error: '' }; }, function (e) { return { list: null, error: e.message }; }).then(function (got) {
        if (mine !== reqToken) return;
        reqLoading = false;
        reqList = got.list; reqError = got.error;
        if (closed || activeTab() !== 'demandes') return;
        render();
        if (key && !H.focus.key(el, key, true) && !(fallback && H.focus.key(el, fallback, true))) H.focus.key(el, 'rf-' + reqFilter, true);
      });
    }
    function afterRequest(key, fallback) { return loadRequests(key, fallback).then(function () { return poll(); }); }
    function requestsTab(body) {
      if (reqList === null && !reqError && !reqLoading) loadRequests();
      var panel = h('section', { class: 'panel requests' }, h('h2', null, H.t('Demandes')),
        h('p', { class: 'muted' }, H.t('Ce que les comptes ont demandé. Le hub ne télécharge rien : ajoute le film à la médiathèque, il passe « Ajouté » tout seul s’il porte son identifiant TMDB ; sinon, marque-le à la main.')));
      panel.appendChild(h('div', { class: 'chips', role: 'group', 'aria-label': H.t('Filtrer les demandes') }, REQ_FILTERS.map(function (f) {
        var on = reqFilter === f[0];
        return h('button', { class: 'chip f' + (on ? ' on' : ''), type: 'button', 'data-key': 'rf-' + f[0], 'aria-pressed': on ? 'true' : 'false',
          onclick: function () { if (reqFilter === f[0]) return; reqFilter = f[0]; reqList = null; reqError = ''; loadRequests('rf-' + f[0]); } }, f[1]);
      })));
      if (reqError) panel.appendChild(h('p', { class: 'muted' }, reqError, ' ', h('button', { class: 'btn f', type: 'button', 'data-key': 'rq-retry', onclick: function () { reqList = null; reqError = ''; loadRequests('rf-' + reqFilter); } }, H.t('Réessayer'))));
      else if (!reqList) panel.appendChild(h('p', { class: 'muted' }, H.t('Chargement…')));
      else if (!reqList.length) panel.appendChild(h('p', { class: 'muted reqempty' }, reqFilter === 'all' ? H.t('Aucune demande pour l’instant : un compte demande un film depuis sa case grisée (page d’une saga, d’une personne).') : H.t('Aucune demande dans cet état.')));
      (reqList || []).forEach(function (r) { panel.appendChild(adminRequestRow(r)); });
      body.appendChild(panel);
    }
    // Une demande : titre, état, demandeurs (nom, date, message), et ce qu'on peut en faire. Noms et messages viennent des comptes : toujours en texte.
    function adminRequestRow(r) {
      var who = r.users || [];
      var list = h('ul', { class: 'rqusers' }, who.map(function (u) {
        return h('li', null, h('b', null, u.name || H.t('compte supprimé')), ' · ' + H.date(u.created), u.note ? [' · ', h('q', null, u.note)] : null);
      }));
      var what = h('div', { class: 'what' }, h('b', null, R.title(r)), h('span', { class: 'rqstatus' }, R.status(r)),
        h('span', null, H.plural(who.length, 'demandeur') + ' · ' + H.t('demandée le {date}', { date: H.date(r.created) })), who.length ? list : null);
      var buttons = [];
      if (r.item) buttons.push(R.open(r));
      if (r.status === 'pending') {
        buttons.push(H.button(H.t('Marquer ajouté'), 'check', { 'data-key': 'rq-added-' + r.id, onclick: function () { setRequest(r, 'added'); } }));
        buttons.push(H.button(H.t('Refuser'), 'close', { 'data-key': 'rq-declined-' + r.id, onclick: function () { declineRequest(r); } }));
      } else buttons.push(H.button(H.t('Remettre en attente'), 'refresh', { 'data-key': 'rq-pending-' + r.id, onclick: function () { setRequest(r, 'pending'); } }));
      buttons.push(H.button(H.t('Supprimer'), 'trash', { 'data-key': 'rq-del-' + r.id, 'aria-label': H.t('Supprimer la demande de « {title} »', { title: R.title(r) }), onclick: function () { deleteRequest(r); } }));
      return h('div', { class: 'line reqrow', 'data-request': r.id, 'data-status': r.status }, R.picture(r), what, buttons);
    }
    // Le bouton qui remplace celui qu'on vient d'utiliser : « Marquer ajouté » et « Refuser » deviennent « Remettre en attente », et l'inverse.
    function setRequest(r, status) {
      var next = R.neighbour(reqList || [], r, 'rq-del-');          // la ligne peut sortir du filtre : le focus va alors à sa voisine
      H.api('/api/requests/' + r.id + '/status', { status: status }).then(function () { return afterRequest(status === 'pending' ? 'rq-added-' + r.id : 'rq-pending-' + r.id, next); })
        .catch(function (e) { H.toast(e.message, 5000); });
    }
    function declineRequest(r) {
      H.form({ title: H.t('Refuser « {title} »', { title: R.title(r) }), ok: H.t('Refuser'),
        lead: [H.t('Le motif est montré à ceux qui l’ont demandé. Tant que la demande est refusée, personne ne peut la refaire.')],
        fields: [{ key: 'reason', label: H.t('Motif (facultatif, 200 caractères au plus)'), maxlength: 200 }],
        submit: function (v) { return H.api('/api/requests/' + r.id + '/status', { status: 'declined', reason: v.reason }); },
        done: function () { afterRequest('rq-pending-' + r.id, R.neighbour(reqList || [], r, 'rq-del-')); } });
    }
    function deleteRequest(r) {
      H.confirm({ title: H.t('Supprimer la demande « {title} » ?', { title: R.title(r) }), ok: H.t('Supprimer'), danger: true,
        lead: [H.t('Elle disparaît pour tous ceux qui l’ont faite ({n}), avec leurs messages. Cela ne s’annule pas.', { n: H.plural((r.users || []).length, 'demandeur') })],
        submit: function () { return H.api('/api/requests/' + r.id + '/delete', {}).catch(R.gone); },
        done: function () { H.toast(H.t('Demande supprimée : {title}', { title: R.title(r) })); afterRequest(R.neighbour(reqList || [], r, 'rq-del-') || 'rf-' + reqFilter); } });
    }

    // --- Suggestions : les idées et les remarques des comptes sur le hub, filtrées par état et par catégorie ------------------
    // Lues à l'ouverture de l'onglet et après chaque action ; la pastille de la barre et de l'onglet suit (/api/state relu par poll).
    var sgList = null, sgError = '', sgStatus = 'all', sgCategory = 'all', sgLoading = false, sgToken = 0;
    function loadSuggestions(key, fallback) {
      var mine = ++sgToken, query = [];
      if (sgStatus !== 'all') query.push('status=' + sgStatus);
      if (sgCategory !== 'all') query.push('category=' + sgCategory);
      sgLoading = true;
      return H.api('/api/suggestions' + (query.length ? '?' + query.join('&') : '')).then(function (res) { return { list: res.suggestions || [], error: '' }; }, function (e) { return { list: null, error: e.message }; }).then(function (got) {
        if (mine !== sgToken) return;
        sgLoading = false;
        sgList = got.list; sgError = got.error;
        if (closed || activeTab() !== 'suggestions') return;
        render();
        if (key && !H.focus.key(el, key, true) && !(fallback && H.focus.key(el, fallback, true))) H.focus.key(el, 'sf-' + sgStatus, true);
      });
    }
    function afterSuggestion(key, fallback) { return loadSuggestions(key, fallback).then(function () { return poll(); }); }
    function suggestionFilters(pairs, current, label, prefix, pick) {
      return h('div', { class: 'chips', role: 'group', 'aria-label': label }, [['all', prefix === 'sf-' ? H.t('Toutes') : H.t('Toutes catégories')]].concat(pairs).map(function (f) {
        var on = current === f[0];
        return h('button', { class: 'chip f' + (on ? ' on' : ''), type: 'button', 'data-key': prefix + f[0], 'aria-pressed': on ? 'true' : 'false',
          onclick: function () { if (current === f[0]) return; pick(f[0]); sgList = null; sgError = ''; loadSuggestions(prefix + f[0]); } }, f[1]);
      }));
    }
    function suggestionsTab(body) {
      if (sgList === null && !sgError && !sgLoading) loadSuggestions();
      var panel = h('section', { class: 'panel suggestions' }, h('h2', null, H.t('Suggestions')),
        h('p', { class: 'muted' }, H.t('Les idées et les remarques des comptes sur le hub. Chacune reste entre son auteur et toi : il voit l’état que tu lui donnes et ta réponse.')));
      panel.appendChild(suggestionFilters(S.statuses, sgStatus, H.t('Filtrer les suggestions par état'), 'sf-', function (v) { sgStatus = v; }));
      panel.appendChild(suggestionFilters(S.categories, sgCategory, H.t('Filtrer les suggestions par catégorie'), 'sc-', function (v) { sgCategory = v; }));
      if (sgError) panel.appendChild(h('p', { class: 'muted' }, sgError, ' ', h('button', { class: 'btn f', type: 'button', 'data-key': 'sg-retry', onclick: function () { sgList = null; sgError = ''; loadSuggestions('sf-' + sgStatus); } }, H.t('Réessayer'))));
      else if (!sgList) panel.appendChild(h('p', { class: 'muted' }, H.t('Chargement…')));
      else if (!sgList.length) panel.appendChild(h('p', { class: 'muted sgempty' }, sgStatus === 'all' && sgCategory === 'all' ? H.t('Aucune suggestion pour l’instant : un compte en propose une depuis Paramètres > Mon compte > Mes suggestions.') : H.t('Aucune suggestion ne correspond à ces filtres.')));
      (sgList || []).forEach(function (s) { panel.appendChild(adminSuggestionRow(s)); });
      body.appendChild(panel);
    }
    // Une suggestion : catégorie, état, texte, réponse, auteur et date, puis Répondre et Supprimer. Le nom et le texte viennent des comptes : toujours en texte.
    // Sur télévision, pas de « Répondre » : la réponse est un texte long (H.canSuggest).
    function adminSuggestionRow(s) {
      var who = (s.user && s.user.name) || H.t('compte supprimé');
      return h('div', { class: 'line sgrow', 'data-suggestion': s.id, 'data-status': s.status },
        S.what(s, h('span', null, H.t('{name} · proposée le {date}', { name: who, date: H.date(s.created) }))),
        H.canSuggest() ? H.button(H.t('Répondre'), 'edit', { 'data-key': 'sg-answer-' + s.id, 'aria-label': H.t('Répondre à la suggestion de {name}', { name: who }), onclick: function () { answer(s, who); } }) : null,
        H.button(H.t('Supprimer'), 'trash', { 'data-key': 'sg-del-' + s.id, 'aria-label': H.t('Supprimer la suggestion de {name}', { name: who }), onclick: function () { deleteSuggestion(s, who); } }));
    }
    // Donner un état et, si on veut, une réponse que l'auteur lira. La réponse d'avant est préremplie : la vider l'efface.
    function answer(s, who) {
      var status = S.choice(S.statuses, s.status, H.t('État'), 'sg-st-');
      H.form({ title: H.t('Suggestion de {name}', { name: who }), ok: H.t('Enregistrer'), cls: 'sganswer',
        lead: [s.text],
        fields: [{ key: 'reply', label: H.t('Ta réponse (facultative, 500 caractères au plus)'), type: 'textarea', maxlength: 500, value: s.reply || '' }],
        extra: [status.el],
        submit: function (v) { return H.api('/api/suggestions/' + s.id + '/status', { status: status.current(), reply: v.reply }); },
        done: function () { H.toast(H.t('Suggestion enregistrée')); afterSuggestion('sg-answer-' + s.id, neighbour(s)); } });
    }
    function neighbour(s) {
      var list = sgList || [], i = list.indexOf(s), next = list[i + 1] || list[i - 1];
      return next ? (H.canSuggest() ? 'sg-answer-' : 'sg-del-') + next.id : null;
    }
    function deleteSuggestion(s, who) {
      H.confirm({ title: H.t('Supprimer la suggestion de {name} ?', { name: who }), ok: H.t('Supprimer'), danger: true,
        lead: [H.t('Elle disparaît aussi pour son auteur, avec ta réponse. Cela ne s’annule pas.')],
        submit: function () { return H.api('/api/suggestions/' + s.id + '/delete', {}).catch(R.gone); },
        done: function () { H.toast(H.t('Suggestion supprimée')); afterSuggestion(neighbour(s) || 'sf-' + sgStatus); } });
    }

    // --- Comptes : la liste des comptes, leurs droits, leurs mots de passe (serveur avec comptes) -----------------------
    function usersPanel() {
      if (!H.session.auth) return h('section', { class: 'panel' }, h('h2', null, H.t('Comptes')), H.openServerNote());
      var panel = h('section', { class: 'panel' }, h('div', { class: 'phead' }, h('h2', null, H.t('Comptes')),
        H.button(H.t('Ajouter un compte'), 'plus', { 'data-key': 'user-add', onclick: addUser })));
      if (usersError) panel.appendChild(h('p', { class: 'muted' }, usersError));
      else if (!users) panel.appendChild(h('p', { class: 'muted' }, H.t('Chargement…')));
      (users || []).forEach(function (u) {
        var mine = H.session.user && u.id === H.session.user.id;
        panel.appendChild(h('div', { class: 'line', 'data-user': u.id }, h('div', { class: 'what' }, h('b', null, u.name + (mine ? ' ' + H.t('(toi)') : '')),
          h('span', null, (H.roles[u.role] || u.role) + ' · ' + H.t('créé le {date}', { date: H.date(u.created) })), u.role === 'admin' ? h('span', { 'data-upload': u.id }, H.t('Peut envoyer des fichiers : toujours') + ' · ' + H.t('Peut demander : toujours')) : h('span', { 'data-rights': u.id }, rightsSummary(u))),
          u.role === 'admin' ? null : H.button(H.t('Droits'), 'edit', { 'data-key': 'rights-' + u.id, 'aria-label': H.t('Droits de {name} : consulter, lecture, télécharger, envoyer, demander', { name: u.name }), onclick: function () { rightsOf(u); } }),
          H.button(H.t('Mot de passe'), 'edit', { 'data-key': 'pw-' + u.id, 'aria-label': H.t('Changer le mot de passe de {name}', { name: u.name }), onclick: function () { passwordOf(u, mine); } }),
          H.button(H.t('Supprimer'), 'trash', { 'data-key': 'del-' + u.id, 'aria-label': H.t('Supprimer le compte de {name}', { name: u.name }), onclick: function () { deleteUser(u, mine); } })));
      });
      return panel;
    }
    function addUser() {
      var role = 'user', toggle = H.button(H.t('Rôle : {role}', { role: H.roles.user }), null, { 'data-key': 'new-role', 'aria-label': H.t('Rôle du nouveau compte, à changer'), onclick: function () {
        role = role === 'user' ? 'admin' : 'user';
        toggle.querySelector('.lbl').textContent = H.t('Rôle : {role}', { role: H.roles[role] });
      } });
      H.form({ title: H.t('Ajouter un compte'), ok: H.t('Créer le compte'),
        lead: [H.t('Un administrateur gère la médiathèque et les comptes ; un utilisateur lit, avec sa propre progression et ses propres playlists.')],
        fields: [{ key: 'name', label: H.t('Nom (40 caractères au plus)'), type: 'text' }, { key: 'pw', label: H.t('Mot de passe (12 caractères au moins)'), type: 'password', auto: 'new-password' }, { key: 'again', label: H.t('Mot de passe, encore une fois'), type: 'password', auto: 'new-password' }],
        extra: [toggle],
        submit: function (v) {
          if (!v.name) return H.t('Donne un nom au compte.');
          var bad = H.checkPassword(v.pw, v.again);
          if (bad) return bad;
          return H.api('/api/users', { name: v.name, password: v.pw, role: role });
        },
        done: function (u) { H.toast(H.t('Compte créé : {name}', { name: u.name })); refreshUsers('user-add'); } });
    }
    // Ce qu'un compte peut consulter, lire et télécharger, puis s'il peut envoyer des fichiers, en une phrase pour la liste des comptes.
    function rightsSummary(u) {
      var libs = (state && state.libraries) || [], rights = u.rights || {}, parts = [];
      function who(perm) {
        var list = libs.filter(function (l) { return !rights[l.id] || rights[l.id][perm]; });
        return list.length === libs.length ? H.t('tout') : list.length ? list.map(function (l) { return H.libLabel(l); }).join(', ') : H.t('rien');
      }
      if (libs.length) parts.push(H.t('Consulter : {view} · Lecture : {play} · Télécharger : {download}', { view: who('view'), play: who('play'), download: who('download') }));
      parts.push(H.t('Envoyer des fichiers : {upload}', { upload: u.can_upload ? H.t('oui') : H.t('non') }));
      parts.push(H.t('Peut demander : {request}', { request: u.can_request ? H.t('oui') : H.t('non') }));
      return parts.join(' · ');
    }
    // Pour chaque bibliothèque : consulter, lecture, télécharger. Retirer « consulter » retire aussi les deux autres. Au-dessous, le droit d'envoyer des fichiers et celui de demander des films (Oui / Non), qui ne tiennent pas à une bibliothèque.
    // Les droits valent tout de suite, sans que la personne se reconnecte.
    function rightsOf(u) {
      var draft = {}, rows = h('div', { class: 'rights' }), upload = !!u.can_upload, ask = u.can_request !== false;
      (state.libraries || []).forEach(function (l) {
        var r = (u.rights || {})[l.id] || { view: true, play: true, download: true };
        draft[l.id] = { view: !!r.view, play: !!(r.view && r.play), download: !!(r.view && r.download) };
      });
      var saved = { rights: JSON.stringify(draft), upload: upload, request: ask, touched: false };      // ce que le serveur a déjà : un second essai n'envoie que le reste
      function toggle(l, which) {
        var button = h('button', { class: 'chip f', type: 'button', 'data-key': 'r-' + which + '-' + l.id, onclick: function () {
          var d = draft[l.id];
          if (which === 'view') { d.view = !d.view; if (!d.view) { d.play = false; d.download = false; } }
          else if (d.view) d[which] = !d[which];
          paint();
        } }, h('span', { class: 'mark', 'aria-hidden': 'true' }, '✓'), h('span', { class: 'lbl' }));        // la coche a toujours sa place (visible ou non) : le bouton garde la même largeur
        return button;
      }
      function choice(which, value, label) {         // `which` : « upload » (envoyer des fichiers) ou « request » (demander des films)
        return h('button', { class: 'chip f', type: 'button', 'data-key': 'r-' + which + '-' + (value ? 'yes' : 'no'), onclick: function () { if (which === 'upload') upload = value; else ask = value; paint(); } },
          h('span', { class: 'mark', 'aria-hidden': 'true' }, '✓'), h('span', { class: 'lbl' }, label));
      }
      var buttons = {}, yes = choice('upload', true, H.t('Oui')), no = choice('upload', false, H.t('Non')), askYes = choice('request', true, H.t('Oui')), askNo = choice('request', false, H.t('Non'));
      (state.libraries || []).forEach(function (l) {
        buttons[l.id] = { view: toggle(l, 'view'), play: toggle(l, 'play'), download: toggle(l, 'download') };
        rows.appendChild(h('div', { class: 'rrow', 'data-lib': l.id }, h('b', null, H.libLabel(l)), buttons[l.id].view, buttons[l.id].play, buttons[l.id].download));
      });
      rows.appendChild(h('div', { class: 'rrow', 'data-upload-right': '1' }, h('b', null, H.t('Envoyer des fichiers')), yes, no));
      rows.appendChild(h('div', { class: 'rrow', 'data-request-right': '1' }, h('b', null, H.t('Peut demander')), askYes, askNo));
      function paint() {
        (state.libraries || []).forEach(function (l) {
          var d = draft[l.id], b = buttons[l.id];
          b.view.classList.toggle('on', d.view); b.view.setAttribute('aria-pressed', d.view ? 'true' : 'false');
          b.view.querySelector('.lbl').textContent = H.t('Consulter');
          b.play.classList.toggle('on', d.play); b.play.setAttribute('aria-pressed', d.play ? 'true' : 'false');
          b.play.querySelector('.lbl').textContent = H.t('Lecture');
          b.play.disabled = !d.view;
          b.download.classList.toggle('on', d.download); b.download.setAttribute('aria-pressed', d.download ? 'true' : 'false');
          b.download.querySelector('.lbl').textContent = H.t('Télécharger');
          b.download.disabled = !d.view;
        });
        yes.classList.toggle('on', upload); yes.setAttribute('aria-pressed', upload ? 'true' : 'false');
        no.classList.toggle('on', !upload); no.setAttribute('aria-pressed', upload ? 'false' : 'true');
        askYes.classList.toggle('on', ask); askYes.setAttribute('aria-pressed', ask ? 'true' : 'false');
        askNo.classList.toggle('on', !ask); askNo.setAttribute('aria-pressed', ask ? 'false' : 'true');
      }
      paint();
      H.form({ title: H.t('Droits de {name}', { name: u.name }), ok: H.t('Enregistrer'), kind: 'form', cls: 'rightsbox',
        lead: [H.t('Consulter : la liste, les fiches, les images. Lecture : vidéo, musique et livres en ligne. Sans « Consulter », la bibliothèque disparaît pour ce compte.')],
        extra: [rows],
        submit: function () { return saveAccount(u, draft, upload, ask, saved); },
        done: function () { H.toast(H.t('Droits enregistrés : {name}', { name: u.name })); refreshUsers('rights-' + u.id); },
        cancel: function () { if (saved.touched) refreshUsers('rights-' + u.id); } });       // fermée après un enregistrement à moitié réussi : la liste montre ce qui l'est
    }
    // Enregistre les droits de bibliothèque, le droit d'envoi et celui de demander, chacun par son appel et seulement s'il a changé. Refuse (avec ce qui a réussi et ce qui a échoué) si l'un échoue.
    function saveAccount(u, draft, upload, ask, saved) {
      var base = '/api/user/' + encodeURIComponent(u.id), now = JSON.stringify(draft), failed = {}, applied = {};
      function run(part, changed, path, body, keep) {
        if (!changed) return Promise.resolve();
        return H.api(path, body).then(function () { keep(); applied[part] = true; saved.touched = true; }, function (e) { failed[part] = e.message; });
      }
      return Promise.all([
        run('rights', now !== saved.rights, base + '/rights', { rights: draft }, function () { saved.rights = now; }),
        run('upload', upload !== saved.upload, base + '/upload', { upload: upload }, function () { saved.upload = upload; }),
        run('request', ask !== saved.request, base + '/request', { request: ask }, function () { saved.request = ask; })
      ]).then(function () {
        var bad = saveFailure(failed, applied);
        if (bad) throw new Error(bad);
      });
    }
    // Dit ce qui a échoué, et seulement ce qui est enregistré s'il l'est : l'un n'est jamais présenté comme raté parce qu'un autre l'est. « Peut demander » s'ajoute à la phrase des deux autres.
    function saveFailure(failed, applied) {
      var said = libraryFailure(failed, applied), more = '';
      if (failed.request) more = H.t('« Peut demander » n’a pas été enregistré : {error}', { error: failed.request }) + (!said && (applied.rights || applied.upload) ? ' ' + H.t('Le reste est enregistré.') : '');
      else if (said && applied.request) more = H.t('« Peut demander » est enregistré.');
      return [said, more].filter(Boolean).join(' ');
    }
    function libraryFailure(failed, applied) {
      if (failed.rights && failed.upload) return H.t('Rien n’a été enregistré. Droits de bibliothèque : {rights}. Envoyer des fichiers : {upload}', { rights: failed.rights, upload: failed.upload });
      if (failed.rights) return applied.upload ? H.t('« Envoyer des fichiers » est enregistré, mais pas les droits de bibliothèque : {error}', { error: failed.rights }) : H.t('Les droits de bibliothèque n’ont pas été enregistrés : {error}', { error: failed.rights });
      if (failed.upload) return applied.rights ? H.t('Les droits de bibliothèque sont enregistrés, mais pas « Envoyer des fichiers » : {error}', { error: failed.upload }) : H.t('« Envoyer des fichiers » n’a pas été enregistré : {error}', { error: failed.upload });
      return '';
    }
    function passwordOf(u, mine) {
      if (mine) { H.changeMyPassword(function () { H.focus.key(el, 'pw-' + u.id); }); return; }          // son propre compte : même formulaire que « Changer mon mot de passe » (le serveur exige le mot de passe actuel)
      H.form({ title: H.t('Mot de passe de {name}', { name: u.name }), ok: H.t('Changer'),
        lead: [H.t('Les sessions ouvertes de ce compte seront fermées ; la personne se reconnectera avec le nouveau mot de passe.')],
        fields: [{ key: 'pw', label: H.t('Nouveau mot de passe (12 caractères au moins)'), type: 'password', auto: 'new-password' }, { key: 'again', label: H.t('Encore une fois'), type: 'password', auto: 'new-password' }],
        submit: function (v) {
          var bad = H.checkPassword(v.pw, v.again);
          if (bad) return bad;
          return H.api('/api/user/' + encodeURIComponent(u.id) + '/password', { password: v.pw });
        },
        done: function () { H.toast(H.t('Mot de passe changé : {name}', { name: u.name })); H.focus.key(el, 'pw-' + u.id); } });
    }
    function deleteUser(u, mine) {
      var lead = [H.t('Supprimer le compte « {name} » ? Sa progression de lecture, ses playlists et ses éléments retirés de « Reprendre » seront perdus, et cela ne s’annule pas. Les fichiers de la médiathèque ne sont pas touchés.', { name: u.name })];
      if (mine) lead.push(H.t('C’est le compte avec lequel tu es connecté : tu seras déconnecté. Il doit rester au moins un autre administrateur.'));
      H.confirm({ title: H.t('Supprimer le compte de {name}', { name: u.name }), ok: H.t('Supprimer définitivement'), danger: true, lead: lead,
        submit: function () { return H.api('/api/user/' + encodeURIComponent(u.id) + '/delete', {}); },
        done: function () {
          if (mine) { H.forgetAccount(); location.href = '/login'; return; }
          H.toast(H.t('Compte supprimé : {name}', { name: u.name }));
          refreshUsers('user-add');
        } });
    }
    function refreshUsers(key) {
      poll().then(function () { if (!(key && H.focus.key(el, key))) H.focus.first(); });
    }

    // --- Serveur : ce qui se lit plus qu'il ne se règle. La mention TMDB est dans Paramètres > À propos, que tous les comptes voient. --------
    function serverFacts(body) {
      var server = h('section', { class: 'panel' }, h('h2', null, H.t('Serveur')));
      var facts = [['Version', state.version], ['ffmpeg', state.ffmpeg ? H.t('présent') : H.t('absent : lecture directe uniquement')], ['TMDB', state.tmdb ? H.t('clé configurée') : H.t('pas de clé (TMDB_KEY)')],
        [H.t('Conversions'), H.t('{n} en cours sur {max}', { n: state.streams, max: state.max_streams })], [H.t('Accès'), state.auth ? H.t('protégé par mot de passe') : H.t('sans mot de passe')]];
      server.appendChild(h('dl', null, facts.map(function (f) { return [h('dt', null, f[0]), h('dd', null, f[1])]; })));
      body.appendChild(server);
    }

    // L'état du serveur, les tâches (avec leurs journaux) et les comptes ; relus toutes les 1,5 s tant qu'une tâche tourne.
    function poll() {
      if (closed) return Promise.resolve();          // la vue est détruite : une réponse tardive ne réarme pas le sondage
      clearTimeout(timer);
      // Si le rôle a changé depuis le démarrage, le 403 du journal se rattrape par un appel sans journal ; la page renvoie alors aux paramètres.
      var jobsCall = H.api('/api/jobs?log=1').catch(function (e) { if (e.status === 403) return H.api('/api/jobs'); throw e; });
      return Promise.all([H.api('/api/state'), jobsCall]).then(function (r) {
        H.setSession(r[0]);
        H.nav.sync();
        if (!(r[0].auth && H.isAdmin())) { users = null; usersError = ''; return r; }
        return H.api('/api/users').then(function (res) { users = res.users; usersError = ''; return r; }, function (e) { users = null; usersError = e.message; return r; });
      }).then(function (r) {
        if (closed) return;
        if (!H.isAdmin()) { closed = true; H.go('#/settings', true); return; }          // le rôle d'administrateur a été retiré pendant qu'on était là
        var page = JSON.stringify([r[0], users, usersError]), tasks = JSON.stringify(r[1].jobs);
        state = r[0]; jobs = r[1].jobs; H.setLibs(state.libraries);
        busy = r[1].busy;
        if (wasBusy && !busy) H.toast(H.t('Terminé'));
        wasBusy = busy;
        // Pendant une tâche on revient ici toutes les 1,5 s : reconstruire la page entière coupait le défilement (molette, tactile). On ne refait que ce qui a changé.
        if (page !== lastPage) render();
        else {
          var mark = el.querySelector('[data-key="tab-mediatheque"]');          // un point sur l'onglet Médiathèque tant qu'une tâche est en cours
          if (mark) mark.classList.toggle('busy', !!busy);
          if (logPanel && tasks !== lastTasks && !patchLogs()) showJobs();
        }
        lastPage = page; lastTasks = tasks;
        if (busy) timer = setTimeout(poll, 1500);
      }).catch(function (e) { if (!closed) H.toast(e.message, 5000); });
    }
    poll();
    return { el: el, nav: '#/admin', refresh: function () { if (activeTab() === 'demandes' && !reqLoading) loadRequests(); if (activeTab() === 'suggestions' && !sgLoading) loadSuggestions(); return poll(); }, destroy: function () { closed = true; clearTimeout(timer); } };          // au retour (une fiche, une saga) : la liste des demandes a pu changer
  });
})();
