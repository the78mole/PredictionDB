// Prognoseverlauf auf der Landing Page: oben die jüngste Tagesprognose (D+0 bis D+15), darunter
// aufklappbar die Vortage mit den Ist-Werten bis jetzt. Prognosen kommen von Pages
// (prognose/de_lu/, gebaut von pages.yml aus GHCR). Ist-Last, ÜNB-Lastprognose und
// Day-Ahead-Preis lädt die Seite EINMAL je Aufruf von SMARD (CORS erlaubt, CC BY 4.0) und
// verteilt sie auf alle Diagramme. Den Day-Ahead-Preis veröffentlicht SMARD erst mit bis zu zwei
// Tagen Verzug; die fehlenden Stunden ergänzt die Seite aus den Prognosen selbst, die die
// Auktionsergebnisse bis morgen als feste Werte (P10 = P50 = P90) enthalten.
(function () {
  "use strict";
  var wurzel = document.getElementById("prognoseverlauf");
  if (!wurzel || !window.fetch || !window.Intl) return;

  var BASIS = "prognose/de_lu/";
  var SMARD = "https://www.smard.de/app/chart_data/";
  var H = 3600000, TAG = 24 * H, TAGE = 16;
  var WT = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];
  var $ = function (id) { return document.getElementById(id); };

  // ---------- Zeit in Europe/Berlin ----------
  var teile = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Berlin", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", hourCycle: "h23"
  });
  function lokal(ms) {
    var p = {};
    teile.formatToParts(new Date(ms)).forEach(function (x) { p[x.type] = +x.value; });
    var wt = new Date(Date.UTC(p.year, p.month - 1, p.day)).getUTCDay();
    return { y: p.year, m: p.month, d: p.day, h: p.hour, wt: wt };
  }
  function mitternacht(y, m, d) {        // 00:00 Ortszeit als UTC-ms
    var u = Date.UTC(y, m - 1, d), l = lokal(u);
    return u - (Date.UTC(l.y, l.m - 1, l.d, l.h) - u);
  }
  function tagPlus(ms0, n) {              // Mitternacht n Tage nach der Mitternacht ms0
    var l = lokal(ms0 + 12 * H), u = new Date(Date.UTC(l.y, l.m - 1, l.d + n));
    return mitternacht(u.getUTCFullYear(), u.getUTCMonth() + 1, u.getUTCDate());
  }
  function datum(ms, mitWt) {
    var l = lokal(ms), s = String(l.d).padStart(2, "0") + "." + String(l.m).padStart(2, "0") + ".";
    return mitWt ? WT[l.wt] + " " + s : s;
  }
  function uhr(ms) { return String(lokal(ms).h).padStart(2, "0") + ":00"; }
  var f1 = function (n) { return n.toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 }); };
  var f0 = function (n) { return n.toLocaleString("de-DE", { maximumFractionDigits: 0 }); };
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  // ---------- Prognosen ----------
  function json(url) { return fetch(url, { cache: "no-cache" }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }); }

  function reihe(rows, faktor) {
    return (rows || []).map(function (r) {
      return { t: Date.parse(r.valid_time), p10: r.p10 * faktor, p50: r.p50 * faktor, p90: r.p90 * faktor,
        fest: r.p10 === r.p90 && r.p50 === r.p10 };
    }).sort(function (a, b) { return a.t - b.t; });
  }
  function lauf(doc) {
    var issue = Date.parse(doc.issue_time), l = lokal(issue);
    var t0 = mitternacht(l.y, l.m, l.d);
    var lastFaktor = (doc.units || {}).load_actual === "MW" ? 0.001 : 1;
    return {
      issue: issue, t0: t0, t1: tagPlus(t0, TAGE),
      modell: (doc.model || {}).version || "unbekannt",
      L: reihe(doc.targets.load_actual, lastFaktor),
      P: reihe(doc.targets.da_price_de_lu, 1)
    };
  }

  // ---------- Ist-Werte: ein Abruf je Seitenaufruf ----------
  var Ist = { last: new Map(), uenb: new Map(), preis: new Map(), stand: null };

  function smardReihe(filter, region, von, bis, faktor) {
    var pfad = SMARD + filter + "/" + region + "/";
    return json(pfad + "index_quarterhour.json").then(function (idx) {
      var wochen = idx.timestamps.filter(function (ts) { return ts <= bis && ts + 7 * TAG + H > von; });
      return Promise.all(wochen.map(function (ts) {
        return json(pfad + filter + "_" + region + "_quarterhour_" + ts + ".json")
          .then(function (d) { return d.series; }, function () { return []; });
      }));
    }).then(function (teile) {
      var summe = new Map(), n = new Map(), out = new Map();
      teile.forEach(function (serie) {
        serie.forEach(function (p) {
          if (p[1] == null) return;
          var h = Math.floor(p[0] / H) * H;
          summe.set(h, (summe.get(h) || 0) + p[1]); n.set(h, (n.get(h) || 0) + 1);
        });
      });
      summe.forEach(function (s, h) { if (n.get(h) === 4) out.set(h, (s / 4) * faktor); });
      return out;
    });
  }

  function istLaden(laeufe) {
    var von = Math.min.apply(null, laeufe.map(function (x) { return x.t0; }));
    var bis = Date.now() + 2 * TAG;
    status(false, "Ist-Werte werden von SMARD geladen …");
    return Promise.allSettled([
      smardReihe(410, "DE", von, bis, 4 / 1000),        // MWh je Viertelstunde → GW
      smardReihe(411, "DE", von, bis, 4 / 1000),
      smardReihe(4169, "DE-LU", von, bis, 1)            // €/MWh
    ]).then(function (r) {
      Ist.last = r[0].status === "fulfilled" ? r[0].value : new Map();
      Ist.uenb = r[1].status === "fulfilled" ? r[1].value : new Map();
      Ist.preis = r[2].status === "fulfilled" ? r[2].value : new Map();
      laeufe.forEach(function (x) {
        x.P.forEach(function (p) { if (p.fest && !Ist.preis.has(p.t)) Ist.preis.set(p.t, p.p50); });
      });
      var letzte = 0; Ist.last.forEach(function (_, h) { if (h > letzte) letzte = h; });
      Ist.stand = letzte ? letzte + H : null;
      var ok = r.every(function (x) { return x.status === "fulfilled"; });
      if (Ist.stand) {
        status(true, "Ist-Werte: SMARD, Stand " + datum(Ist.stand - H) + " " + uhr(Ist.stand) +
          (ok ? "" : " (teilweise)"));
      } else {
        status(false, "Ist-Werte von SMARD gerade nicht erreichbar");
      }
    });
  }
  function status(ok, text) {
    $("pv-status").classList.toggle("ok", ok);
    $("pv-status-text").textContent = text;
  }

  // ---------- Kennzahlen ----------
  function mae(x, ziel, vonMs, bisMs) {
    var s = 0, n = 0, ist = ziel === "L" ? Ist.last : Ist.preis;
    x[ziel].forEach(function (p) {
      if (p.t < vonMs || p.t >= bisMs || (ziel === "P" && p.fest)) return;
      var v = ist.get(p.t); if (v == null) return;
      s += Math.abs(p.p50 - v); n++;
    });
    return n ? { mae: s / n, n: n } : null;
  }
  function maeUenb(x, vonMs, bisMs) {
    var s = 0, n = 0;
    x.L.forEach(function (p) {
      if (p.t < vonMs || p.t >= bisMs) return;
      var u = Ist.uenb.get(p.t), v = Ist.last.get(p.t);
      if (u == null || v == null) return;
      s += Math.abs(u - v); n++;
    });
    return n ? { mae: s / n, n: n } : null;
  }

  // ---------- Diagramm ----------
  function ticks(lo, hi, n) {
    var step0 = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
    var step = [1, 2, 2.5, 5, 10].map(function (m) { return m * mag; }).find(function (s) { return s >= step0; });
    var out = []; for (var v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
    return out;
  }
  function pfad(punkte, x, y) {
    var d = "", vorher = null;
    punkte.forEach(function (p) {
      if (p[1] == null) { vorher = null; return; }
      d += (vorher != null && p[0] - vorher <= H ? "L" : "M") + x(p[0]).toFixed(1) + "," + y(p[1]).toFixed(1);
      vorher = p[0];
    });
    return d;
  }
  function stunden(t0, t1, map) {
    var out = []; for (var t = t0; t < t1; t += H) out.push([t, map.has(t) ? map.get(t) : null]);
    return out;
  }

  function zeichne(el, x0, kompakt) {
    var W = Math.max(300, el.clientWidth), schmal = W < 560;
    var ml = 44, mr = 10, hP = kompakt ? 120 : 160, gap = 34, mt = 18;
    var iw = W - ml - mr, span = x0.t1 - x0.t0;
    var x = function (t) { return ml + ((t - x0.t0) / span) * iw; };
    var jetzt = Date.now(), tage = [];
    for (var d = 0; d <= TAGE; d++) tage.push(tagPlus(x0.t0, d));

    function panel(top, prog, istMap, kl, bkl, titel, einheit, extra) {
      var lo = Infinity, hi = -Infinity;
      function mit(v) { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
      prog.forEach(function (p) { mit(p.p10); mit(p.p90); });
      var ist = stunden(x0.t0, x0.t1, istMap), ex = extra ? stunden(x0.t0, x0.t1, extra) : [];
      ist.forEach(function (p) { mit(p[1]); }); ex.forEach(function (p) { mit(p[1]); });
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      var pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
      var y = function (v) { return top + hP - ((v - lo) / (hi - lo)) * hP; };
      var s = '<text class="ptitel" x="' + ml + '" y="' + (top - 6) + '">' + titel + ' <tspan class="einheit">in ' + einheit + "</tspan></text>";
      ticks(lo, hi, kompakt ? 3 : 4).forEach(function (t) {
        s += '<line class="raster" x1="' + ml + '" x2="' + (W - mr) + '" y1="' + y(t) + '" y2="' + y(t) + '"/>' +
          '<text x="' + (ml - 6) + '" y="' + (y(t) + 3.5) + '" text-anchor="end">' + f0(t) + "</text>";
      });
      if (jetzt < x0.t1) {
        var xa = x(Math.max(x0.t0, jetzt));
        s += '<rect class="zukunft" x="' + xa + '" y="' + top + '" width="' + (x(x0.t1) - xa) + '" height="' + hP + '"/>';
      }
      tage.slice(1, -1).forEach(function (t) { s += '<line class="tag" x1="' + x(t) + '" x2="' + x(t) + '" y1="' + top + '" y2="' + (top + hP) + '"/>'; });
      if (prog.length) {
        var o = "", u = "";
        prog.forEach(function (p, i) { o += (i ? "L" : "M") + x(p.t).toFixed(1) + "," + y(p.p90).toFixed(1); });
        for (var i = prog.length - 1; i >= 0; i--) u += "L" + x(prog[i].t).toFixed(1) + "," + y(prog[i].p10).toFixed(1);
        s += '<path class="' + bkl + '" d="' + o + u + 'Z"/>';
        s += '<path class="' + kl + '" d="' + pfad(prog.map(function (p) { return [p.t, p.p50]; }), x, y) + '"/>';
      }
      if (extra) s += '<path class="uenb" d="' + pfad(ex, x, y) + '"/>';
      s += '<path class="ist" d="' + pfad(ist, x, y) + '"/>';
      if (jetzt > x0.t0 && jetzt < x0.t1) s += '<line class="jetzt" x1="' + x(jetzt) + '" x2="' + x(jetzt) + '" y1="' + top + '" y2="' + (top + hP) + '"/>';
      return { s: s, y: y };
    }
    var topL = mt, topP = mt + hP + gap;
    var pL = panel(topL, x0.L, Ist.last, "pl", "bl", "Last", "GW", Ist.uenb);
    var pP = panel(topP, x0.P, Ist.preis, "pp", "bp", "Preis", "€/MWh", null);
    var ax = "", jedes = W < 420 ? 4 : schmal ? 2 : 1;
    for (var k = 0; k < TAGE; k++) {
      if (k % jedes) continue;
      var xm = (x(tage[k]) + x(tage[k + 1])) / 2, yb = topP + hP + 14;
      ax += '<text x="' + xm + '" y="' + yb + '" text-anchor="middle">D+' + k + "</text>";
      if (!schmal || k % 4 === 0) ax += '<text x="' + xm + '" y="' + (yb + 12) + '" text-anchor="middle">' + datum(tage[k], !schmal) + "</text>";
    }
    if (jetzt > x0.t0 && jetzt < x0.t1) ax += '<text class="jetztt" x="' + (x(jetzt) + 4) + '" y="' + (topL + 11) + '">jetzt</text>';
    var Hg = topP + hP + 38;
    el.innerHTML = '<svg viewBox="0 0 ' + W + " " + Hg + '" width="' + W + '" height="' + Hg + '" role="img" aria-label="Prognose vom ' +
      datum(x0.t0) + ' mit Ist-Werten">' + pL.s + pP.s + ax +
      '<g class="hover" visibility="hidden"><line class="cross" y1="' + topL + '" y2="' + (topP + hP) + '"/>' +
      '<circle r="3.5" class="ptk pl-f" data-k="pl"/><circle r="3.5" class="ptk ist-f" data-k="il"/>' +
      '<circle r="3.5" class="ptk uenb-f" data-k="ul"/><circle r="3.5" class="ptk pp-f" data-k="pp"/>' +
      '<circle r="3.5" class="ptk ist-f" data-k="ip"/></g>' +
      '<rect class="fang" x="' + ml + '" y="' + topL + '" width="' + iw + '" height="' + (topP + hP - topL) + '" fill="transparent"/></svg>' +
      '<div class="pv-tip" hidden></div>';

    var svg = el.querySelector("svg"), g = svg.querySelector(".hover"), tip = el.querySelector(".pv-tip");
    var cross = g.querySelector("line"), pts = {};
    g.querySelectorAll("circle").forEach(function (c) { pts[c.dataset.k] = c; });
    var mapL = new Map(x0.L.map(function (p) { return [p.t, p]; })), mapP = new Map(x0.P.map(function (p) { return [p.t, p]; }));
    function setze(c, xv, yv) {
      if (yv == null) c.setAttribute("visibility", "hidden");
      else { c.setAttribute("visibility", "visible"); c.setAttribute("cx", xv); c.setAttribute("cy", yv); }
    }
    function abw(a, b) { return a == null || b == null ? "–" : (a - b >= 0 ? "+" : "−") + f1(Math.abs(a - b)); }
    function zeige(ev) {
      var r = svg.getBoundingClientRect(), sx = (ev.clientX - r.left) * (W / r.width);
      var t = x0.t0 + Math.round((((sx - ml) / iw) * span) / H) * H;
      t = Math.max(x0.t0, Math.min(x0.t1 - H, t));
      var xv = x(t), l = mapL.get(t), p = mapP.get(t);
      var il = Ist.last.get(t), ul = Ist.uenb.get(t), ip = Ist.preis.get(t);
      g.setAttribute("visibility", "visible"); cross.setAttribute("x1", xv); cross.setAttribute("x2", xv);
      setze(pts.pl, xv, l ? pL.y(l.p50) : null); setze(pts.il, xv, il == null ? null : pL.y(il));
      setze(pts.ul, xv, ul == null ? null : pL.y(ul));
      setze(pts.pp, xv, p ? pP.y(p.p50) : null); setze(pts.ip, xv, ip == null ? null : pP.y(ip));
      var tagNr = 0; while (tagNr < TAGE && t >= tage[tagNr + 1]) tagNr++;
      var zeile = function (a, b) { return '<div class="r"><span>' + a + "</span><span>" + b + "</span></div>"; };
      tip.innerHTML = '<div class="t">' + datum(t, true) + " " + uhr(t) + " · D+" + tagNr + "</div>" +
        zeile("Lastprognose", l ? f1(l.p50) + " GW" : "–") +
        zeile("Ist-Last", il == null ? "–" : f1(il) + " GW") +
        zeile("Abweichung", l && il != null ? abw(l.p50, il) + " GW" : "–") +
        zeile("ÜNB-Prognose", ul == null ? "–" : f1(ul) + " GW") +
        zeile("Abweichung ÜNB", abw(ul, il) + (ul != null && il != null ? " GW" : "")) +
        zeile(p && p.fest ? "Preis (Auktion)" : "Preisprognose", p ? f1(p.p50) + " €/MWh" : "–") +
        zeile("Day-Ahead", ip == null ? "–" : f1(ip) + " €/MWh") +
        zeile("Abweichung", p && !p.fest && ip != null ? abw(p.p50, ip) + " €/MWh" : "–");
      tip.hidden = false;
      var px = (xv / W) * r.width, tw = tip.offsetWidth;
      tip.style.left = (px + 14 + tw > r.width ? px - tw - 14 : px + 14) + "px";
      tip.style.top = Math.max(0, ev.clientY - r.top - 60) + "px";
    }
    var fang = svg.querySelector(".fang");
    fang.addEventListener("pointermove", zeige);
    fang.addEventListener("pointerdown", zeige);
    fang.addEventListener("pointerleave", function () { g.setAttribute("visibility", "hidden"); tip.hidden = true; });
  }

  // ---------- Aufbau ----------
  var LAEUFE = [], heute = null;

  function kopf() {
    $("pv-datum").textContent = datum(heute.t0, true) + lokal(heute.t0).y;
    $("pv-sub").textContent = "D+0 bis D+15 · gerechnet " + datum(heute.issue) + " " + uhr(heute.issue) +
      " Uhr nach den 00Z-Wetterläufen · Modell " + heute.modell;
  }
  function kzHeute() {
    var l = mae(heute, "L", heute.issue, heute.t1), u = maeUenb(heute, heute.issue, heute.t1);
    var fest = heute.P.filter(function (p) { return p.fest; });
    var bis = fest.length ? fest[fest.length - 1].t : null;
    $("pv-kz").innerHTML =
      "<span>Last seit Prognose (" + (l ? l.n : 0) + " h): MAE Modell <b>" + (l ? f1(l.mae) : "–") +
      " GW</b>, ÜNB <b>" + (u ? f1(u.mae) : "–") + " GW</b></span>" +
      (bis ? "<span>Preis bis " + datum(bis, true) + " aus der Auktion bekannt, Prognose danach</span>" : "");
  }
  function relTag(x) {
    var t = Math.round((heute.t0 - x.t0) / TAG);
    return t === 1 ? "Vortag" : "vor " + t + " Tagen";
  }
  function spark(x) {
    var w = 200, h = 26, lo = Infinity, hi = -Infinity, span = x.t1 - x.t0;
    var ist = stunden(x.t0, x.t1, Ist.preis);
    x.P.forEach(function (p) { lo = Math.min(lo, p.p50); hi = Math.max(hi, p.p50); });
    ist.forEach(function (p) { if (p[1] != null) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); } });
    if (!isFinite(lo) || hi === lo) return "";
    var fx = function (t) { return ((t - x.t0) / span) * w; }, fy = function (v) { return h - 2 - ((v - lo) / (hi - lo)) * (h - 4); };
    return '<svg viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" aria-hidden="true">' +
      '<path class="pp" d="' + pfad(x.P.map(function (p) { return [p.t, p.p50]; }), fx, fy) + '" vector-effect="non-scaling-stroke"/>' +
      '<path class="ist" d="' + pfad(ist, fx, fy) + '" vector-effect="non-scaling-stroke"/></svg>';
  }
  function liste() {
    var frueher = LAEUFE.slice(1), box = $("pv-laeufe");
    $("pv-frueher").hidden = frueher.length === 0;
    $("pv-erste").hidden = frueher.length > 0;
    if (!frueher.length) return;
    $("pv-frueher-titel").textContent = "Frühere Prognosen (" + frueher.length + ") · " +
      datum(frueher[0].t0) + (frueher.length > 1 ? " bis " + datum(frueher[frueher.length - 1].t0) : "");
    var offen = new Set(Array.prototype.map.call(box.querySelectorAll("details[open]"), function (d) { return d.id; }));
    box.innerHTML = "";
    frueher.forEach(function (x) {
      var l = mae(x, "L", x.issue, x.t1), p = mae(x, "P", x.issue, x.t1);
      var d1 = [tagPlus(x.t0, 1), tagPlus(x.t0, 2)], a = mae(x, "L", d1[0], d1[1]), b = maeUenb(x, d1[0], d1[1]);
      var det = document.createElement("details");
      det.className = "pv-lauf"; det.id = "pv-lauf-" + x.t0;
      det.innerHTML = '<summary><span class="chev" aria-hidden="true">▸</span>' +
        '<span class="datum"><b>' + datum(x.t0, true) + "</b> <span>" + relTag(x) + "</span>" +
        (x.modell !== heute.modell ? ' <span class="pv-chip" title="' + esc(x.modell) + '">anderes Modell</span>' : "") + "</span>" +
        '<span class="spark">' + spark(x) + "</span>" +
        '<span class="m">Last <b>' + (l ? f1(l.mae) : "–") + "</b> GW · Preis <b>" + (p ? f0(p.mae) : "–") + "</b> €/MWh" +
        (a && b && a.n >= 12 ? "<br>Last D+1: Modell <b>" + f1(a.mae) + "</b> · ÜNB <b>" + f1(b.mae) + "</b> GW" : "") +
        "</span></summary>" +
        '<div class="innen"><div class="pv-chart"></div><div class="pv-modell">Modell ' + esc(x.modell) + "</div></div>";
      det.addEventListener("toggle", function () { if (det.open) zeichne(det.querySelector(".pv-chart"), x, true); });
      box.appendChild(det);
      if (offen.has(det.id)) det.open = true;
    });
    heatmap();
  }

  var hmZiel = "P";
  function heatmap() {
    var frueher = LAEUFE.slice(1), tab = $("pv-heat");
    var werte = frueher.map(function (x) {
      var out = [];
      for (var d = 0; d < TAGE; d++) {
        var f = mae(x, hmZiel, Math.max(x.issue, tagPlus(x.t0, d)), tagPlus(x.t0, d + 1));
        out.push(f && f.n >= 12 ? f.mae : null);
      }
      return out;
    });
    var alle = [].concat.apply([], werte).filter(function (v) { return v != null; });
    $("pv-heat-box").hidden = alle.length === 0;
    if (!alle.length) return;
    var lo = Math.min.apply(null, alle), hi = Math.max.apply(null, alle), sp = hi - lo || 1;
    var farbe = function (v) { return "color-mix(in oklab, var(--heat1) " + Math.round(8 + (92 * (v - lo)) / sp) + "%, var(--heat0))"; };
    var fmt = hmZiel === "P" ? f0 : f1, e = hmZiel === "P" ? " €/MWh" : " GW";
    var s = "<thead><tr><th></th>";
    for (var d = 0; d < TAGE; d++) s += '<th scope="col">D+' + d + "</th>";
    s += "</tr></thead><tbody>";
    frueher.forEach(function (x, r) {
      s += '<tr><th scope="row" data-t="' + x.t0 + '">' + datum(x.t0) + "</th>";
      werte[r].forEach(function (v, d) {
        if (v == null) { s += '<td class="leer"></td>'; return; }
        var dunkel = (v - lo) / sp > 0.55;
        s += '<td data-t="' + x.t0 + '" style="background:' + farbe(v) + ";color:" + (dunkel ? "var(--card)" : "var(--text)") +
          '" title="' + datum(x.t0) + " D+" + d + ": MAE " + fmt(v) + e + '">' + fmt(v) + "</td>";
      });
      s += "</tr>";
    });
    tab.innerHTML = s + "</tbody>";
    $("pv-hm-min").textContent = fmt(lo) + e;
    $("pv-hm-max").textContent = fmt(hi) + e;
    $("pv-hm-skala").style.background = "linear-gradient(90deg, " + farbe(lo) + ", " + farbe(hi) + ")";
  }

  function allesZeichnen() {
    zeichne($("pv-chart-heute"), heute, false);
    kzHeute();
    liste();
  }

  function verdrahten() {
    $("pv-heat").addEventListener("click", function (ev) {
      var z = ev.target.closest("[data-t]"); if (!z) return;
      var det = $("pv-lauf-" + z.dataset.t);
      if (det) { det.open = true; det.scrollIntoView({ behavior: "smooth", block: "start" }); }
    });
    function setHm(z) {
      hmZiel = z;
      $("pv-hm-preis").setAttribute("aria-pressed", String(z === "P"));
      $("pv-hm-last").setAttribute("aria-pressed", String(z === "L"));
      heatmap();
    }
    $("pv-hm-preis").addEventListener("click", function () { setHm("P"); });
    $("pv-hm-last").addEventListener("click", function () { setHm("L"); });
    $("pv-alle-auf").addEventListener("click", function () { $("pv-laeufe").querySelectorAll("details").forEach(function (d) { d.open = true; }); });
    $("pv-alle-zu").addEventListener("click", function () { $("pv-laeufe").querySelectorAll("details").forEach(function (d) { d.open = false; }); });
    var rz;
    window.addEventListener("resize", function () {
      clearTimeout(rz);
      rz = setTimeout(function () {
        zeichne($("pv-chart-heute"), heute, false);
        $("pv-laeufe").querySelectorAll("details[open]").forEach(function (d) {
          var x = LAEUFE.find(function (y) { return "pv-lauf-" + y.t0 === d.id; });
          if (x) zeichne(d.querySelector(".pv-chart"), x, true);
        });
      }, 150);
    });
  }

  json(BASIS + "index.json").then(function (idx) {
    var tage = (idx.days || []).slice().sort(function (a, b) { return a.day < b.day ? 1 : -1; }).slice(0, 15);
    return Promise.all(tage.map(function (t) {
      return json(BASIS + encodeURIComponent(t.file)).catch(function () { return null; });
    }));
  }).then(function (docs) {
    LAEUFE = docs.filter(function (d) { return d && d.targets; }).map(lauf)
      .sort(function (a, b) { return b.t0 - a.t0; });
    if (!LAEUFE.length) throw new Error("keine Prognosen");
    heute = LAEUFE[0];
    wurzel.hidden = false;
    kopf(); verdrahten(); allesZeichnen();
    return istLaden(LAEUFE).then(allesZeichnen);
  }).catch(function (e) {
    if (!heute) { wurzel.hidden = true; return; }
    console.warn("Prognoseverlauf:", e);
  });
})();
