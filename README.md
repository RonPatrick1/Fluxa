# Fluxa

Fluxa is a household media server for an existing video and audio library. It
is being built around the useful local-library parts of Plex, with predictive
audio leveling as its defining feature. Local clients can connect directly;
remote browser access is protected by HTTPS and a household password.

## What exists now

Fluxa's server is a native C++20 executable backed by SQLite, OpenSSL, and
FFmpeg. The browser and future TV clients use the same JSON and HLS API:

- staged, read-only discovery of configured video and music folders;
- a private SQLite catalog containing paths, technical metadata, analysis, and
  one household-wide playback position;
- a responsive browser library with search and library filters;
- direct audio playback with HTTP byte-range seeking;
- automatic compatibility playback for MKV, MPEG-2, AC-3, DTS, and other
  browser-dependent combinations, converted while watching to H.264/AAC;
- NVIDIA GPU encoding when available, with a software H.264 fallback;
- on-demand FFprobe metadata for duration, codecs, resolution, audio tracks,
  and subtitle counts;
- one low-impact background FFprobe worker that fills metadata for discovered
  items incrementally without blocking library scans;
- on-demand EBU R128 audio analysis that detects sustained loudness jumps and
  creates a predictive gain envelope before the spike begins;
- keyboard/remote-control focus navigation as a base for the Samsung TV client.
- imported Plex playlists with their original names and item ordering, mapped
  to the existing Fluxa catalog.
- playlist Play and Shuffle Play queues, click-to-start at any item, and
  playlist-scoped search;
- proactive episode and chapter preview backfill using low-priority background
  FFmpeg workers, independent of which screens are opened;
- browser Media Source playback through a vendored HLS.js 1.7.0 build, plus
  native HLS delivery for Samsung TVs and other native-HLS clients;
- a full-program player timeline independent of the rolling HLS window, with
  icon-only transport, mute, chapter grid, separate previous/next video and
  previous/next chapter controls, and restartable seeking that preserves the
  playing/paused state;
- per-video and device-wide caption controls: text subtitles become WebVTT,
  while DVD and PGS image subtitles are optionally burned into the stream;
- sample-aspect-aware preview generation and non-cropping artwork display.

Video starts a managed FFmpeg session automatically so live audio leveling is
consistent even when the source would otherwise direct-play. Desktop
browsers receive fragmented-MP4 HLS; native-HLS clients receive MPEG-TS HLS.
The session retains a rolling window of at most 24 listed segments plus a small
deletion margin, pauses FFmpeg when the player pauses, and is removed on close
or after 75 seconds without a heartbeat. It never writes a converted episode
or movie to the library.

At server startup, Fluxa walks the video catalog and queues any missing episode
or chapter previews. Newly scanned and newly probed items join that same queue,
so artwork work continues in the background instead of being deferred until a
user opens a playlist or player. A request can still queue a missing image as a
safety fallback, but the browser never blocks while it is generated.

Every compatibility stream performs live lookahead leveling: FFmpeg buffers
about 7.5 seconds of upcoming audio, never boosts quieter dialogue, eases loud
regions toward the target, and finishes with a peak safety limiter. The full
audio scan is now optional. If requested, its more program-aware, time-aligned
gain map replaces the live calculation on subsequent playback, including after
resuming partway through a video.

## Storage guarantee

Fluxa never imports or duplicates the source media. Library folders are opened
for discovery, probing, analysis, and streaming. The `.fluxa/` data directory
contains only the SQLite catalog, small replaceable preview thumbnails and
text-caption caches, and bounded temporary playback segments under
`.fluxa/streams/`. Those segments are disposable and automatically deleted.
The status API separately exposes
temporary compatibility bytes and `media_copied_bytes`, which is currently and
intentionally zero.

Plex can continue using the same source folders. Fluxa does not read or modify
Plex's metadata after initial read-only discovery of its configured library
locations.

## FredPlayer album artwork

Fluxa can show the album covers already managed by FredPlayer without running
another artwork downloader or storing another server-side copy. Enable the
`fredplayer_artwork` block shown in `config.example.json`; matching audio files
are mapped to FredPlayer by their path beneath `music_dir`, and Fluxa proxies
FredPlayer's cached image from its localhost API. Video preview artwork remains
in Fluxa's own thumbnail cache.

## Plex playlists

Import or refresh every playlist from the local Plex database with:

```bash
cd /var/www/Fluxa && ./build/fluxa-server --config fluxa.json import-plex-playlists
```

The importer opens Plex's database read-only and copies only playlist metadata:
names, ordering, source identifiers, and file-path references. It maps those
references to Fluxa's existing catalog without copying media. Entries whose
files are no longer available are retained in their original positions and
shown as unavailable instead of silently disappearing.

## Local configuration

`fluxa.json` is intentionally ignored by Git because paths belong to this
machine. `config.example.json` documents the format. This workstation is
currently configured with the five local/network folders already used by Plex.

## Run

Build the native server (Ubuntu 24.04):

```sh
sudo apt install build-essential cmake libcpp-httplib-dev nlohmann-json3-dev libsqlite3-dev libssl-dev
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
```

Scan the configured folders in place:

```sh
./build/fluxa-server --config fluxa.json scan
```

Start the server:

```sh
./build/fluxa-server --config fluxa.json serve
```

Open `http://localhost:8097`. Other devices on the LAN can use this machine's
LAN address on port 8097. Direct requests from non-local IP addresses are
rejected; public access goes through the authenticated HTTPS proxy described
below.

Install it as a persistent user service on this workstation:

```sh
./systemd/install.sh
```

Inspect it with `systemctl --user status fluxa.service` and remove it with
`./systemd/uninstall.sh`.

Probe or analyze one catalog item from the command line:

```sh
./build/fluxa-server --config fluxa.json probe MEDIA_ID
./build/fluxa-server --config fluxa.json analyze MEDIA_ID
```

Audio analysis reads the selected audio track end to end. It stores a compact,
one-second gain envelope and spike-region list; it does not write beside or
modify the media file.

## Public access

The public browser URL on this workstation is:

`https://patrick-lamphier.com/fluxa/`

Fluxa requires the household password on that route. The generated initial
password is stored locally in `.fluxa/initial-password.txt` with owner-only
permissions. Set a new password (at least 12 characters) with:

```bash
cd /var/www/Fluxa && ./build/fluxa-server --config fluxa.json auth-set-password
```

Changing the password invalidates all existing login sessions. Public login
attempts are throttled by both Fluxa and Nginx. Session cookies are Secure,
HttpOnly, SameSite Strict, and scoped to `/fluxa` so the site's other apps do
not receive them.

The checked-in Nginx snippets are `deploy/nginx/fluxa-security.conf` and
`deploy/nginx/fluxa-location.conf`. The first is installed in Nginx's `http`
context; the second is included in the existing TLS virtual host. Nginx sends
media byte ranges without buffering so seeking does not create a media copy.

## Playback diagnostics

Browser stalls, buffering, media errors, and HLS failures are recorded in
`.fluxa/logs/playback-events.jsonl`. Each compatibility-stream session also
gets an FFmpeg progress/error log under `.fluxa/logs/transcodes/`. Fluxa keeps
the latest 100 transcode logs and rotates the browser event log at 5 MiB.

For service and HTTP request diagnostics, use:

```sh
journalctl --user -u fluxa -f
```

## Verify

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
./build/fluxa-server --config fluxa.json status
```

The browser tests exercise full-runtime timing and sound, chapter seeking,
device-wide and per-video PGS/DVD caption burn-in, selectable WebVTT captions,
and temporary-session cleanup in both Google Chrome and Firefox.

HLS.js is vendored at `vendor/hls.min.js` under its Apache-2.0 license, retained
in `vendor/HLS-JS-LICENSE.txt`.

## Product scope

Planned local-server functions include richer movies/shows/seasons/episodes
and music organization, metadata and artwork, audio/subtitle selection, and a
Samsung Tizen package spanning Frame generations. Direct play, compatibility
transcoding, predictive audio leveling metadata, household resume state, and
browser clients now have working first versions. Accounts, profiles,
recommendations, Plex's online services, and casting are not first-version
scope.

The unrelated Bose Battery Voice projects were moved to
`/var/www/BoseVoiceStatus`.
