(function () {
    "use strict";

    var CONFIG = {
        urls: [
            "https://vflorio.github.io/dash-test/out/v10_single_missing_lang.mpd",
            "https://vflorio.github.io/dash-test/out/v00_baseline.mpd",
            "https://vflorio.github.io/dash-test/out/v01_consistent_audio_lang.mpd",
            "https://vflorio.github.io/dash-test/out/v02_progressive_no_scantype.mpd",
            "https://vflorio.github.io/dash-test/out/v03_period_continuity.mpd",
            "https://vflorio.github.io/dash-test/out/v04_aac_lc.mpd",
            "https://vflorio.github.io/dash-test/out/v05_single_period.mpd",
            "https://vflorio.github.io/dash-test/out/v06_all_fixes.mpd",
            "https://vflorio.github.io/dash-test/out/v07_missing_lang_attr.mpd",
            "https://vflorio.github.io/dash-test/out/v08_many_periods.mpd",
            "https://vflorio.github.io/dash-test/out/v09_faithful_v2.mpd",
        ],

        backend: "html5", advanceAfter: 0, stallTimeout: 15, gap: 1.5, loop: false
    };

    var ui = (function () {
        function el(tag, css, parent) {
            var e = document.createElement(tag);
            if (css) e.style.cssText = css;
            (parent || document.body).appendChild(e);
            return e;
        }
        var root = el("div",
            "position:fixed;left:0;top:0;right:0;bottom:0;z-index:2147483600;" +
            "pointer-events:none;font-family:monospace;");
        var banner = el("div",
            "position:absolute;left:40px;top:200px;right:40px;padding:10px 16px;" +
            "background:rgba(0,0,0,.72);color:#fff;font-size:26px;line-height:1.3;" +
            "border-left:8px solid #1d6fe0;border-radius:4px;", root);
        var logBox = el("div",
            "position:absolute;left:40px;bottom:30px;width:60%;max-height:45%;" +
            "overflow:hidden;padding:8px 12px;background:rgba(0,0,0,.6);color:#9f9;" +
            "font-size:18px;line-height:1.35;border-radius:4px;white-space:pre-wrap;", root);
        var lines = [];
        function setBanner(html, color) {
            banner.innerHTML = html;
            banner.style.borderLeftColor = color || "#1d6fe0";
        }
        function log(msg) {
            var ts = new Date().toISOString().substr(11, 12);
            var line = "[" + ts + "] " + msg;
            lines.push(line);
            if (lines.length > 16) lines.shift();
            logBox.textContent = lines.join("\n");
            try { console.log("[dashTest] " + msg); } catch (e) { }
        }
        return { setBanner: setBanner, log: log, root: root };
    })();

    function log(m) { ui.log(m); }
    function shortUrl(u) {
        var s = String(u).split("?")[0];
        var p = s.split("/");
        return p[p.length - 1] || s;
    }

    function stopBroadcast() {
        var vbs = document.querySelectorAll('object[type="video/broadcast"]');
        if (!vbs.length) {
            try {
                var ob = document.createElement("object");
                ob.type = "video/broadcast";
                ob.style.cssText = "position:absolute;left:-10px;top:-10px;width:1px;height:1px";
                document.body.appendChild(ob);
                vbs = [ob];
            } catch (e) { log("video/broadcast non creabile: " + e); return; }
        }
        for (var i = 0; i < vbs.length; i++) {
            var vb = vbs[i];
            ["stop", "release", "releaseScalers"].forEach(function (m) {
                try {
                    if (typeof vb[m] === "function") { vb[m](); log("broadcast." + m + "()"); }
                } catch (e) { log("broadcast." + m + " err: " + e); }
            });
        }
    }

    function Html5Backend() {
        var video = document.createElement("video");
        video.id = "__dt_video";
        video.setAttribute("width", "1280");
        video.setAttribute("height", "720");
        video.style.cssText =
            "position:fixed;left:0;top:0;width:100%;height:100%;background:#000;" +
            "z-index:2147483000;";
        document.body.appendChild(video);
        var self = this;
        this.name = "html5";

        ["loadedmetadata", "playing", "waiting", "stalled", "canplay",
            "ended", "error"].forEach(function (ev) {
                video.addEventListener(ev, function () {
                    if (ev === "error") {
                        var er = video.error || {};
                        self.onError && self.onError("MediaError code=" + er.code +
                            (er.message ? " " + er.message : ""));
                    } else if (ev === "ended") {
                        self.onEnded && self.onEnded();
                    } else {
                        self.onEvent && self.onEvent(ev);
                    }
                }, false);
            });

        this.start = function (url) {
            try { while (video.firstChild) video.removeChild(video.firstChild); } catch (e) { }
            var src = document.createElement("source");
            src.setAttribute("type", "application/dash+xml");
            src.setAttribute("src", url);
            video.appendChild(src);
            try { video.load(); } catch (e) { }
            var p = video.play();
            if (p && p.catch) p.catch(function (e) { log("play() rifiutato: " + e); });
        };
        this.stop = function () {
            try { video.pause(); } catch (e) { }
            try { while (video.firstChild) video.removeChild(video.firstChild); } catch (e) { }
            try { video.removeAttribute("src"); video.load(); } catch (e) { }
        };
        this.currentTime = function () { return video.currentTime || 0; };
        this.destroy = function () { try { document.body.removeChild(video); } catch (e) { } };
    }

    function AvControlBackend() {
        var obj = document.createElement("object");
        obj.setAttribute("type", "application/dash+xml");
        obj.style.cssText =
            "position:fixed;left:0;top:0;width:100%;height:100%;background:#000;" +
            "z-index:2147483000;";
        document.body.appendChild(obj);
        var self = this;
        this.name = "avcontrol";

        obj.onPlayStateChange = function (state, err) {
            self.onEvent && self.onEvent("playState=" + state);
            if (state === 6) self.onError && self.onError("A/V error " + (err == null ? "" : err));
            else if (state === 5) self.onEnded && self.onEnded();
        };
        obj.onError = function (err) { self.onError && self.onError("A/V onError " + err); };

        this.start = function (url) {
            try { obj.setAttribute("data", url); } catch (e) { }
            try { obj.data = url; } catch (e) { }
            try { if (typeof obj.play === "function") obj.play(1); } catch (e) { log("play(1) err: " + e); }
        };
        this.stop = function () { try { if (typeof obj.stop === "function") obj.stop(); } catch (e) { } };
        this.currentTime = function () {
            try { return (obj.playPosition || 0) / 1000; } catch (e) { return 0; }
        };
        this.destroy = function () { try { document.body.removeChild(obj); } catch (e) { } };
    }

    function Controller(urls, opts) {
        this.urls = urls.slice();
        this.opts = opts;
        this.i = -1;
        this.backend = null;
        this.watch = null;
        this.lastT = 0;
        this.lastMove = 0;
        this.finished = false;
    }

    Controller.prototype.makeBackend = function () {
        return this.opts.backend === "avcontrol" ? new AvControlBackend() : new Html5Backend();
    };

    Controller.prototype.clearWatch = function () {
        if (this.watch) { clearInterval(this.watch); this.watch = null; }
    };

    Controller.prototype.finish = function (status, note) {
        if (this.finished) return;
        this.finished = true;
        this.clearWatch();
        var url = this.urls[this.i];
        log(shortUrl(url) + " => " + status.toUpperCase() + (note ? " (" + note + ")" : ""));
        var self = this;
        setTimeout(function () { self.next(); }, Math.max(0, (this.opts.gap || 0) * 1000));
    };

    Controller.prototype.next = function () {
        this.clearWatch();
        this.i++;
        if (this.i >= this.urls.length) {
            if (this.opts.loop) { this.i = -1; return this.next(); }
            ui.setBanner("&#10003; SEQUENZA COMPLETATA (" + this.urls.length + " URL)", "#2ecc71");
            log("=== fine sequenza ===");
            return;
        }
        var url = this.urls[this.i];
        this.finished = false;
        this.lastT = 0;
        this.lastMove = Date.now();

        ui.setBanner("&#9654; TEST " + (this.i + 1) + "/" + this.urls.length +
            " &mdash; " + shortUrl(url) + " &mdash; <span style='color:#ffd23f'>PLAYING</span>");
        log("> " + url);

        if (!this.backend) {
            this.backend = this.makeBackend();
            var self = this;
            this.backend.onEvent = function (ev) { log("  ev: " + ev); };
            this.backend.onEnded = function () { self.finish("ok", "ended " + self.backend.currentTime().toFixed(1) + "s"); };
            this.backend.onError = function (msg) { self.finish("error", msg); };
            log("backend: " + this.backend.name);
        }

        try {
            this.backend.start(url);
        } catch (e) {
            return this.finish("error", "start: " + e);
        }
        this.startWatch();
    };

    Controller.prototype.startWatch = function () {
        var self = this;
        this.watch = setInterval(function () {
            if (self.finished) return;
            var now = Date.now();
            var t = self.backend.currentTime();
            if (t > self.lastT + 0.05) { self.lastT = t; self.lastMove = now; }
            var adv = self.opts.advanceAfter || 0;
            if (adv > 0 && t >= adv) return self.finish("ok", "advanceAfter " + t.toFixed(1) + "s");
            var stall = (self.opts.stallTimeout || 15) * 1000;
            if (now - self.lastMove > stall) {
                self.finish("stall", "nessun avanzamento " +
                    ((now - self.lastMove) / 1000).toFixed(0) + "s @ " + t.toFixed(1) + "s");
            }
        }, 1000);
    };

    Controller.prototype.stop = function () {
        this.clearWatch();
        this.finished = true;
        if (this.backend) { try { this.backend.stop(); } catch (e) { } }
        ui.setBanner("&#9209; STOP", "#e74c3c");
        log("stop manuale");
    };

    function start(urls, opts) {
        var o = {};
        for (var k in CONFIG) o[k] = CONFIG[k];
        if (opts) for (var k2 in opts) o[k2] = opts[k2];
        var list = (urls && urls.length) ? urls : o.urls;

        if (window.__dashTest && window.__dashTest._ctrl) {
            try { window.__dashTest._ctrl.stop(); } catch (e) { }
        }
        log("=== avvio test (" + list.length + " URL, backend=" + o.backend + ") ===");
        stopBroadcast();
        var ctrl = new Controller(list, o);
        window.__dashTest = {
            _ctrl: ctrl,
            next: function () { ctrl.finished = true; ctrl.next(); },
            stop: function () { ctrl.stop(); },
            start: function (u, op) { return start(u, op); }
        };
        ctrl.next();
        return window.__dashTest;
    }

    start(CONFIG.urls, CONFIG);
})();
