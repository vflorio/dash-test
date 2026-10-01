#!/usr/bin/env python3
"""fetch_real.py - Scarica il media REALE di un MPD DAI e lo ripacchettizza con
un manifest LOCALE (BaseURL relativa, niente SSAI, niente redirect 302).

Scopo: esperimento decisivo. Se il media reale servito da un manifest locale
"pulito" fallisce comunque sui Samsung => la causa e' nel bitstream (SPS/VUI/
priming). Se funziona => la causa e' nella delivery (redirect 302 / SSAI).

Uso:
  python3 fetch_real.py <mpd_url_o_file> [--periods 0,1] [--out out/real]
Esempio:
  python3 fetch_real.py http://hbbtv.prod-wbd.serversideai.com/.../xxx.mpd --periods 0,1
Poi:
  cd out/real && python3 -m http.server 8000
  # apri http://<ip>:8000/stream.mpd sulla TV (o via tv-inject.js)
"""

from __future__ import annotations

import os
import sys
import urllib.request
import xml.etree.ElementTree as ET

NS = "urn:mpeg:dash:schema:mpd:2011"
ET.register_namespace("", NS)


def q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def seg_count(template) -> int:
    tl = template.find(q("SegmentTimeline"))
    if tl is None:
        return 0
    n = 0
    for s in tl.findall(q("S")):
        n += int(s.get("r", "0")) + 1
    return n


def media_name(pattern: str, rep_id: str, number: int) -> str:
    out = pattern.replace("$RepresentationID$", rep_id)
    # gestisce $Number%0Nd$ e $Number$
    if "$Number%" in out:
        pre, rest = out.split("$Number%", 1)
        fmt, post = rest.split("$", 1)
        out = pre + ("%0" + fmt if fmt.startswith("0") else "%" + fmt) % number + post
    else:
        out = out.replace("$Number$", str(number))
    return out


def download(url: str, dest: str) -> int:
    req = urllib.request.Request(url, headers={"User-Agent": "fetch_real/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # segue i 302
        data = resp.read()
    with open(dest, "wb") as fh:
        fh.write(data)
    return len(data)


def load_mpd(src: str) -> ET.ElementTree:
    if src.startswith("http://") or src.startswith("https://"):
        req = urllib.request.Request(src, headers={"User-Agent": "fetch_real/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        return ET.ElementTree(ET.fromstring(raw))
    return ET.parse(src)


def iso_duration(seconds: float) -> str:
    s = round(seconds, 3)
    if s == int(s):
        s = int(s)
    return f"PT{s}S"


def parse_iso_seconds(text: str) -> float:
    # supporta PT#H#M#S / P0D
    if not text or not text.startswith("P"):
        return 0.0
    import re
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?", text)
    if not m:
        return 0.0
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 2
    src = argv[0]
    periods_filter = None
    out_dir = "out/real"
    i = 1
    while i < len(argv):
        if argv[i] == "--periods" and i + 1 < len(argv):
            periods_filter = [int(x) for x in argv[i + 1].split(",")]
            i += 2
        elif argv[i] == "--out" and i + 1 < len(argv):
            out_dir = argv[i + 1]
            i += 2
        else:
            i += 1

    tree = load_mpd(src)
    root = tree.getroot()
    all_periods = root.findall(q("Period"))
    os.makedirs(out_dir, exist_ok=True)

    selected = []
    total_bytes = 0
    for idx, period in enumerate(all_periods):
        if periods_filter is not None and idx not in periods_filter:
            continue
        base_el = period.find(q("BaseURL"))
        if base_el is None or not base_el.text:
            print(f"  [SKIP] Period {idx}: nessun BaseURL")
            continue
        base = base_el.text.strip()
        pdir_name = f"p{idx}"
        pdir = os.path.join(out_dir, pdir_name)
        os.makedirs(pdir, exist_ok=True)
        print(f"== Period {idx}  <- {base}")

        for aset in period.findall(q("AdaptationSet")):
            tmpl_as = aset.find(q("SegmentTemplate"))
            for rep in aset.findall(q("Representation")):
                rep_id = rep.get("id")
                tmpl = rep.find(q("SegmentTemplate"))
                if tmpl is None:
                    tmpl = tmpl_as
                if tmpl is None:
                    continue
                init = tmpl.get("initialization").replace("$RepresentationID$", rep_id)
                media = tmpl.get("media")
                count = seg_count(tmpl)
                # init
                dest = os.path.join(pdir, init)
                sz = download(base + init, dest)
                total_bytes += sz
                # chunks
                for n in range(1, count + 1):
                    name = media_name(media, rep_id, n)
                    sz = download(base + name, os.path.join(pdir, name))
                    total_bytes += sz
                print(f"   rep {rep_id}: {init} + {count} chunk ({count + 1} file)")

        # BaseURL -> relativa locale
        base_el.text = pdir_name + "/"
        selected.append((idx, period))

    if not selected:
        print("Nessun periodo scaricato.")
        return 1

    # Ricostruisci un MPD con solo i periodi scelti, start/durata ricalcolati.
    for child in list(root.findall(q("Period"))):
        root.remove(child)
    start = 0.0
    for idx, period in selected:
        dur_text = period.get("duration")
        dur = parse_iso_seconds(dur_text) if dur_text else 0.0
        period.set("start", iso_duration(start))
        root.append(period)
        start += dur
    root.set("mediaPresentationDuration", iso_duration(start))

    out_mpd = os.path.join(out_dir, "stream.mpd")
    tree.write(out_mpd, encoding="UTF-8", xml_declaration=True)
    print(f"\nScaricati {total_bytes/1e6:.1f} MB. Manifest locale: {out_mpd}")
    print(f"Servi con:  cd {out_dir} && python3 -m http.server 8000")
    print("Poi apri http://<ip>:8000/stream.mpd sulla TV (o via tv-inject.js).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
