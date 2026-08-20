(function () {
    "use strict";

    var localUrl = "http://192.168.0.178:8097/";
    var publicUrl = "https://patrick-lamphier.com/fluxa/";
    var launchStartedAt = Date.now();
    var minimumIntroMs = 4700;
    var maximumStartupMs = 20000;
    var selectedUrl = null;
    var appReportedReady = false;
    var revealed = false;
    var probe = new Image();
    var introVideo = document.getElementById("launch-video");
    var appFrame = document.getElementById("app-frame");
    var pendingMediaAction = null;
    var mediaKeyActions = {};
    var introReplayTimer = null;

    function report(eventName, detail, reason) {
        var destination = selectedUrl || localUrl;
        try {
            var request = new XMLHttpRequest();
            request.open("POST", destination + "api/playback-events", true);
            request.setRequestHeader("Content-Type", "application/json");
            request.send(JSON.stringify({
                event: eventName,
                hls_detail: typeof detail === "string" ? detail : JSON.stringify(detail || []),
                hls_reason: reason || "",
                client_version: "tizen-launcher-1.0.13",
                tizen_mode: true
            }));
        } catch (_error) {}
    }

    function registerRemoteKeys() {
        if (!window.tizen || !window.tizen.tvinputdevice) {
            report("tv_launcher_remote_api_unavailable", [], "tizen.tvinputdevice unavailable");
            return;
        }
        var mediaKeys = [
            ["MediaPlayPause", "toggle", 10252], ["MediaPlay", "play", 415],
            ["MediaPause", "pause", 19], ["MediaStop", "stop", 413],
            ["MediaRewind", "rewind", 412], ["MediaFastForward", "fast-forward", 417],
            ["MediaTrackPrevious", "previous", 10232], ["MediaTrackNext", "next", 10233]
        ];
        mediaKeys.forEach(function (entry) {
            var name = entry[0];
            var action = entry[1];
            var code = entry[2];
            try {
                var key = window.tizen.tvinputdevice.getKey(name);
                if (key && typeof key.code === "number") { code = key.code; }
                // Samsung owns the physical Play/Pause launcher button and uses
                // it to open the TV's compact transport popup. Registering that
                // launcher key suppresses the popup. The individual choices in
                // the popup are registered below and still reach Fluxa.
                if (name !== "MediaPlayPause") {
                    window.tizen.tvinputdevice.registerKey(name);
                }
            } catch (_error) {}
            mediaKeyActions[code] = action;
        });
        try {
            var supported = Array.prototype.slice.call(
                window.tizen.tvinputdevice.getSupportedKeys() || []
            );
            var branded = supported.filter(function (key) {
                return /netflix|prime|amazon|disney|tvplus|samsungtv/i.test(key.name || "");
            }).map(function (key) { return { name: key.name, code: key.code }; });
            report(branded.length ? "tv_launcher_brand_keys_detected" : "tv_launcher_brand_keys_not_exposed",
                branded, "supported-key-count=" + supported.length);
        } catch (error) {
            report("tv_launcher_brand_key_detection_failed", [], error.message || String(error));
        }
    }

    function forwardMediaAction(action) {
        if (!appFrame || !appFrame.contentWindow) { return; }
        if (!appReportedReady) {
            pendingMediaAction = action;
            return;
        }
        appFrame.contentWindow.postMessage({ type: "fluxa-tv-media-key", action: action }, "*");
    }

    document.addEventListener("keydown", function (event) {
        var action = mediaKeyActions[event.keyCode];
        if (!action) { return; }
        report("tv_launcher_media_key", [], action + " code=" + event.keyCode);
        forwardMediaAction(action);
    }, true);

    function revealWhenReady() {
        if (revealed || !appReportedReady || !selectedUrl) { return; }
        var remaining = Math.max(0, minimumIntroMs - (Date.now() - launchStartedAt));
        window.setTimeout(function () {
            if (revealed) { return; }
            revealed = true;
            document.body.className = "app-ready";
            appFrame.setAttribute("aria-hidden", "false");
            try { appFrame.contentWindow.focus(); } catch (_error) {}
            if (pendingMediaAction) {
                var action = pendingMediaAction;
                pendingMediaAction = null;
                forwardMediaAction(action);
            }
        }, remaining);
    }

    window.addEventListener("message", function (event) {
        if (!event.data || event.data.type !== "fluxa-tv-ready") { return; }
        if (event.source !== appFrame.contentWindow) { return; }
        appReportedReady = true;
        report("tv_shell_hosted_ui_ready", [], event.data.screen || "unknown");
        revealWhenReady();
    });

    function loadFluxa(url) {
        if (selectedUrl) { return; }
        selectedUrl = url;
        appFrame.src = url + (url.indexOf("?") === -1 ? "?" : "&") + "platform=tizen&tv_shell=1";
    }

    if (introVideo) {
        introVideo.muted = true;
        introVideo.loop = false;
        introVideo.addEventListener("ended", function () {
            window.clearTimeout(introReplayTimer);
            introReplayTimer = window.setTimeout(function () {
                if (revealed) { return; }
                introVideo.currentTime = 0;
                var replay = introVideo.play();
                if (replay && replay.catch) { replay.catch(function () {}); }
            }, 2000);
        });
        var playback = introVideo.play();
        if (playback && playback.catch) { playback.catch(function () {}); }
    }

    registerRemoteKeys();
    probe.onload = function () { loadFluxa(localUrl); };
    probe.onerror = function () { loadFluxa(publicUrl); };
    probe.src = localUrl + "favicon.svg?tv-probe=" + Date.now();
    window.setTimeout(function () { loadFluxa(publicUrl); }, 1800);
    window.setTimeout(function () {
        if (!revealed && selectedUrl) {
            report("tv_shell_startup_timeout", [], "Hosted interface did not report ready");
            appReportedReady = true;
            revealWhenReady();
        }
    }, maximumStartupMs);
}());
