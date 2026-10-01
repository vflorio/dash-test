#!/usr/bin/env python3
"""Assembla varianti di manifest DASH HbbTV per il test di compatibilita' Samsung.

Legge gli MPD generati da ffmpeg (uno per "gruppo" di encoding dentro build/)
ed emette in out/ una serie di manifest multi-periodo, ognuno dei quali isola
UNA caratteristica sospetta di rompere la riproduzione sulle TV Samsung/Tizen.

Riferimenti: profilo HbbTV/OIPF -> DVB-DASH (ETSI TS 103 285) + DASH-IF IOP v5.
Non scarica nulla: lavora solo su file locali gia' prodotti da generate.sh.
"""

from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET

NS = "urn:mpeg:dash:schema:mpd:2011"
ET.register_namespace("", NS)

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(ROOT, "build")
OUT = os.path.join(ROOT, "out")


def _resolve_base_url() -> str:
    """BaseURL assoluta opzionale per hosting remoto (GitHub raw / Pages).

    Uso:
      DASH_BASE_URL="https://raw.githubusercontent.com/vflorio/dash-test/main/" \\
        python3 build_manifests.py
      python3 build_manifests.py --base-url https://vflorio.github.io/dash-test/
    Vuota => BaseURL relativa "../build/<gruppo>/" (hosting locale).
    """
    val = os.environ.get("DASH_BASE_URL", "")
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--base-url" and i + 1 < len(argv):
            val = argv[i + 1]
        elif a.startswith("--base-url="):
            val = a.split("=", 1)[1]
    val = val.strip()
    if val and not val.endswith("/"):
        val += "/"
    return val


BASE_URL = _resolve_base_url()


def q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def fmt_dur(seconds: float) -> str:
    """Formatta una durata in notazione ISO-8601 (PTxS) senza zeri inutili."""
    s = round(seconds, 3)
    if s == int(s):
        s = int(s)
    return f"PT{s}S"


class Group:
    """Rappresenta un gruppo di media (3 video + 1 audio) prodotto da ffmpeg."""

    def __init__(self, name: str):
        self.name = name
        self.baseurl = f"{BASE_URL}build/{name}/" if BASE_URL else f"../build/{name}/"
        mpd_path = os.path.join(BUILD, name, "stream.mpd")
        if not os.path.exists(mpd_path):
            raise FileNotFoundError(mpd_path)
        tree = ET.parse(mpd_path)
        root = tree.getroot()
        period = root.find(q("Period"))
        self.video = None  # dict
        self.audio = None  # dict
        for aset in period.findall(q("AdaptationSet")):
            info = self._parse_adaptationset(aset)
            if info["contentType"] == "audio":
                self.audio = info
            else:
                self.video = info
        if not self.video or not self.audio:
            raise ValueError(f"Gruppo {name}: manca video o audio nell'MPD ffmpeg")

    def _parse_adaptationset(self, aset) -> dict:
        reps = aset.findall(q("Representation"))
        # ffmpeg mette SegmentTemplate a livello di AdaptationSet.
        st = aset.find(q("SegmentTemplate"))
        if st is None and reps:
            st = reps[0].find(q("SegmentTemplate"))
        timescale = st.get("timescale")
        timeline = st.find(q("SegmentTimeline"))
        segs = []
        for s in timeline.findall(q("S")):
            segs.append({
                "t": s.get("t"),
                "d": s.get("d"),
                "r": s.get("r"),
            })
        rep_list = []
        content_type = None
        for rep in reps:
            mime = rep.get("mimeType", "")
            if mime.startswith("audio"):
                content_type = "audio"
            elif mime.startswith("video"):
                content_type = "video"
            acc = rep.find(q("AudioChannelConfiguration"))
            rep_list.append({
                "id": rep.get("id"),
                "mimeType": mime,
                "codecs": rep.get("codecs"),
                "bandwidth": rep.get("bandwidth"),
                "width": rep.get("width"),
                "height": rep.get("height"),
                "audioSamplingRate": rep.get("audioSamplingRate"),
                "channels": acc.get("value") if acc is not None else None,
            })
        return {
            "contentType": content_type or aset.get("contentType"),
            "timescale": timescale,
            "init": st.get("initialization"),
            "media": st.get("media"),
            "startNumber": st.get("startNumber", "1"),
            "segments": segs,
            "reps": rep_list,
        }

    def duration_seconds(self) -> float:
        info = self.video
        ts = float(info["timescale"])
        total = 0.0
        for s in info["segments"]:
            r = int(s["r"]) if s["r"] is not None else 0
            total += float(s["d"]) * (r + 1)
        return total / ts


# --------------------------------------------------------------------------
# Rendering XML
# --------------------------------------------------------------------------

def render_timeline(segs: list[dict], indent: str) -> str:
    lines = [f"{indent}<SegmentTimeline>"]
    for s in segs:
        attrs = ""
        if s["t"] is not None:
            attrs += f' t="{s["t"]}"'
        attrs += f' d="{s["d"]}"'
        if s["r"] is not None and s["r"] != "0":
            attrs += f' r="{s["r"]}"'
        lines.append(f'{indent}  <S{attrs}/>')
    lines.append(f"{indent}</SegmentTimeline>")
    return "\n".join(lines)


def render_video_adaptationset(group: Group, as_id: str, scan_type: str | None,
                               continuity_value: str | None) -> str:
    v = group.video
    widths = [int(r["width"]) for r in v["reps"]]
    heights = [int(r["height"]) for r in v["reps"]]
    ind = "      "
    out = []
    out.append(
        f'{ind}<AdaptationSet id="{as_id}" contentType="video" startWithSAP="1" '
        f'segmentAlignment="true" bitstreamSwitching="true" frameRate="25/1" '
        f'maxWidth="{max(widths)}" maxHeight="{max(heights)}" par="16:9">'
    )
    if continuity_value is not None:
        out.append(
            f'{ind}  <SupplementalProperty '
            f'schemeIdUri="urn:mpeg:dash:period-continuity:2015" '
            f'value="{continuity_value}"/>'
        )
    for r in v["reps"]:
        scan_attr = f' scanType="{scan_type}"' if scan_type else ""
        out.append(
            f'{ind}  <Representation id="{r["id"]}" mimeType="{r["mimeType"]}" '
            f'codecs="{r["codecs"]}" bandwidth="{r["bandwidth"]}" '
            f'width="{r["width"]}" height="{r["height"]}"{scan_attr} sar="1:1">'
        )
        out.append(
            f'{ind}    <SegmentTemplate timescale="{v["timescale"]}" '
            f'initialization="{v["init"]}" media="{v["media"]}" '
            f'startNumber="{v["startNumber"]}">'
        )
        out.append(render_timeline(v["segments"], ind + "      "))
        out.append(f'{ind}    </SegmentTemplate>')
        out.append(f'{ind}  </Representation>')
    out.append(f'{ind}</AdaptationSet>')
    return "\n".join(out)


def render_audio_adaptationset(group: Group, as_id: str, lang: str | None,
                               continuity_value: str | None) -> str:
    a = group.audio
    r = a["reps"][0]
    ind = "      "
    out = []
    # lang=None => attributo @lang del tutto assente (caso del secondo MPD rotto,
    # diverso da lang="und": alcuni parser Tizen li trattano in modo diverso).
    lang_attr = f' lang="{lang}"' if lang is not None else ""
    out.append(
        f'{ind}<AdaptationSet id="{as_id}" contentType="audio" startWithSAP="1" '
        f'segmentAlignment="true" bitstreamSwitching="true"{lang_attr}>'
    )
    out.append(
        f'{ind}  <Role schemeIdUri="urn:mpeg:dash:role:2011" value="main"/>'
    )
    if continuity_value is not None:
        out.append(
            f'{ind}  <SupplementalProperty '
            f'schemeIdUri="urn:mpeg:dash:period-continuity:2015" '
            f'value="{continuity_value}"/>'
        )
    out.append(
        f'{ind}  <Representation id="{r["id"]}" mimeType="{r["mimeType"]}" '
        f'codecs="{r["codecs"]}" bandwidth="{r["bandwidth"]}" '
        f'audioSamplingRate="{r["audioSamplingRate"]}">'
    )
    out.append(
        f'{ind}    <AudioChannelConfiguration '
        f'schemeIdUri="urn:mpeg:dash:23003:3:audio_channel_configuration:2011" '
        f'value="{r["channels"] or "2"}"/>'
    )
    out.append(
        f'{ind}    <SegmentTemplate timescale="{a["timescale"]}" '
        f'initialization="{a["init"]}" media="{a["media"]}" '
        f'startNumber="{a["startNumber"]}">'
    )
    out.append(render_timeline(a["segments"], ind + "      "))
    out.append(f'{ind}    </SegmentTemplate>')
    out.append(f'{ind}  </Representation>')
    out.append(f'{ind}</AdaptationSet>')
    return "\n".join(out)


def render_period(period_id: str, start: str, duration: float, group: Group,
                  lang: str | None, scan_type: str | None, continuity: bool,
                  consistent_as_id: bool) -> str:
    # Con consistent_as_id gli AdaptationSet mantengono lo stesso id tra periodi
    # (requisito per la period-continuity DASH-IF).
    video_as_id = "0"
    audio_as_id = "1"
    cont_v = video_as_id if (continuity and consistent_as_id) else None
    cont_a = audio_as_id if (continuity and consistent_as_id) else None
    out = []
    out.append(f'  <Period id="{period_id}" start="{start}" duration="{fmt_dur(duration)}">')
    out.append(f'    <BaseURL>{group.baseurl}</BaseURL>')
    out.append(render_video_adaptationset(group, video_as_id, scan_type, cont_v))
    out.append(render_audio_adaptationset(group, audio_as_id, lang, cont_a))
    out.append('  </Period>')
    return "\n".join(out)


def build_variant(spec: dict) -> str:
    periods_xml = []
    total = 0.0
    start = 0.0
    period_specs = spec["periods"]
    for idx, p in enumerate(period_specs):
        group = Group(p["group"])
        dur = group.duration_seconds()
        periods_xml.append(render_period(
            period_id=str(idx),
            start=fmt_dur(start),
            duration=dur,
            group=group,
            lang=p.get("lang"),
            scan_type=p.get("scanType"),
            continuity=spec.get("continuity", False),
            consistent_as_id=spec.get("consistent_as_id", False),
        ))
        start += dur
        total += dur

    header = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<MPD xmlns="{NS}" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'profiles="urn:hbbtv:dash:profile:isoff-live:2012" '
        'type="static" maxSegmentDuration="PT1.92S" minBufferTime="PT3.84S" '
        f'mediaPresentationDuration="{fmt_dur(total)}">\n'
    )
    body = "\n".join(periods_xml)
    return header + body + "\n</MPD>\n"


# --------------------------------------------------------------------------
# Definizione delle varianti
# --------------------------------------------------------------------------

def alternating_periods(n_breaks: int, content_group: str, ad_group: str,
                        content_lang: str | None, ad_lang: str | None,
                        scan: str | None = "interlaced") -> list[dict]:
    """Sequenza content/ad ripetuta, per replicare un DAI reale con molte
    transizioni di periodo (il secondo MPD rotto ne ha 10)."""
    periods = []
    for _ in range(n_breaks):
        periods.append({"group": content_group, "lang": content_lang, "scanType": scan})
        periods.append({"group": ad_group, "lang": ad_lang, "scanType": scan})
    return periods


VARIANTS = [
    {
        "name": "v00_baseline",
        "desc": "Riproduzione fedele del manifest rotto: multi-periodo, lingua "
                "pol->und, scanType interlaced solo sul Period0, HE-AAC, nessun "
                "segnale di continuita'. ATTESO: fallisce su Samsung.",
        "periods": [
            {"group": "content_ildct_heaac", "lang": "pol", "scanType": "interlaced"},
            {"group": "ad_prog_heaac", "lang": "und"},
        ],
    },
    {
        "name": "v01_consistent_audio_lang",
        "desc": "Come baseline ma stessa lingua audio su entrambi i periodi "
                "(pol/pol). Isola l'ipotesi 'cambio @lang al period boundary'.",
        "periods": [
            {"group": "content_ildct_heaac", "lang": "pol", "scanType": "interlaced"},
            {"group": "ad_prog_heaac", "lang": "pol"},
        ],
    },
    {
        "name": "v02_progressive_no_scantype",
        "desc": "Come baseline ma nessun scanType interlaced (video progressivo "
                "coerente tra i periodi). Isola l'ipotesi interlaced.",
        "periods": [
            {"group": "content_prog_heaac", "lang": "pol"},
            {"group": "ad_prog_heaac", "lang": "und"},
        ],
    },
    {
        "name": "v03_period_continuity",
        "desc": "Come baseline + lingua coerente + SupplementalProperty "
                "period-continuity:2015 e AdaptationSet @id costanti tra periodi. "
                "Isola l'ipotesi 'manca il segnale di transizione seamless'.",
        "continuity": True,
        "consistent_as_id": True,
        "periods": [
            {"group": "content_ildct_heaac", "lang": "pol", "scanType": "interlaced"},
            {"group": "ad_prog_heaac", "lang": "pol"},
        ],
    },
    {
        "name": "v04_aac_lc",
        "desc": "Come baseline ma audio AAC-LC (mp4a.40.2) invece di HE-AAC "
                "(mp4a.40.5). Isola l'ipotesi codec audio.",
        "periods": [
            {"group": "content_ildct_aaclc", "lang": "pol", "scanType": "interlaced"},
            {"group": "ad_prog_aaclc", "lang": "und"},
        ],
    },
    {
        "name": "v05_single_period",
        "desc": "Contenuto+spot concatenati in un UNICO periodo progressivo. "
                "Controllo: elimina del tutto la transizione di periodo.",
        "periods": [
            {"group": "concat_prog_heaac", "lang": "und"},
        ],
    },
    {
        "name": "v06_all_fixes",
        "desc": "Tutte le correzioni insieme: progressivo, lingua coerente, "
                "AAC-LC, period-continuity + AdaptationSet @id costanti. "
                "Candidato manifest che DOVREBBE funzionare su Samsung.",
        "continuity": True,
        "consistent_as_id": True,
        "periods": [
            {"group": "content_prog_aaclc", "lang": "und"},
            {"group": "ad_prog_aaclc", "lang": "und"},
        ],
    },
    {
        "name": "v07_missing_lang_attr",
        "desc": "Come baseline ma l'AdaptationSet audio dello spot NON ha affatto "
                "l'attributo @lang (invece di 'und'). Isola il secondo MPD rotto: "
                "@lang assente != @lang=und per i parser Tizen.",
        "periods": [
            {"group": "content_ildct_heaac", "lang": "pol", "scanType": "interlaced"},
            {"group": "ad_prog_heaac"},  # nessuna chiave 'lang' => attributo assente
        ],
    },
    {
        "name": "v08_many_periods",
        "desc": "Molti periodi (10) content/spot alternati, TUTTI interlacciati, "
                "HE-AAC, lingua coerente pol ovunque. Isola l'ipotesi 'sono le "
                "tante transizioni di periodo / re-init a rompere', non "
                "l'interlacciamento ne' il cambio di lingua.",
        "periods": alternating_periods(
            5, "content_ildct_heaac", "ad_ildct_heaac",
            content_lang="pol", ad_lang="pol", scan="interlaced"),
    },
    {
        "name": "v09_faithful_v2",
        "desc": "Replica fedele del SECONDO MPD rotto: 10 periodi, tutti "
                "interlacciati, HE-AAC, @lang=pol sul contenuto e ASSENTE sugli "
                "spot, BaseURL distinta per periodo. ATTESO: fallisce su Samsung.",
        "periods": alternating_periods(
            5, "content_ildct_heaac", "ad_ildct_heaac",
            content_lang="pol", ad_lang=None, scan="interlaced"),
    },
]


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    if BASE_URL:
        print(f"  BaseURL assoluta: {BASE_URL}build/<gruppo>/")
        if "github.com" in BASE_URL and "/blob/" in BASE_URL:
            print("  [ATTENZIONE] URL 'blob' = pagina HTML, non il file binario. "
                  "Usa raw.githubusercontent.com/<user>/<repo>/<branch>/ "
                  "oppure GitHub Pages.")
    generated = []
    for spec in VARIANTS:
        try:
            xml = build_variant(spec)
        except FileNotFoundError as exc:
            print(f"  [SKIP] {spec['name']}: media mancante ({exc})")
            continue
        path = os.path.join(OUT, f"{spec['name']}.mpd")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(xml)
        generated.append(spec)
        print(f"  [OK]   out/{spec['name']}.mpd")

    write_index(generated)
    return 0


def write_index(specs: list[dict]) -> None:
    """Genera un banco di test HTML con dash.js: riproduce gli MPD uno per uno,
    in sequenza automatica (Play all) o singolarmente, registrando per ciascuno
    OK / ERRORE / STALLO. Pensato per lo smoke test locale su desktop."""
    variants = [{"name": s["name"], "desc": s["desc"]} for s in specs]
    variants_json = json.dumps(variants, ensure_ascii=False)

    html = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DASH Samsung compat test</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin: 1rem; background:#111; color:#eee; }
  h1 { font-size: 1.3rem; }
  .wrap { display:flex; gap:1rem; flex-wrap:wrap; align-items:flex-start; }
  .left { flex:1 1 520px; min-width:320px; }
  .right { flex:1 1 360px; min-width:300px; }
  video { width:100%; background:#000; border:1px solid #333; }
  .controls { margin:.6rem 0; display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; }
  button { cursor:pointer; padding:.4rem .7rem; background:#2a2a2a; color:#eee;
           border:1px solid #444; border-radius:4px; }
  button:hover { background:#333; }
  button.primary { background:#1d6fe0; border-color:#1d6fe0; }
  ul { list-style:none; padding:0; margin:0; }
  li { display:flex; gap:.5rem; align-items:flex-start; padding:.35rem .4rem;
       border-bottom:1px solid #222; }
  li.active { background:#15233b; }
  .dot { flex:0 0 auto; width:.8rem; height:.8rem; border-radius:50%;
         margin-top:.25rem; background:#555; }
  .dot.ok { background:#2ecc71; } .dot.err { background:#e74c3c; }
  .dot.stall { background:#f39c12; } .dot.play { background:#1d6fe0;
         animation:pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  .vname { font-weight:600; min-width:170px; }
  .vname button { width:100%; text-align:left; }
  .vdesc { opacity:.75; font-size:.85rem; }
  .vres { margin-left:auto; font-size:.8rem; opacity:.9; white-space:nowrap; }
  #log { width:100%; height:220px; background:#000; color:#9f9; border:1px solid #333;
         font-family:ui-monospace, monospace; font-size:.78rem; padding:.4rem; }
  label.quick { font-size:.85rem; opacity:.85; }
  code { color:#6cf; }
</style>
<script src="https://cdn.dashjs.org/latest/dash.all.min.js"></script>
</head>
<body>
<h1>DASH Samsung / HbbTV &mdash; banco di test varianti</h1>
<p>Smoke test locale via dash.js. <strong>Play all</strong> riproduce gli MPD uno
per uno e segna OK / ERRORE / STALLO. Sulla TV Samsung apri invece l'URL del
singolo <code>.mpd</code> nell'app HbbTV.</p>
<p style="color:#f39c12;font-size:.85rem">Nota: serve un browser con decoder
<strong>AAC/HE-AAC</strong> (Chrome o Edge). Il browser integrato di VS Code e
alcune build Chromium OSS non includono AAC e daranno
<code>audio decoder initialization failed</code> su tutte le varianti.</p>
<div class="wrap">
  <div class="left">
    <video id="v" controls muted playsinline></video>
    <div class="controls">
      <button id="playAll" class="primary">&#9654; Play all</button>
      <button id="skip">&#9197; Skip</button>
      <button id="stop">&#9209; Stop</button>
      <label class="quick"><input type="checkbox" id="quick" checked>
        modalita' rapida: avanza dopo <input type="number" id="quickSecs" value="15"
        min="3" max="120" style="width:3.2rem"> s</label>
    </div>
    <div id="summary" class="controls"></div>
    <ul id="list"></ul>
  </div>
  <div class="right">
    <h2 style="font-size:1rem">Log</h2>
    <textarea id="log" readonly></textarea>
    <div class="controls">
      <button id="clearLog">Pulisci log</button>
      <span id="dashver" style="font-size:.8rem;opacity:.7"></span>
    </div>
  </div>
</div>
<script>
  var VARIANTS = __VARIANTS_JSON__;
  var E = dashjs.MediaPlayer.events;
  var video = document.getElementById('v');
  var listEl = document.getElementById('list');
  var logEl = document.getElementById('log');
  var summaryEl = document.getElementById('summary');
  var player = dashjs.MediaPlayer().create();
  player.initialize(video, null, false);
  document.getElementById('dashver').textContent = 'dash.js ' + dashjs.Version;

  var results = {};          // name -> {status, note}
  var runAll = false;        // true durante una sessione "Play all"
  var current = -1;          // indice della variante in riproduzione
  var watchdog = null;       // interval di sorveglianza stallo/durata
  var lastT = 0, lastMove = 0, startedAt = 0, finished = false;
  var STALL_LIMIT = 12000;   // ms senza avanzamento del currentTime => stallo

  function log(msg) {
    var ts = new Date().toISOString().substr(11, 12);
    logEl.value += '[' + ts + '] ' + msg + '\n';
    logEl.scrollTop = logEl.scrollHeight;
  }

  function renderList() {
    listEl.innerHTML = '';
    VARIANTS.forEach(function (v, i) {
      var r = results[v.name] || {};
      var li = document.createElement('li');
      li.id = 'row-' + i;
      if (i === current) li.className = 'active';
      var cls = r.status === 'ok' ? 'ok' : r.status === 'error' ? 'err'
              : r.status === 'stall' ? 'stall' : i === current ? 'play' : '';
      li.innerHTML =
        '<span class="dot ' + cls + '"></span>' +
        '<span class="vname"><button data-i="' + i + '">' + v.name + '</button></span>' +
        '<span class="vdesc">' + v.desc + '</span>' +
        '<span class="vres">' + (r.note || '') + '</span>';
      listEl.appendChild(li);
    });
    listEl.querySelectorAll('button[data-i]').forEach(function (b) {
      b.onclick = function () { runAll = false; startVariant(+b.dataset.i); };
    });
  }

  function renderSummary() {
    var ok = 0, err = 0, st = 0, done = 0;
    VARIANTS.forEach(function (v) {
      var s = (results[v.name] || {}).status;
      if (s && s !== 'playing') done++;
      if (s === 'ok') ok++; else if (s === 'error') err++; else if (s === 'stall') st++;
    });
    summaryEl.textContent = 'Testati ' + done + '/' + VARIANTS.length +
      '  \u2022  OK ' + ok + '  \u2022  ERRORE ' + err + '  \u2022  STALLO ' + st;
  }

  function clearWatchdog() { if (watchdog) { clearInterval(watchdog); watchdog = null; } }

  function finishVariant(status, note) {
    if (finished) return;
    finished = true;
    clearWatchdog();
    var v = VARIANTS[current];
    if (v) {
      results[v.name] = { status: status, note: note || status };
      log(v.name + ' => ' + status.toUpperCase() + (note ? ' (' + note + ')' : ''));
    }
    renderList(); renderSummary();
    if (runAll) {
      if (current + 1 < VARIANTS.length) {
        setTimeout(function () { startVariant(current + 1); }, 600);
      } else {
        runAll = false;
        log('--- Play all completato ---');
      }
    }
  }

  function startVariant(i) {
    clearWatchdog();
    current = i; finished = false;
    var v = VARIANTS[i];
    var url = v.name + '.mpd';
    results[v.name] = { status: 'playing', note: '' };
    renderList(); renderSummary();
    log('> ' + v.name + '  (' + url + ')');
    lastT = 0; lastMove = Date.now(); startedAt = Date.now();
    try {
      player.attachSource(url);
      player.setMute(true);
      var p = video.play();
      if (p && p.catch) p.catch(function () {});
    } catch (e) {
      finishVariant('error', 'attach: ' + e.message);
      return;
    }
    watchdog = setInterval(function () {
      if (finished) return;
      var now = Date.now();
      var t = video.currentTime || 0;
      if (t > lastT + 0.05) { lastT = t; lastMove = now; }
      var quick = document.getElementById('quick').checked;
      var quickSecs = +document.getElementById('quickSecs').value || 15;
      if (quick && t >= quickSecs) { finishVariant('ok', 'quick ' + t.toFixed(1) + 's'); return; }
      if (now - lastMove > STALL_LIMIT) {
        finishVariant('stall', 'nessun avanzamento ' + ((now - lastMove) / 1000).toFixed(0) + 's @ ' + t.toFixed(1) + 's');
      }
    }, 1000);
  }

  // Eventi dash.js --------------------------------------------------------
  player.on(E.PLAYBACK_ENDED, function () {
    log('PLAYBACK_ENDED @ ' + (video.currentTime || 0).toFixed(1) + 's');
    finishVariant('ok', 'ended ' + (video.currentTime || 0).toFixed(1) + 's');
  });
  player.on(E.ERROR, function (e) {
    var m = (e && e.error && (e.error.message || e.error.code)) || JSON.stringify(e && e.error) || 'ERROR';
    log('ERROR: ' + m);
    finishVariant('error', String(m).substr(0, 60));
  });
  player.on(E.PLAYBACK_ERROR, function (e) {
    log('PLAYBACK_ERROR: ' + JSON.stringify(e && e.error).substr(0, 120));
    finishVariant('error', 'playback');
  });
  player.on(E.PLAYBACK_STALLED, function () { log('PLAYBACK_STALLED'); });
  player.on(E.PERIOD_SWITCH_COMPLETED, function (e) {
    var idx = e && e.toStreamInfo ? e.toStreamInfo.index : '?';
    log('  period switch -> #' + idx);
  });

  // Controlli -------------------------------------------------------------
  document.getElementById('playAll').onclick = function () {
    results = {}; runAll = true; log('=== Play all (' + VARIANTS.length + ' varianti) ===');
    startVariant(0);
  };
  document.getElementById('skip').onclick = function () {
    if (current >= 0) finishVariant('stall', 'skip manuale');
  };
  document.getElementById('stop').onclick = function () {
    runAll = false; clearWatchdog(); finished = true;
    try { player.attachSource(null); } catch (e) {}
    video.pause(); log('Stop.');
  };
  document.getElementById('clearLog').onclick = function () { logEl.value = ''; };

  renderList(); renderSummary();
</script>
</body>
</html>
"""
    html = html.replace("__VARIANTS_JSON__", variants_json)
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    print("  [OK]   out/index.html")


if __name__ == "__main__":
    sys.exit(main())
