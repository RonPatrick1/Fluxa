(function () {
    "use strict";

    var basePath = window.location.pathname.replace(/\/(?:index\.html)?$/, "");
    if (basePath === "/") { basePath = ""; }
    var isTizenTv = window.location.search.indexOf("platform=tizen") !== -1;
    var isTeslaBrowser = detectTeslaBrowser();
    var clientVersion = "20260820-35";
    var fredPlayerSessionKey = "fluxa-fredplayer-session-v1";

    function detectTeslaBrowser() {
        var userAgent = navigator.userAgent || "";
        if (/Tesla|QtCarBrowser/i.test(userAgent)) { return true; }

        // Recent Tesla firmware removes its identifying token and presents as
        // ordinary desktop Chrome on X11. Use the remaining car-browser
        // fingerprint, based on physical rather than CSS pixels so Tesla's
        // device-pixel-ratio changes do not break detection.
        if (!/\(X11;\s+(?:GNU\/)?Linux\s+x86_64\)/i.test(userAgent)
                || !/Chrome\/\d+/i.test(userAgent)
                || (navigator.maxTouchPoints || 0) < 10) {
            return false;
        }
        var ratio = Number(window.devicePixelRatio) || 1;
        var width = Math.round(Math.max(window.screen.width, window.screen.height) * ratio);
        var height = Math.round(Math.min(window.screen.width, window.screen.height) * ratio);
        var standardPanel = width >= 1840 && width <= 2000 && height >= 1120 && height <= 1280;
        var largePanel = width >= 2100 && width <= 2320 && height >= 1200 && height <= 1420;
        return standardPanel || largePanel;
    }

    var state = {
        libraries: [],
        playlists: [],
        items: [],
        filter: new URLSearchParams(window.location.search).get("music") === "1" ? "audio" : "all",
        library: null,
        playlist: null,
        folder: "",
        search: "",
        currentItem: null,
        player: null,
        lastProgressSent: 0,
        analysisTimer: null,
        toastTimer: null,
        openToken: 0,
        playQueue: [],
        queueOriginal: [],
        queueIndex: -1,
        queueShuffled: false,
        repeatMode: "none",
        compatibilitySession: null,
        compatibilityControlQueue: Promise.resolve(),
        compatibilityHeartbeat: null,
        hls: null,
        playbackOffsetMs: 0,
        suppressPlaybackControls: false,
        compatibilityStarting: false,
        playerControls: null,
        captionsEnabled: false,
        captionTrackOrdinal: null,
        seeking: false,
        volume: 1,
        muted: false,
        transitionFrame: null,
        stallStartedAt: null,
        controlsHideTimer: null,
        closingPlayer: false,
        lastFullscreenActive: false,
        compressorEnabled: true,
        compressorThreshold: -24,
        compressorRatio: 8,
        compressorOutputGain: 9,
        compressorCeiling: -3,
        compressorAttack: 15,
        compressorRelease: 750,
        compressorKnee: 4,
        bassEnhancement: getStoredPreference("fluxa-bass-enhancement")
            ? getStoredPreference("fluxa-bass-enhancement") === "yes"
            : isTizenTv,
        bassGain: boundedNumber(getStoredPreference("fluxa-bass-gain"), 0, 9, 4),
        globalCompressor: null,
        videoCompressor: null,
        compressorSource: "global",
        mediaPage: 0,
        mediaPageSize: isTizenTv ? 25 : 120,
        mediaTotal: 0,
        mediaPageChanging: false,
        folders: [],
        mediaView: getStoredPreference("fluxa-media-view") === "list" ? "list" : "grid",
        welcomeHidden: getStoredPreference("fluxa-hide-welcome") === "yes",
        fredplayerOnly: new URLSearchParams(window.location.search).get("music") === "1",
        fredplayerAuthorized: getStoredPreference("fluxa-fredplayer-authorized") === "yes",
        fredplayerFrameReady: false,
        musicView: "albums",
        musicCollection: null,
        pendingMusicQueue: null,
        playbackSamplePositionMs: null,
        playbackSampleAt: null,
        collectionStartPending: false,
        videoMediaSessionInstalled: false,
        playerGeometryFrame: null,
        playerControlsResizeObserver: null,
        pauseReason: "",
        lastPlaybackToggleAt: 0,
        playerAssignedAt: 0
    };

    var elements = {};

    document.addEventListener("DOMContentLoaded", function () {
        elements.serverState = document.getElementById("server-state");
        elements.serverCopy = elements.serverState.querySelector(".status-copy");
        elements.search = document.getElementById("search-input");
        elements.libraryNavigation = document.getElementById("library-navigation");
        elements.playlistNavigation = document.getElementById("playlist-navigation");
        elements.grid = document.getElementById("media-grid");
        elements.musicTabs = document.getElementById("music-browser-tabs");
        elements.musicBack = document.getElementById("music-back-button");
        elements.pagination = document.getElementById("media-pagination");
        elements.previousPage = document.getElementById("previous-media-page");
        elements.nextPage = document.getElementById("next-media-page");
        elements.pageStatus = document.getElementById("media-page-status");
        elements.empty = document.getElementById("empty-state");
        elements.count = document.getElementById("result-count");
        elements.context = document.getElementById("section-context");
        elements.collectionTitle = document.getElementById("collection-title");
        elements.folderNav = document.getElementById("folder-nav");
        elements.hero = document.querySelector(".hero");
        elements.main = document.getElementById("library");
        elements.playlistActions = document.getElementById("playlist-actions");
        elements.playPlaylist = document.getElementById("play-playlist");
        elements.shufflePlaylist = document.getElementById("shuffle-playlist");
        elements.addToPlaylist = document.getElementById("add-to-playlist");
        elements.newPlaylist = document.getElementById("new-playlist-button");
        elements.newPlaylistToolbar = document.getElementById("new-playlist-toolbar");
        elements.playlistPicker = document.getElementById("playlist-picker");
        elements.playlistPickerTitle = document.getElementById("playlist-picker-title");
        elements.playlistPickerCopy = document.getElementById("playlist-picker-copy");
        elements.playlistPickerList = document.getElementById("playlist-picker-list");
        elements.playlistPickerForm = document.getElementById("playlist-picker-create");
        elements.playlistPickerName = document.getElementById("playlist-picker-name");
        elements.playlistPickerCreate = document.getElementById("playlist-picker-create-button");
        elements.playlistPickerCancel = document.getElementById("playlist-picker-cancel");
        elements.gridView = document.getElementById("grid-view");
        elements.listView = document.getElementById("list-view");
        elements.sidebar = document.querySelector(".sidebar");
        elements.sidebarResizer = document.getElementById("sidebar-resizer");
        elements.musicWorkspace = document.getElementById("music-workspace");
        elements.scan = document.getElementById("scan-button");
        elements.scanToolbar = document.getElementById("scan-toolbar-button");
        elements.hideWelcome = document.getElementById("hide-welcome-panel");
        elements.storageNote = document.getElementById("storage-note");
        elements.modal = document.getElementById("player-modal");
        elements.playerPanel = elements.modal.querySelector(".player-panel");
        elements.stage = document.getElementById("player-stage");
        elements.playerDetails = document.querySelector(".player-details");
        elements.playerLibrary = document.getElementById("player-library");
        elements.playerTitle = document.getElementById("player-title");
        elements.playerTechnical = document.getElementById("player-technical");
        elements.analyze = document.getElementById("analyze-button");
        elements.analysisTitle = document.getElementById("analysis-title");
        elements.analysisCopy = document.getElementById("analysis-copy");
        elements.captionsCurrent = document.getElementById("captions-current");
        elements.captionsGlobal = document.getElementById("captions-global");
        elements.captionTrack = document.getElementById("caption-track");
        elements.bassEnhancement = document.getElementById("bass-enhancement");
        elements.bassGain = document.getElementById("bass-gain");
        elements.bassGainValue = document.getElementById("bass-gain-value");
        elements.compressorEnabled = document.getElementById("compressor-enabled");
        elements.compressorThreshold = document.getElementById("compressor-threshold");
        elements.compressorThresholdValue = document.getElementById("compressor-threshold-value");
        elements.compressorRatio = document.getElementById("compressor-ratio");
        elements.compressorRatioValue = document.getElementById("compressor-ratio-value");
        elements.compressorOutputGain = document.getElementById("compressor-output-gain");
        elements.compressorOutputGainValue = document.getElementById("compressor-output-gain-value");
        elements.compressorCeiling = document.getElementById("compressor-ceiling");
        elements.compressorCeilingValue = document.getElementById("compressor-ceiling-value");
        elements.compressorAttack = document.getElementById("compressor-attack");
        elements.compressorAttackValue = document.getElementById("compressor-attack-value");
        elements.compressorRelease = document.getElementById("compressor-release");
        elements.compressorReleaseValue = document.getElementById("compressor-release-value");
        elements.compressorKnee = document.getElementById("compressor-knee");
        elements.compressorKneeValue = document.getElementById("compressor-knee-value");
        elements.compressorScope = document.getElementById("compressor-scope");
        elements.compressorCreateVideo = document.getElementById("compressor-create-video");
        elements.compressorVideoToGlobal = document.getElementById("compressor-video-to-global");
        elements.compressorGlobalToVideo = document.getElementById("compressor-global-to-video");
        elements.compressorFollowGlobal = document.getElementById("compressor-follow-global");
        elements.toast = document.getElementById("toast");
        elements.fredPlayerLayer = document.getElementById("fredplayer-layer");
        elements.fredPlayerFrame = document.getElementById("fredplayer-frame");
        elements.fredPlayerLogout = document.getElementById("fredplayer-logout-button");
        elements.logout = document.getElementById("logout-button");

        bindNavigation();
        bindMusicWorkspace();
        bindMusicBrowser();
        bindViewControls();
        bindSidebarResizer();
        bindWelcomePanel();
        bindPlayer();
        installVideoMediaSession();
        bindFredPlayerFrame();
        registerTizenRemoteKeys();
        bindRemoteKeys();
        elements.playPlaylist.addEventListener("click", function () { startActiveCollection(false); });
        elements.shufflePlaylist.addEventListener("click", function () { startActiveCollection(true); });
        bindPlaylistPicker();
        elements.previousPage.addEventListener("click", function () { changeMediaPage(-1); });
        elements.nextPage.addEventListener("click", function () { changeMediaPage(1); });
        elements.fredPlayerLogout.addEventListener("click", logoutFredPlayer);
        elements.logout.addEventListener("click", logout);
        showSkeletons();
        if (state.fredplayerAuthorized && !isTizenTv) { ensureFredPlayerFrame(); }
        handleFredPlayerReturn().then(loadApplication);
    });

    window.addEventListener("pagehide", function () {
        if (state.compatibilitySession) {
            closeCompatibilitySession(state.compatibilitySession.session_id);
        }
    });

    function api(path, options) {
        var requestOptions = options || {};
        var fredSession = isTizenTv ? getStoredPreference(fredPlayerSessionKey) : "";
        if (fredSession) {
            requestOptions.headers = Object.assign({}, requestOptions.headers || {}, {
                "X-Fluxa-FredPlayer-Session": fredSession
            });
        }
        return fetch(appUrl(path), requestOptions).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (body) {
                if (!response.ok) {
                    throw new Error(body.error || ("Request failed with status " + response.status));
                }
                return body;
            });
        });
    }

    function loadApplication() {
        Promise.all([
            api("/api/status"),
            api("/api/libraries"),
            api("/api/playlists"),
            api("/api/auth/status"),
            api("/api/compressor-settings"),
            api("/api/fredplayer/status")
        ]).then(function (responses) {
            var status = responses[0];
            state.libraries = responses[1].libraries || [];
            state.playlists = responses[2].playlists || [];
            elements.logout.hidden = !responses[3].public_proxy;
            configureCompressor(responses[4]);
            configureBass();
            state.fredplayerAuthorized = responses[5].authenticated === true;
            setStoredPreference("fluxa-fredplayer-authorized", state.fredplayerAuthorized ? "yes" : "no");
            elements.fredPlayerLogout.hidden = !state.fredplayerAuthorized;
            if (state.fredplayerAuthorized && !isTizenTv) { ensureFredPlayerFrame(); }
            setOnline(status);
            renderPlaylists();
            renderLibraries();
            if (state.fredplayerOnly) {
                setActiveNavigation(elements.musicWorkspace);
            }
            updateHeading();
            document.body.classList.remove("app-booting");
            return loadMedia().then(function () {
                if (isTizenTv && window.parent !== window) {
                    window.parent.postMessage({ type: "fluxa-tv-ready", screen: "library" }, "*");
                }
            });
        }).catch(function (error) {
            document.body.classList.remove("app-booting");
            setOffline(error.message);
            showToast(error.message, true);
            elements.grid.innerHTML = "";
            elements.empty.hidden = false;
        });
    }

    function setOnline(status) {
        elements.serverState.classList.remove("offline");
        elements.serverState.classList.add("online");
        elements.serverCopy.textContent = status.media.available + " items local";
        elements.storageNote.textContent = formatBytes(status.storage.media_copied_bytes) + " of media copied";
    }

    function setOffline(message) {
        elements.serverState.classList.remove("online");
        elements.serverState.classList.add("offline");
        elements.serverCopy.textContent = message || "Server unavailable";
    }

    function bindNavigation() {
        var navigation = document.querySelectorAll("[data-filter]");
        var i;
        for (i = 0; i < navigation.length; i += 1) {
            navigation[i].addEventListener("click", function (event) {
                state.mediaPage = 0;
                state.filter = event.currentTarget.getAttribute("data-filter");
                state.library = null;
                state.playlist = null;
                state.folder = "";
                state.fredplayerOnly = false;
                setMusicLocation(false);
                setActiveNavigation(event.currentTarget);
                updateHeading();
                loadMedia();
            });
        }
        elements.search.addEventListener("input", debounce(function () {
            state.mediaPage = 0;
            state.search = elements.search.value.trim();
            updateHeading();
            loadMedia();
        }, 260));
        elements.scan.addEventListener("click", scanLibraries);
        elements.scanToolbar.addEventListener("click", scanLibraries);
    }

    function bindMusicWorkspace() {
        elements.musicWorkspace.addEventListener("click", function () {
            state.mediaPage = 0;
            state.filter = "audio";
            state.library = null;
            state.playlist = null;
            state.folder = "";
            state.fredplayerOnly = true;
            state.musicView = "albums";
            state.musicCollection = null;
            setMusicLocation(true);
            setActiveNavigation(elements.musicWorkspace);
            updateHeading();
            loadMedia();
        });
    }

    function bindMusicBrowser() {
        elements.musicTabs.querySelectorAll("[data-music-view]").forEach(function (button) {
            button.addEventListener("click", function () {
                state.mediaPage = 0;
                state.musicView = button.getAttribute("data-music-view");
                state.musicCollection = null;
                updateMusicBrowser();
                updateHeading();
                loadMedia();
            });
        });
        elements.musicBack.addEventListener("click", function () {
            state.mediaPage = 0;
            state.musicCollection = null;
            updateMusicBrowser();
            updateHeading();
            loadMedia();
        });
    }

    function updateMusicBrowser() {
        elements.musicTabs.hidden = !state.fredplayerOnly || !state.fredplayerAuthorized;
        elements.musicBack.hidden = !state.musicCollection;
        elements.musicTabs.querySelectorAll("[data-music-view]").forEach(function (button) {
            button.classList.toggle("active", button.getAttribute("data-music-view") === state.musicView
                && !state.musicCollection);
        });
    }

    function fredPlayerUrl(parameters) {
        var url = new URL("https://patrick-lamphier.com/fredplayer-media/web/");
        url.searchParams.set("fluxa", "1");
        url.searchParams.set("return", parameters.returnUrl || window.location.href);
        if (parameters.authorize) { url.searchParams.set("authorize", "1"); }
        if (parameters.play) { url.searchParams.set("play", parameters.play); }
        if (parameters.embedded) { url.searchParams.set("embedded", "1"); }
        if (isTizenTv) { url.searchParams.set("platform", "tizen"); }
        return url.href;
    }

    function musicReturnUrl() {
        var url = new URL(window.location.href);
        url.searchParams.set("music", "1");
        url.searchParams.delete("fred_grant");
        return url.href;
    }

    function setMusicLocation(enabled) {
        var url = new URL(window.location.href);
        if (enabled) { url.searchParams.set("music", "1"); }
        else {
            url.searchParams.delete("music");
            url.searchParams.delete("fred_grant");
        }
        window.history.replaceState({}, "", url.href);
    }

    function openFredPlayerAuthorization() {
        window.location.assign(fredPlayerUrl({ authorize: true, returnUrl: musicReturnUrl() }));
    }

    function openFredPlayerTrack(item) {
        if (!item || !item.fredplayer_path) {
            showToast("This track is not in FredPlayer's music library.", true);
            return;
        }
        if (isTizenTv) {
            openFredPlayerTrackOnTv(item.fredplayer_path);
            return;
        }
        if (openTrackInEmbeddedFredPlayer(item.fredplayer_path)) { return; }
        window.location.assign(fredPlayerUrl({ play: item.fredplayer_path, returnUrl: window.location.href }));
    }

    function fredPlayerQueueMessage(paths, sourceName, sourceKind, shuffle, startPath) {
        return {
            type: "fluxa-fredplayer-queue",
            paths: paths,
            sourceName: sourceName,
            sourceKind: sourceKind,
            shuffle: shuffle === true,
            startPath: startPath || ""
        };
    }

    function openFredPlayerQueue(paths, sourceName, sourceKind, shuffle, startPath) {
        if (!paths.length) {
            showToast("This collection has no playable tracks.", true);
            return;
        }
        var message = fredPlayerQueueMessage(paths, sourceName, sourceKind, shuffle, startPath);
        if (isTizenTv) {
            api("/api/fredplayer/launch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    track_paths: paths,
                    source_name: sourceName,
                    source_kind: sourceKind,
                    shuffle: shuffle === true,
                    start_path: startPath || "",
                    return_url: window.location.href
                })
            }).then(function (result) {
                window.location.assign("https://patrick-lamphier.com/fredplayer-media/web/auth/fluxa-launch/"
                    + encodeURIComponent(result.ticket));
            }).catch(function (error) {
                showToast(error.message, true);
            });
            return;
        }
        ensureFredPlayerFrame();
        state.pendingMusicQueue = message;
        if (!sendPendingFredPlayerQueue()) {
            showToast("Opening " + sourceName + " in FredPlayer…");
        }
    }

    function sendPendingFredPlayerQueue() {
        var message = state.pendingMusicQueue;
        if (!message || !elements.fredPlayerFrame || !elements.fredPlayerFrame.contentWindow) { return false; }
        try {
            var bridge = elements.fredPlayerFrame.contentWindow.FluxaFredPlayer;
            if (bridge && typeof bridge.playQueue === "function") {
                elements.fredPlayerLayer.hidden = false;
                document.body.classList.add("fredplayer-open");
                state.pendingMusicQueue = null;
                bridge.playQueue(message.paths, message.sourceName, message.sourceKind,
                    message.shuffle, message.startPath);
                return true;
            }
        } catch (_error) {}
        if (!state.fredplayerFrameReady) { return false; }
        elements.fredPlayerLayer.hidden = false;
        document.body.classList.add("fredplayer-open");
        elements.fredPlayerFrame.contentWindow.postMessage(message, "https://patrick-lamphier.com");
        state.pendingMusicQueue = null;
        return true;
    }

    function openFredPlayerTrackOnTv(path) {
        api("/api/fredplayer/launch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ track_path: path, return_url: window.location.href })
        }).then(function (result) {
            window.location.assign("https://patrick-lamphier.com/fredplayer-media/web/auth/fluxa-launch/"
                + encodeURIComponent(result.ticket));
        }).catch(function (error) {
            showToast(error.message, true);
        });
    }

    function logoutFredPlayer() {
        elements.fredPlayerLogout.disabled = true;
        api("/api/fredplayer/logout", { method: "POST" }).catch(function () {}).then(function () {
            setStoredPreference(fredPlayerSessionKey, "");
            state.fredplayerAuthorized = false;
            state.fredplayerFrameReady = false;
            setStoredPreference("fluxa-fredplayer-authorized", "no");
            var returnUrl = new URL(window.location.href);
            returnUrl.searchParams.delete("fred_grant");
            var url = new URL(fredPlayerUrl({ returnUrl: returnUrl.href }));
            url.searchParams.set("logout", "1");
            window.location.assign(url.href);
        });
    }

    function ensureFredPlayerFrame() {
        if (!elements.fredPlayerFrame || elements.fredPlayerFrame.src) { return; }
        elements.fredPlayerFrame.src = fredPlayerUrl({
            embedded: true,
            returnUrl: window.location.href
        });
    }

    function openTrackInEmbeddedFredPlayer(path) {
        ensureFredPlayerFrame();
        if (!elements.fredPlayerFrame || !elements.fredPlayerFrame.contentWindow) { return false; }
        try {
            var bridge = elements.fredPlayerFrame.contentWindow.FluxaFredPlayer;
            if (!bridge || typeof bridge.playPath !== "function") { return false; }
            elements.fredPlayerLayer.hidden = false;
            document.body.classList.add("fredplayer-open");
            if (bridge.playPath(path) !== true) {
                closeEmbeddedFredPlayer();
                return false;
            }
            return true;
        } catch (_error) {
            if (!state.fredplayerFrameReady) { return false; }
            elements.fredPlayerLayer.hidden = false;
            document.body.classList.add("fredplayer-open");
            elements.fredPlayerFrame.contentWindow.postMessage({
                type: "fluxa-fredplayer-play",
                path: path
            }, "https://patrick-lamphier.com");
            return true;
        }
    }

    function closeEmbeddedFredPlayer() {
        if (elements.fredPlayerLayer) { elements.fredPlayerLayer.hidden = true; }
        document.body.classList.remove("fredplayer-open");
        try {
            var bridge = elements.fredPlayerFrame.contentWindow.FluxaFredPlayer;
            if (bridge && typeof bridge.stop === "function") { bridge.stop(); }
        } catch (_error) {
            elements.fredPlayerFrame.contentWindow.postMessage({
                type: "fluxa-fredplayer-stop"
            }, "https://patrick-lamphier.com");
        }
        focusFirstMediaCard();
    }

    function bindFredPlayerFrame() {
        window.addEventListener("message", function (event) {
            if (event.source !== elements.fredPlayerFrame.contentWindow) { return; }
            if (!event.data) { return; }
            if (event.data.type === "fluxa-fredplayer-ready") {
                state.fredplayerFrameReady = true;
                sendPendingFredPlayerQueue();
            } else if (event.data.type === "fluxa-fredplayer-close") {
                closeEmbeddedFredPlayer();
            }
        });
    }

    function handleFredPlayerReturn() {
        var url = new URL(window.location.href);
        var grant = url.searchParams.get("fred_grant");
        if (!grant) { return Promise.resolve(); }
        return api("/api/fredplayer/authorize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ grant: grant })
        }).then(function (result) {
            state.fredplayerAuthorized = true;
            state.fredplayerOnly = true;
            state.filter = "audio";
            if (isTizenTv && result.access_token) {
                setStoredPreference(fredPlayerSessionKey, result.access_token);
            }
            setStoredPreference("fluxa-fredplayer-authorized", "yes");
        }).catch(function (error) {
            state.fredplayerAuthorized = false;
            setStoredPreference("fluxa-fredplayer-authorized", "no");
            showToast(error.message, true);
        }).then(function () {
            url.searchParams.delete("fred_grant");
            window.history.replaceState({}, "", url.href);
        });
    }

    function bindWelcomePanel() {
        elements.hideWelcome.checked = state.welcomeHidden;
        elements.hero.hidden = state.welcomeHidden;
        elements.hideWelcome.addEventListener("change", function () {
            if (!elements.hideWelcome.checked) { return; }
            state.welcomeHidden = true;
            setStoredPreference("fluxa-hide-welcome", "yes");
            elements.hero.hidden = true;
            focusElement(elements.scanToolbar);
        });
    }

    function bindViewControls() {
        function selectView(view) {
            state.mediaView = view;
            setStoredPreference("fluxa-media-view", view);
            updateMediaView();
        }
        elements.gridView.addEventListener("click", function () { selectView("grid"); });
        elements.listView.addEventListener("click", function () { selectView("list"); });
        updateMediaView();
    }

    function updateMediaView() {
        var list = state.mediaView === "list";
        elements.grid.classList.toggle("list-view", list);
        elements.gridView.classList.toggle("active", !list);
        elements.listView.classList.toggle("active", list);
        elements.gridView.setAttribute("aria-pressed", String(!list));
        elements.listView.setAttribute("aria-pressed", String(list));
    }

    function bindSidebarResizer() {
        if (isTizenTv || !elements.sidebarResizer) { return; }
        var stored = Number(getStoredPreference("fluxa-sidebar-width"));
        if (stored >= 170 && stored <= 420) { setSidebarWidth(stored); }
        var startX = 0;
        var startWidth = 0;
        function resize(event) {
            setSidebarWidth(startWidth + event.clientX - startX);
        }
        function finish() {
            document.removeEventListener("pointermove", resize);
            document.removeEventListener("pointerup", finish);
            document.body.classList.remove("resizing-sidebar");
            setStoredPreference("fluxa-sidebar-width", String(Math.round(elements.sidebar.offsetWidth)));
        }
        elements.sidebarResizer.addEventListener("pointerdown", function (event) {
            if (window.innerWidth <= 900) { return; }
            startX = event.clientX;
            startWidth = elements.sidebar.offsetWidth;
            document.body.classList.add("resizing-sidebar");
            document.addEventListener("pointermove", resize);
            document.addEventListener("pointerup", finish);
            event.preventDefault();
        });
        elements.sidebarResizer.addEventListener("keydown", function (event) {
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") { return; }
            setSidebarWidth(elements.sidebar.offsetWidth + (event.key === "ArrowRight" ? 12 : -12));
            setStoredPreference("fluxa-sidebar-width", String(Math.round(elements.sidebar.offsetWidth)));
            event.preventDefault();
        });
    }

    function setSidebarWidth(width) {
        var bounded = Math.max(170, Math.min(420, Math.round(width)));
        document.documentElement.style.setProperty("--sidebar-width", bounded + "px");
    }

    function renderLibraries() {
        elements.libraryNavigation.innerHTML = "";
        state.libraries.forEach(function (library) {
            var button = document.createElement("button");
            var glyph = document.createElement("span");
            var name = document.createElement("span");
            var count = document.createElement("span");
            button.className = "nav-item focusable";
            button.setAttribute("data-focusable", "true");
            button.setAttribute("data-library", String(library.id));
            glyph.className = "nav-glyph";
            glyph.textContent = library.kind === "video" ? "▸" : "♪";
            name.textContent = library.name;
            count.className = "library-count";
            count.textContent = String(library.item_count);
            button.appendChild(glyph);
            button.appendChild(name);
            button.appendChild(count);
            button.addEventListener("click", function () {
                state.mediaPage = 0;
                state.library = library.id;
                state.playlist = null;
                state.folder = "";
                state.filter = "all";
                state.fredplayerOnly = false;
                setMusicLocation(false);
                setActiveNavigation(button);
                updateHeading();
                loadMedia();
            });
            elements.libraryNavigation.appendChild(button);
        });
    }

    function renderPlaylists() {
        elements.playlistNavigation.innerHTML = "";
        state.playlists.forEach(function (playlist) {
            var button = document.createElement("button");
            var glyph = document.createElement("span");
            var name = document.createElement("span");
            var count = document.createElement("span");
            button.className = "nav-item focusable";
            button.setAttribute("data-focusable", "true");
            button.setAttribute("data-playlist", String(playlist.id));
            button.title = playlist.title;
            glyph.className = "nav-glyph";
            glyph.textContent = playlist.kind === "audio" ? "♫" : "≡";
            name.className = "nav-text";
            name.textContent = playlist.title;
            count.className = "library-count";
            count.textContent = String(playlist.item_count);
            button.appendChild(glyph);
            button.appendChild(name);
            button.appendChild(count);
            button.addEventListener("click", function () {
                state.mediaPage = 0;
                state.playlist = playlist.id;
                state.library = null;
                state.folder = "";
                state.filter = "all";
                state.fredplayerOnly = false;
                setMusicLocation(false);
                setActiveNavigation(button);
                updateHeading();
                loadMedia();
            });
            elements.playlistNavigation.appendChild(button);
        });
    }

    function setActiveNavigation(active) {
        var buttons = document.querySelectorAll(".nav-item");
        var i;
        for (i = 0; i < buttons.length; i += 1) {
            buttons[i].classList.toggle("active", buttons[i] === active);
        }
    }

    function updateHeading() {
        var selected = findLibrary(state.library);
        var selectedPlaylist = findPlaylist(state.playlist);
        if (state.search && selectedPlaylist) {
            elements.context.textContent = "SEARCH IN PLAYLIST";
            elements.collectionTitle.textContent = selectedPlaylist.title + " · “" + state.search + "”";
        } else if (state.search) {
            elements.context.textContent = "SEARCH RESULTS";
            elements.collectionTitle.textContent = "“" + state.search + "”";
        } else if (selectedPlaylist) {
            elements.context.textContent = "PLAYLIST";
            elements.collectionTitle.textContent = selectedPlaylist.title;
        } else if (selected) {
            elements.context.textContent = selected.kind === "video" ? "VIDEO LIBRARY" : "MUSIC LIBRARY";
            elements.collectionTitle.textContent = selected.name;
        } else if (state.filter === "video") {
            elements.context.textContent = "MOVIES · SHOWS · HOME VIDEO";
            elements.collectionTitle.textContent = "All video";
        } else if (state.fredplayerOnly && state.musicCollection) {
            elements.context.textContent = state.musicCollection.collection_type === "playlist"
                ? "FREDPLAYER PLAYLIST" : "FREDPLAYER ALBUM";
            elements.collectionTitle.textContent = state.musicCollection.title;
        } else if (state.fredplayerOnly && state.musicView === "playlists") {
            elements.context.textContent = "FREDPLAYER MUSIC";
            elements.collectionTitle.textContent = "Playlists";
        } else if (state.fredplayerOnly && state.musicView === "albums") {
            elements.context.textContent = "FREDPLAYER MUSIC";
            elements.collectionTitle.textContent = "Albums";
        } else if (state.filter === "audio") {
            elements.context.textContent = "FREDPLAYER MUSIC";
            elements.collectionTitle.textContent = "All Music";
        } else {
            elements.context.textContent = "LOCAL LIBRARY";
            elements.collectionTitle.textContent = "All media";
        }
        var contextual = Boolean(
            selectedPlaylist || selected || state.filter !== "all" || state.search
        );
        elements.hero.hidden = contextual || state.welcomeHidden;
        elements.main.classList.toggle("contextual", contextual);
        elements.playlistActions.hidden = !selectedPlaylist
            && !(state.fredplayerOnly && state.musicCollection);
        elements.addToPlaylist.hidden = !canAddCurrentResultsToPlaylist();
        var hideNewPlaylist = state.fredplayerOnly;
        if (elements.newPlaylist) { elements.newPlaylist.hidden = hideNewPlaylist; }
        if (elements.newPlaylistToolbar) { elements.newPlaylistToolbar.hidden = hideNewPlaylist; }
        elements.search.placeholder = selectedPlaylist
            ? "Search " + selectedPlaylist.title
            : "Search this library";
        renderFolderNav();
        updateMusicBrowser();
    }

    function loadMedia() {
        state.items = [];
        state.folders = [];
        if (state.fredplayerOnly && !state.fredplayerAuthorized) {
            renderFredPlayerGate();
            return Promise.resolve();
        }
        showSkeletons();
        if (state.fredplayerOnly && !state.musicCollection
                && (state.musicView === "playlists" || state.musicView === "albums")) {
            var collectionParts = [
                "kind=" + encodeURIComponent(state.musicView),
                "limit=" + state.mediaPageSize,
                "offset=" + (state.mediaPage * state.mediaPageSize)
            ];
            if (state.search) { collectionParts.push("q=" + encodeURIComponent(state.search)); }
            return api("/api/fredplayer/collections?" + collectionParts.join("&")).then(function (payload) {
                state.items = payload.items || [];
                renderMusicCollections(payload.total || 0);
            }).catch(renderMediaError);
        }
        if (state.playlist !== null) {
            elements.playPlaylist.disabled = true;
            elements.shufflePlaylist.disabled = true;
            var playlistParts = [];
            if (state.search) {
                playlistParts.push("q=" + encodeURIComponent(state.search));
            }
            var playlistQuery = playlistParts.length ? "?" + playlistParts.join("&") : "";
            return api("/api/playlists/" + state.playlist + playlistQuery).then(function (payload) {
                state.items = payload.items || [];
                renderMedia(payload.total || 0, payload.available || 0);
            }).catch(function (error) {
                elements.grid.innerHTML = "";
                elements.empty.hidden = false;
                elements.count.textContent = "Unavailable";
                showToast(error.message, true);
            });
        }
        if (!state.fredplayerOnly && !state.search && !state.library
                && (state.filter === "all" || state.filter === "video")) {
            state.folders = videoLibraryFolders();
            state.items = [];
            renderMedia(0);
            return Promise.resolve();
        }
        if (!state.fredplayerOnly && state.filter === "all" && !state.library
                && state.fredplayerAuthorized) {
            return loadCombinedAllMedia();
        }
        var parts = [
            "limit=" + state.mediaPageSize,
            "offset=" + (state.mediaPage * state.mediaPageSize)
        ];
        if (state.filter === "video" || state.filter === "audio") {
            parts.push("type=" + encodeURIComponent(state.filter));
        }
        if (state.library) {
            parts.push("library=" + encodeURIComponent(state.library));
            parts.push("folder=" + encodeURIComponent(state.folder || ""));
        }
        if (state.search) {
            parts.push("q=" + encodeURIComponent(state.search));
        }
        if (state.fredplayerOnly && state.musicCollection) {
            elements.playPlaylist.disabled = true;
            elements.shufflePlaylist.disabled = true;
            if (state.musicCollection.collection_type === "playlist") {
                parts.push("playlist=" + encodeURIComponent(state.musicCollection.name));
            } else {
                parts.push("album=" + encodeURIComponent(state.musicCollection.name));
                parts.push("artist=" + encodeURIComponent(state.musicCollection.artist));
            }
        }
        if (state.fredplayerOnly) { parts.push("fredplayer=1"); }
        var mediaEndpoint = state.fredplayerOnly ? "/api/fredplayer/library?" : "/api/media?";
        return api(mediaEndpoint + parts.join("&")).then(function (payload) {
            state.items = payload.items || [];
            state.folders = payload.folders || [];
            renderMedia(payload.total || 0);
        }).catch(renderMediaError);
    }

    function videoLibraryFolders() {
        return state.libraries.filter(function (library) {
            return library.kind === "video";
        }).map(function (library) {
            return {
                name: library.name,
                path: "",
                library_id: library.id,
                item_count: library.item_count
            };
        });
    }

    function renderFolderNav() {
        var selected = findLibrary(state.library);
        elements.folderNav.innerHTML = "";
        if (state.playlist !== null || state.fredplayerOnly || state.search) {
            elements.folderNav.hidden = true;
            return;
        }
        if (!selected && state.filter !== "all" && state.filter !== "video") {
            elements.folderNav.hidden = true;
            return;
        }
        elements.folderNav.hidden = false;
        var trail = [{
            label: state.filter === "video" ? "All video" : "All media",
            path: "",
            library_id: null
        }];
        if (selected) {
            trail.push({ label: selected.name, path: "", library_id: selected.id });
            if (state.folder) {
                var acc = "";
                state.folder.split("/").forEach(function (part) {
                    acc = acc ? acc + "/" + part : part;
                    trail.push({ label: part, path: acc, library_id: selected.id });
                });
            }
        }
        trail.forEach(function (crumb, index) {
            if (index > 0) {
                var sep = document.createElement("span");
                sep.className = "folder-sep";
                sep.textContent = "/";
                elements.folderNav.appendChild(sep);
            }
            if (index === trail.length - 1) {
                var current = document.createElement("span");
                current.className = "folder-current";
                current.textContent = crumb.label;
                elements.folderNav.appendChild(current);
            } else {
                var button = document.createElement("button");
                button.type = "button";
                button.className = "folder-crumb focusable";
                button.setAttribute("data-focusable", "true");
                button.textContent = crumb.label;
                button.addEventListener("click", function () {
                    openFolder(crumb.path, crumb.library_id);
                });
                elements.folderNav.appendChild(button);
            }
        });
    }

    function openFolder(path, libraryId) {
        state.mediaPage = 0;
        state.playlist = null;
        state.folder = path || "";
        if (libraryId === null) {
            state.library = null;
            var allButton = document.querySelector(state.filter === "video"
                ? "[data-filter=\"video\"]" : "[data-filter=\"all\"]");
            if (allButton) { setActiveNavigation(allButton); }
        } else if (libraryId !== undefined) {
            state.library = libraryId;
            var libraryButton = document.querySelector("[data-library=\"" + libraryId + "\"]");
            if (libraryButton) { setActiveNavigation(libraryButton); }
        }
        updateHeading();
        loadMedia();
    }

    function folderCard(folder) {
        var card = document.createElement("button");
        var poster = document.createElement("span");
        var initials = document.createElement("span");
        var kind = document.createElement("span");
        var title = document.createElement("p");
        var meta = document.createElement("p");
        card.className = "media-card folder-card focusable";
        card.setAttribute("data-focusable", "true");
        card.setAttribute("aria-label", "Open folder " + folder.name);
        poster.className = "poster";
        initials.className = "poster-initials";
        initials.textContent = "▸";
        kind.className = "media-kind";
        kind.textContent = "folder";
        poster.appendChild(initials);
        poster.appendChild(kind);
        title.className = "card-title";
        title.textContent = folder.name;
        meta.className = "card-meta";
        meta.textContent = (folder.item_count || 0).toLocaleString()
            + ((folder.item_count || 0) === 1 ? " item" : " items");
        card.appendChild(poster);
        card.appendChild(title);
        card.appendChild(meta);
        card.fluxaActivate = function () {
            if (folder.library_id) {
                openFolder(folder.path || "", folder.library_id);
            } else {
                openFolder(folder.path);
            }
        };
        card.addEventListener("click", card.fluxaActivate);
        return card;
    }

    function renderMediaError(error) {
        elements.grid.innerHTML = "";
        elements.empty.hidden = false;
        elements.count.textContent = "Unavailable";
        showToast(error.message, true);
    }

    function renderMusicCollections(total) {
        state.mediaTotal = total;
        elements.grid.innerHTML = "";
        elements.grid.classList.remove("list-view");
        elements.empty.hidden = state.items.length !== 0;
        elements.count.textContent = total.toLocaleString()
            + (total === 1 ? " collection" : " collections");
        state.items.forEach(function (collection) {
            elements.grid.appendChild(musicCollectionCard(collection));
        });
        renderMediaPagination(total);
        updatePlaylistActions();
    }

    function musicCollectionCard(collection) {
        var card = document.createElement("button");
        var poster = document.createElement("span");
        var initials = document.createElement("span");
        var kind = document.createElement("span");
        var title = document.createElement("p");
        var meta = document.createElement("p");
        card.className = "media-card collection-card focusable";
        card.setAttribute("data-focusable", "true");
        card.setAttribute("aria-label", "Open " + collection.title);
        poster.className = "poster";
        initials.className = "poster-initials";
        initials.textContent = collection.collection_type === "playlist" ? "♫" : makeInitials(collection.title);
        if (collection.thumbnail_url) {
            var artwork = document.createElement("img");
            artwork.className = "poster-artwork";
            artwork.alt = "";
            poster.appendChild(artwork);
            loadThumbnailWhenVisible(artwork, poster, collection.thumbnail_url);
        }
        kind.className = "media-kind";
        kind.textContent = collection.collection_type;
        poster.appendChild(initials);
        poster.appendChild(kind);
        title.className = "card-title";
        title.textContent = collection.title;
        meta.className = "card-meta";
        meta.textContent = (collection.artist ? collection.artist + " · " : "")
            + collection.count.toLocaleString() + (collection.count === 1 ? " track" : " tracks");
        card.appendChild(poster);
        card.appendChild(title);
        card.appendChild(meta);
        card.fluxaActivate = function () {
            state.mediaPage = 0;
            state.musicCollection = collection;
            updateMusicBrowser();
            updateHeading();
            loadMedia().then(focusFirstMediaCard);
        };
        card.addEventListener("click", card.fluxaActivate);
        return card;
    }

    function loadCombinedAllMedia() {
        var countQuery = ["limit=1", "offset=0"];
        if (state.search) { countQuery.push("q=" + encodeURIComponent(state.search)); }
        return Promise.all([
            api("/api/media?" + countQuery.join("&")),
            api("/api/fredplayer/library?" + countQuery.join("&"))
        ]).then(function (counts) {
            var videoTotal = counts[0].total || 0;
            var musicTotal = counts[1].total || 0;
            var total = videoTotal + musicTotal;
            var start = Math.min(total, state.mediaPage * state.mediaPageSize);
            var end = Math.min(total, start + state.mediaPageSize);
            var videoStart = total ? Math.round(start * videoTotal / total) : 0;
            var videoEnd = total ? Math.round(end * videoTotal / total) : 0;
            var musicStart = start - videoStart;
            var musicEnd = end - videoEnd;
            function query(limit, offset) {
                var parts = ["limit=" + Math.max(1, limit), "offset=" + offset];
                if (state.search) { parts.push("q=" + encodeURIComponent(state.search)); }
                return parts.join("&");
            }
            return Promise.all([
                videoEnd > videoStart
                    ? api("/api/media?" + query(videoEnd - videoStart, videoStart))
                    : Promise.resolve({ items: [] }),
                musicEnd > musicStart
                    ? api("/api/fredplayer/library?" + query(musicEnd - musicStart, musicStart))
                    : Promise.resolve({ items: [] })
            ]).then(function (responses) {
                return { responses: responses, total: total };
            });
        }).then(function (payload) {
            var videos = payload.responses[0].items || [];
            var music = payload.responses[1].items || [];
            var combined = [];
            var count = Math.max(videos.length, music.length);
            var index;
            for (index = 0; index < count; index += 1) {
                if (index < videos.length) { combined.push(videos[index]); }
                if (index < music.length) { combined.push(music[index]); }
            }
            state.items = combined;
            renderMedia(payload.total);
        }).catch(function (error) {
            elements.grid.innerHTML = "";
            elements.empty.hidden = false;
            elements.count.textContent = "Unavailable";
            showToast(error.message, true);
        });
    }

    function renderFredPlayerGate() {
        var gate = document.createElement("div");
        var icon = document.createElement("span");
        var title = document.createElement("h3");
        var copy = document.createElement("p");
        var button = document.createElement("button");
        elements.grid.innerHTML = "";
        elements.grid.classList.remove("list-view");
        elements.empty.hidden = true;
        elements.pagination.hidden = true;
        elements.count.textContent = "Sign in required";
        gate.className = "fredplayer-gate";
        icon.className = "fredplayer-gate-icon";
        icon.textContent = "♫";
        title.textContent = "Sign in to FredPlayer";
        copy.textContent = "Your FredPlayer music library will appear here after you sign in once on this device.";
        button.type = "button";
        button.className = "primary-button compact focusable";
        button.setAttribute("data-focusable", "true");
        button.textContent = "Sign in";
        button.fluxaActivate = openFredPlayerAuthorization;
        button.addEventListener("click", openFredPlayerAuthorization);
        gate.appendChild(icon);
        gate.appendChild(title);
        gate.appendChild(copy);
        gate.appendChild(button);
        elements.grid.appendChild(gate);
        if (isTizenTv) { setTimeout(function () { focusElement(button); }, 0); }
    }

    function renderMedia(total, available) {
        state.mediaTotal = total;
        elements.grid.innerHTML = "";
        updateMediaView();
        var folders = (state.mediaPage === 0 && !state.search) ? (state.folders || []) : [];
        elements.empty.hidden = state.items.length !== 0 || folders.length !== 0;
        var countText = total.toLocaleString() + (total === 1 ? " item" : " items");
        if (folders.length) {
            countText = folders.length.toLocaleString() + (folders.length === 1 ? " folder" : " folders")
                + (total ? " · " + countText : "");
        }
        elements.count.textContent = countText;
        if (typeof available === "number" && available !== total) {
            elements.count.textContent += " · " + available.toLocaleString() + " playable";
        }
        folders.forEach(function (folder) {
            elements.grid.appendChild(folderCard(folder));
        });
        var visibleItems = state.items;
        if (isTizenTv && state.playlist !== null) {
            var start = state.mediaPage * state.mediaPageSize;
            visibleItems = state.items.slice(start, start + state.mediaPageSize);
        }
        visibleItems.forEach(function (item) {
            elements.grid.appendChild(mediaCard(item));
        });
        renderMediaPagination(total);
        updatePlaylistActions();
    }

    function renderMediaPagination(total) {
        if (total <= state.mediaPageSize || (!isTizenTv && state.library === null)) {
            elements.pagination.hidden = true;
            return;
        }
        var pageCount = Math.max(1, Math.ceil(total / state.mediaPageSize));
        if (state.mediaPage >= pageCount) { state.mediaPage = pageCount - 1; }
        elements.pagination.hidden = false;
        elements.previousPage.disabled = state.mediaPage <= 0;
        elements.nextPage.disabled = state.mediaPage >= pageCount - 1;
        elements.pageStatus.textContent = "Page " + (state.mediaPage + 1) + " of " + pageCount;
    }

    function changeMediaPage(direction) {
        if (state.mediaPageChanging && state.playlist === null) { return; }
        var pageCount = Math.max(1, Math.ceil(state.mediaTotal / state.mediaPageSize));
        var nextPage = Math.max(0, Math.min(pageCount - 1, state.mediaPage + direction));
        if (nextPage === state.mediaPage) { return; }
        state.mediaPage = nextPage;
        if (state.playlist !== null) {
            renderMedia(state.mediaTotal, playablePlaylistItems().length);
            focusFirstMediaCard();
        } else {
            state.mediaPageChanging = true;
            elements.pageStatus.textContent = "Loading page " + (nextPage + 1) + "…";
            loadMedia().then(function () {
                focusFirstMediaCard();
                state.mediaPageChanging = false;
            }, function () {
                state.mediaPageChanging = false;
            });
        }
    }

    function focusFirstMediaCard() {
        var first = elements.grid.querySelector(".media-card:not([disabled])");
        if (first) { focusElement(first); }
    }

    function mediaCard(item) {
        var card = document.createElement("button");
        var poster = document.createElement("span");
        var initials = document.createElement("span");
        var kind = document.createElement("span");
        var analysis = document.createElement("span");
        var title = document.createElement("p");
        var meta = document.createElement("p");
        card.className = "media-card focusable " + item.media_type + (item.available === false ? " unavailable" : "");
        if (item.available !== false) {
            card.setAttribute("data-focusable", "true");
            card.setAttribute("data-media-id", String(item.id));
            card.setAttribute("aria-label", "Play " + item.title);
        } else {
            card.disabled = true;
            card.setAttribute("aria-label", item.title + " is unavailable");
        }
        poster.className = "poster";
        initials.className = "poster-initials";
        initials.textContent = makeInitials(item.title);
        if (item.available !== false && item.thumbnail_url) {
            var artwork = document.createElement("img");
            artwork.className = "poster-artwork";
            artwork.alt = "";
            poster.appendChild(artwork);
            loadThumbnailWhenVisible(artwork, poster, item.thumbnail_url);
        }
        kind.className = "media-kind";
        kind.textContent = item.extension ? item.extension.replace(".", "") : "media";
        analysis.className = "analysis-indicator " + item.analysis.status;
        analysis.title = item.media_type === "audio" ? "Managed by FredPlayer" : "Audio analysis: " + item.analysis.status;
        poster.appendChild(initials);
        poster.appendChild(kind);
        poster.appendChild(analysis);
        if (item.progress.duration_ms > 0 && item.progress.position_ms > 0) {
            var progress = document.createElement("span");
            var progressValue = document.createElement("span");
            var percentage = Math.min(100, (item.progress.position_ms / item.progress.duration_ms) * 100);
            progress.className = "card-progress";
            progressValue.style.width = percentage + "%";
            progress.appendChild(progressValue);
            poster.appendChild(progress);
        }
        title.className = "card-title";
        title.textContent = item.title;
        meta.className = "card-meta";
        meta.textContent = cardMetadata(item);
        card.appendChild(poster);
        card.appendChild(title);
        card.appendChild(meta);
        if (item.available !== false) {
            card.fluxaActivate = function () {
                if (item.media_type === "audio") {
                    if (state.fredplayerOnly && state.musicCollection) {
                        startMusicCollection(false, item);
                        return;
                    }
                    openFredPlayerTrack(item);
                    return;
                }
                if (state.playlist !== null) {
                    beginPlaylistAt(item);
                } else {
                    beginCurrentViewAt(item);
                }
            };
            card.addEventListener("click", card.fluxaActivate);
        }
        return card;
    }

    function loadThumbnailWhenVisible(image, poster, thumbnailUrl) {
        var started = false;
        function start() {
            if (started) { return; }
            started = true;
            pollThumbnail(image, poster, thumbnailUrl, 0);
        }
        if ("IntersectionObserver" in window) {
            var observer = new IntersectionObserver(function (entries) {
                if (entries.some(function (entry) { return entry.isIntersecting; })) {
                    observer.disconnect();
                    start();
                }
            }, { rootMargin: "500px" });
            observer.observe(image);
        } else {
            start();
        }
    }

    function pollThumbnail(image, poster, thumbnailUrl, attempt) {
        fetch(appUrl(thumbnailUrl), { method: "HEAD", cache: "no-store" }).then(function (response) {
            if (response.status === 200) {
                image.addEventListener("load", function () {
                    poster.classList.add("has-artwork");
                }, { once: true });
                image.src = appUrl(thumbnailUrl);
                return;
            }
            if (response.status === 202 && attempt < 24) {
                setTimeout(function () {
                    pollThumbnail(image, poster, thumbnailUrl, attempt + 1);
                }, 1600);
            }
        }).catch(function () {});
    }

    function playablePlaylistItems() {
        return state.items.filter(function (item) {
            return item.available !== false && item.id !== null;
        }).map(function (item) {
            return { id: item.id, position: item.playlist_position };
        });
    }

    function updatePlaylistActions() {
        if (state.fredplayerOnly && state.musicCollection) {
            var musicEmpty = state.mediaTotal === 0;
            elements.playPlaylist.disabled = musicEmpty;
            elements.shufflePlaylist.disabled = musicEmpty;
            return;
        }
        if (state.playlist === null) { return; }
        var empty = playablePlaylistItems().length === 0;
        elements.playPlaylist.disabled = empty;
        elements.shufflePlaylist.disabled = empty;
    }

    function canAddCurrentResultsToPlaylist() {
        return !state.fredplayerOnly && state.playlist === null
            && (state.library !== null || Boolean(state.search));
    }

    function bindPlaylistPicker() {
        playlistPickerCreateOnly = false;
        elements.addToPlaylist.addEventListener("click", function () {
            openPlaylistPicker(false);
        });
        function startNewPlaylist() {
            openPlaylistPicker(true);
        }
        if (elements.newPlaylist) {
            elements.newPlaylist.addEventListener("click", startNewPlaylist);
        }
        if (elements.newPlaylistToolbar) {
            elements.newPlaylistToolbar.addEventListener("click", startNewPlaylist);
        }
        elements.playlistPickerCancel.addEventListener("click", closePlaylistPicker);
        elements.playlistPicker.addEventListener("click", function (event) {
            if (event.target === elements.playlistPicker) { closePlaylistPicker(); }
        });
        elements.playlistPickerForm.addEventListener("submit", function (event) {
            event.preventDefault();
            var title = elements.playlistPickerName.value.trim();
            if (!title) {
                showToast("Name the new playlist first.", true);
                return;
            }
            if (playlistPickerCreateOnly) {
                createEmptyPlaylist(title);
                return;
            }
            addMatchingResultsToPlaylist(null, title);
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && !elements.playlistPicker.hidden) {
                event.preventDefault();
                closePlaylistPicker();
            }
        });
    }

    var playlistPickerCreateOnly = false;

    function openPlaylistPicker(createOnly) {
        playlistPickerCreateOnly = Boolean(createOnly);
        if (!playlistPickerCreateOnly && !canAddCurrentResultsToPlaylist()) { return; }
        if (playlistPickerCreateOnly) {
            elements.playlistPickerTitle.textContent = "New playlist";
            elements.playlistPickerCopy.textContent = "Name the playlist. Add files from a library folder after it exists.";
            elements.playlistPickerList.innerHTML = "";
            elements.playlistPickerCreate.textContent = "Create";
            elements.playlistPickerCreate.disabled = false;
        } else {
            elements.playlistPickerTitle.textContent = "Add to a playlist";
            elements.playlistPickerCreate.textContent = "Create and add";
            renderPlaylistPickerList();
            var count = state.mediaTotal || 0;
            var folderCount = (state.folders || []).length;
            if (state.library && !state.search) {
                elements.playlistPickerCopy.textContent = "Add every video in this folder, including subfolders.";
                elements.playlistPickerCreate.disabled = count === 0 && folderCount === 0;
            } else {
                elements.playlistPickerCopy.textContent = count
                    ? "Add " + count.toLocaleString() + (count === 1 ? " matching file" : " matching files") + "."
                    : "No matching files to add.";
                elements.playlistPickerCreate.disabled = count === 0;
            }
        }
        elements.playlistPicker.hidden = false;
        var first = elements.playlistPickerList.querySelector(".focusable") || elements.playlistPickerName;
        focusElement(first);
    }

    function createEmptyPlaylist(title) {
        elements.playlistPickerCreate.disabled = true;
        jsonPost("/api/playlists", { title: title, kind: "video" }).then(function (payload) {
            if (payload.playlists) {
                state.playlists = payload.playlists;
                renderPlaylists();
            }
            closePlaylistPicker();
            var created = payload.playlist;
            showToast("Created " + (created && created.title ? created.title : title) + ".");
            if (created && created.id) {
                var button = document.querySelector("[data-playlist=\"" + created.id + "\"]");
                if (button) { button.click(); }
            }
        }).catch(function (error) {
            showToast(error.message, true);
        }).then(function () {
            elements.playlistPickerCreate.disabled = false;
        });
    }

    function closePlaylistPicker() {
        elements.playlistPicker.hidden = true;
        elements.playlistPickerName.value = "";
        if (!elements.addToPlaylist.hidden) { focusElement(elements.addToPlaylist); }
    }

    function renderPlaylistPickerList() {
        elements.playlistPickerList.innerHTML = "";
        if (!state.playlists.length) {
            var empty = document.createElement("p");
            empty.className = "playlist-picker-empty";
            empty.textContent = "No playlists yet. Create one below.";
            elements.playlistPickerList.appendChild(empty);
            return;
        }
        state.playlists.forEach(function (playlist) {
            var button = document.createElement("button");
            button.type = "button";
            button.className = "nav-item focusable";
            button.setAttribute("data-focusable", "true");
            button.textContent = playlist.title + " · " + playlist.item_count;
            button.addEventListener("click", function () {
                addMatchingResultsToPlaylist(playlist.id, null);
            });
            elements.playlistPickerList.appendChild(button);
        });
    }

    function jsonPost(path, body) {
        return api(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
    }

    function fetchMatchingVideoIds() {
        var ids = [];
        var limit = 250;
        function page(offset) {
            var parts = ["limit=" + limit, "offset=" + offset, "type=video"];
            if (state.search) { parts.push("q=" + encodeURIComponent(state.search)); }
            if (state.library) {
                parts.push("library=" + encodeURIComponent(state.library));
                parts.push("folder=" + encodeURIComponent(state.folder || ""));
                parts.push("recursive=1");
            }
            return api("/api/media?" + parts.join("&")).then(function (payload) {
                (payload.items || []).forEach(function (item) {
                    if (item.id != null) { ids.push(item.id); }
                });
                var total = payload.total || 0;
                if (ids.length < total && (payload.items || []).length > 0) {
                    return page(offset + limit);
                }
                return ids;
            });
        }
        return page(0);
    }

    function addIdsToPlaylist(playlistId, ids) {
        var added = 0;
        var skipped = 0;
        function next(start) {
            if (start >= ids.length) {
                return Promise.resolve({ added: added, skipped: skipped, playlists: state.playlists });
            }
            return jsonPost("/api/playlists/" + playlistId + "/items", {
                media_ids: ids.slice(start, start + 500)
            }).then(function (result) {
                added += result.added || 0;
                skipped += result.skipped || 0;
                if (result.playlists) { state.playlists = result.playlists; }
                return next(start + 500);
            });
        }
        return next(0);
    }

    function addMatchingResultsToPlaylist(playlistId, newTitle) {
        elements.addToPlaylist.disabled = true;
        elements.playlistPickerCreate.disabled = true;
        fetchMatchingVideoIds().then(function (ids) {
            if (!ids.length) {
                throw new Error("No matching videos to add.");
            }
            if (newTitle) {
                return jsonPost("/api/playlists", { title: newTitle, kind: "video" }).then(function (payload) {
                    var created = payload.playlist;
                    return addIdsToPlaylist(created.id, ids).then(function (added) {
                        added.playlist_id = created.id;
                        added.playlist_title = created.title;
                        return added;
                    });
                });
            }
            var playlist = findPlaylist(playlistId);
            return addIdsToPlaylist(playlistId, ids).then(function (added) {
                added.playlist_id = playlistId;
                added.playlist_title = playlist ? playlist.title : "playlist";
                return added;
            });
        }).then(function (result) {
            if (result.playlists) {
                state.playlists = result.playlists;
                renderPlaylists();
            }
            closePlaylistPicker();
            var message = "Added " + (result.added || 0).toLocaleString() + " to " + result.playlist_title + ".";
            if (result.skipped) {
                message += " " + result.skipped.toLocaleString() + " already there.";
            }
            showToast(message);
        }).catch(function (error) {
            showToast(error.message, true);
        }).then(function () {
            elements.addToPlaylist.disabled = false;
            elements.playlistPickerCreate.disabled = false;
        });
    }

    function startPlaylist(shuffle) {
        var queue = playablePlaylistItems();
        if (!queue.length) {
            showToast("This playlist has no currently available media.", true);
            return;
        }
        state.queueOriginal = queue.slice();
        if (shuffle) { shuffleItems(queue); }
        state.playQueue = queue;
        state.queueShuffled = shuffle;
        openPlayer(queue[0].id, 0);
    }

    function startActiveCollection(shuffle) {
        if (state.fredplayerOnly && state.musicCollection) {
            startMusicCollection(shuffle);
            return;
        }
        startPlaylist(shuffle);
    }

    function musicCollectionQuery(collection, limit, offset) {
        var parts = ["fredplayer=1", "limit=" + limit, "offset=" + offset];
        if (collection.collection_type === "playlist") {
            parts.push("playlist=" + encodeURIComponent(collection.name));
        } else {
            parts.push("album=" + encodeURIComponent(collection.name));
            parts.push("artist=" + encodeURIComponent(collection.artist));
        }
        return "/api/fredplayer/library?" + parts.join("&");
    }

    function allMusicCollectionItems(collection) {
        var collected = [];
        function next(offset) {
            return api(musicCollectionQuery(collection, 250, offset)).then(function (payload) {
                var items = payload.items || [];
                collected = collected.concat(items);
                if (collected.length < (payload.total || 0) && items.length) {
                    return next(collected.length);
                }
                return collected;
            });
        }
        return next(0);
    }

    function startMusicCollection(shuffle, startItem) {
        var collection = state.musicCollection;
        if (!collection || state.collectionStartPending) { return; }
        state.collectionStartPending = true;
        elements.playPlaylist.disabled = true;
        elements.shufflePlaylist.disabled = true;
        allMusicCollectionItems(collection).then(function (items) {
            var paths = items.map(function (item) { return item.fredplayer_path; }).filter(Boolean);
            openFredPlayerQueue(paths, collection.title,
                collection.collection_type === "album" ? "Album" : "Playlist",
                shuffle, startItem ? startItem.fredplayer_path : "");
        }).catch(function (error) {
            showToast(error.message, true);
        }).then(function () {
            state.collectionStartPending = false;
            elements.playPlaylist.disabled = state.mediaTotal === 0;
            elements.shufflePlaylist.disabled = state.mediaTotal === 0;
        });
    }

    function beginPlaylistAt(item) {
        var queue = playablePlaylistItems();
        var index = queue.findIndex(function (entry) {
            return entry.position === item.playlist_position;
        });
        if (index < 0) { return; }
        state.playQueue = queue;
        state.queueOriginal = queue.slice();
        state.queueShuffled = false;
        openPlayer(item.id, index);
    }

    function beginCurrentViewAt(item) {
        var queue = state.items.filter(function (entry) {
            return entry.available !== false && entry.id !== null;
        }).map(function (entry) {
            return { id: entry.id };
        });
        var index = queue.findIndex(function (entry) { return entry.id === item.id; });
        if (index < 0) { return; }
        state.playQueue = queue;
        state.queueOriginal = queue.slice();
        state.queueShuffled = false;
        openPlayer(item.id, index);
    }

    function shuffleItems(items) {
        var index;
        for (index = items.length - 1; index > 0; index -= 1) {
            var swap = Math.floor(Math.random() * (index + 1));
            var value = items[index];
            items[index] = items[swap];
            items[swap] = value;
        }
    }

    function playNextQueueItem() {
        if (state.repeatMode === "one" && state.queueIndex >= 0) {
            openPlayer(state.playQueue[state.queueIndex].id, state.queueIndex);
            return;
        }
        if (state.queueIndex === state.playQueue.length - 1) {
            if (state.repeatMode === "all" && state.playQueue.length) {
                openPlayer(state.playQueue[0].id, 0);
            }
            return;
        }
        playAdjacentQueueItem(1);
    }

    function playAdjacentQueueItem(direction) {
        if (!state.playQueue.length || state.queueIndex < 0) { return; }
        var nextIndex = state.queueIndex + direction;
        if (nextIndex >= 0 && nextIndex < state.playQueue.length) {
            openPlayer(state.playQueue[nextIndex].id, nextIndex);
            return;
        }
        if (state.repeatMode === "all" && state.playQueue.length) {
            var wrapped = direction > 0 ? 0 : state.playQueue.length - 1;
            openPlayer(state.playQueue[wrapped].id, wrapped);
            return;
        }
        showToast(direction > 0 ? "End of playlist." : "Start of playlist.");
    }

    function cycleRepeatMode() {
        state.repeatMode = state.repeatMode === "none"
            ? "one" : (state.repeatMode === "one" ? "all" : "none");
        updatePlaybackControls();
    }

    function toggleQueueShuffle() {
        if (!state.playQueue.length || state.queueIndex < 0) { return; }
        var current = state.playQueue[state.queueIndex];
        if (state.queueShuffled) {
            state.playQueue = state.queueOriginal.slice();
            state.queueIndex = findQueueEntry(state.playQueue, current);
            state.queueShuffled = false;
        } else {
            var remaining = state.playQueue.filter(function (_entry, index) {
                return index !== state.queueIndex;
            });
            shuffleItems(remaining);
            state.playQueue = [current].concat(remaining);
            state.queueIndex = 0;
            state.queueShuffled = true;
        }
        updatePlaybackControls();
    }

    function findQueueEntry(queue, wanted) {
        var index = queue.findIndex(function (entry) {
            return entry.id === wanted.id && entry.position === wanted.position;
        });
        if (index < 0) {
            index = queue.findIndex(function (entry) { return entry.id === wanted.id; });
        }
        return Math.max(0, index);
    }

    function showSkeletons() {
        var i;
        elements.grid.innerHTML = "";
        elements.empty.hidden = true;
        elements.count.textContent = "Loading…";
        for (i = 0; i < 12; i += 1) {
            var skeleton = document.createElement("div");
            var poster = document.createElement("div");
            var line = document.createElement("div");
            skeleton.className = "skeleton";
            poster.className = "skeleton-poster";
            line.className = "skeleton-line";
            skeleton.appendChild(poster);
            skeleton.appendChild(line);
            elements.grid.appendChild(skeleton);
        }
    }

    function scanLibraries() {
        setScanControls(true, "Scanning in place…");
        api("/api/scan", { method: "POST" }).then(function (payload) {
            var found = 0;
            var added = 0;
            (payload.results || []).forEach(function (result) {
                found += result.discovered;
                added += result.added;
            });
            showToast("Scan complete: " + found.toLocaleString() + " files found, " + added.toLocaleString() + " new.");
            return Promise.all([api("/api/status"), api("/api/libraries")]);
        }).then(function (responses) {
            setOnline(responses[0]);
            state.libraries = responses[1].libraries || [];
            renderLibraries();
            return loadMedia();
        }).catch(function (error) {
            showToast(error.message, true);
        }).then(function () {
            setScanControls(false, "Scan libraries");
        });
    }

    function setScanControls(disabled, label) {
        [elements.scan, elements.scanToolbar].forEach(function (button) {
            button.disabled = disabled;
            button.textContent = label;
        });
    }

    function bindPlayer() {
        document.addEventListener("fullscreenchange", handleFullscreenChange);
        document.addEventListener("webkitfullscreenchange", handleFullscreenChange);
        window.addEventListener("resize", schedulePlayerGeometry);
        window.addEventListener("orientationchange", schedulePlayerGeometry);
        if (window.visualViewport) {
            window.visualViewport.addEventListener("resize", schedulePlayerGeometry);
            window.visualViewport.addEventListener("scroll", schedulePlayerGeometry);
        }
        elements.playerPanel.addEventListener("pointermove", revealPlayerControls);
        elements.playerPanel.addEventListener("pointerdown", revealPlayerControls);
        elements.playerPanel.addEventListener("touchstart", revealPlayerControls, {
            passive: true
        });
        elements.modal.addEventListener("click", function (event) {
            var controls = state.playerControls;
            if (controls && controls.chapterPopover && !controls.chapterPopover.hidden
                    && !controls.chapterPopover.contains(event.target)
                    && !controls.chapterGrid.contains(event.target)) {
                controls.chapterPopover.hidden = true;
                controls.chapterGrid.classList.remove("active");
                controls.chapterGrid.setAttribute("aria-expanded", "false");
                schedulePlayerGeometry();
            }
            if (event.target.getAttribute("data-close-player") === "true") {
                closePlayer();
            }
        });
        elements.analyze.addEventListener("click", startAnalysis);
        elements.captionsCurrent.addEventListener("change", function () {
            setCaptionsEnabled(elements.captionsCurrent.checked);
        });
        elements.captionsGlobal.addEventListener("change", function () {
            setStoredPreference("fluxa-captions-global", elements.captionsGlobal.checked ? "on" : "off");
            setCaptionsEnabled(elements.captionsGlobal.checked);
        });
        elements.captionTrack.addEventListener("change", function () {
            state.captionTrackOrdinal = Number(elements.captionTrack.value);
            if (state.captionsEnabled) { refreshCaptionSelection(); }
        });
        elements.bassGain.addEventListener("input", updateBassControls);
        elements.bassGain.addEventListener("change", applyBassSettings);
        elements.bassEnhancement.addEventListener("change", applyBassSettings);
        [elements.compressorThreshold, elements.compressorRatio, elements.compressorOutputGain,
            elements.compressorCeiling, elements.compressorAttack,
            elements.compressorRelease, elements.compressorKnee].forEach(function (control) {
            control.addEventListener("input", updateCompressorLabels);
            control.addEventListener("change", applyCompressorSettings);
        });
        elements.compressorEnabled.addEventListener("change", applyCompressorSettings);
        elements.compressorCreateVideo.addEventListener("click", createVideoCompressor);
        elements.compressorVideoToGlobal.addEventListener("click", copyVideoCompressorToGlobal);
        elements.compressorGlobalToVideo.addEventListener("click", copyGlobalCompressorToVideo);
        elements.compressorFollowGlobal.addEventListener("click", followGlobalCompressor);
    }

    function hlsPlayerConfiguration() {
        return {
            // Fluxa's CSP deliberately blocks blob: workers. Running the
            // small transmux step on the page avoids HLS.js attempting a
            // worker and then recovering through a lossy fallback path.
            enableWorker: false,
            backBufferLength: 60,
            maxBufferLength: 60,
            maxMaxBufferLength: 90,
            liveSyncDurationCount: 3,
            maxLiveSyncPlaybackRate: 1
        };
    }

    function primeTeslaVideoGesture() {
        if (!isTeslaBrowser || !window.Hls || !window.Hls.isSupported()) { return; }
        if (navigator.userActivation && !navigator.userActivation.isActive) { return; }

        var media = document.createElement("video");
        media.controls = false;
        media.autoplay = false;
        media.preload = "auto";
        media.volume = state.volume;
        media.muted = state.muted;
        media.className = "player-video pending";
        media.setAttribute("playsinline", "true");
        media.fluxaTeslaGestureState = "pending";
        state.player = media;
        state.playerAssignedAt = Date.now();
        elements.stage.appendChild(media);

        state.hls = new window.Hls(hlsPlayerConfiguration());
        state.hls.attachMedia(media);
        var attempt = media.play();
        if (attempt && typeof attempt.then === "function") {
            attempt.then(function () {
                media.fluxaTeslaGestureState = "accepted";
            }).catch(function () {
                media.fluxaTeslaGestureState = "rejected";
            });
        } else {
            media.fluxaTeslaGestureState = "accepted";
        }
    }

    function openPlayer(mediaId, queueIndex) {
        if (typeof queueIndex === "number") {
            state.queueIndex = queueIndex;
        } else {
            state.playQueue = [];
            state.queueOriginal = [];
            state.queueIndex = -1;
            state.queueShuffled = false;
        }
        rememberCurrentFrame();
        stopActivePlayback(true);
        state.openToken += 1;
        var requestToken = state.openToken;
        elements.modal.removeAttribute("hidden");
        elements.modal.hidden = false;
        document.body.classList.add("player-open");
        if (isTizenTv) { void elements.modal.offsetHeight; }
        state.closingPlayer = false;
        revealPlayerControls();
        schedulePlayerGeometry();
        document.body.style.overflow = "hidden";
        elements.stage.innerHTML = "";
        showTransitionFrame();
        primeTeslaVideoGesture();
        elements.playerDetails.classList.remove("open");
        elements.playerTitle.textContent = "Loading…";
        elements.playerTechnical.textContent = "Reading stream information";
        elements.analysisTitle.textContent = "Checking analysis…";
        elements.analyze.disabled = true;
        api("/api/media/" + mediaId).then(function (item) {
            if (requestToken !== state.openToken || elements.modal.hidden) { return; }
            if (item.media_type === "audio") {
                finishClosePlayer();
                openFredPlayerTrack(item);
                return;
            }
            state.currentItem = item;
            configureCompressor(item.compressor);
            renderPlayer(item);
        }).catch(function (error) {
            closePlayer();
            showToast(error.message, true);
        });
    }

    function renderPlayer(item) {
        var technical = item.technical || {};
        state.playbackOffsetMs = 0;
        updateVideoMediaSessionMetadata(item);
        configureCaptions(item);
        elements.playerLibrary.textContent = item.library_name;
        elements.playerTitle.textContent = item.title;
        elements.playerTechnical.textContent = technicalDescription(item, technical);
        appendQueuePosition();
        renderAnalysis(item.analysis);
        if (item.media_type === "video") {
            startCompatibilityPlayer(item);
        } else {
            startDirectPlayer(item);
        }
    }

    function startDirectPlayer(item) {
        var media = document.createElement(item.media_type === "audio" ? "audio" : "video");
        media.controls = true;
        media.autoplay = true;
        media.preload = "metadata";
        media.src = appUrl(item.stream_url);
        media.setAttribute("playsinline", "true");
        media.addEventListener("loadedmetadata", function () {
            var resume = item.progress.position_ms / 1000;
            if (resume > 5 && resume < media.duration - 10) {
                media.currentTime = resume;
            }
            media.play().catch(function () {});
        });
        attachTextCaptions(media, item, 0);
        bindPlaybackEvents(media, item, false);
        media.addEventListener("error", function () {
            if (item.media_type === "video" && state.currentItem === item) {
                startCompatibilityPlayer(item);
                return;
            }
            showToast("The browser could not play this file’s codec combination.", true);
        });
        state.player = media;
        elements.stage.innerHTML = "";
        elements.stage.appendChild(media);
    }

    function startCompatibilityPlayer(item, requestedStartMs, resumePlayback) {
        if (state.compatibilityStarting || state.compatibilitySession) { return; }
        state.compatibilityStarting = true;
        var requestToken = state.openToken;
        var teslaPrimed = isTeslaBrowser && state.player
            && typeof state.player.fluxaTeslaGestureState === "string";
        if (!teslaPrimed) { detachMediaElement(); }
        showTransitionFrame(item);
        elements.stage.setAttribute("aria-busy", "true");
        var selectedCaption = selectedCaptionTrack(item);
        var shouldResume = resumePlayback !== false;
        var startMs = typeof requestedStartMs === "number"
            ? requestedStartMs
            : (item.progress.position_ms || 0);
        var requestBody = {
            start_ms: Math.max(0, Math.round(startMs)),
            // Tesla's Chromium build accepts MPEG-TS through HLS.js but can
            // advance its clock without presenting audio or video. Give only
            // that browser fragmented MP4; all existing clients keep their
            // established MPEG-TS path and identical encode settings.
            segment_format: isTeslaBrowser ? "fmp4" : "mpegts",
            compressor: {
                enabled: state.compressorEnabled,
                threshold_db: state.compressorThreshold,
                ratio: state.compressorRatio,
                output_gain_db: state.compressorOutputGain,
                ceiling_db: state.compressorCeiling,
                attack_ms: state.compressorAttack,
                release_ms: state.compressorRelease,
                knee: state.compressorKnee
            },
            bass: {
                enabled: state.bassEnhancement,
                gain_db: state.bassGain
            }
        };
        if (state.captionsEnabled && selectedCaption && selectedCaption.kind === "bitmap") {
            requestBody.subtitle_ordinal = selectedCaption.ordinal;
        }
        api("/api/media/" + item.id + "/compatibility", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(requestBody)
        }).then(function (session) {
            state.compatibilityStarting = false;
            if (requestToken !== state.openToken || state.currentItem !== item) {
                closeCompatibilitySession(session.session_id);
                return;
            }
            state.compatibilitySession = session;
            state.compatibilityControlQueue = Promise.resolve();
            state.playbackOffsetMs = session.start_ms || 0;
            attachCompatibilityStream(item, session, shouldResume);
            elements.playerTechnical.textContent = technicalDescription(item, item.technical || {})
                + " · " + session.video_mode + " · " + session.audio_mode
                + (session.leveling_mode === "mapped"
                    ? " · precise level map active"
                    : " · live lookahead leveling active")
                + (session.subtitle_ordinal !== null ? " · captions burned in" : "");
            appendQueuePosition();
            startCompatibilityHeartbeat();
        }).catch(function (error) {
            state.compatibilityStarting = false;
            if (requestToken !== state.openToken) { return; }
            elements.stage.removeAttribute("aria-busy");
            showToast(error.message, true);
        });
    }

    function attachCompatibilityStream(item, session, shouldResume) {
        var media = isTeslaBrowser && state.player
                && typeof state.player.fluxaTeslaGestureState === "string"
            ? state.player : document.createElement("video");
        var primedHls = media.fluxaTeslaGestureState && state.hls ? state.hls : null;
        var manifest = appUrl(session.manifest_url);
        media.controls = false;
        media.autoplay = false;
        media.preload = "auto";
        media.volume = state.volume;
        media.muted = state.muted;
        media.className = "player-video pending";
        media.setAttribute("playsinline", "true");
        bindPlaybackEvents(media, item, true);
        attachTextCaptions(media, item, session.start_ms || 0);
        state.player = media;
        state.playerAssignedAt = Date.now();
        state.pauseReason = "";
        state.lastPlaybackToggleAt = 0;
        if (!media.parentNode) { elements.stage.appendChild(media); }
        buildCompatibilityControls(media, item);
        var revealed = false;
        function revealMedia() {
            if (revealed || state.player !== media) { return; }
            revealed = true;
            media.classList.remove("pending");
            var hold = elements.stage.querySelector(".stream-hold");
            if (hold) { hold.remove(); }
            elements.stage.removeAttribute("aria-busy");
            state.transitionFrame = null;
        }
        media.addEventListener("loadeddata", revealMedia, { once: true });
        media.addEventListener("playing", revealMedia, { once: true });

        if (window.Hls && window.Hls.isSupported()) {
            var recoveryCount = 0;
            state.hls = primedHls || new window.Hls(hlsPlayerConfiguration());
            if (!primedHls) {
                state.hls.on(window.Hls.Events.MEDIA_ATTACHED, function () {
                    if (state.hls) { state.hls.loadSource(manifest); }
                });
            }
            state.hls.on(window.Hls.Events.MANIFEST_PARSED, function () {
                updatePlaybackControls();
                if (shouldResume && media.fluxaTeslaGestureState !== "pending"
                        && media.fluxaTeslaGestureState !== "accepted") {
                    resumeMedia(media);
                }
            });
            state.hls.on(window.Hls.Events.ERROR, function (_event, data) {
                recordPlaybackEvent("hls_error", media, {
                    hls_type: data.type,
                    hls_detail: data.details || data.reason || "unknown",
                    hls_reason: data.reason || (data.error && data.error.message) || "",
                    hls_buffer: data.parent || data.sourceBufferName || ""
                });
                if (window.console && console.warn) {
                    console.warn("Fluxa HLS", data.type, data.details, data.reason || "");
                }
                if (!data.fatal || !state.hls) { return; }
                recoveryCount += 1;
                if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR && recoveryCount <= 3) {
                    window.setTimeout(function () {
                        if (state.hls) { state.hls.startLoad(); }
                    }, 800);
                } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR && recoveryCount <= 3) {
                    state.hls.recoverMediaError();
                } else {
                    showToast("The compatibility stream stopped unexpectedly.", true);
                }
            });
            if (primedHls) { state.hls.loadSource(manifest); }
            else { state.hls.attachMedia(media); }
            return;
        }
        if (media.canPlayType("application/vnd.apple.mpegurl")) {
            media.src = manifest;
            media.addEventListener("loadedmetadata", function () {
                updatePlaybackControls();
                if (shouldResume) { resumeMedia(media); }
            }, { once: true });
            media.addEventListener("error", function () {
                showToast("The compatibility stream stopped unexpectedly.", true);
            });
            return;
        }
        showToast("This browser does not provide HLS or Media Source playback.", true);
        closeCompatibilitySession(session.session_id);
        state.compatibilitySession = null;
    }

    function buildCompatibilityControls(media, item) {
        var controls = document.createElement("div");
        var timeline = document.createElement("div");
        var seek = document.createElement("input");
        var time = document.createElement("span");
        var actions = document.createElement("div");
        var transport = document.createElement("div");
        var utilities = document.createElement("div");
        var previousItem = playerButton("previousItem", "Previous video");
        var previousChapter = playerButton("previousChapter", "Previous chapter");
        var rewind = playerButton("rewind", "Back 10 seconds");
        var play = playerButton("play", "Play");
        var stop = playerButton("stop", "Stop and close player");
        var forward = playerButton("forward", "Forward 30 seconds");
        var nextChapter = playerButton("nextChapter", "Next chapter");
        var nextItem = playerButton("nextItem", "Next video");
        var captions = playerButton("captions", "Closed captions");
        var chapterGrid = playerButton("chapters", "Show chapters");
        var mute = playerButton("volume", "Mute");
        var volume = document.createElement("input");
        var settings = playerButton("settings", "Playback settings");
        var shuffle = playerButton("shuffle", "Turn shuffle on");
        var repeat = playerButton("repeat", "Repeat off");
        var fullscreen = playerButton("fullscreen", "Enter full screen");
        var chapterPopover = buildChapterPopover(item);

        controls.className = "fluxa-player-controls";
        timeline.className = "player-timeline";
        seek.type = "range";
        seek.className = "player-seek focusable";
        seek.setAttribute("data-focusable", "true");
        seek.min = "0";
        seek.max = String(Math.max(1, Math.round((item.duration_ms || 0) / 1000)));
        seek.step = "1";
        seek.value = String(Math.round(state.playbackOffsetMs / 1000));
        time.className = "player-time";
        actions.className = "player-control-actions";
        transport.className = "player-transport";
        utilities.className = "player-utilities";
        volume.type = "range";
        volume.className = "player-volume focusable";
        volume.setAttribute("data-focusable", "true");
        volume.min = "0";
        volume.max = "1";
        volume.step = "0.05";
        volume.value = String(state.volume);
        chapterGrid.disabled = !(item.chapters || []).length;
        chapterGrid.setAttribute("aria-expanded", "false");
        previousItem.disabled = !(state.queueIndex > 0);
        nextItem.disabled = !(
            state.queueIndex >= 0 && state.queueIndex < state.playQueue.length - 1
        );
        previousChapter.disabled = !(item.chapters || []).length;
        nextChapter.disabled = !(item.chapters || []).length;

        timeline.appendChild(seek);
        timeline.appendChild(time);
        transport.appendChild(previousItem);
        transport.appendChild(previousChapter);
        transport.appendChild(rewind);
        transport.appendChild(play);
        transport.appendChild(stop);
        transport.appendChild(forward);
        transport.appendChild(nextChapter);
        transport.appendChild(nextItem);
        utilities.appendChild(captions);
        utilities.appendChild(chapterGrid);
        utilities.appendChild(mute);
        utilities.appendChild(volume);
        utilities.appendChild(shuffle);
        utilities.appendChild(repeat);
        utilities.appendChild(settings);
        utilities.appendChild(fullscreen);
        actions.appendChild(transport);
        actions.appendChild(utilities);
        controls.appendChild(timeline);
        controls.appendChild(actions);
        elements.stage.appendChild(chapterPopover);
        elements.stage.appendChild(controls);
        state.playerControls = {
            root: controls,
            play: play,
            stop: stop,
            mute: mute,
            volume: volume,
            captions: captions,
            settings: settings,
            chapterGrid: chapterGrid,
            chapterPopover: chapterPopover,
            previousItem: previousItem,
            nextItem: nextItem,
            previousChapter: previousChapter,
            nextChapter: nextChapter,
            shuffle: shuffle,
            repeat: repeat,
            fullscreen: fullscreen,
            seek: seek,
            time: time
        };
        if (state.playerControlsResizeObserver) {
            state.playerControlsResizeObserver.disconnect();
            state.playerControlsResizeObserver = null;
        }
        if (window.ResizeObserver) {
            state.playerControlsResizeObserver = new ResizeObserver(schedulePlayerGeometry);
            state.playerControlsResizeObserver.observe(controls);
        }
        schedulePlayerGeometry();

        play.addEventListener("click", function () {
            toggleMediaPlayback(media, "play-button");
        });
        stop.addEventListener("click", closePlayer);
        media.addEventListener("click", function () {
            toggleMediaPlayback(media, "video-surface");
        });
        mute.addEventListener("click", function () {
            media.muted = !media.muted;
            state.muted = media.muted;
            updatePlaybackControls();
        });
        volume.addEventListener("input", function () {
            media.volume = Number(volume.value);
            media.muted = media.volume === 0;
            state.volume = media.volume;
            state.muted = media.muted;
            updatePlaybackControls();
        });
        captions.addEventListener("click", function () {
            setCaptionsEnabled(!state.captionsEnabled);
        });
        seek.addEventListener("input", function () {
            state.seeking = true;
            time.textContent = formatClock(Number(seek.value) * 1000)
                + " / " + formatClock(item.duration_ms || 0);
        });
        seek.addEventListener("change", function () {
            state.seeking = false;
            seekToOriginalTime(Number(seek.value) * 1000);
        });
        rewind.addEventListener("click", function () {
            seekToOriginalTime(originalPlaybackPosition() - 10000);
        });
        forward.addEventListener("click", function () {
            seekToOriginalTime(originalPlaybackPosition() + 30000);
        });
        previousItem.addEventListener("click", function () {
            playAdjacentQueueItem(-1);
        });
        previousChapter.addEventListener("click", function () {
            jumpToAdjacentChapter(-1);
        });
        nextChapter.addEventListener("click", function () {
            jumpToAdjacentChapter(1);
        });
        nextItem.addEventListener("click", function () {
            playAdjacentQueueItem(1);
        });
        shuffle.addEventListener("click", toggleQueueShuffle);
        bindSinglePlayerAction(repeat, cycleRepeatMode);
        chapterGrid.addEventListener("click", toggleChapterPicker);
        var settingsOpen = elements.playerDetails.classList.contains("open");
        settings.setAttribute("aria-expanded", String(settingsOpen));
        settings.classList.toggle("active", settingsOpen);
        settings.addEventListener("click", function () {
            var open = !elements.playerDetails.classList.contains("open");
            if (!open) { commitPendingPlayerSettingRanges(); }
            elements.playerDetails.classList.toggle("open", open);
            settings.classList.toggle("active", open);
            settings.setAttribute("aria-expanded", String(open));
            revealPlayerControls();
            schedulePlayerGeometry();
            if (isTizenTv && open) {
                setTimeout(function () { focusFirstPlayerSetting(); }, 0);
            }
        });
        fullscreen.addEventListener("click", function () {
            var active = document.fullscreenElement || document.webkitFullscreenElement;
            if (active) {
                recordPlaybackEvent("fullscreen_exit_requested", media);
                var exit = document.exitFullscreen || document.webkitExitFullscreen;
                if (exit) {
                    try {
                        var exitResult = exit.call(document);
                        if (exitResult && exitResult.catch) {
                            exitResult.catch(function (error) {
                                recordPlaybackEvent("fullscreen_error", media, {
                                    fullscreen_reason: error && error.message
                                        ? error.message : String(error || "exit rejected")
                                });
                            });
                        }
                    } catch (error) {
                        recordPlaybackEvent("fullscreen_error", media, {
                            fullscreen_reason: error && error.message
                                ? error.message : String(error || "exit failed")
                        });
                    }
                }
            } else {
                // Keep fullscreen attached to Fluxa's permanent document root.
                // The player modal can then close without removing the browser's
                // fullscreen element or leaving an invisible panel over the UI.
                var root = document.documentElement;
                var request = root.requestFullscreen || root.webkitRequestFullscreen;
                if (request) {
                    recordPlaybackEvent("fullscreen_enter_requested", media, {
                        fullscreen_target: "document"
                    });
                    try {
                        var result = request.call(root);
                        if (result && result.catch) {
                            result.catch(function (error) {
                                recordPlaybackEvent("fullscreen_error", media, {
                                    fullscreen_reason: error && error.message
                                        ? error.message : String(error || "request rejected"),
                                    fullscreen_target: "document"
                                });
                            });
                        }
                    } catch (error) {
                        recordPlaybackEvent("fullscreen_error", media, {
                            fullscreen_reason: error && error.message
                                ? error.message : String(error || "request failed"),
                            fullscreen_target: "document"
                        });
                    }
                }
            }
        });
        media.addEventListener("play", revealPlayerControls);
        media.addEventListener("pause", revealPlayerControls);
        updatePlaybackControls();
        if (isTizenTv) {
            setTimeout(function () {
                if (!elements.modal.hidden && state.playerControls
                        && state.playerControls.play === play) {
                    focusElement(play);
                    revealPlayerControls();
                }
            }, 0);
        }
    }

    function playerButton(icon, label) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "player-control-button focusable";
        button.setAttribute("data-focusable", "true");
        button.setAttribute("aria-label", label);
        button.title = label;
        setPlayerButtonIcon(button, icon);
        return button;
    }

    function bindSinglePlayerAction(button, action) {
        var lastActivation = 0;
        function activate() {
            var now = Date.now();
            if (now - lastActivation < 140) { return; }
            lastActivation = now;
            action();
            revealPlayerControls();
        }
        button.fluxaActivate = activate;
        button.addEventListener("click", activate);
    }

    function setPlayerButtonIcon(button, icon) {
        var paths = {
            play: '<path d="M8 5v14l11-7z"/>',
            pause: '<path d="M7 5h4v14H7zm6 0h4v14h-4z"/>',
            previousItem: '<path d="M6 5h2v14H6zm3 7 10-7v14z"/>',
            nextItem: '<path d="M16 5h2v14h-2zM5 5l10 7-10 7z"/>',
            previousChapter: '<path d="M11 6 4 12l7 6v-4h3v-4h-3z"/><path d="M16 5h5v5h-5zm0 9h5v5h-5z" opacity=".72"/>',
            nextChapter: '<path d="M13 6l7 6-7 6v-4h-3v-4h3z"/><path d="M3 5h5v5H3zm0 9h5v5H3z" opacity=".72"/>',
            rewind: '<path d="M8 7V3L2 8l6 5V9c5-1 9 1 10 6-2-3-5-4-9-3l1 2-7 1 1-7 2 2c3-2 7-2 10 0C14 7 11 6 8 7z"/>',
            forward: '<path d="M16 7V3l6 5-6 5V9c-5-1-9 1-10 6 2-3 5-4 9-3l-1 2 7 1-1-7-2 2c-3-2-7-2-10 0 2-3 5-4 8-3z"/>',
            volume: '<path d="M4 9v6h4l5 4V5L8 9zm11.5-.5a5 5 0 0 1 0 7l1.5 1.5a7 7 0 0 0 0-10z"/>',
            muted: '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 8l5 5m0-5-5 5" fill="none" stroke="currentColor" stroke-width="2"/>',
            captions: '<rect x="3" y="5" width="18" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M7 10h4v2H9v2h2v2H7zm6 0h4v2h-2v2h2v2h-4z"/>',
            chapters: '<circle cx="7" cy="8" r="1.7"/><circle cx="12" cy="8" r="1.7"/><circle cx="17" cy="8" r="1.7"/><circle cx="7" cy="14" r="1.7"/><circle cx="12" cy="14" r="1.7"/><circle cx="17" cy="14" r="1.7"/>',
            stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
            shuffle: '<path d="M4 7h3c4 0 5 10 9 10h1v-3l4 4-4 4v-3h-1c-5 0-6-10-9-10H4zm11 0h2V4l4 4-4 4V9h-2z"/>',
            repeat: '<path d="M7 5h10V2l4 4-4 4V7H7c-1.7 0-3 1.3-3 3v1H2v-1c0-2.8 2.2-5 5-5zm10 12H7v3l-4-4 4-4v3h10c1.7 0 3-1.3 3-3v-1h2v1c0 2.8-2.2 5-5 5z"/>',
            repeatOne: '<path d="M7 5h10V2l4 4-4 4V7H7c-1.7 0-3 1.3-3 3v1H2v-1c0-2.8 2.2-5 5-5zm10 12H7v3l-4-4 4-4v3h10c1.7 0 3-1.3 3-3v-1h2v1c0 2.8-2.2 5-5 5z"/><path d="M11 9h2v6h-2z" fill="#071015" stroke="currentColor" stroke-width=".7"/>',
            settings: '<path d="M4 7h7v2H4zm11 0h5v2h-5zM4 15h3v2H4zm7 0h9v2h-9z"/><circle cx="13" cy="8" r="2.5" fill="#071015" stroke="currentColor" stroke-width="2"/><circle cx="9" cy="16" r="2.5" fill="#071015" stroke="currentColor" stroke-width="2"/>',
            fullscreen: '<path d="M4 9V4h5v2H6v3zm11-5h5v5h-2V6h-3zM4 15h2v3h3v2H4zm14 0h2v5h-5v-2h3z"/>',
            exitFullscreen: '<path d="M9 4v5H4V7h3V4zm6 0h2v3h3v2h-5zM4 15h5v5H7v-3H4zm11 0h5v2h-3v3h-2z"/>'
        };
        button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">'
            + (paths[icon] || paths.play) + '</svg>';
    }

    function buildChapterPopover(item) {
        var popover = document.createElement("section");
        var heading = document.createElement("h3");
        var rail = document.createElement("div");
        var previous = document.createElement("button");
        var grid = document.createElement("div");
        var next = document.createElement("button");
        popover.className = "chapter-popover";
        popover.hidden = true;
        heading.textContent = "Chapters";
        rail.className = "chapter-rail";
        previous.type = "button";
        previous.className = "chapter-scroll-button previous";
        previous.setAttribute("aria-label", "Scroll chapters left");
        previous.title = "Previous chapters";
        previous.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15.5 4-8 8 8 8 1.5-1.5L10.5 12 17 5.5z"/></svg>';
        grid.className = "chapter-grid";
        next.type = "button";
        next.className = "chapter-scroll-button next";
        next.setAttribute("aria-label", "Scroll chapters right");
        next.title = "Next chapters";
        next.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8.5 4 8 8-8 8L7 18.5l6.5-6.5L7 5.5z"/></svg>';
        popover.appendChild(heading);
        rail.appendChild(previous);
        rail.appendChild(grid);
        rail.appendChild(next);
        popover.appendChild(rail);
        function refreshScrollButtons() {
            var maximum = Math.max(0, grid.scrollWidth - grid.clientWidth);
            previous.disabled = grid.scrollLeft <= 2;
            next.disabled = grid.scrollLeft >= maximum - 2;
        }
        function scrollChapters(direction) {
            var distance = Math.max(280, Math.round(grid.clientWidth * 0.82));
            if (typeof grid.scrollBy === "function") {
                try { grid.scrollBy({ left: distance * direction, behavior: "smooth" }); }
                catch (_error) { grid.scrollLeft += distance * direction; }
            } else {
                grid.scrollLeft += distance * direction;
            }
            setTimeout(refreshScrollButtons, 300);
        }
        previous.addEventListener("click", function () { scrollChapters(-1); });
        next.addEventListener("click", function () { scrollChapters(1); });
        grid.addEventListener("scroll", refreshScrollButtons, { passive: true });
        popover.fluxaRefreshScrollButtons = refreshScrollButtons;
        var chapters = item.chapters || [];
        chapters.forEach(function (chapter) {
            var card = document.createElement("button");
            var artwork = document.createElement("span");
            var image = document.createElement("img");
            var current = document.createElement("span");
            var title = document.createElement("strong");
            var time = document.createElement("small");
            card.type = "button";
            card.className = "chapter-card focusable";
            card.setAttribute("data-focusable", "true");
            card.setAttribute("data-start-ms", String(chapter.start_ms));
            artwork.className = "chapter-artwork";
            image.alt = "";
            image.dataset.src = chapter.thumbnail_url ? appUrl(chapter.thumbnail_url) : "";
            current.className = "chapter-current-indicator";
            current.textContent = "Now playing";
            current.setAttribute("aria-hidden", "true");
            title.textContent = chapter.title;
            time.textContent = formatClock(chapter.start_ms);
            artwork.appendChild(image);
            artwork.appendChild(current);
            card.appendChild(artwork);
            card.appendChild(title);
            card.appendChild(time);
            card.fluxaActivate = function () {
                popover.hidden = true;
                if (state.playerControls && state.playerControls.chapterGrid) {
                    state.playerControls.chapterGrid.classList.remove("active");
                    state.playerControls.chapterGrid.setAttribute("aria-expanded", "false");
                }
                seekToOriginalTime(chapter.start_ms);
            };
            card.addEventListener("click", card.fluxaActivate);
            grid.appendChild(card);
        });
        return popover;
    }

    function focusCurrentChapter(popover) {
        if (!popover) { return; }
        var active = popover.querySelector(".chapter-card.active");
        var first = popover.querySelector(".chapter-card");
        focusChapterCard(active || first);
    }

    function focusChapterCard(card) {
        if (!card) { return; }
        card.focus();
        scrollChapterCardIntoView(card);
    }

    function scrollChapterCardIntoView(card) {
        if (!card) { return; }
        try { card.scrollIntoView({ block: "nearest", inline: "center", behavior: "auto" }); }
        catch (_error) { card.scrollIntoView(false); }
        var popover = card.closest(".chapter-popover");
        if (popover && popover.fluxaRefreshScrollButtons) {
            setTimeout(popover.fluxaRefreshScrollButtons, 0);
        }
    }

    function loadChapterPreviewImages(popover) {
        var images = popover.querySelectorAll("img[data-src]");
        Array.prototype.forEach.call(images, function (image) {
            if (!image.dataset.src || image.dataset.loading) { return; }
            image.dataset.loading = "true";
            pollChapterPreview(image, image.dataset.src, 0);
        });
    }

    function toggleChapterPicker() {
        var controls = state.playerControls;
        if (elements.modal.hidden || !state.player || !controls
                || !controls.chapterPopover || !controls.chapterGrid
                || controls.chapterGrid.disabled) {
            return false;
        }
        var popover = controls.chapterPopover;
        var open = popover.hidden;
        popover.hidden = !open;
        controls.chapterGrid.classList.toggle("active", open);
        controls.chapterGrid.setAttribute("aria-expanded", String(open));
        if (open) {
            highlightCurrentChapter(popover, originalPlaybackPosition());
            loadChapterPreviewImages(popover);
            if (isTizenTv) {
                setTimeout(function () { focusCurrentChapter(popover); }, 0);
            } else {
                setTimeout(function () {
                    scrollChapterCardIntoView(popover.querySelector(".chapter-card.active"));
                    if (popover.fluxaRefreshScrollButtons) {
                        popover.fluxaRefreshScrollButtons();
                    }
                }, 0);
            }
        } else if (isTizenTv) {
            focusElement(controls.chapterGrid);
        }
        revealPlayerControls();
        schedulePlayerGeometry();
        return true;
    }

    function pollChapterPreview(image, url, attempt) {
        fetch(url, { method: "HEAD", cache: "no-store" }).then(function (response) {
            if (response.status === 200) {
                image.src = url;
                image.removeAttribute("data-src");
            } else if (response.status === 202 && attempt < 30) {
                setTimeout(function () { pollChapterPreview(image, url, attempt + 1); }, 1200);
            }
        }).catch(function () {});
    }

    function resumeMedia(media) {
        // A compatibility stream may have been suspended while playback was
        // paused. Wake FFmpeg before asking the media element to play so the
        // browser never waits for data from a process that is still stopped.
        // This also avoids racing an initial pause request against the first
        // play request on slower clients and TV WebViews.
        if (state.compatibilitySession) { sendCompatibilityControl("resume"); }
        media.play().catch(function (error) {
            recordPlaybackEvent("play_rejected", media, {
                hls_reason: (error && error.name ? error.name + ": " : "")
                    + (error && error.message ? error.message : String(error || "play rejected"))
            });
            if (state.compatibilitySession) { sendCompatibilityControl("pause"); }
            updatePlaybackControls();
        });
    }

    function pauseMedia(media, reason) {
        if (state.player === media) { state.pauseReason = reason || "fluxa"; }
        media.pause();
    }

    function toggleMediaPlayback(media, reason) {
        var now = Date.now();
        if (now - state.lastPlaybackToggleAt < 350) { return; }
        state.lastPlaybackToggleAt = now;
        if (media.paused) { resumeMedia(media); }
        else { pauseMedia(media, reason || "toggle"); }
    }

    function installVideoMediaSession() {
        if (!("mediaSession" in navigator) || state.videoMediaSessionInstalled) { return; }
        function register(action, handler) {
            try {
                navigator.mediaSession.setActionHandler(action, function (details) {
                    if (elements.modal.hidden || !state.player) { return; }
                    handler(details || {});
                });
            } catch (_error) {}
        }
        register("play", function () { resumeMedia(state.player); });
        register("pause", function () {
            // Firefox/Android can deliver the old player's Media Session pause
            // after Fluxa has already replaced it. Do not let that stale action
            // stop the first frames of the newly assigned video.
            if (Date.now() - state.playerAssignedAt < 1500) {
                recordPlaybackEvent("stale_media_session_pause_ignored", state.player, {
                    hls_reason: "new player replacement window"
                });
                return;
            }
            pauseMedia(state.player, "media-session");
        });
        register("stop", closePlayer);
        register("previoustrack", function () { playAdjacentQueueItem(-1); });
        register("nexttrack", function () { playAdjacentQueueItem(1); });
        register("seekbackward", function (details) {
            seekToOriginalTime(originalPlaybackPosition()
                - (Number(details.seekOffset) || 10) * 1000);
        });
        register("seekforward", function (details) {
            seekToOriginalTime(originalPlaybackPosition()
                + (Number(details.seekOffset) || 30) * 1000);
        });
        register("seekto", function (details) {
            if (Number.isFinite(details.seekTime)) {
                seekToOriginalTime(details.seekTime * 1000);
            }
        });
        state.videoMediaSessionInstalled = true;
    }

    function updateVideoMediaSessionMetadata(item) {
        if (!("mediaSession" in navigator) || typeof window.MediaMetadata !== "function") { return; }
        var metadata = {
            title: item.title || item.file_name || "Fluxa video",
            artist: item.show_title || item.library_name || "Fluxa",
            album: item.library_name || ""
        };
        if (item.thumbnail_url) {
            metadata.artwork = [{ src: appUrl(item.thumbnail_url) }];
        }
        try { navigator.mediaSession.metadata = new window.MediaMetadata(metadata); }
        catch (_error) {}
    }

    function updatePlaybackControls() {
        var controls = state.playerControls;
        var media = state.player;
        if (!controls || !media || !state.currentItem) { return; }
        var position = originalPlaybackPosition();
        var duration = state.currentItem.duration_ms || 0;
        if (!state.seeking) { controls.seek.value = String(Math.round(position / 1000)); }
        controls.seek.max = String(Math.max(1, Math.round(duration / 1000)));
        controls.time.textContent = formatClock(position) + " / " + formatClock(duration);
        if ("mediaSession" in navigator) {
            try { navigator.mediaSession.playbackState = media.paused ? "paused" : "playing"; }
            catch (_error) {}
            if (typeof navigator.mediaSession.setPositionState === "function" && duration > 0) {
                try {
                    navigator.mediaSession.setPositionState({
                        duration: duration / 1000,
                        playbackRate: media.playbackRate || 1,
                        position: Math.min(duration - 1, Math.max(0, position)) / 1000
                    });
                } catch (_error) {}
            }
        }
        setPlayerButtonIcon(controls.play, media.paused ? "play" : "pause");
        controls.play.setAttribute("aria-label", media.paused ? "Play" : "Pause");
        controls.play.title = media.paused ? "Play" : "Pause";
        setPlayerButtonIcon(controls.mute, media.muted || media.volume === 0 ? "muted" : "volume");
        controls.mute.setAttribute("aria-label", media.muted ? "Unmute" : "Mute");
        controls.mute.title = media.muted ? "Unmute" : "Mute";
        controls.volume.value = String(media.volume);
        controls.captions.classList.toggle("active", state.captionsEnabled);
        controls.shuffle.classList.toggle("active", state.queueShuffled);
        controls.shuffle.setAttribute("aria-label", state.queueShuffled
            ? "Turn shuffle off" : "Turn shuffle on");
        controls.shuffle.title = state.queueShuffled ? "Shuffle on" : "Shuffle off";
        setPlayerButtonIcon(controls.repeat, state.repeatMode === "one" ? "repeatOne" : "repeat");
        controls.repeat.classList.toggle("active", state.repeatMode !== "none");
        var repeatLabel = state.repeatMode === "one"
            ? "Repeat one" : (state.repeatMode === "all" ? "Repeat all" : "Repeat off");
        controls.repeat.setAttribute("aria-label", repeatLabel);
        controls.repeat.title = repeatLabel;
        updateFullscreenControl();
        controls.previousItem.disabled = !(state.queueIndex > 0
            || (state.repeatMode === "all" && state.playQueue.length > 1));
        controls.nextItem.disabled = !(
            state.queueIndex >= 0 && (state.queueIndex < state.playQueue.length - 1
                || (state.repeatMode === "all" && state.playQueue.length > 1))
        );
        var chapters = state.currentItem.chapters || [];
        controls.previousChapter.disabled = !chapters.length
            || position <= (chapters[0].start_ms || 0) + 4000;
        controls.nextChapter.disabled = !chapters.length
            || position >= chapters[chapters.length - 1].start_ms;
        highlightCurrentChapter(controls.chapterPopover, position);
    }

    function updateFullscreenControl() {
        var controls = state.playerControls;
        if (!controls || !controls.fullscreen) { return; }
        var active = Boolean(document.fullscreenElement || document.webkitFullscreenElement);
        setPlayerButtonIcon(controls.fullscreen, active ? "exitFullscreen" : "fullscreen");
        controls.fullscreen.setAttribute(
            "aria-label", active ? "Exit full screen" : "Enter full screen"
        );
        controls.fullscreen.title = active ? "Exit full screen" : "Enter full screen";
    }

    function handleFullscreenChange() {
        var active = Boolean(document.fullscreenElement || document.webkitFullscreenElement);
        if (active !== state.lastFullscreenActive) {
            state.lastFullscreenActive = active;
            recordPlaybackEvent(active ? "fullscreen_entered" : "fullscreen_exited", state.player, {
                fullscreen_target: active && document.fullscreenElement === document.documentElement
                    ? "document" : (active ? "other" : "none")
            });
        }
        updateFullscreenControl();
        schedulePlayerGeometry();
        revealPlayerControls();
        if (state.closingPlayer && !playerPanelIsFullscreen()) {
            finishClosePlayer();
        }
    }

    function revealPlayerControls() {
        if (!elements.playerPanel) { return; }
        schedulePlayerGeometry();
        if (state.controlsHideTimer) {
            clearTimeout(state.controlsHideTimer);
            state.controlsHideTimer = null;
        }
        elements.playerPanel.classList.remove("controls-hidden");
        if (!playerPanelIsFullscreen() || elements.modal.hidden
                || !state.player || state.player.paused || playerOverlayIsOpen()) {
            return;
        }
        state.controlsHideTimer = setTimeout(function () {
            state.controlsHideTimer = null;
            if (playerPanelIsFullscreen() && !elements.modal.hidden
                    && state.player && !state.player.paused
                    && !state.seeking && !playerOverlayIsOpen()) {
                elements.playerPanel.classList.add("controls-hidden");
            }
        }, 2000);
    }

    function hidePlayerControls() {
        if (!elements.playerPanel) { return; }
        if (state.controlsHideTimer) {
            clearTimeout(state.controlsHideTimer);
            state.controlsHideTimer = null;
        }
        elements.playerPanel.classList.add("controls-hidden");
    }

    function schedulePlayerGeometry() {
        if (!elements.playerPanel || state.playerGeometryFrame !== null) { return; }
        state.playerGeometryFrame = window.requestAnimationFrame(updatePlayerGeometry);
    }

    function updatePlayerGeometry() {
        state.playerGeometryFrame = null;
        if (!elements.playerPanel || elements.modal.hidden) { return; }
        var panelRect = elements.playerPanel.getBoundingClientRect();
        var viewportBottom = window.innerHeight || document.documentElement.clientHeight;
        if (window.visualViewport) {
            viewportBottom = window.visualViewport.offsetTop + window.visualViewport.height;
        }
        var bottomInset = Math.max(0, Math.min(
            panelRect.height,
            panelRect.bottom - viewportBottom
        ));
        var controlsHeight = state.playerControls && state.playerControls.root
            ? state.playerControls.root.getBoundingClientRect().height : 0;
        elements.playerPanel.style.setProperty(
            "--fluxa-player-bottom-inset", Math.round(bottomInset) + "px"
        );
        elements.playerPanel.style.setProperty(
            "--fluxa-player-controls-height", Math.ceil(controlsHeight) + "px"
        );
    }

    function playerPanelIsFullscreen() {
        // The installed TV application is already a full-screen surface and
        // does not enter the browser Fullscreen API when a video opens.
        if (isTizenTv && !elements.modal.hidden) { return true; }
        var active = document.fullscreenElement || document.webkitFullscreenElement;
        return Boolean(active && !elements.modal.hidden && (
            active === document.documentElement || active === elements.playerPanel
        ));
    }

    function playerOverlayIsOpen() {
        var controls = state.playerControls;
        return elements.playerDetails.classList.contains("open") || Boolean(
            controls && controls.chapterPopover && !controls.chapterPopover.hidden
        );
    }

    function focusFirstPlayerSetting() {
        var first = elements.playerDetails.querySelector("[data-focusable='true']:not([disabled])");
        if (first && isVisible(first)) { focusElement(first); }
    }

    function highlightCurrentChapter(popover, position) {
        if (!popover) { return; }
        var chapters = state.currentItem.chapters || [];
        var cards = popover.querySelectorAll(".chapter-card");
        var activeIndex = chapters.length ? 0 : -1;
        chapters.forEach(function (chapter, index) {
            if (position >= Number(chapter.start_ms || 0)) { activeIndex = index; }
        });
        Array.prototype.forEach.call(cards, function (card, index) {
            var active = index === activeIndex;
            card.classList.toggle("active", active);
            if (active) { card.setAttribute("aria-current", "true"); }
            else { card.removeAttribute("aria-current"); }
        });
    }

    function seekToOriginalTime(positionMs) {
        if (!state.currentItem || !state.player) { return; }
        var bounded = Math.max(0, Math.min(positionMs, state.currentItem.duration_ms || positionMs));
        var wasPlaying = !state.player.paused;
        if (state.compatibilitySession) {
            saveProgress(false);
            rememberCurrentFrame();
            stopActivePlayback(false);
            startCompatibilityPlayer(state.currentItem, bounded, wasPlaying);
        } else {
            state.player.currentTime = bounded / 1000;
            if (wasPlaying) { resumeMedia(state.player); }
        }
    }

    function jumpToAdjacentChapter(direction) {
        var chapters = state.currentItem ? (state.currentItem.chapters || []) : [];
        if (!chapters.length) { return; }
        var position = originalPlaybackPosition();
        var currentIndex = 0;
        chapters.forEach(function (chapter, index) {
            if (position >= chapter.start_ms) { currentIndex = index; }
        });
        var targetIndex;
        if (direction > 0) {
            if (currentIndex >= chapters.length - 1) { return; }
            targetIndex = Math.min(chapters.length - 1, currentIndex + 1);
        } else {
            var currentStart = chapters[currentIndex].start_ms;
            if (currentIndex === 0 && position - currentStart <= 4000) { return; }
            targetIndex = position - currentStart > 4000
                ? currentIndex
                : Math.max(0, currentIndex - 1);
        }
        seekToOriginalTime(chapters[targetIndex].start_ms);
    }

    function originalPlaybackPosition() {
        if (!state.player) { return state.playbackOffsetMs || 0; }
        return Math.max(0, state.playbackOffsetMs + Math.round(state.player.currentTime * 1000));
    }

    function configureCaptions(item) {
        var tracks = usableCaptionTracks(item);
        var globalEnabled = getStoredPreference("fluxa-captions-global") === "on";
        elements.captionsGlobal.checked = globalEnabled;
        elements.captionTrack.innerHTML = "";
        tracks.forEach(function (track) {
            var option = document.createElement("option");
            option.value = String(track.ordinal);
            option.textContent = track.title + (track.kind === "bitmap" ? " · image" : "");
            elements.captionTrack.appendChild(option);
        });
        var preferred = tracks.find(function (track) {
            return track.hearing_impaired;
        }) || tracks.find(function (track) {
            return track.default;
        }) || tracks.find(function (track) {
            return (track.language || "").toLowerCase() === "eng";
        }) || tracks[0];
        state.captionTrackOrdinal = preferred ? preferred.ordinal : null;
        state.captionsEnabled = Boolean(globalEnabled && preferred);
        elements.captionsCurrent.checked = state.captionsEnabled;
        elements.captionsCurrent.disabled = !tracks.length;
        elements.captionsGlobal.disabled = false;
        elements.captionTrack.disabled = tracks.length < 2;
        if (preferred) {
            elements.captionTrack.value = String(preferred.ordinal);
        } else {
            var empty = document.createElement("option");
            empty.textContent = "No supported captions";
            elements.captionTrack.appendChild(empty);
        }
    }

    function configureCompressor(payload) {
        if (!payload || !payload.effective) { return; }
        state.globalCompressor = payload.global;
        state.videoCompressor = payload.video || null;
        state.compressorSource = payload.source === "video" ? "video" : "global";
        var settings = payload.effective;
        state.compressorEnabled = settings.enabled !== false;
        state.compressorThreshold = boundedNumber(settings.threshold_db, -36, -8, -24);
        state.compressorRatio = boundedNumber(settings.ratio, 2, 16, 8);
        state.compressorOutputGain = boundedNumber(settings.output_gain_db, -12, 18, 9);
        state.compressorCeiling = boundedNumber(settings.ceiling_db, -12, -1, -3);
        state.compressorAttack = boundedNumber(settings.attack_ms, 1, 200, 15);
        state.compressorRelease = boundedNumber(settings.release_ms, 50, 3000, 750);
        state.compressorKnee = boundedNumber(settings.knee, 1, 8, 4);
        elements.compressorEnabled.checked = state.compressorEnabled;
        elements.compressorThreshold.value = String(state.compressorThreshold);
        elements.compressorRatio.value = String(state.compressorRatio);
        elements.compressorOutputGain.value = String(state.compressorOutputGain);
        elements.compressorCeiling.value = String(state.compressorCeiling);
        elements.compressorAttack.value = String(state.compressorAttack);
        elements.compressorRelease.value = String(state.compressorRelease);
        elements.compressorKnee.value = String(state.compressorKnee);
        var videoScope = state.compressorSource === "video";
        elements.compressorScope.textContent = videoScope
            ? "Using settings saved for this video"
            : "Using global settings";
        elements.compressorCreateVideo.hidden = videoScope || !state.currentItem;
        elements.compressorVideoToGlobal.hidden = !videoScope;
        elements.compressorGlobalToVideo.hidden = !videoScope;
        elements.compressorFollowGlobal.hidden = !videoScope;
        updateCompressorLabels();
    }

    function configureBass() {
        elements.bassEnhancement.checked = state.bassEnhancement;
        elements.bassGain.value = String(state.bassGain);
        updateBassControls();
    }

    function updateBassControls() {
        elements.bassGainValue.textContent = signedDb(elements.bassGain.value);
        elements.bassGain.disabled = !elements.bassEnhancement.checked;
    }

    function applyBassSettings() {
        state.bassEnhancement = elements.bassEnhancement.checked;
        state.bassGain = boundedNumber(elements.bassGain.value, 0, 9, 4);
        setStoredPreference("fluxa-bass-enhancement", state.bassEnhancement ? "yes" : "no");
        setStoredPreference("fluxa-bass-gain", String(state.bassGain));
        updateBassControls();
        restartCompressorStream();
    }

    function updateCompressorLabels() {
        elements.compressorThresholdValue.textContent = signedDb(elements.compressorThreshold.value);
        elements.compressorRatioValue.textContent = Number(elements.compressorRatio.value) + ":1";
        elements.compressorOutputGainValue.textContent = signedDb(elements.compressorOutputGain.value);
        elements.compressorCeilingValue.textContent = signedDb(elements.compressorCeiling.value);
        elements.compressorAttackValue.textContent = Number(elements.compressorAttack.value) + " ms";
        elements.compressorReleaseValue.textContent = Number(elements.compressorRelease.value) + " ms";
        elements.compressorKneeValue.textContent = Number(elements.compressorKnee.value).toFixed(1).replace(".0", "");
        var disabled = !elements.compressorEnabled.checked;
        elements.compressorThreshold.disabled = disabled;
        elements.compressorRatio.disabled = disabled;
        elements.compressorOutputGain.disabled = disabled;
        elements.compressorCeiling.disabled = disabled;
        elements.compressorAttack.disabled = disabled;
        elements.compressorRelease.disabled = disabled;
        elements.compressorKnee.disabled = disabled;
    }

    function applyCompressorSettings() {
        updateCompressorLabels();
        var settings = compressorFormSettings();
        var endpoint = state.compressorSource === "video" && state.currentItem
            ? "/api/media/" + state.currentItem.id + "/compressor-settings"
            : "/api/compressor-settings";
        saveCompressor(endpoint, settings, true);
    }

    function compressorFormSettings() {
        return {
            enabled: elements.compressorEnabled.checked,
            threshold_db: Number(elements.compressorThreshold.value),
            ratio: Number(elements.compressorRatio.value),
            output_gain_db: Number(elements.compressorOutputGain.value),
            ceiling_db: Number(elements.compressorCeiling.value),
            attack_ms: Number(elements.compressorAttack.value),
            release_ms: Number(elements.compressorRelease.value),
            knee: Number(elements.compressorKnee.value)
        };
    }

    function saveCompressor(endpoint, settings, restart) {
        api(endpoint, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(settings)
        }).then(function (payload) {
            configureCompressor(payload);
            if (restart) { restartCompressorStream(); }
        }).catch(function (error) {
            showToast(error.message, true);
        });
    }

    function createVideoCompressor() {
        if (!state.currentItem) { return; }
        saveCompressor(
            "/api/media/" + state.currentItem.id + "/compressor-settings",
            compressorFormSettings(),
            false
        );
    }

    function copyVideoCompressorToGlobal() {
        if (state.compressorSource !== "video") { return; }
        api("/api/compressor-settings", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(compressorFormSettings())
        }).then(function (payload) {
            state.globalCompressor = payload.global;
            showToast("This video’s compressor settings are now the global settings.");
        }).catch(function (error) { showToast(error.message, true); });
    }

    function copyGlobalCompressorToVideo() {
        if (!state.currentItem || !state.globalCompressor) { return; }
        saveCompressor(
            "/api/media/" + state.currentItem.id + "/compressor-settings",
            state.globalCompressor,
            true
        );
    }

    function followGlobalCompressor() {
        if (!state.currentItem) { return; }
        saveCompressor(
            "/api/media/" + state.currentItem.id + "/compressor-settings",
            { inherit_global: true },
            true
        );
    }

    function restartCompressorStream() {
        if (!state.currentItem || !state.player || state.currentItem.media_type !== "video") {
            return;
        }
        var position = originalPlaybackPosition();
        var wasPlaying = !state.player.paused;
        rememberCurrentFrame();
        stopActivePlayback(false);
        startCompatibilityPlayer(state.currentItem, position, wasPlaying);
    }

    function boundedNumber(value, minimum, maximum, fallback) {
        var number = Number(value);
        return isFinite(number) ? Math.max(minimum, Math.min(maximum, number)) : fallback;
    }

    function signedDb(value) {
        var number = Number(value);
        return (number < 0 ? "−" + Math.abs(number) : number) + " dB";
    }

    function usableCaptionTracks(item) {
        return (item.subtitles || []).filter(function (track) {
            return track.kind === "text" || track.kind === "bitmap";
        });
    }

    function selectedCaptionTrack(item) {
        return usableCaptionTracks(item).find(function (track) {
            return track.ordinal === state.captionTrackOrdinal;
        }) || null;
    }

    function setCaptionsEnabled(enabled) {
        state.captionsEnabled = Boolean(enabled && selectedCaptionTrack(state.currentItem || {}));
        elements.captionsCurrent.checked = state.captionsEnabled;
        refreshCaptionSelection();
        updatePlaybackControls();
    }

    function refreshCaptionSelection() {
        if (!state.currentItem || !state.player) { return; }
        var selected = selectedCaptionTrack(state.currentItem);
        var wantedBitmap = state.captionsEnabled && selected && selected.kind === "bitmap"
            ? selected.ordinal
            : null;
        var currentBitmap = state.compatibilitySession
            ? state.compatibilitySession.subtitle_ordinal
            : null;
        if (wantedBitmap !== currentBitmap && (wantedBitmap !== null || currentBitmap !== null)) {
            var position = originalPlaybackPosition();
            var wasPlaying = !state.player.paused;
            rememberCurrentFrame();
            stopActivePlayback(false);
            startCompatibilityPlayer(state.currentItem, position, wasPlaying);
            return;
        }
        if (wantedBitmap !== null && !state.compatibilitySession) {
            var directPosition = originalPlaybackPosition();
            var directWasPlaying = !state.player.paused;
            rememberCurrentFrame();
            stopActivePlayback(false);
            startCompatibilityPlayer(state.currentItem, directPosition, directWasPlaying);
            return;
        }
        applyTextCaptionPreference();
    }

    function attachTextCaptions(media, item, offsetMs) {
        (item.subtitles || []).forEach(function (track) {
            if (track.kind !== "text" || !track.url) { return; }
            var element = document.createElement("track");
            element.kind = "captions";
            element.label = track.title;
            element.srclang = track.language || "und";
            element.src = appUrl(track.url);
            element.track.fluxaOrdinal = track.ordinal;
            element.addEventListener("load", function () {
                if (offsetMs > 0 && !element.track.fluxaShifted) {
                    var cues = Array.from(element.track.cues || []);
                    cues.forEach(function (cue) {
                        if (cue.endTime * 1000 <= offsetMs) {
                            element.track.removeCue(cue);
                        } else {
                            cue.startTime = Math.max(0, cue.startTime - offsetMs / 1000);
                            cue.endTime = Math.max(0.05, cue.endTime - offsetMs / 1000);
                        }
                    });
                    element.track.fluxaShifted = true;
                }
                applyTextCaptionPreference();
            });
            media.appendChild(element);
            if (track.ordinal === state.captionTrackOrdinal) {
                element.track.mode = state.captionsEnabled ? "showing" : "hidden";
            }
        });
    }

    function applyTextCaptionPreference() {
        if (!state.player || !state.player.textTracks) { return; }
        var tracks = state.player.textTracks;
        var index;
        for (index = 0; index < tracks.length; index += 1) {
            var selected = tracks[index].fluxaOrdinal === state.captionTrackOrdinal;
            tracks[index].mode = state.captionsEnabled && selected ? "showing" : "disabled";
        }
    }

    function getStoredPreference(key) {
        try { return window.localStorage.getItem(key); } catch (_error) { return null; }
    }

    function setStoredPreference(key, value) {
        try { window.localStorage.setItem(key, value); } catch (_error) {}
    }

    function bindPlaybackEvents(media, item, compatibility) {
        media.addEventListener("waiting", function () {
            if (state.stallStartedAt === null) {
                state.stallStartedAt = performance.now();
            }
            showPlayerLoader();
            recordPlaybackEvent("waiting", media);
        });
        media.addEventListener("stalled", function () {
            recordPlaybackEvent("stalled", media);
        });
        media.addEventListener("seeking", function () {
            recordPlaybackEvent("seeking", media);
            state.playbackSamplePositionMs = null;
            state.playbackSampleAt = null;
        });
        media.addEventListener("seeked", function () {
            recordPlaybackEvent("seeked", media);
            state.playbackSamplePositionMs = originalPlaybackPosition();
            state.playbackSampleAt = performance.now();
        });
        media.addEventListener("timeupdate", function () {
            detectPlaybackTimelineJump(media);
            updatePlaybackControls();
            if (Date.now() - state.lastProgressSent > 8000) {
                saveProgress(false);
            }
        });
        media.addEventListener("pause", function () {
            if (state.suppressPlaybackControls || media.ended) { return; }
            var pauseReason = state.pauseReason || "browser-or-system";
            state.pauseReason = "";
            recordPlaybackEvent("paused", media, { hls_reason: pauseReason });
            saveProgress(false);
            if (compatibility) { sendCompatibilityControl("pause"); }
            updatePlaybackControls();
        });
        media.addEventListener("play", function () {
            recordPlaybackEvent("play", media);
            if (compatibility) { sendCompatibilityControl("resume"); }
            updatePlaybackControls();
        });
        media.addEventListener("playing", function () {
            hidePlayerLoader();
            if (state.stallStartedAt !== null) {
                recordPlaybackEvent("stall_recovered", media, {
                    stall_ms: Math.round(performance.now() - state.stallStartedAt)
                });
                state.stallStartedAt = null;
            } else {
                recordPlaybackEvent("playing", media);
            }
        });
        media.addEventListener("error", function () {
            recordPlaybackEvent("media_error", media);
        });
        media.addEventListener("volumechange", function () {
            state.volume = media.volume;
            state.muted = media.muted;
            updatePlaybackControls();
        });
        media.addEventListener("loadedmetadata", updatePlaybackControls);
        media.addEventListener("ended", function () {
            saveProgress(true);
            rememberCurrentFrame();
            stopActivePlayback(false);
            playNextQueueItem();
        });
    }

    function detectPlaybackTimelineJump(media) {
        var now = performance.now();
        var position = originalPlaybackPosition();
        if (state.playbackSamplePositionMs !== null && state.playbackSampleAt !== null
                && !state.seeking && !media.seeking) {
            var elapsed = now - state.playbackSampleAt;
            var mediaDelta = position - state.playbackSamplePositionMs;
            var expected = elapsed * (media.playbackRate || 1);
            var jump = mediaDelta - expected;
            if (elapsed < 5000 && jump > 3000) {
                recordPlaybackEvent("timeline_jump", media, {
                    jump_ms: Math.round(jump),
                    elapsed_ms: Math.round(elapsed),
                    media_delta_ms: Math.round(mediaDelta),
                    expected_delta_ms: Math.round(expected)
                });
            }
        }
        state.playbackSamplePositionMs = position;
        state.playbackSampleAt = now;
    }

    function recordPlaybackEvent(eventName, media, extra) {
        if (!state.currentItem) { return; }
        var bufferedAhead = 0;
        if (media && media.buffered) {
            var index;
            for (index = 0; index < media.buffered.length; index += 1) {
                if (media.buffered.start(index) <= media.currentTime + 0.05
                        && media.buffered.end(index) >= media.currentTime) {
                    bufferedAhead = Math.max(
                        0, (media.buffered.end(index) - media.currentTime) * 1000
                    );
                    break;
                }
            }
        }
        var payload = {
            event: eventName,
            media_id: state.currentItem.id,
            session_id: state.compatibilitySession
                ? state.compatibilitySession.session_id
                : null,
            position_ms: media
                ? Math.round((state.playbackOffsetMs + media.currentTime * 1000))
                : state.playbackOffsetMs,
            buffered_ahead_ms: Math.round(bufferedAhead),
            ready_state: media ? media.readyState : 0,
            network_state: media ? media.networkState : 0,
            paused: media ? media.paused : true,
            hidden: document.hidden,
            client_version: clientVersion,
            tizen_mode: isTizenTv,
            tesla_browser: isTeslaBrowser,
            segment_format: state.compatibilitySession
                ? state.compatibilitySession.segment_format
                : null,
            player_modal_hidden: elements.modal.hidden,
            player_modal_display: window.getComputedStyle(elements.modal).display
        };
        Object.keys(extra || {}).forEach(function (key) { payload[key] = extra[key]; });
        fetch(appUrl("/api/playback-events"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
            keepalive: true
        }).catch(function () {});
    }

    function appendQueuePosition() {
        if (state.queueIndex >= 0 && state.playQueue.length) {
            elements.playerTechnical.textContent += " · "
                + (state.queueShuffled ? "Shuffled item " : "Playlist item ")
                + (state.queueIndex + 1) + " of " + state.playQueue.length;
        }
    }

    function renderAnalysis(analysis) {
        var status = analysis.status || "pending";
        elements.analyze.disabled = false;
        elements.analyze.hidden = false;
        if (status === "done") {
            var count = analysis.segments ? analysis.segments.length : analysis.spike_count;
            elements.analysisTitle.textContent = count + (count === 1 ? " spike region mapped" : " spike regions mapped");
            elements.analysisCopy.textContent = "A precise gain map is stored as metadata and used on the next compatibility stream.";
            elements.analyze.textContent = "Rebuild map";
            clearAnalysisTimer();
        } else if (status === "running") {
            elements.analysisTitle.textContent = "Building a precise map…";
            elements.analysisCopy.textContent = "Live lookahead remains available; this optional scan improves program-aware leveling.";
            elements.analyze.textContent = "Mapping…";
            elements.analyze.disabled = true;
            scheduleAnalysisPoll();
        } else if (status === "error") {
            elements.analysisTitle.textContent = "Live lookahead active";
            elements.analysisCopy.textContent = (analysis.error || "The precise map could not be built.") + " Live leveling still works.";
            elements.analyze.textContent = "Try again";
            clearAnalysisTimer();
        } else {
            elements.analysisTitle.textContent = "Live lookahead active";
            elements.analysisCopy.textContent = "Fluxa buffers upcoming audio and reduces louder regions while you watch. A full scan is optional.";
            elements.analyze.textContent = "Build precise map";
            clearAnalysisTimer();
        }
    }

    function startAnalysis() {
        if (!state.currentItem) { return; }
        elements.analyze.disabled = true;
        api("/api/media/" + state.currentItem.id + "/analyze", { method: "POST" }).then(function () {
            renderAnalysis({ status: "running" });
            showToast("Optional precise audio map started. Live leveling remains available.");
        }).catch(function (error) {
            elements.analyze.disabled = false;
            showToast(error.message, true);
        });
    }

    function scheduleAnalysisPoll() {
        clearAnalysisTimer();
        state.analysisTimer = setTimeout(function poll() {
            if (!state.currentItem || elements.modal.hidden) { return; }
            api("/api/media/" + state.currentItem.id + "/loudness").then(function (analysis) {
                renderAnalysis(analysis);
            }).catch(function () {
                state.analysisTimer = setTimeout(poll, 4000);
            });
        }, 3500);
    }

    function clearAnalysisTimer() {
        if (state.analysisTimer) {
            clearTimeout(state.analysisTimer);
            state.analysisTimer = null;
        }
    }

    function saveProgress(completed) {
        if (!state.currentItem || !state.player) { return; }
        state.lastProgressSent = Date.now();
        var positionMs = originalPlaybackPosition();
        var durationMs = state.currentItem.duration_ms;
        if (!durationMs && isFinite(state.player.duration)) {
            durationMs = Math.round(state.player.duration * 1000);
        }
        var payload = {
            position_ms: Math.max(0, positionMs),
            duration_ms: Math.max(0, durationMs || 0),
            completed: Boolean(completed)
        };
        api("/api/media/" + state.currentItem.id + "/progress", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        }).catch(function () {});
    }

    function closePlayer() {
        if (state.closingPlayer) { return; }
        finishClosePlayer();
    }

    function finishClosePlayer() {
        state.closingPlayer = false;
        if (state.controlsHideTimer) {
            clearTimeout(state.controlsHideTimer);
            state.controlsHideTimer = null;
        }
        elements.playerPanel.classList.remove("controls-hidden");
        state.openToken += 1;
        clearAnalysisTimer();
        stopActivePlayback(true);
        state.currentItem = null;
        state.playQueue = [];
        state.queueOriginal = [];
        state.queueIndex = -1;
        state.queueShuffled = false;
        state.transitionFrame = null;
        elements.stage.innerHTML = "";
        elements.stage.removeAttribute("aria-busy");
        elements.playerDetails.classList.remove("open");
        elements.modal.hidden = true;
        document.body.classList.remove("player-open");
        document.body.style.overflow = "";
        loadMedia();
    }

    function rememberCurrentFrame() {
        var media = state.player;
        if (!media || media.tagName !== "VIDEO" || media.readyState < 2
                || !media.videoWidth || !media.videoHeight) {
            return;
        }
        var canvas = document.createElement("canvas");
        var scale = Math.min(1, 1280 / media.videoWidth, 720 / media.videoHeight);
        canvas.width = Math.max(1, Math.round(media.videoWidth * scale));
        canvas.height = Math.max(1, Math.round(media.videoHeight * scale));
        try {
            canvas.getContext("2d").drawImage(media, 0, 0, canvas.width, canvas.height);
            state.transitionFrame = canvas.toDataURL("image/jpeg", 0.82);
        } catch (_error) {
            state.transitionFrame = null;
        }
    }

    function hudPolar(radius, deg) {
        var angle = (deg - 90) * Math.PI / 180;
        return [Math.cos(angle) * radius, Math.sin(angle) * radius];
    }

    function hudBox(innerR, outerR, startDeg, endDeg) {
        var outerStart = hudPolar(outerR, startDeg);
        var outerEnd = hudPolar(outerR, endDeg);
        var innerEnd = hudPolar(innerR, endDeg);
        var innerStart = hudPolar(innerR, startDeg);
        var large = (endDeg - startDeg) > 180 ? 1 : 0;
        return "<path d=\"M" + outerStart[0].toFixed(2) + " " + outerStart[1].toFixed(2)
            + " A" + outerR + " " + outerR + " 0 " + large + " 1 "
            + outerEnd[0].toFixed(2) + " " + outerEnd[1].toFixed(2)
            + " L" + innerEnd[0].toFixed(2) + " " + innerEnd[1].toFixed(2)
            + " A" + innerR + " " + innerR + " 0 " + large + " 0 "
            + innerStart[0].toFixed(2) + " " + innerStart[1].toFixed(2)
            + " Z\"/>";
    }

    function hudTee(radius, deg, stem, bar, inward) {
        var angle = (deg - 90) * Math.PI / 180;
        var tipR = inward ? radius - stem : radius + stem;
        var base = hudPolar(radius, deg);
        var tip = hudPolar(tipR, deg);
        var tx = -Math.sin(angle) * bar;
        var ty = Math.cos(angle) * bar;
        return "<path fill=\"none\" d=\"M" + base[0].toFixed(2) + " " + base[1].toFixed(2)
            + " L" + tip[0].toFixed(2) + " " + tip[1].toFixed(2)
            + " M" + (tip[0] - tx).toFixed(2) + " " + (tip[1] - ty).toFixed(2)
            + " L" + (tip[0] + tx).toFixed(2) + " " + (tip[1] + ty).toFixed(2) + "\"/>";
    }

    function hudTeeSolid(radius, deg, stem, bar, thick, inward) {
        var angle = (deg - 90) * Math.PI / 180;
        var ux = Math.cos(angle);
        var uy = Math.sin(angle);
        var vx = -uy;
        var vy = ux;
        var sign = inward ? -1 : 1;
        var r0 = radius;
        var r1 = radius + sign * (stem - thick);
        var r2 = radius + sign * stem;
        var hs = thick / 2;
        function pt(r, t) {
            return [(ux * r + vx * t).toFixed(2) + " " + (uy * r + vy * t).toFixed(2)];
        }
        return "<path d=\"M" + pt(r0, -hs) + " L" + pt(r1, -hs) + " L" + pt(r1, -bar)
            + " L" + pt(r2, -bar) + " L" + pt(r2, bar) + " L" + pt(r1, bar)
            + " L" + pt(r1, hs) + " L" + pt(r0, hs) + " Z\"/>";
    }

    function hudBoxes(innerR, outerR, startDeg, count, spanDeg, stepDeg) {
        var i;
        var markup = "";
        for (i = 0; i < count; i += 1) {
            var start = startDeg + i * stepDeg;
            markup += hudBox(innerR, outerR, start, start + spanDeg);
        }
        return markup;
    }

    function hudMarkup() {
        return "<svg viewBox=\"0 0 200 200\" aria-hidden=\"true\">"
            + "<defs><filter id=\"hud-glow\" x=\"-20%\" y=\"-20%\" width=\"140%\" height=\"140%\">"
            + "<feGaussianBlur stdDeviation=\"1.4\" result=\"blur\"/>"
            + "<feMerge><feMergeNode in=\"blur\"/><feMergeNode in=\"SourceGraphic\"/></feMerge>"
            + "</filter></defs>"
            + "<g transform=\"translate(100 100)\">"

            /* A restrained inner lock: broad, dim pieces establish the depth
               without competing with the brighter moving rings. */
            + "<g class=\"hud-layer hud-spin hud-ring-1\" fill=\"#176d66\" stroke=\"none\">"
            + hudBox(18, 29, 6, 66)
            + hudBox(19, 32, 94, 145)
            + hudBox(16, 28, 193, 255)
            + hudBox(20, 30, 288, 334)
            + "</g>"

            /* Three neighboring pieces deliberately vary in width and nearly
               collide, like tumblers in a ring-puzzle lock. */
            + "<g class=\"hud-layer hud-spin hud-ring-2 hud-lit\" fill=\"#29d3ae\" stroke=\"rgba(164,255,232,.35)\" stroke-width=\".6\">"
            + hudBox(30, 42, 12, 38)
            + hudBox(32, 47, 38, 55)
            + hudBox(27, 40, 59, 95)
            + hudBox(31, 43, 130, 181)
            + hudBox(29, 45, 221, 249)
            + hudBox(34, 44, 286, 328)
            + "</g>"

            /* An unfilled overlay crosses the adjacent rings instead of
               creating another cleanly divided circular track. */
            + "<g class=\"hud-layer hud-spin hud-ring-4 hud-lit\" fill=\"none\" stroke=\"#8aead4\" stroke-width=\"1.2\">"
            + hudBox(40, 51, -2, 25)
            + hudBox(45, 58, 25, 42)
            + hudBox(39, 54, 88, 130)
            + hudBox(44, 55, 174, 205)
            + hudBox(40, 57, 256, 302)
            + "</g>"

            /* A quiet, non-glowing middle layer remains visible when the
               brighter fading layers disappear. */
            + "<g class=\"hud-layer hud-spin hud-ring-3\" fill=\"#176d66\" stroke=\"#29d3ae\" stroke-width=\".5\">"
            + hudBox(50, 63, 15, 65)
            + hudBox(54, 68, 96, 137)
            + hudBox(48, 61, 173, 234)
            + hudBox(53, 66, 271, 326)
            + "</g>"

            /* The outer outline has breathing room; its first two pieces sit
               side by side, while the remaining gaps stay irregular. */
            + "<g class=\"hud-layer hud-spin hud-ring-7 hud-lit\" fill=\"none\" stroke=\"#bffbef\" stroke-width=\"1.3\">"
            + hudBox(62, 73, 5, 27)
            + hudBox(65, 80, 27, 44)
            + hudBox(60, 72, 102, 143)
            + hudBox(66, 77, 184, 214)
            + hudBox(61, 75, 244, 291)
            + hudBox(68, 79, 328, 348)
            + "</g>"

            /* Small outer accents finish the puzzle without forming a dense
               rim or an oversized band. */
            + "<g class=\"hud-layer hud-spin hud-ring-6 hud-lit\" fill=\"#29d3ae\" stroke=\"rgba(200,255,240,.7)\" stroke-width=\"1\">"
            + hudBox(76, 84, 18, 31)
            + hudBox(71, 82, 78, 109)
            + hudBox(77, 87, 153, 169)
            + hudBox(69, 81, 224, 251)
            + hudBox(75, 85, 302, 321)
            + "</g>"

            /* T marks stay sparse, independent, and within the box field.
               There is intentionally no T-only outer ring. */
            + "<g class=\"hud-layer hud-spin hud-tee-1 hud-lit\" fill=\"none\" stroke=\"#d7fff4\" stroke-width=\"1.15\" stroke-opacity=\".38\">"
            + hudTee(34, 24, 6, 3.2, false)
            + hudTee(32, 112, 7, 3.4, true)
            + hudTee(35, 218, 5.5, 3.1, false)
            + hudTee(33, 307, 6.5, 3.3, true)
            + "</g>"
            + "<g class=\"hud-layer hud-spin hud-tee-3\" fill=\"none\" stroke=\"#8aead4\" stroke-width=\"1.2\" stroke-opacity=\".32\">"
            + hudTee(55, 19, 7, 3.6, false)
            + hudTee(53, 151, 6, 3.4, true)
            + hudTee(56, 278, 5.5, 3.3, false)
            + "</g>"
            + "<g class=\"hud-layer hud-spin hud-tee-4 hud-lit\" fill=\"#9ff5e2\" stroke=\"none\">"
            + hudTeeSolid(71, 37, 7, 3.5, 1.5, false)
            + hudTeeSolid(69, 126, 6, 3.1, 1.4, true)
            + hudTeeSolid(72, 239, 8, 3.7, 1.6, false)
            + hudTeeSolid(70, 334, 6.5, 3.3, 1.4, true)
            + "</g>"

            + "<g class=\"hud-layer hud-lit\" fill=\"none\" stroke=\"#29d3ae\">"
            + "<circle r=\"13\" stroke-width=\"2.2\" stroke-opacity=\".9\"/>"
            + "</g>"

            + "</g></svg>";
    }

    function createFluxaLoader() {
        var wrap = document.createElement("div");
        var loader = document.createElement("div");
        wrap.className = "fluxa-loader-wrap";
        loader.className = "fluxa-loader fluxa-loader-hud";
        loader.innerHTML = hudMarkup();
        wrap.appendChild(loader);
        return wrap;
    }

    function showTransitionFrame(item) {
        if (elements.stage.querySelector(".stream-hold")) { return; }
        elements.stage.innerHTML = "";
        var source = state.transitionFrame;
        if (!source && item && item.thumbnail_url) {
            source = appUrl(item.thumbnail_url);
        }
        var hold = document.createElement("div");
        var loader = createFluxaLoader();
        hold.className = "stream-hold";
        if (source) {
            var image = document.createElement("img");
            image.alt = "";
            image.src = source;
            hold.appendChild(image);
        }
        hold.appendChild(loader);
        elements.stage.appendChild(hold);
    }

    function showPlayerLoader() {
        if (elements.stage.querySelector(".playback-buffering")) { return; }
        var overlay = document.createElement("div");
        var loader = createFluxaLoader();
        overlay.className = "playback-buffering";
        overlay.appendChild(loader);
        elements.stage.appendChild(overlay);
    }

    function hidePlayerLoader() {
        var overlay = elements.stage.querySelector(".playback-buffering");
        if (overlay) { overlay.remove(); }
    }

    function detachMediaElement() {
        if (!state.player) { return; }
        if ("mediaSession" in navigator) {
            try { navigator.mediaSession.playbackState = "none"; }
            catch (_error) {}
            try { navigator.mediaSession.metadata = null; }
            catch (_error) {}
        }
        state.volume = state.player.volume;
        state.muted = state.player.muted;
        state.suppressPlaybackControls = true;
        pauseMedia(state.player, "detach");
        state.player.removeAttribute("src");
        state.player.load();
        state.player = null;
        state.playerAssignedAt = 0;
        state.suppressPlaybackControls = false;
    }

    function stopActivePlayback(save) {
        if (save) { saveProgress(false); }
        detachMediaElement();
        if (state.playerControlsResizeObserver) {
            state.playerControlsResizeObserver.disconnect();
            state.playerControlsResizeObserver = null;
        }
        if (state.playerGeometryFrame !== null) {
            window.cancelAnimationFrame(state.playerGeometryFrame);
            state.playerGeometryFrame = null;
        }
        if (elements.playerPanel) {
            elements.playerPanel.style.removeProperty("--fluxa-player-bottom-inset");
            elements.playerPanel.style.removeProperty("--fluxa-player-controls-height");
        }
        if (state.hls) {
            state.hls.destroy();
            state.hls = null;
        }
        if (state.compatibilitySession) {
            closeCompatibilitySession(state.compatibilitySession.session_id);
            state.compatibilitySession = null;
        }
        if (state.compatibilityHeartbeat) {
            clearInterval(state.compatibilityHeartbeat);
            state.compatibilityHeartbeat = null;
        }
        state.compatibilityStarting = false;
        state.playerControls = null;
        state.seeking = false;
        state.stallStartedAt = null;
        state.playbackSamplePositionMs = null;
        state.playbackSampleAt = null;
        state.playbackOffsetMs = 0;
    }

    function sendCompatibilityControl(action) {
        if (!state.compatibilitySession) { return; }
        var sessionId = state.compatibilitySession.session_id;
        state.compatibilityControlQueue = state.compatibilityControlQueue
            .catch(function () {})
            .then(function () {
                if (!state.compatibilitySession
                        || state.compatibilitySession.session_id !== sessionId) { return; }
                return fetch(appUrl("/api/compat/" + sessionId + "/control"), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action: action }),
                    keepalive: action === "close"
                });
            })
            .catch(function () {});
    }

    function closeCompatibilitySession(sessionId) {
        fetch(appUrl("/api/compat/" + sessionId + "/control"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "close" }),
            keepalive: true
        }).catch(function () {});
    }

    function startCompatibilityHeartbeat() {
        if (state.compatibilityHeartbeat) { clearInterval(state.compatibilityHeartbeat); }
        state.compatibilityHeartbeat = setInterval(function () {
            sendCompatibilityControl("heartbeat");
        }, 20000);
    }

    function bindRemoteKeys() {
        var lastMediaAction = "";
        var lastMediaActionAt = 0;
        function handleShellMediaAction(action) {
            var now = Date.now();
            if (action === lastMediaAction && now - lastMediaActionAt < 250) { return true; }
            lastMediaAction = action;
            lastMediaActionAt = now;
            if (action === "next-page" || action === "previous-page") {
                if (!isTizenTv || !elements.modal.hidden || elements.pagination.hidden) { return false; }
                changeMediaPage(action === "next-page" ? 1 : -1);
                return true;
            }
            if (action === "chapters") { return toggleChapterPicker(); }
            if (elements.modal.hidden && (action === "play" || action === "toggle")) {
                return playAvailableCollectionFromRemote();
            }
            if (elements.modal.hidden || !state.player) { return false; }
            if (action === "toggle") {
                toggleMediaPlayback(state.player, "tv-shell-toggle");
            } else if (action === "play") { resumeMedia(state.player); }
            else if (action === "pause") { pauseMedia(state.player, "tv-shell-pause"); }
            else if (action === "stop") { closePlayer(); }
            else if (action === "rewind") { seekToOriginalTime(originalPlaybackPosition() - 10000); }
            else if (action === "fast-forward") { seekToOriginalTime(originalPlaybackPosition() + 30000); }
            else if (action === "previous") { playAdjacentQueueItem(-1); }
            else if (action === "next") { playAdjacentQueueItem(1); }
            else { return false; }
            return true;
        }
        window.addEventListener("message", function (event) {
            if (!isTizenTv || window.parent === window || event.source !== window.parent
                    || !event.data || event.data.type !== "fluxa-tv-media-key") { return; }
            handleShellMediaAction(event.data.action);
        });
        document.addEventListener("keydown", function (event) {
            var key = event.key;
            var controlsWereHidden = !elements.modal.hidden
                && elements.playerPanel.classList.contains("controls-hidden");
            var isBackKey = event.keyCode === 10009 || key === "Escape";
            if (!elements.modal.hidden && !isBackKey) { revealPlayerControls(); }
            if (handleTvPageRemoteKey(event)) {
                event.preventDefault();
                return;
            }
            if (handleMediaRemoteKey(event)) {
                event.preventDefault();
                return;
            }
            if (event.keyCode === 10009) {
                event.preventDefault();
                if (!elements.playlistPicker.hidden) {
                    closePlaylistPicker();
                    return;
                }
                if (!elements.modal.hidden) {
                    if (elements.playerDetails.classList.contains("open") && state.playerControls) {
                        commitPendingPlayerSettingRanges();
                        elements.playerDetails.classList.remove("open");
                        state.playerControls.settings.classList.remove("active");
                        state.playerControls.settings.setAttribute("aria-expanded", "false");
                        focusElement(state.playerControls.settings);
                    } else if (state.playerControls && state.playerControls.chapterPopover
                            && !state.playerControls.chapterPopover.hidden) {
                        state.playerControls.chapterPopover.hidden = true;
                        state.playerControls.chapterGrid.classList.remove("active");
                        state.playerControls.chapterGrid.setAttribute("aria-expanded", "false");
                        focusElement(state.playerControls.chapterGrid);
                    } else if (!controlsWereHidden) {
                        hidePlayerControls();
                    } else {
                        closePlayer();
                    }
                } else if (window.tizen && window.tizen.application) {
                    try { window.tizen.application.getCurrentApplication().exit(); }
                    catch (_error) {}
                }
                return;
            }
            var activateKey = key === "Enter" || event.keyCode === 13
                || key === " " || key === "Spacebar" || event.code === "Space";
            if ((isTizenTv || !elements.modal.hidden) && activateKey) {
                if (event.repeat) {
                    event.preventDefault();
                    return;
                }
                if (!elements.modal.hidden && controlsWereHidden) {
                    event.preventDefault();
                    return;
                }
                var selected = document.activeElement;
                if (selected && typeof selected.fluxaActivate === "function") {
                    event.preventDefault();
                    selected.fluxaActivate();
                } else if (selected && (selected.tagName === "BUTTON" || selected.tagName === "A")) {
                    event.preventDefault();
                    selected.click();
                } else if (selected && selected.tagName === "INPUT" && selected.type === "checkbox") {
                    event.preventDefault();
                    selected.click();
                } else if (isTizenTv && selected && selected.tagName === "SELECT") {
                    event.preventDefault();
                    selected.click();
                } else if (!isTizenTv && !elements.modal.hidden && state.player
                        && key !== "Enter" && event.keyCode !== 13
                        && (!selected || !elements.playerPanel.contains(selected)
                            || !/^(INPUT|SELECT|TEXTAREA)$/.test(selected.tagName))) {
                    event.preventDefault();
                    toggleMediaPlayback(state.player, "keyboard-toggle");
                }
                return;
            }
            if (key === "Escape") {
                if (!elements.modal.hidden) {
                    event.preventDefault();
                    if (document.fullscreenElement || document.webkitFullscreenElement) {
                        var exit = document.exitFullscreen || document.webkitExitFullscreen;
                        if (exit) { exit.call(document); }
                    } else {
                        closePlayer();
                    }
                }
                return;
            }
            if (key === "ArrowUp" || key === "ArrowDown" || key === "ArrowLeft" || key === "ArrowRight") {
                var activeControl = document.activeElement;
                if (!isTizenTv && elements.modal.hidden && activeControl
                        && /^(INPUT|SELECT|TEXTAREA)$/.test(activeControl.tagName)) {
                    return;
                }
                if (moveFocus(key)) {
                    event.preventDefault();
                }
            }
        });
    }

    function playAvailableCollectionFromRemote() {
        if (!isTizenTv || !elements.modal.hidden || elements.playlistActions.hidden) {
            return false;
        }
        if (state.fredplayerOnly && state.musicCollection) {
            startMusicCollection(document.activeElement === elements.shufflePlaylist);
            return true;
        }
        if (state.playlist === null) { return false; }
        startActiveCollection(document.activeElement === elements.shufflePlaylist);
        return true;
    }

    function registerTizenRemoteKeys() {
        if (isTizenTv) { document.body.classList.add("tizen-tv"); }
        function reportBrandKeys(eventName, keys, reason) {
            fetch(appUrl("/api/playback-events"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    event: eventName,
                    hls_detail: JSON.stringify(keys || []),
                    hls_reason: reason || "",
                    client_version: clientVersion,
                    tizen_mode: true
                }),
                keepalive: true
            }).catch(function () {});
        }
        if (!window.tizen || !window.tizen.tvinputdevice) {
            reportBrandKeys("tv_brand_key_api_unavailable", [], "tizen.tvinputdevice unavailable");
            return;
        }
        try {
            var supported = Array.prototype.slice.call(
                window.tizen.tvinputdevice.getSupportedKeys() || []
            );
            var branded = supported.filter(function (key) {
                return /netflix|prime|amazon|disney|tvplus|samsungtv/i.test(key.name || "");
            });
            reportBrandKeys(
                branded.length ? "tv_brand_keys_detected" : "tv_brand_keys_not_exposed",
                branded.map(function (key) { return { name: key.name, code: key.code }; }),
                "supported-key-count=" + supported.length
            );
        } catch (error) {
            reportBrandKeys("tv_brand_key_detection_failed", [],
                (error && error.name ? error.name + ": " : "")
                    + (error && error.message ? error.message : String(error)));
        }
        [
            "MediaPlay", "MediaPause", "MediaStop",
            "MediaRewind", "MediaFastForward", "MediaTrackPrevious", "MediaTrackNext",
            "ChannelUp", "ChannelDown", "Guide"
        ].forEach(function (name) {
            try { window.tizen.tvinputdevice.registerKey(name); }
            catch (_error) {}
        });
        // Leave Samsung's launcher Play/Pause key to the TV. It opens the
        // compact native transport popup; the individual actions selected in
        // that popup are registered above and handled by Fluxa.
        try { window.tizen.tvinputdevice.unregisterKey("MediaPlayPause"); }
        catch (_error) {}
    }

    function handleTvPageRemoteKey(event) {
        if (!isTizenTv || !elements.modal.hidden || elements.pagination.hidden) { return false; }
        if (event.keyCode === 427) {
            changeMediaPage(1);
            return true;
        }
        if (event.keyCode === 428) {
            changeMediaPage(-1);
            return true;
        }
        return false;
    }

    function handleMediaRemoteKey(event) {
        var code = event.keyCode;
        if (code === 458) { return toggleChapterPicker(); }
        if (elements.modal.hidden || !state.player) {
            if (code === 10252 || code === 415) { return playAvailableCollectionFromRemote(); }
            return false;
        }
        if (code === 10252) {
            toggleMediaPlayback(state.player, "media-key-toggle");
        } else if (code === 415) {
            resumeMedia(state.player);
        } else if (code === 19) {
            pauseMedia(state.player, "media-key-pause");
        } else if (code === 413) {
            closePlayer();
        } else if (code === 412) {
            seekToOriginalTime(originalPlaybackPosition() - 10000);
        } else if (code === 417) {
            seekToOriginalTime(originalPlaybackPosition() + 30000);
        } else if (code === 10232) {
            playAdjacentQueueItem(-1);
        } else if (code === 10233) {
            playAdjacentQueueItem(1);
        } else {
            return false;
        }
        return true;
    }

    function moveFocus(direction) {
        if (moveChapterFocus(direction)) { return true; }
        if (movePlayerSettingsFocus(direction)) { return true; }
        if (movePlayerControlFocus(direction)) { return true; }
        if (moveGridFocus(direction)) { return true; }
        if (moveSidebarFocus(direction)) { return true; }
        var nodes = document.querySelectorAll("[data-focusable='true']:not([disabled])");
        var focusables = [];
        var i;
        for (i = 0; i < nodes.length; i += 1) {
            if (isVisible(nodes[i]) && !(isTizenTv && elements.main.contains(document.activeElement)
                    && direction !== "ArrowLeft" && elements.sidebar.contains(nodes[i]))) {
                focusables.push(nodes[i]);
            }
        }
        if (!focusables.length) { return false; }
        var current = document.activeElement;
        if (focusables.indexOf(current) === -1) {
            focusables[0].focus();
            return true;
        }
        var best = findDirectionalCandidate(current, focusables, direction, false);
        if (best) { focusElement(best); return true; }
        return false;
    }

    function findDirectionalCandidate(current, candidates, direction, requireCrossAxisOverlap) {
        var source = current.getBoundingClientRect();
        var sourceCenter = centerOf(source);
        var best = null;
        var bestScore = Infinity;
        candidates.forEach(function (candidate) {
            if (candidate === current || !isVisible(candidate)) { return; }
            var target = candidate.getBoundingClientRect();
            var targetCenter = centerOf(target);
            var primary;
            var crossGap;
            var centerDrift;
            if (direction === "ArrowRight" || direction === "ArrowLeft") {
                if (direction === "ArrowRight" && targetCenter.x <= sourceCenter.x + 2) { return; }
                if (direction === "ArrowLeft" && targetCenter.x >= sourceCenter.x - 2) { return; }
                primary = direction === "ArrowRight"
                    ? Math.max(0, target.left - source.right)
                    : Math.max(0, source.left - target.right);
                crossGap = Math.max(0, target.top - source.bottom, source.top - target.bottom);
                centerDrift = Math.abs(targetCenter.y - sourceCenter.y);
            } else {
                if (direction === "ArrowDown" && targetCenter.y <= sourceCenter.y + 2) { return; }
                if (direction === "ArrowUp" && targetCenter.y >= sourceCenter.y - 2) { return; }
                primary = direction === "ArrowDown"
                    ? Math.max(0, target.top - source.bottom)
                    : Math.max(0, source.top - target.bottom);
                crossGap = Math.max(0, target.left - source.right, source.left - target.right);
                centerDrift = Math.abs(targetCenter.x - sourceCenter.x);
            }
            if (requireCrossAxisOverlap && crossGap > 0) { return; }
            var score = primary * 3 + crossGap * 12 + centerDrift * 0.2;
            if (score < bestScore) { bestScore = score; best = candidate; }
        });
        return best;
    }

    function moveChapterFocus(direction) {
        var controls = state.playerControls;
        if (elements.modal.hidden || !controls || !controls.chapterPopover
                || controls.chapterPopover.hidden) { return false; }
        var cards = Array.prototype.slice.call(
            controls.chapterPopover.querySelectorAll(".chapter-card:not([disabled])")
        );
        if (!cards.length) { return false; }
        var index = cards.indexOf(document.activeElement);
        if (index < 0) {
            focusCurrentChapter(controls.chapterPopover);
            return true;
        }
        if (direction === "ArrowLeft" && index > 0) {
            focusChapterCard(cards[index - 1]);
            return true;
        }
        if (direction === "ArrowRight" && index < cards.length - 1) {
            focusChapterCard(cards[index + 1]);
            return true;
        }
        if (direction === "ArrowDown") {
            controls.chapterPopover.hidden = true;
            controls.chapterGrid.classList.remove("active");
            controls.chapterGrid.setAttribute("aria-expanded", "false");
            focusElement(controls.chapterGrid);
            return true;
        }
        return direction === "ArrowLeft" || direction === "ArrowRight";
    }

    function movePlayerSettingsFocus(direction) {
        if (elements.modal.hidden
                || !elements.playerDetails.classList.contains("open")) {
            return false;
        }
        var nodes = elements.playerDetails.querySelectorAll("[data-focusable='true']:not([disabled])");
        var focusables = [];
        var i;
        for (i = 0; i < nodes.length; i += 1) {
            if (isVisible(nodes[i])) { focusables.push(nodes[i]); }
        }
        if (!focusables.length) { return false; }
        var current = document.activeElement;
        var index = focusables.indexOf(current);
        if (index < 0) {
            focusElement(focusables[0]);
            return true;
        }
        if (current.tagName === "INPUT" && current.type === "range"
                && (direction === "ArrowLeft" || direction === "ArrowRight")) {
            adjustRangeWithRemote(current, direction === "ArrowRight" ? 1 : -1);
            return true;
        }
        if (direction === "ArrowDown" && index < focusables.length - 1) {
            commitRemoteSettingRange(current);
            focusElement(focusables[index + 1]);
            return true;
        }
        if (direction === "ArrowUp" && index > 0) {
            commitRemoteSettingRange(current);
            focusElement(focusables[index - 1]);
            return true;
        }
        if (direction === "ArrowLeft" || direction === "ArrowRight") {
            var candidate = findHorizontalSetting(focusables, current, direction);
            if (candidate) { focusElement(candidate); return true; }
        }
        if (direction === "ArrowLeft" && index === 0 && state.playerControls) {
            elements.playerDetails.classList.remove("open");
            state.playerControls.settings.classList.remove("active");
            state.playerControls.settings.setAttribute("aria-expanded", "false");
            focusElement(state.playerControls.settings);
            return true;
        }
        return false;
    }

    function adjustRangeWithRemote(control, direction) {
        var step = Number(control.step) || 1;
        var minimum = Number(control.min);
        var maximum = Number(control.max);
        var value = Math.max(minimum, Math.min(maximum, Number(control.value) + step * direction));
        control.value = String(value);
        control.dispatchEvent(new Event("input", { bubbles: true }));
        if (elements.playerDetails.contains(control)) {
            control.dataset.fluxaRemoteDirty = "true";
        } else {
            control.dispatchEvent(new Event("change", { bubbles: true }));
        }
    }

    function commitRemoteSettingRange(control) {
        if (!control || control.dataset.fluxaRemoteDirty !== "true") { return; }
        delete control.dataset.fluxaRemoteDirty;
        control.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function commitPendingPlayerSettingRanges() {
        var ranges = elements.playerDetails.querySelectorAll(
            "input[type='range'][data-fluxa-remote-dirty='true']"
        );
        Array.prototype.forEach.call(ranges, commitRemoteSettingRange);
    }

    function findHorizontalSetting(focusables, current, direction) {
        var source = centerOf(current.getBoundingClientRect());
        var best = null;
        var bestScore = Infinity;
        focusables.forEach(function (candidate) {
            if (candidate === current) { return; }
            var target = centerOf(candidate.getBoundingClientRect());
            var dx = target.x - source.x;
            var dy = Math.abs(target.y - source.y);
            if ((direction === "ArrowRight" && dx <= 2)
                    || (direction === "ArrowLeft" && dx >= -2) || dy > 50) { return; }
            var score = Math.abs(dx) + dy * 3;
            if (score < bestScore) { bestScore = score; best = candidate; }
        });
        return best;
    }

    function movePlayerControlFocus(direction) {
        if (elements.modal.hidden || !state.playerControls
                || elements.playerDetails.classList.contains("open")
                || (state.playerControls.chapterPopover
                    && !state.playerControls.chapterPopover.hidden)) {
            return false;
        }
        var controls = state.playerControls;
        var nodes = controls.root.querySelectorAll(".player-control-button:not([disabled])");
        var buttons = [];
        var i;
        for (i = 0; i < nodes.length; i += 1) {
            if (isVisible(nodes[i])) { buttons.push(nodes[i]); }
        }
        if (!buttons.length) { return false; }

        var current = document.activeElement;
        var index = buttons.indexOf(current);
        if (current === controls.volume) {
            if (direction === "ArrowLeft" || direction === "ArrowRight") {
                adjustRangeWithRemote(current, direction === "ArrowRight" ? 1 : -1);
                return true;
            }
            if (direction === "ArrowUp") {
                focusElement(controls.seek);
                return true;
            }
            if (direction === "ArrowDown") {
                focusElement(controls.play);
                return true;
            }
        }
        if (current === controls.seek) {
            if (direction === "ArrowLeft" || direction === "ArrowRight") {
                seekToOriginalTime(originalPlaybackPosition()
                    + (direction === "ArrowRight" ? 10000 : -10000));
                return true;
            }
            if (direction === "ArrowDown") {
                focusElement(controls.play);
                return true;
            }
            return false;
        }
        if (index < 0) {
            focusElement(controls.play);
            return true;
        }
        if (direction === "ArrowUp") {
            focusElement(controls.seek);
            return true;
        }
        if (direction === "ArrowDown") {
            focusElement(controls.play);
            return true;
        }
        if (direction === "ArrowLeft" && index > 0) {
            focusElement(buttons[index - 1]);
            return true;
        }
        if (direction === "ArrowRight" && index < buttons.length - 1) {
            focusElement(buttons[index + 1]);
            return true;
        }
        return false;
    }

    function moveGridFocus(direction) {
        if (!isTizenTv || !document.activeElement.classList
                || !document.activeElement.classList.contains("media-card")) {
            return false;
        }
        var nodes = elements.grid.querySelectorAll(".media-card:not([disabled])");
        var cards = Array.prototype.slice.call(nodes).filter(isVisible);
        var current = document.activeElement;
        if (cards.indexOf(current) < 0 || !cards.length) { return false; }
        var horizontal = direction === "ArrowLeft" || direction === "ArrowRight";
        var target = findDirectionalCandidate(current, cards, direction, horizontal);
        if (target) {
            focusElement(target);
            return true;
        }

        if (direction === "ArrowLeft") {
            var sidebarNodes = elements.sidebar.querySelectorAll("[data-focusable='true']:not([disabled])");
            target = closestByVerticalPosition(current, Array.prototype.slice.call(sidebarNodes));
        } else if (direction === "ArrowRight" && elements.grid.classList.contains("list-view")) {
            target = closestByHorizontalPosition(current, collectionHeaderTargets());
        } else if (direction === "ArrowUp") {
            target = closestByHorizontalPosition(current, collectionHeaderTargets());
        } else if (direction === "ArrowDown" && !elements.pagination.hidden) {
            target = !elements.nextPage.disabled ? elements.nextPage : elements.previousPage;
        }
        if (target && isVisible(target) && !target.disabled) { focusElement(target); }
        // Consume edge movement so left/right never wrap into another row or
        // jump to an unrelated control on the far side of the screen.
        return true;
    }

    function collectionHeaderTargets() {
        var selector = ".section-heading-actions [data-focusable='true']:not([disabled]),"
            + "#music-browser-tabs [data-focusable='true']:not([disabled]),"
            + "#folder-nav [data-focusable='true']:not([disabled])";
        return Array.prototype.slice.call(elements.main.querySelectorAll(selector)).filter(isVisible);
    }

    function closestByHorizontalPosition(current, candidates) {
        var sourceX = centerOf(current.getBoundingClientRect()).x;
        var best = null;
        var bestDistance = Infinity;
        candidates.forEach(function (candidate) {
            if (!isVisible(candidate)) { return; }
            var distance = Math.abs(centerOf(candidate.getBoundingClientRect()).x - sourceX);
            if (distance < bestDistance) { bestDistance = distance; best = candidate; }
        });
        return best;
    }

    function moveSidebarFocus(direction) {
        if (!isTizenTv || !elements.sidebar.contains(document.activeElement)) { return false; }
        if (direction === "ArrowLeft") { return true; }
        if (direction !== "ArrowRight") { return false; }
        var cards = Array.prototype.slice.call(
            elements.grid.querySelectorAll(".media-card:not([disabled])")
        );
        var target = closestByVerticalPosition(document.activeElement, cards);
        if (target) { focusElement(target); }
        return true;
    }

    function closestByVerticalPosition(current, candidates) {
        var sourceY = centerOf(current.getBoundingClientRect()).y;
        var best = null;
        var bestDistance = Infinity;
        candidates.forEach(function (candidate) {
            if (!isVisible(candidate)) { return; }
            var distance = Math.abs(centerOf(candidate.getBoundingClientRect()).y - sourceY);
            if (distance < bestDistance) { bestDistance = distance; best = candidate; }
        });
        return best;
    }

    function focusElement(element) {
        element.focus();
        if (!isTizenTv || !element.scrollIntoView) { return; }
        try { element.scrollIntoView({ block: "center", inline: "nearest", behavior: "auto" }); }
        catch (_error) { element.scrollIntoView(false); }
    }

    function centerOf(rect) {
        return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    }

    function isVisible(element) {
        var rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && element.offsetParent !== null;
    }

    function technicalDescription(item, technical) {
        var parts = [];
        if (item.duration_ms) { parts.push(formatDuration(item.duration_ms)); }
        if (technical.width && technical.height) { parts.push(technical.width + "×" + technical.height); }
        if (technical.video_codec) { parts.push(technical.video_codec.toUpperCase()); }
        if (technical.audio_codec) { parts.push(technical.audio_codec.toUpperCase() + " audio"); }
        if (technical.audio_tracks > 1) { parts.push(technical.audio_tracks + " audio tracks"); }
        if (technical.subtitle_tracks) { parts.push(technical.subtitle_tracks + " subtitle tracks"); }
        parts.push(formatBytes(item.size_bytes));
        if (!item.direct_play_likely) { parts.push("browser conversion required"); }
        return parts.join(" · ");
    }

    function cardMetadata(item) {
        var parts = [];
        if (item.available === false) { return "Unavailable in the current libraries"; }
        if (item.media_type === "audio") {
            if (item.artist) { parts.push(item.artist); }
            if (item.album) { parts.push(item.album); }
            return parts.join(" · ") || "FredPlayer";
        }
        if (item.show_title) { parts.push(item.show_title); }
        if (item.duration_ms) { parts.push(formatDuration(item.duration_ms)); }
        if (!parts.length) { parts.push(item.library_name); }
        return parts.join(" · ");
    }

    function makeInitials(title) {
        var words = title.replace(/[^\w\s]/g, " ").trim().split(/\s+/).filter(Boolean);
        if (!words.length) { return "F"; }
        if (words.length === 1) { return words[0].slice(0, 2).toUpperCase(); }
        return (words[0].charAt(0) + words[words.length - 1].charAt(0)).toUpperCase();
    }

    function formatBytes(bytes) {
        var value = Number(bytes) || 0;
        var units = ["bytes", "KB", "MB", "GB", "TB"];
        var unit = 0;
        while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
        return (unit === 0 ? Math.round(value) : value.toFixed(value >= 10 ? 1 : 2)) + " " + units[unit];
    }

    function formatDuration(milliseconds) {
        var totalMinutes = Math.round(milliseconds / 60000);
        var hours = Math.floor(totalMinutes / 60);
        var minutes = totalMinutes % 60;
        if (hours) { return hours + "h " + minutes + "m"; }
        return minutes + "m";
    }

    function formatClock(milliseconds) {
        var totalSeconds = Math.max(0, Math.floor((Number(milliseconds) || 0) / 1000));
        var hours = Math.floor(totalSeconds / 3600);
        var minutes = Math.floor((totalSeconds % 3600) / 60);
        var seconds = totalSeconds % 60;
        if (hours) {
            return hours + ":" + ("0" + minutes).slice(-2)
                + ":" + ("0" + seconds).slice(-2);
        }
        return minutes + ":" + ("0" + seconds).slice(-2);
    }

    function findLibrary(id) {
        var found = null;
        state.libraries.forEach(function (library) {
            if (library.id === id) { found = library; }
        });
        return found;
    }

    function findPlaylist(id) {
        var found = null;
        state.playlists.forEach(function (playlist) {
            if (playlist.id === id) { found = playlist; }
        });
        return found;
    }

    function showToast(message, isError) {
        clearTimeout(state.toastTimer);
        elements.toast.textContent = message;
        elements.toast.classList.toggle("error", Boolean(isError));
        elements.toast.hidden = false;
        state.toastTimer = setTimeout(function () { elements.toast.hidden = true; }, 5200);
    }

    function debounce(callback, delay) {
        var timer;
        return function () {
            clearTimeout(timer);
            timer = setTimeout(callback, delay);
        };
    }

    function appUrl(path) {
        return basePath + path;
    }

    function logout() {
        fetch(appUrl("/api/auth/logout"), { method: "POST" }).then(function () {
            window.location.assign(appUrl("/login"));
        }).catch(function () {
            window.location.assign(appUrl("/login"));
        });
    }
}());
