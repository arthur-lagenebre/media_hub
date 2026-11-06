/* Les sous-titres en image des Blu-ray (PGS, fichier .sup) : lus et dessinés par la page, sans rien convertir côté serveur.
   Un .sup est une suite de segments (PCS, WDS, PDS, ODS, END) ; chaque « ensemble » (de PCS à END) dit ce qui est à l'écran à partir de son horodatage,
   un ensemble sans objet l'efface. On garde les images compressées (RLE) et on ne décode que celle qui est affichée : un film en compte des milliers. */
(function () {
  'use strict';
  var H = window.Hub;
  var P = H.pgs = {};

  var PDS = 0x14, ODS = 0x15, PCS = 0x16, END = 0x80, EPOCH_START = 0x80;

  function u16(b, o) { return (b[o] << 8) | b[o + 1]; }

  // Une couleur Y Cr Cb -> R G B ; BT.709 pour une image HD, BT.601 en dessous.
  function rgb(y, cr, cb, hd) {
    var l = 1.164 * (y - 16), r, g, b;
    if (hd) { r = l + 1.793 * (cr - 128); g = l - 0.213 * (cb - 128) - 0.533 * (cr - 128); b = l + 2.112 * (cb - 128); }
    else { r = l + 1.596 * (cr - 128); g = l - 0.392 * (cb - 128) - 0.813 * (cr - 128); b = l + 2.017 * (cb - 128); }
    return [r, g, b];
  }

  function concat(parts) {
    if (parts.length === 1) return parts[0];
    var n = 0, i, out, at = 0;
    for (i = 0; i < parts.length; i++) n += parts[i].length;
    out = new Uint8Array(n);
    for (i = 0; i < parts.length; i++) { out.set(parts[i], at); at += parts[i].length; }
    return out;
  }

  // Le contenu d'un fichier .sup -> { width, height, sets: [{ s, e, objs: [{ x, y, w, h, rle, pal, crop }] }] } (temps en secondes, trié).
  P.parse = function (buffer) {
    var b = new Uint8Array(buffer), n = b.length, o = 0;
    var width = 1920, height = 1080;
    var palettes = {}, objects = {}, building = {}, sets = [], pcs = null;

    while (o + 13 <= n) {
      if (b[o] !== 0x50 || b[o + 1] !== 0x47) { o++; continue; }               // « PG » : sinon on cherche le segment suivant
      var pts = (b[o + 2] * 16777216 + (b[o + 3] << 16) + (b[o + 4] << 8) + b[o + 5]) / 90000;
      var type = b[o + 10], size = u16(b, o + 11), at = o + 13, end = at + size;
      if (end > n) break;
      o = end;

      if (type === PCS && size >= 11) {
        width = u16(b, at) || width; height = u16(b, at + 2) || height;
        pcs = { s: pts, palette: b[at + 9], comps: [] };
        if (b[at + 7] === EPOCH_START) { palettes = {}; objects = {}; building = {}; }
        var count = b[at + 10], p = at + 11;
        for (var k = 0; k < count && p + 8 <= end; k++) {
          var cropped = (b[p + 3] & 0x80) !== 0, comp = { id: u16(b, p), x: u16(b, p + 4), y: u16(b, p + 6), crop: null };
          p += 8;
          if (cropped && p + 8 <= end) { comp.crop = { x: u16(b, p), y: u16(b, p + 2), w: u16(b, p + 4), h: u16(b, p + 6) }; p += 8; }
          pcs.comps.push(comp);
        }
      } else if (type === PDS && size >= 2) {
        var id = b[at], pal = palettes[id] || (palettes[id] = new Uint8ClampedArray(1024)), hd = width >= 1280;
        for (var q = at + 2; q + 5 <= end; q += 5) {
          var c = rgb(b[q + 1], b[q + 2], b[q + 3], hd), slot = b[q] * 4;
          pal[slot] = c[0]; pal[slot + 1] = c[1]; pal[slot + 2] = c[2]; pal[slot + 3] = b[q + 4];
        }
      } else if (type === ODS && size >= 4) {
        var oid = u16(b, at), flag = b[at + 3];
        if (flag & 0x80) {                                                      // premier fragment : la taille de l'image, puis le début des données
          if (size < 11) continue;
          building[oid] = { w: u16(b, at + 7), h: u16(b, at + 9), parts: [b.subarray(at + 11, end)] };
        } else if (building[oid]) {
          building[oid].parts.push(b.subarray(at + 4, end));
        }
        if ((flag & 0x40) && building[oid]) { objects[oid] = { w: building[oid].w, h: building[oid].h, rle: concat(building[oid].parts) }; delete building[oid]; }
      } else if (type === END && pcs) {
        var pal2 = palettes[pcs.palette], objs = [];
        if (pal2) {
          var snapshot = pal2.slice();
          pcs.comps.forEach(function (c) {
            var img = objects[c.id];
            if (img && img.w > 0 && img.h > 0) objs.push({ x: c.x, y: c.y, w: img.w, h: img.h, rle: img.rle, pal: snapshot, crop: c.crop });
          });
        }
        sets.push({ s: pcs.s, e: pcs.s + 5, objs: objs });
        pcs = null;
      }
    }
    sets.sort(function (x, y) { return x.s - y.s; });
    for (var i = 0; i + 1 < sets.length; i++) sets[i].e = sets[i + 1].s;
    return { width: width, height: height, sets: sets };
  };

  // L'ensemble affiché à l'instant `t` (secondes), ou null.
  P.at = function (track, t) {
    var sets = track.sets, lo = 0, hi = sets.length - 1, best = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (sets[mid].s <= t) { best = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return best >= 0 && t < sets[best].e && sets[best].objs.length ? sets[best] : null;
  };

  // Les pixels d'une image RLE : un octet seul est une couleur ; 0 suivi d'un code donne une répétition (ou, si le code est 0, la fin de la ligne).
  function decode(o, ctx) {
    var w = o.w, h = o.h, image = ctx.createImageData(w, h), out = image.data, pal = o.pal, src = o.rle, i = 0, n = src.length, x = 0, y = 0;
    function put(color, len) {
      if (y >= h) return;
      if (len > w - x) len = w - x;
      if (len <= 0) return;
      var p = (y * w + x) * 4, s = color * 4, r = pal[s], g = pal[s + 1], bl = pal[s + 2], a = pal[s + 3];
      if (a) for (var k = 0; k < len; k++, p += 4) { out[p] = r; out[p + 1] = g; out[p + 2] = bl; out[p + 3] = a; }
      x += len;
    }
    while (i < n) {
      var c = src[i++];
      if (c) { put(c, 1); continue; }
      if (i >= n) break;
      var f = src[i++];
      if (f === 0) { x = 0; y++; continue; }
      var len, color = 0;
      if (f < 0x40) len = f;
      else if (f < 0x80) len = ((f & 0x3f) << 8) | src[i++];
      else if (f < 0xC0) { len = f & 0x3f; color = src[i++]; }
      else { len = ((f & 0x3f) << 8) | src[i++]; color = src[i++]; }
      put(color, len);
    }
    return image;
  }

  var scratch = null;
  // Dessine un ensemble (ou rien, pour l'effacer) sur le canvas, à la taille de l'écran du Blu-ray : c'est le CSS qui l'adapte à la vidéo.
  P.draw = function (canvas, track, set) {
    if (canvas.width !== track.width || canvas.height !== track.height) { canvas.width = track.width; canvas.height = track.height; }
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!set) return;
    scratch = scratch || document.createElement('canvas');
    set.objs.forEach(function (o) {
      scratch.width = o.w; scratch.height = o.h;
      var from = scratch.getContext('2d'), c = o.crop;
      from.putImageData(decode(o, from), 0, 0);
      if (c && c.w > 0 && c.h > 0) ctx.drawImage(scratch, c.x, c.y, c.w, c.h, o.x, o.y, c.w, c.h);          // la partie découpée se pose à la position de l'objet
      else ctx.drawImage(scratch, o.x, o.y);
    });
  };
})();
