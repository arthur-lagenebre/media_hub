/* Les langues de l'interface : le français (celui du code) et l'anglais (lang-en.js).
   Le texte français sert de clé : `H.t('Paramètres')` rend « Settings » en anglais et le texte tel quel en français ; une clé sans traduction reste en français.
   `{nom}` marque une valeur : H.t('{n} fichiers', { n: 3 }).
   `H.tr(texte)` traduit un texte que le serveur a écrit en français (message d'erreur, libellé de piste…) : mot pour mot, ou d'après un modèle de H.srv.en. Le nom d'une bibliothèque se traduit par `H.libLabel` (views.js), pas par H.tr.
   La langue se retient dans un cookie (le serveur y lit celle de la page de connexion) et dans le navigateur ; sans choix, c'est le français. */
(function () {
  'use strict';
  var H = window.Hub = window.Hub || {};
  var has = Object.prototype.hasOwnProperty;

  H.langs = [['fr', 'Français'], ['en', 'English']];
  H.dict = H.dict || {};       // H.dict.en : le texte de l'interface
  H.srv = H.srv || {};         // H.srv.en : ce que le serveur écrit (messages d'erreur, libellés)
  var LOCALES = { fr: 'fr-FR', en: 'en-GB' };

  function known(code) { return H.langs.some(function (l) { return l[0] === code; }); }
  function stored() {
    var m = /(?:^|;\s*)hub_lang=([a-z]+)/.exec(document.cookie || '');
    if (m && known(m[1])) return m[1];
    try { var v = JSON.parse(localStorage.getItem('hub.lang')); if (known(v)) return v; } catch (e) { /* tant pis */ }
    return 'fr';
  }
  H.lang = stored();
  H.locale = LOCALES[H.lang];
  document.documentElement.setAttribute('lang', H.lang);

  // Choisit la langue et recharge la page : tout le texte est écrit à la construction des vues.
  H.setLang = function (code) {
    if (!known(code)) return;
    try { document.cookie = 'hub_lang=' + code + '; path=/; max-age=31536000; SameSite=Lax'; } catch (e) { /* tant pis */ }
    try { localStorage.setItem('hub.lang', JSON.stringify(code)); } catch (e) { /* tant pis */ }
    location.reload();
  };

  function fill(text, params) {
    return text.replace(/\{(\w+)\}/g, function (m, k) { return has.call(params, k) && params[k] !== undefined && params[k] !== null ? params[k] : m; });
  }
  function entry(table, key) { return table && has.call(table, key) ? table[key] : null; }

  H.missing = [];              // les clés sans traduction qu'une page a demandées (les tests de l'interface vérifient que la liste reste vide)
  H.t = function (key, params) {
    var text = H.lang === 'fr' ? null : entry(H.dict[H.lang], key);
    if (text === null) {
      text = key;
      if (H.lang !== 'fr' && H.missing.indexOf(key) < 0) H.missing.push(key);
    }
    return params ? fill(text, params) : text;
  };

  // Les modèles du serveur (« fichier trop gros (au plus {limit} Mo) ») deviennent des expressions régulières, une fois par langue.
  var compiled = {};
  function patterns(lang) {
    if (compiled[lang]) return compiled[lang];
    var list = [], table = H.srv[lang] || {};
    Object.keys(table).forEach(function (key) {
      if (key.indexOf('{') < 0) return;
      var names = [];
      var source = key.split(/(\{\w+\})/).map(function (part) {
        var m = /^\{(\w+)\}$/.exec(part);
        if (!m) return part.replace(/[.*+?^$()|[\]\\{}]/g, '\\$&');
        names.push(m[1]);
        return '(.+?)';
      }).join('');
      list.push({ re: new RegExp('^' + source + '$'), names: names, to: table[key], weight: key.replace(/\{\w+\}/g, '').length });
    });
    list.sort(function (a, b) { return b.weight - a.weight; });      // le modèle qui fixe le plus de texte d'abord
    compiled[lang] = list;
    return list;
  }
  H.tr = function (text) {
    if (H.lang === 'fr' || typeof text !== 'string' || !text) return text;
    var exact = entry(H.srv[H.lang], text);
    if (exact !== null) return exact;
    exact = entry(H.dict[H.lang], text);
    if (exact !== null) return exact;
    var list = patterns(H.lang);
    for (var i = 0; i < list.length; i++) {
      var m = list[i].re.exec(text);
      if (!m) continue;
      var params = {};
      list[i].names.forEach(function (name, k) {                                              // une bibliothèque nommée dans le message (« Musique ») se traduit aussi,
        var part = m[k + 1];                                                                  // sauf un nom que l'administrateur a choisi : il vient de lui, pas du dictionnaire
        params[name] = H.isChosenLabel && H.isChosenLabel(part) ? part : H.tr(part);
      });
      return fill(list[i].to, params);
    }
    return text;
  };

  // « 3 films » / « 1 film » : le français met au singulier 0 et 1, l'anglais seulement 1. Les deux mots sont des clés de traduction.
  H.plural = function (n, one, many) {
    var more = H.lang === 'fr' ? n > 1 : n !== 1;
    return n + ' ' + H.t(more ? (many || one + 's') : one);
  };
  H.number = function (n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, H.lang === 'fr' ? ' ' : ','); };
  H.date = function (seconds) { return new Date(seconds * 1000).toLocaleDateString(H.locale); };
})();
