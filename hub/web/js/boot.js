(function () {
  'use strict';
  var H = window.Hub;
  H.audio.init();
  H.api('/api/state').then(function (state) {
    H.libs = state.libraries;
    H.nav.build();
    H.start();
  }).catch(function (e) {
    document.getElementById('views').appendChild(H.h('section', { class: 'view blank' }, H.h('h1', null, 'Le serveur ne répond pas'), H.h('p', null, e.message), H.h('button', { class: 'btn f', onclick: function () { location.reload(); } }, 'Réessayer')));
  });
})();
