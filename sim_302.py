#!/usr/bin/env python3
"""sim_302.py - Simula il comportamento del CDN "rotto sui Samsung": MPD servito
200 diretto, ma OGNI segmento (.m4s) risponde con un 302 verso una posizione
alternativa (edge) che poi serve 200. Replica r.dcs.redcdn.pl.

Eseguilo DENTRO la cartella che contiene stream.mpd + p0/ p1/ ... (es. out/real):
    cd out/real
    python3 /percorso/sim_302.py --port 18080

Opzioni:
    --port N        porta (default 18080)
    --alt-host H    host:porta per il redirect dei segmenti (default: stesso host
                    della richiesta -> simula solo cambio di path). Metti l'IP:porta
                    di QUESTA macchina per restare raggiungibile dalla TV, oppure
                    un secondo IP per simulare il cambio di host come l'originale.
    --redirect-mpd  redirige con 302 anche lo stream.mpd (default: no, come l'originale)

Il redirect dei segmenti usa il prefisso /e/ : /p0/x.m4s -> /e/p0/x.m4s (200).
"""

from __future__ import annotations

import argparse
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]

        # Segmenti non ancora "edged" -> 302 verso /e/<path> (eventuale host alt).
        if path.endswith(".m4s") and not path.startswith("/e/"):
            host = ARGS.alt_host or self.headers.get("Host") or "localhost"
            location = "http://%s/e%s" % (host, path)
            self.send_response(302, "Moved Temporarily")
            self.send_header("Location", location)
            self.send_header("Content-Length", "1")  # come redcdn
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b" ")
            return

        # Redirect opzionale del manifest (NON conforme all'originale: solo per prove).
        if path.endswith(".mpd") and ARGS.redirect_mpd and not path.startswith("/e/"):
            host = self.headers.get("Host") or "localhost"
            self.send_response(302, "Found")
            self.send_header("Location", "http://%s/e%s" % (host, path))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Serve il file reale: /e/<path> mappa sullo stesso file di <path>.
        rel = path[3:] if path.startswith("/e/") else path
        rel = rel.lstrip("/")
        fp = os.path.join(ARGS.root, rel)
        if not os.path.isfile(fp):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ctype = ("application/dash+xml" if fp.endswith(".mpd")
                 else "video/iso.segment" if fp.endswith(".m4s")
                 else "application/octet-stream")
        with open(fp, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))


def main():
    global ARGS
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=18080)
    p.add_argument("--alt-host", default=None,
                   help="host:porta per il 302 dei segmenti (default: stesso host)")
    p.add_argument("--redirect-mpd", action="store_true")
    p.add_argument("--root", default=".")
    ARGS = p.parse_args()
    ARGS.root = os.path.abspath(ARGS.root)
    srv = ThreadingHTTPServer(("0.0.0.0", ARGS.port), Handler)
    print("sim_302 su :%d  root=%s  alt_host=%s  redirect_mpd=%s"
          % (ARGS.port, ARGS.root, ARGS.alt_host, ARGS.redirect_mpd))
    print("  MPD: 200 diretto  |  .m4s: 302 -> /e/...  (come redcdn)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
