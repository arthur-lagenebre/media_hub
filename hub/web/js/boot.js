(function () {
  'use strict';
  var H = window.Hub;
  document.title = H.t('Médiathèque');
  H.api('/api/state').then(function (state) {
    H.libs = state.libraries;
    H.setSession(state);
    H.syncAccount();      // avant que la barre audio ne relise la file enregistrée : elle appartient peut-être à un autre compte
    H.audio.init();
    H.nav.build();
    H.start();
  }).catch(function (e) {
    document.getElementById('views').appendChild(H.h('section', { class: 'view blank' }, H.h('h1', null, H.t('Le serveur ne répond pas')), H.h('p', null, e.message), H.h('button', { class: 'btn f', onclick: function () { location.reload(); } }, H.t('Réessayer'))));
  });
})();
