#!/usr/bin/env python3
"""Shelf — a tiny self-hosted music streamer.

Run on the computer that holds your music. Open it in a phone browser
(or Add to Home Screen). No account, no subscription, no telemetry.

    python3 shelf.py --music ~/Music --password 'something-long'

Then reach it from your phone over Tailscale (recommended) or a VPN.
Do not port-forward this to the open internet.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import subprocess

def ensure_deps() -> None:
    """Install spotdl only when it is missing. Never upgrade on a working install."""
    try:
        import spotdl  # noqa: F401
        return
    except Exception:
        pass
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "yt-dlp", "spotdl", "--quiet"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


FOUND_RE = re.compile(r"Found\s+(\d+)\s+songs", re.IGNORECASE)
FAIL_RE = re.compile(r"returned no usable results for (.+?) after \d+ attempts", re.IGNORECASE)
MAX_ATTEMPTS = 3

def spotify_download(url: str, music_root: Path, threads: int = 4, on_progress=None):
    """One pass. Returns (code, done, total, failed, newly_downloaded)."""
    music_root = Path(music_root).expanduser().resolve()
    music_root.mkdir(parents=True, exist_ok=True)
    output_template = str(music_root / "{artists}/{album}/{track-number} - {title}.{output-ext}")
    cmd = [
        sys.executable, "-m", "spotdl", "download", url,
        "--output", output_template,
        "--format", "m4a",
        "--bitrate", "320k",
        "--threads", str(threads),
        "--audio", "youtube-music", "youtube",
        "--simple-tui",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    done = 0
    total = 0
    newly = 0
    failed = []
    if proc.stdout is not None:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            sys.stderr.write(line + "\n")
            match = FOUND_RE.search(line)
            if match:
                total = int(match.group(1))
            elif line.startswith("Downloaded "):
                done += 1
                newly += 1
                if total and done > total:
                    total = done
            elif line.startswith("Skipping "):
                done += 1
                if total and done > total:
                    total = done
            else:
                fail = FAIL_RE.search(line)
                if fail:
                    name = fail.group(1).strip()
                    if name and name not in failed:
                        failed.append(name)
                elif "AudioProviderError" in line or "download error" in line.lower():
                    snippet = line[:160]
                    if snippet not in failed:
                        failed.append(snippet)
            if on_progress:
                on_progress(done, total, failed)
    code = proc.wait()
    if on_progress:
        on_progress(done, total or done, failed)
    return code, done, total or done, failed, newly


def spotify_download_with_retries(url: str, music_root: Path, threads: int = 4, on_progress=None):
    """Download, then re-run the same URL so skipped files stay put and missing ones retry."""
    last = (1, 0, 0, [], 0)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        def wrapped(done, total, failed, attempt=attempt):
            if on_progress:
                on_progress(done, total, failed, attempt)
        code, done, total, failed, newly = spotify_download(
            url, music_root, threads=threads, on_progress=wrapped,
        )
        last = (code, done, total, failed, newly)
        # Nothing left to fetch, or a retry made no new progress.
        if not failed:
            break
        if attempt > 1 and newly == 0:
            break
    return last


AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".webm"}
COVER_NAMES = (
    "cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.png", "album.jpg", "album.png",
)

INDEX: dict = {"tracks": [], "built": 0}
INDEX_LOCK = threading.Lock()
DOWNLOAD_STATE = {"status": "idle", "message": "", "url": "", "done": 0, "total": 0, "failed": [], "attempt": 0}
DOWNLOAD_LOCK = threading.Lock()
PASSWORD = ""
TOKEN = ""
MUSIC_ROOT = Path.home() / "Music"


def token_for(password: str) -> str:
    return hashlib.sha256(f"shelf-v1:{password}".encode()).hexdigest()


def safe_join(root: Path, rel: str) -> Path | None:
    rel = rel.replace("\\", "/").lstrip("/")
    if ".." in Path(rel).parts:
        return None
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def title_from_tags(path: Path) -> str:
    """Read the real track title from embedded tags. Empty if none."""
    try:
        from mutagen import File
        audio = File(path)
        if audio is None:
            return ""
        tags = getattr(audio, "tags", None)
        if tags is not None:
            for key in ("\xa9nam", "TIT2", "title"):
                if key in tags:
                    value = tags[key]
                    text = value[0] if isinstance(value, (list, tuple)) else value
                    text = getattr(text, "text", text)
                    if isinstance(text, (list, tuple)):
                        text = text[0] if text else ""
                    text = str(text).strip()
                    if text and "{" not in text:
                        return text
        info_title = getattr(getattr(audio, "info", None), "title", None)
        if info_title and "{" not in str(info_title):
            return str(info_title).strip()
    except Exception:
        return ""
    return ""


def title_from_name(name: str) -> str:
    stem = Path(name).stem
    stem = stem.replace("_", " ")
    # "Artist - Album - 01-02 Song" or "01-02 Song" or "01. Song"
    parts = [p.strip() for p in stem.split(" - ")]
    if len(parts) >= 2:
        tail = parts[-1]
        cleaned = tail
        for sep in (" ", ". ", "-"):
            head, _, rest = cleaned.partition(sep)
            compact = head.replace("-", "")
            if compact.isdigit() and rest.strip():
                cleaned = rest.strip()
                break
        if cleaned:
            return cleaned
    for sep in (" - ", ". ", " "):
        if sep in stem:
            head, tail = stem.split(sep, 1)
            if head.strip().isdigit():
                return tail.strip() or stem
    return stem


def scan(root: Path) -> dict:
    tracks = []
    root = root.resolve()
    if not root.exists():
        return {"tracks": [], "built": time.time(), "root": str(root)}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        folder = Path(dirpath)
        rel_dir = folder.relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        cover = ""
        lower = {f.lower(): f for f in filenames}
        for candidate in COVER_NAMES:
            if candidate in lower:
                cover = (Path(rel_dir) / lower[candidate]).as_posix() if rel_dir else lower[candidate]
                break
        audio = sorted(
            f for f in filenames
            if Path(f).suffix.lower() in AUDIO_EXTS and not f.startswith(".")
        )
        # Downloaded files keep art inside the audio. Point at the track so /cover can extract it.
        if not cover and audio:
            cover = (Path(rel_dir) / audio[0]).as_posix() if rel_dir else audio[0]
        parts = [p for p in rel_dir.split("/") if p] if rel_dir else []
        artist = parts[0] if parts else "Unknown artist"
        album = parts[1] if len(parts) > 1 else (parts[0] if parts else "Singles")
        for i, filename in enumerate(audio, start=1):
            rel = (Path(rel_dir) / filename).as_posix() if rel_dir else filename
            title = title_from_name(filename)
            if not title or "{" in title or title.lower() in {"track name", "track-name", "title"}:
                tagged = title_from_tags(folder / filename)
                if tagged:
                    title = tagged
            # No folder cover: use this track's own embedded art, not a sibling's.
            track_cover = cover or rel
            tracks.append({
                "id": hashlib.sha1(rel.encode()).hexdigest()[:12],
                "rel": rel,
                "title": title,
                "artist": artist,
                "album": album,
                "track": i,
                "cover": track_cover,
                "ext": Path(filename).suffix.lower().lstrip("."),
            })
    tracks.sort(key=lambda t: (t["artist"].lower(), t["album"].lower(), t["track"], t["title"].lower()))
    return {"tracks": tracks, "built": time.time(), "root": str(root), "count": len(tracks)}


def rebuild():
    data = scan(MUSIC_ROOT)
    with INDEX_LOCK:
        INDEX.clear()
        INDEX.update(data)
    return data


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#12110f">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Shelf">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" href="/icon-192.png">
<title>Shelf</title>
<style>
  :root {
    --bg: #12110f;
    --bg2: #1c1a17;
    --card: #24211c;
    --line: #34302a;
    --text: #f4efe6;
    --muted: #a3988b;
    --accent: #e7a15a;
    --accent2: #f0d2a8;
    --danger: #e07a6a;
    --safe-b: env(safe-area-inset-bottom);
    --bar: 84px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text);
    font: 17px/1.35 ui-sans-serif, system-ui, sans-serif; overflow-x: hidden; }
  button, input { font: inherit; color: inherit; }
  button { background: none; border: 0; padding: 0; cursor: pointer; }
  #app { min-height: 100%; padding-bottom: calc(var(--bar) + var(--safe-b) + 28px); max-width: 720px; margin: 0 auto; }
  header {
    position: sticky; top: 0; z-index: 5;
    background: color-mix(in srgb, var(--bg) 92%, transparent);
    backdrop-filter: blur(10px);
    padding: 12px 14px 10px;
    border-bottom: 1px solid transparent;
  }
  header.scrolled { border-bottom-color: var(--line); }
  .brand { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
  h1 { font-size: 26px; font-weight: 650; margin: 0; letter-spacing: -0.03em; }
  h1 span { color: var(--accent); }
  .count { color: var(--muted); font-size: 13px; }
  .search { margin-top: 10px; display: flex; gap: 8px; }
  .search input {
    flex: 1; min-width: 0; background: var(--bg2); border: 1px solid var(--line); border-radius: 12px;
    padding: 14px 14px; outline: none; font-size: 16px;
  }
  .search input:focus { border-color: var(--accent); }
  .tabs { display: flex; gap: 6px; margin-top: 10px; overflow-x: auto; }
  .tabs button {
    font-size: 14px; letter-spacing: 0.03em; text-transform: uppercase;
    padding: 10px 14px; border-radius: 999px; color: var(--muted); border: 1px solid transparent;
    flex: none;
  }
  .tabs button.on { color: var(--bg); background: var(--accent2); }
  main { padding: 4px 8px 20px; }
  .row {
    display: flex; gap: 12px; align-items: center; width: 100%;
    padding: 10px 8px; border-radius: 14px; text-align: left;
  }
  .row:active { background: var(--card); }
  .art {
    width: 56px; height: 56px; border-radius: 10px; background: var(--card);
    flex: none; object-fit: cover; display: grid; place-items: center;
    color: var(--accent); font-size: 18px; border: 1px solid var(--line);
  }
  .meta { min-width: 0; flex: 1; }
  .meta b { display: block; font-weight: 650; font-size: 16px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .meta small { color: var(--muted); font-size: 13px;
    display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
  .section { margin: 16px 8px 8px; color: var(--muted); font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase; }
  #player {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 10;
    background: #1a1815; border-top: 1px solid var(--line);
    padding: 10px 12px calc(10px + var(--safe-b));
    display: grid; grid-template-columns: 48px minmax(0, 1fr) auto; gap: 10px; align-items: center;
    min-height: var(--bar);
  }
  #player .art { width: 48px; height: 48px; }
  #player b { font-size: 15px; }
  .controls { display: flex; align-items: center; gap: 0; }
  .controls button { width: 44px; height: 44px; border-radius: 50%; font-size: 18px; }
  .controls button.play {
    background: var(--accent); color: #1a120b; width: 52px; height: 52px; font-size: 22px;
  }
  #seek {
    position: absolute; left: 0; right: 0; top: -3px; height: 3px; width: 100%;
    appearance: none; background: transparent; margin: 0;
  }
  #seek::-webkit-slider-runnable-track { height: 3px; background: var(--line); }
  #seek::-webkit-slider-thumb {
    appearance: none; width: 12px; height: 12px; border-radius: 50%;
    background: var(--accent); margin-top: -4.5px;
  }
  #now { position: fixed; inset: 0; background: var(--bg); z-index: 20; display: none;
    padding: 20px 20px calc(24px + var(--safe-b)); flex-direction: column; }
  #now.open { display: flex; }
  #now .big {
    width: min(78vw, 360px); height: min(78vw, 360px); margin: 24px auto 20px;
    border-radius: 16px; object-fit: cover; background: var(--card); border: 1px solid var(--line);
  }
  #now h2 { margin: 0; font-size: 26px; letter-spacing: -0.03em; }
  #now p { margin: 6px 0 0; color: var(--muted); }
  .transport { display: flex; justify-content: center; align-items: center; gap: 18px; margin-top: 22px; }
  .transport button { font-size: 26px; width: 56px; height: 56px; }
  .transport .play { background: var(--accent); color: #1a120b; border-radius: 50%; font-size: 28px; width: 72px; height: 72px; }
  .fine { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px;
    font-family: ui-sans-serif, system-ui, sans-serif; margin-top: 8px; }
  #login {
    min-height: 100%; display: grid; place-items: center; padding: 24px;
  }
  #login form { width: min(420px, 100%); }
  #login input, #login button[type=submit] {
    width: 100%; margin-top: 10px; padding: 14px; border-radius: 12px; border: 1px solid var(--line);
    background: var(--bg2);
  }
  #login button[type=submit] { background: var(--accent); color: #1a120b; border: 0; font-weight: 600; }
  .empty { color: var(--muted); padding: 32px 12px; text-align: center; }
  .hidden { display: none !important; }
  #pull {
    position: fixed; top: 0; left: 0; right: 0; z-index: 30;
    height: 0; overflow: hidden;
    display: flex; align-items: flex-end; justify-content: center;
    padding-bottom: 8px;
    color: var(--accent); font-size: 13px; letter-spacing: 0.04em;
    background: color-mix(in srgb, var(--bg) 92%, transparent);
    pointer-events: none;
    transition: height .15s ease;
  }
</style>
</head>
<body>
<div id="login" class="hidden">
  <form id="login-form">
    <h1>Shelf<span>.</span></h1>
    <p style="color:var(--muted)">This library is on your computer. Enter the password you set when you started Shelf.</p>
    <input id="pw" type="password" placeholder="Password" autocomplete="current-password" required>
    <button type="submit">Open library</button>
    <p id="login-err" style="color:var(--danger)"></p>
  </form>
</div>
<div id="pull"></div>
<div id="app" class="hidden">
  <header id="top">
    <div class="brand">
      <h1>Shelf<span>.</span></h1>
      <div class="count" id="count"></div>
    </div>
    <div class="search">
      <input id="q" placeholder="Search songs, albums, artists" autocomplete="off">
    </div>
    <div class="tabs">
      <button data-view="songs" class="on">Songs</button>
      <button data-view="albums">Albums</button>
      <button data-view="artists">Artists</button>
      <button data-view="queue">Queue</button>
      <button data-view="download">Downloader</button>
    </div>
    <div id="ride" style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
      <button id="save-ride" style="font-family:ui-sans-serif,system-ui,sans-serif;font-size:13px;padding:8px 12px;border-radius:999px;background:var(--accent);color:#1a120b">Save queue offline</button>
      <button id="install" class="hidden" style="font-family:ui-sans-serif,system-ui,sans-serif;font-size:13px;padding:8px 12px;border-radius:999px;border:1px solid var(--line)">Install app</button>
    </div>
    <p id="ride-status" style="color:var(--muted);font-size:13px;margin:8px 0 0"></p>
  </header>
  <main id="list"></main>
</div>
<div id="player" class="hidden">
  <input id="seek" type="range" min="0" max="1000" value="0">
  <img class="art" id="bar-art" alt="">
  <div class="meta" id="open-now">
    <b id="bar-title">Nothing playing</b>
    <small id="bar-sub"></small>
  </div>
  <div class="controls">
    <button id="prev" aria-label="Previous">⏮</button>
    <button class="play" id="play" aria-label="Play">▶</button>
    <button id="next" aria-label="Next">⏭</button>
  </div>
</div>
<div id="now">
  <button id="close-now" style="align-self:flex-start;color:var(--muted)">Close</button>
  <img class="big" id="now-art" alt="">
  <h2 id="now-title"></h2>
  <p id="now-sub"></p>
  <input id="seek2" type="range" min="0" max="1000" value="0" style="width:100%;margin-top:18px">
  <div class="fine"><span id="cur">0:00</span><span id="dur">0:00</span></div>
  <div class="transport">
    <button id="shuffle" aria-label="Shuffle">⇄</button>
    <button id="prev2">⏮</button>
    <button class="play" id="play2">▶</button>
    <button id="next2">⏭</button>
    <button id="repeat" aria-label="Repeat">↻</button>
  </div>
</div>
<audio id="audio" preload="auto" playsinline></audio>
<script>
const $ = (s) => document.querySelector(s);
const audio = $("#audio");
const state = {
  token: localStorage.getItem("shelf-token") || "",
  tracks: [],
  view: "songs",
  filter: "",
  album: null,
  artist: null,
  queue: [],
  index: -1,
  shuffle: false,
  repeat: "off",
  plain: [],
  objectUrl: "",
};
function headers() { return { "Authorization": "Bearer " + state.token }; }
async function api(path) {
  const res = await fetch(path, { headers: headers() });
  if (res.status === 401) { showLogin(); throw new Error("auth"); }
  return res.json();
}
function showLogin() {
  $("#login").classList.remove("hidden");
  $("#app").classList.add("hidden");
  $("#player").classList.add("hidden");
}
function showApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#player").classList.remove("hidden");
}
$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  const pw = $("#pw").value;
  const res = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pw }) });
  if (!res.ok) { $("#login-err").textContent = "Wrong password."; return; }
  const data = await res.json();
  state.token = data.token;
  localStorage.setItem("shelf-token", state.token);
  boot();
};
function coverUrl(rel) {
  if (!rel) return "";
  return "/cover?path=" + encodeURIComponent(rel) + "&t=" + state.token;
}
function streamUrl(rel) {
  return "/stream?path=" + encodeURIComponent(rel) + "&t=" + state.token;
}
function fmt(sec) {
  if (!isFinite(sec)) return "0:00";
  sec = Math.max(0, Math.floor(sec));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ":" + String(s).padStart(2, "0");
}
function artFail(img, mark) {
  const tile = document.createElement("div");
  tile.className = "art";
  tile.textContent = mark || "♪";
  img.replaceWith(tile);
}
function artEl(cover, letter) {
  const mark = String(letter || "♪").slice(0, 1).toUpperCase().replace(/["'<>&]/g, "");
  if (!cover) return `<div class="art">${mark}</div>`;
  return `<img class="art" loading="lazy" src="${coverUrl(cover)}" alt="" onerror="artFail(this,'${mark}')">`;
}
function render() {
  const q = state.filter.trim().toLowerCase();
  const list = $("#list");
  let html = "";
  const tracks = state.tracks.filter(t => !q || (t.title + " " + t.artist + " " + t.album).toLowerCase().includes(q));
  if (state.view === "download") {
    list.innerHTML = `
      <div style="padding:20px 12px">
        <h2 style="margin:0 0 12px;font-size:22px">Downloader</h2>
        <p style="color:var(--muted);margin:0 0 16px">Paste a playlist, album, or track URL. Files are saved into the Music folder in Artist/Album layout and the library rescans when done.</p>
        <input id="spotify-url" placeholder="https://open.spotify.com/..." style="width:100%;background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:14px;font-size:16px;margin-bottom:12px">
        <button id="dl-btn" style="width:100%;padding:14px;border-radius:12px;background:var(--accent);color:#1a120b;font-weight:600;font-size:16px">Start download</button>
        <div id="dl-bar-wrap" style="display:none;margin-top:16px">
          <div style="height:10px;background:var(--line);border-radius:999px;overflow:hidden">
            <div id="dl-bar" style="height:100%;width:0%;background:var(--accent);transition:width .25s ease"></div>
          </div>
          <p id="dl-count" style="color:var(--text);margin:8px 0 0;font-size:15px"></p>
        </div>
        <p id="dl-status" style="color:var(--muted);margin:12px 0 0;min-height:1.2em"></p>
        <p id="dl-failed" style="color:var(--danger);margin:8px 0 0;font-size:13px;white-space:pre-wrap"></p>
        <div style="margin-top:24px;color:var(--muted);font-size:14px">
          <strong>Requirements</strong><br>
          • <code>spotdl</code> and <code>yt-dlp</code> installed (<code>py -m pip install spotdl yt-dlp</code>)<br>
          • FFmpeg on PATH<br>
          • Output: Music/Artist/Album/01 - Title.m4a
        </div>
      </div>`;
    bindDownload();
    return;
  }
  if (state.view === "queue") {
    if (!state.queue.length) html = `<div class="empty">Queue is empty. Play a song to start one.</div>`;
    else html = state.queue.map((t, i) => row(t, i === state.index, () => playQueue(i))).join("");
    list.innerHTML = html;
    bindRows();
    return;
  }
  if (state.view === "songs") {
    html = tracks.map(t => row(t, false, null)).join("") || `<div class="empty">No matches.</div>`;
  } else if (state.view === "albums") {
    if (state.album) {
      const subset = tracks.filter(t => t.artist === state.album.artist && t.album === state.album.album);
      html = `<div class="section">${esc(state.album.artist)} · ${esc(state.album.album)} · <button id="back" style="color:var(--accent)">Back</button></div>` + subset.map(t => row(t)).join("");
    } else {
      const map = new Map();
      for (const t of tracks) {
        const key = t.artist + "\0" + t.album;
        if (!map.has(key)) map.set(key, t);
      }
      html = [...map.values()].map(t => `
        <button class="row" data-album="${esc(t.album)}" data-artist="${esc(t.artist)}">
          ${artEl(t.cover, t.album)}
          <div class="meta"><b>${esc(t.album)}</b><small>${esc(t.artist)}</small></div>
        </button>`).join("") || `<div class="empty">No albums.</div>`;
    }
  } else if (state.view === "artists") {
    if (state.artist) {
      const subset = tracks.filter(t => t.artist === state.artist);
      html = `<div class="section">${esc(state.artist)} · <button id="back" style="color:var(--accent)">Back</button></div>` + subset.map(t => row(t)).join("");
    } else {
      const map = new Map();
      for (const t of tracks) if (!map.has(t.artist)) map.set(t.artist, t);
      html = [...map.values()].map(t => `
        <button class="row" data-artist-only="${esc(t.artist)}">
          ${artEl(t.cover, t.artist)}
          <div class="meta"><b>${esc(t.artist)}</b><small>Artist</small></div>
        </button>`).join("") || `<div class="empty">No artists.</div>`;
    }
  }
  list.innerHTML = html;
  bindRows();
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => {
    if (c === "&") return "&" + "amp;";
    if (c === "<") return "&" + "lt;";
    if (c === ">") return "&" + "gt;";
    if (c === '"') return "&" + "quot;";
    return "&" + "#39;";
  });
}
function row(t, on) {
  return `<button class="row" data-id="${t.id}" style="${on ? 'background:var(--card)' : ''}">
    ${artEl(t.cover, t.title)}
    <div class="meta"><b>${esc(t.title)}</b><small>${esc(t.artist)} · ${esc(t.album)}</small></div>
  </button>`;
}
function bindRows() {
  document.querySelectorAll("[data-id]").forEach(el => el.onclick = () => {
    const id = el.dataset.id;
    const t = state.tracks.find(x => x.id === id);
    if (!t) return;
    if (state.view === "queue") {
      playQueue(state.queue.findIndex(x => x.id === id));
      return;
    }
    const visible = [...document.querySelectorAll("[data-id]")].map(n => state.tracks.find(x => x.id === n.dataset.id)).filter(Boolean);
    queueFrom(visible, visible.findIndex(x => x.id === id));
  });
  document.querySelectorAll("[data-album]").forEach(el => el.onclick = () => {
    state.album = { album: el.dataset.album, artist: el.dataset.artist };
    render();
  });
  document.querySelectorAll("[data-artist-only]").forEach(el => el.onclick = () => {
    state.artist = el.dataset.artistOnly;
    render();
  });
  const back = $("#back");
  if (back) back.onclick = () => { state.album = null; state.artist = null; render(); };
}
function queueFrom(list, startIndex) {
  state.plain = list.slice();
  let q = list.slice();
  let i = startIndex < 0 ? 0 : startIndex;
  if (state.shuffle && q.length) {
    const cur = q[Math.min(i, q.length - 1)];
    const rest = q.filter(t => t !== cur);
    for (let n = rest.length - 1; n > 0; n--) {
      const j = Math.floor(Math.random() * (n + 1));
      [rest[n], rest[j]] = [rest[j], rest[n]];
    }
    q = cur ? [cur, ...rest] : rest;
    i = 0;
  }
  state.queue = q;
  playQueue(Math.min(i, q.length - 1));
}
function playQueue(i) {
  if (i < 0 || i >= state.queue.length) return;
  state.index = i;
  const t = state.queue[i];
  startTrack(t);
}
let playGen = 0;
async function startTrack(t) {
  const gen = ++playGen;
  const saved = await cachedBlob(t.id);
  if (gen !== playGen) return;
  if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
  state.objectUrl = "";
  if (saved && saved.blob) {
    state.objectUrl = URL.createObjectURL(saved.blob);
    audio.src = state.objectUrl;
  } else {
    audio.src = streamUrl(t.rel);
  }
  const played = audio.play();
  if (played && played.catch) played.catch(() => {
    setTimeout(() => { if (gen === playGen) audio.play().catch(() => {}); }, 400);
  });
  paintNow();
}
function paintNow() {
  const t = state.queue[state.index];
  if (!t) return;
  $("#bar-title").textContent = t.title;
  $("#bar-sub").textContent = t.artist + " · " + t.album;
  $("#now-title").textContent = t.title;
  $("#now-sub").textContent = t.artist + " · " + t.album;
  const art = t.cover ? coverUrl(t.cover) : "";
  $("#bar-art").src = art;
  $("#now-art").src = art;
  setMediaSession(t);
  tellNative(!audio.paused);
  try { localStorage.setItem("shelf-queue", JSON.stringify({ids: state.queue.map(t => t.id), index: state.index})); } catch (e) {}
  if (state.view === "queue") render();
}
function tellNative(playing) {
  try {
    if (!window.Android) return;
    const t = state.queue[state.index] || {};
    if (Android.setTrack) Android.setTrack(t.title || "Shelf", t.artist || "", t.album || "", !!playing);
    else if (Android.setPlaying) Android.setPlaying(!!playing);
  } catch (e) {}
}
window.shelfNative = function (cmd) {
  if (cmd === "play") audio.play();
  else if (cmd === "pause") audio.pause();
  else if (cmd === "next") next();
  else if (cmd === "prev") prev();
};
function updatePosition() {
  if (!("mediaSession" in navigator) || !navigator.mediaSession.setPositionState) return;
  if (!audio.duration || !isFinite(audio.duration)) return;
  try {
    navigator.mediaSession.setPositionState({
      duration: audio.duration,
      playbackRate: audio.playbackRate || 1,
      position: Math.min(audio.currentTime || 0, audio.duration),
    });
  } catch (e) {}
}
function setMediaSession(t) {
  if (!("mediaSession" in navigator)) return;
  const artwork = t.cover
    ? [{src: new URL(coverUrl(t.cover), location.origin).href, sizes: "512x512", type: "image/jpeg"}]
    : [{src: new URL("/icon-512.png", location.origin).href, sizes: "512x512", type: "image/png"}];
  navigator.mediaSession.metadata = new MediaMetadata({title: t.title, artist: t.artist, album: t.album, artwork});
  navigator.mediaSession.playbackState = audio.paused ? "paused" : "playing";
  const handlers = {
    play: () => audio.play(),
    pause: () => audio.pause(),
    previoustrack: () => prev(),
    nexttrack: () => next(),
    seekbackward: (d) => { audio.currentTime = Math.max(0, audio.currentTime - ((d && d.seekOffset) || 10)); updatePosition(); },
    seekforward: (d) => { audio.currentTime = Math.min(audio.duration || 1e9, audio.currentTime + ((d && d.seekOffset) || 10)); updatePosition(); },
    seekto: (d) => { if (d && typeof d.seekTime === "number") { audio.currentTime = d.seekTime; updatePosition(); } },
    stop: () => audio.pause(),
  };
  for (const [name, fn] of Object.entries(handlers)) {
    try { navigator.mediaSession.setActionHandler(name, fn); } catch (e) {}
  }
  updatePosition();
}
let dbPromise = null;
function idb() {
  if (!dbPromise) {
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open("shelf-offline", 1);
      req.onupgradeneeded = () => req.result.createObjectStore("tracks");
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => { dbPromise = null; reject(req.error); };
    });
  }
  return dbPromise;
}
async function cachedBlob(id) {
  try {
    const db = await idb();
    return await new Promise((resolve) => {
      const req = db.transaction("tracks").objectStore("tracks").get(id);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => resolve(null);
    });
  } catch (e) { return null; }
}
function next() {
  if (!state.queue.length) return;
  if (state.repeat === "one") { audio.currentTime = 0; audio.play(); return; }
  let i = state.index + 1;
  if (i >= state.queue.length) {
    if (state.repeat === "all") i = 0;
    else return;
  }
  playQueue(i);
}
function prev() {
  if (audio.currentTime > 3) { audio.currentTime = 0; return; }
  playQueue(Math.max(0, state.index - 1));
}
$("#play").onclick = $("#play2").onclick = () => {
  if (!audio.src) { if (state.queue.length) playQueue(0); return; }
  audio.paused ? audio.play() : audio.pause();
};
$("#next").onclick = $("#next2").onclick = next;
$("#prev").onclick = $("#prev2").onclick = prev;
$("#shuffle").onclick = () => {
  state.shuffle = !state.shuffle;
  $("#shuffle").style.color = state.shuffle ? "var(--accent)" : "";
  const cur = state.queue[state.index];
  if (state.shuffle && state.queue.length) {
    if (!state.plain || !state.plain.length) state.plain = state.queue.slice();
    const rest = state.plain.filter(t => !cur || t.id !== cur.id);
    for (let n = rest.length - 1; n > 0; n--) {
      const j = Math.floor(Math.random() * (n + 1));
      [rest[n], rest[j]] = [rest[j], rest[n]];
    }
    state.queue = cur ? [cur, ...rest] : rest;
    state.index = 0;
  } else if (state.plain && state.plain.length) {
    state.queue = state.plain.slice();
    state.index = Math.max(0, cur ? state.queue.findIndex(t => t.id === cur.id) : 0);
  }
  if (state.view === "queue") render();
};
$("#repeat").onclick = () => {
  state.repeat = state.repeat === "off" ? "all" : state.repeat === "all" ? "one" : "off";
  $("#repeat").style.color = state.repeat === "off" ? "" : "var(--accent)";
  $("#repeat").textContent = state.repeat === "one" ? "1" : "↻";
};
let stopTimer = 0;
let errorSkips = 0;
audio.onplay = () => {
  $("#play").textContent = $("#play2").textContent = "⏸";
  if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "playing";
  clearTimeout(stopTimer);
  errorSkips = 0;
  tellNative(true);
  updatePosition();
};
audio.onpause = () => {
  $("#play").textContent = $("#play2").textContent = "▶";
  // Changing songs pauses the element for a moment. Don't drop the
  // background service until the pause is actually sticking.
  clearTimeout(stopTimer);
  stopTimer = setTimeout(() => {
    if (!audio.paused) return;
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "paused";
    tellNative(false);
  }, 1500);
};
audio.onended = () => { next(); };
audio.onerror = () => {
  if (!audio.error || audio.error.code === 1) return;
  if (errorSkips >= 3 || !state.queue.length) return;
  errorSkips++;
  next();
};
audio.addEventListener("timeupdate", updatePosition);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden" && !audio.paused) tellNative(true);
});
audio.ontimeupdate = () => {
  if (!audio.duration) return;
  const v = Math.floor(audio.currentTime / audio.duration * 1000);
  if (!seeking) { $("#seek").value = v; $("#seek2").value = v; }
  $("#cur").textContent = fmt(audio.currentTime);
  $("#dur").textContent = fmt(audio.duration);
};
let seeking = false;
function bindSeek(el) {
  el.onpointerdown = () => seeking = true;
  el.onchange = () => {
    if (audio.duration) audio.currentTime = (el.value / 1000) * audio.duration;
    seeking = false;
  };
}
bindSeek($("#seek")); bindSeek($("#seek2"));
$("#open-now").onclick = () => $("#now").classList.add("open");
$("#close-now").onclick = () => $("#now").classList.remove("open");
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach(x => x.classList.remove("on"));
  b.classList.add("on");
  state.view = b.dataset.view;
  state.album = null; state.artist = null;
  render();
});
$("#q").oninput = () => { state.filter = $("#q").value; state.album = null; state.artist = null; render(); };
function paintDownload(s) {
  const el = document.querySelector("#dl-status");
  const wrap = document.querySelector("#dl-bar-wrap");
  const bar = document.querySelector("#dl-bar");
  const count = document.querySelector("#dl-count");
  const failedEl = document.querySelector("#dl-failed");
  if (!el) return;
  const done = s.done || 0;
  const total = s.total || 0;
  const failed = s.failed || [];
  if (failedEl) failedEl.textContent = failed.length ? failed.join("\n") : "";
  if (s.status === "running") {
    if (wrap) wrap.style.display = "block";
    if (total > 0) {
      const pct = Math.min(100, Math.round(done / total * 100));
      if (bar) bar.style.width = pct + "%";
      if (count) count.textContent = done + " / " + total + " downloaded";
      el.textContent = s.message || "Downloading";
    } else {
      if (bar) bar.style.width = "8%";
      if (count) count.textContent = "Looking up tracks…";
      el.textContent = s.message || "";
    }
  } else if (s.status === "done") {
    if (wrap) wrap.style.display = "block";
    const denom = total || done;
    const pct = denom ? Math.min(100, Math.round(done / denom * 100)) : 100;
    if (bar) bar.style.width = pct + "%";
    if (count) count.textContent = done + " / " + denom + " downloaded";
    el.textContent = s.message || "Done.";
  } else if (s.status === "error") {
    if (wrap) wrap.style.display = "none";
    el.textContent = s.message || "Download failed.";
  } else {
    el.textContent = s.message || "";
  }
}
async function pollDownload() {
  if (state.view !== "download") return;
  try {
    const res = await fetch("/api/download", { headers: headers() });
    if (!res.ok) return;
    const s = await res.json();
    paintDownload(s);
    if (s.status === "running") {
      setTimeout(pollDownload, 1000);
    } else if (s.status === "done") {
      const data = await api("/api/library");
      state.tracks = data.tracks;
      $("#count").textContent = data.count + " tracks";
    }
  } catch (e) {}
}
function bindDownload() {
  const btn = document.querySelector("#dl-btn");
  if (!btn) return;
  btn.onclick = async () => {
    const url = document.querySelector("#spotify-url").value.trim();
    if (!url) return;
    document.querySelector("#dl-status").textContent = "Starting download…";
    try {
      const res = await fetch("/api/download", {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      if (res.status === 409) {
        document.querySelector("#dl-status").textContent = "A download is already running.";
        return;
      }
      if (res.ok) {
        document.querySelector("#dl-status").textContent = "";
        const wrap = document.querySelector("#dl-bar-wrap");
        if (wrap) wrap.style.display = "block";
        const count = document.querySelector("#dl-count");
        if (count) count.textContent = "Looking up tracks…";
        document.querySelector("#spotify-url").value = "";
        setTimeout(pollDownload, 1500);
      } else {
        document.querySelector("#dl-status").textContent = "Could not start download.";
      }
    } catch (e) {
      document.querySelector("#dl-status").textContent = "Error: " + e;
    }
  };
  pollDownload();
}
$("#save-ride").onclick = async () => {
  const items = state.queue;
  if (!items.length) { $("#ride-status").textContent = "Play something first. This saves the current queue, not the whole library."; return; }
  const db = await idb();
  let n = 0;
  for (const t of items) {
    $("#ride-status").textContent = "Saving " + (n + 1) + " / " + items.length + " — " + t.title;
    const res = await fetch(streamUrl(t.rel));
    if (!res.ok) continue;
    const blob = await res.blob();
    await new Promise((resolve, reject) => {
      const tx = db.transaction("tracks", "readwrite");
      tx.objectStore("tracks").put({blob, meta: t, saved: Date.now()}, t.id);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
    n++;
  }
  $("#ride-status").textContent = n + " tracks saved on this phone. They keep playing with the screen off, even if the computer drops out.";
};
let deferredInstall = null;
addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredInstall = e;
  $("#install").classList.remove("hidden");
});
$("#install").onclick = async () => {
  if (!deferredInstall) return;
  deferredInstall.prompt();
  await deferredInstall.userChoice;
  deferredInstall = null;
  $("#install").classList.add("hidden");
};
addEventListener("scroll", () => $("#top").classList.toggle("scrolled", scrollY > 4));
const pullEl = $("#pull");
let pullStartY = 0, pulling = false, pullDist = 0, refreshing = false;
const PULL_AT = 90;
function setPull(h, label) {
  pullEl.style.height = h + "px";
  pullEl.textContent = label || (h >= PULL_AT ? "Release to refresh" : "Pull to refresh");
  if (h <= 0) pullEl.textContent = "";
}
function atTop() {
  const scrolling = document.scrollingElement || document.documentElement;
  const y = window.scrollY || scrolling.scrollTop || document.body.scrollTop || 0;
  return y <= 0;
}
addEventListener("touchstart", (e) => {
  if (refreshing || !atTop()) return;
  if ($("#now").classList.contains("open")) return;
  const target = e.target;
  if (target && target.closest && target.closest("#player, #now")) return;
  pullStartY = e.touches[0].clientY;
  pulling = true;
  pullDist = 0;
}, {passive: true});
addEventListener("touchmove", (e) => {
  if (!pulling || refreshing) return;
  if (!atTop()) { pulling = false; setPull(0); return; }
  const dy = e.touches[0].clientY - pullStartY;
  if (dy <= 0) { pullDist = 0; setPull(0); return; }
  pullDist = Math.min(dy * 0.55, 130);
  setPull(pullDist);
  if (dy > 12 && atTop()) e.preventDefault();
}, {passive: false});
addEventListener("touchend", async () => {
  if (!pulling) return;
  pulling = false;
  if (pullDist < PULL_AT) { setPull(0); return; }
  refreshing = true;
  setPull(52, "Refreshing…");
  try {
    const data = await api("/api/rescan");
    state.tracks = data.tracks;
    $("#count").textContent = data.count + " tracks";
    render();
  } catch (e) {}
  refreshing = false;
  setPull(0);
});
addEventListener("touchcancel", () => { pulling = false; setPull(0); });
async function boot() {
  try {
    const data = await api("/api/library");
    state.tracks = data.tracks;
    $("#count").textContent = data.count + " tracks";
    showApp();
    try {
      const saved = JSON.parse(localStorage.getItem("shelf-queue") || "null");
      const ids = saved && (saved.ids || (saved.queue || []).map(t => t.id));
      if (ids && ids.length) {
        const q = ids.map(id => state.tracks.find(t => t.id === id)).filter(Boolean);
        if (q.length) {
          state.plain = q.slice();
          state.queue = q;
          state.index = Math.min(Math.max(saved.index || 0, 0), q.length - 1);
          paintNow();
        }
      }
    } catch (e) {}
    render();
  } catch (err) {
    if (String(err.message) !== "auth") $("#login-err").textContent = "Could not load library.";
  }
}
if (state.token) boot(); else showLogin();
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
</script>
</body>
</html>
"""

MANIFEST = json.dumps({
    "name": "Shelf",
    "short_name": "Shelf",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#12110f",
    "theme_color": "#12110f",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
    ],
}, indent=2)

ICONS = {
    "/icon-192.png": Path(__file__).with_name("icons") / "icon-192.png",
    "/icon-512.png": Path(__file__).with_name("icons") / "icon-512.png",
    "/apple-touch-icon.png": Path(__file__).with_name("icons") / "apple-touch-icon.png",
}

SW = """
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(clients.claim()));
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.pathname === '/stream' || url.pathname === '/cover') return;
  event.respondWith(fetch(event.request));
});
"""


COVER_CACHE: dict = {}
COVER_LOCKS: dict = {}
COVER_LOCKS_GUARD = threading.Lock()
FFMPEG_SEM = threading.Semaphore(2)

def sniff_image(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def extract_cover(path: Path):
    """Pull embedded album art out of an audio file. One extract per file, even if many rows ask at once."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return b"", "image/jpeg"
    cache_key = (str(path), stamp)
    with COVER_LOCKS_GUARD:
        lock = COVER_LOCKS.setdefault(str(path), threading.Lock())
    with lock:
        cached = COVER_CACHE.get(cache_key)
        if cached is not None:
            return cached
        data = b""
        try:
            from mutagen import File
            audio = File(path)
            tags = getattr(audio, "tags", None) if audio is not None else None
            if tags is not None and "covr" in tags and tags["covr"]:
                data = bytes(tags["covr"][0])
            if not data and tags is not None:
                for key_name in list(tags.keys()):
                    if str(key_name).startswith("APIC"):
                        data = getattr(tags[key_name], "data", b"") or b""
                        if data:
                            break
            if not data and audio is not None and getattr(audio, "pictures", None):
                data = audio.pictures[0].data or b""
        except Exception:
            data = b""
        if not sniff_image(data):
            try:
                with FFMPEG_SEM:
                    proc = subprocess.run(
                        ["ffmpeg", "-v", "error", "-i", str(path), "-an", "-c:v", "copy", "-f", "image2", "pipe:1"],
                        capture_output=True, timeout=20,
                    )
                if sniff_image(proc.stdout):
                    data = proc.stdout
            except Exception:
                data = b""
        ctype = sniff_image(data)
        if not ctype:
            data = b""
            ctype = "image/jpeg"
        else:
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[ctype]
            dest = path.parent / ("cover" + ext)
            if not any((path.parent / name).is_file() for name in COVER_NAMES):
                try:
                    dest.write_bytes(data)
                except OSError:
                    pass
        COVER_CACHE[cache_key] = (data, ctype)
        if len(COVER_CACHE) > 128:
            COVER_CACHE.pop(next(iter(COVER_CACHE)))
        return data, ctype


class Handler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _authorized(self) -> bool:
        if not PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and hmac.compare_digest(header[7:], TOKEN):
            return True
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        supplied = (qs.get("t") or [""])[0]
        return bool(supplied) and hmac.compare_digest(supplied, TOKEN)

    def _send(self, code: int, body: bytes, content_type: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        extra = dict(extra or {})
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if path in ICONS:
            icon = ICONS[path]
            if icon.is_file():
                self._send(200, icon.read_bytes(), "image/png", {"Cache-Control": "public, max-age=86400"})
                return
        if path == "/manifest.webmanifest":
            self._send(200, MANIFEST.encode(), "application/manifest+json")
            return
        if path == "/sw.js":
            self._send(200, SW.encode(), "application/javascript")
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        if path == "/api/library":
            with INDEX_LOCK:
                payload = dict(INDEX)
            self._json(200, payload)
            return
        if path == "/api/download":
            with DOWNLOAD_LOCK:
                payload = dict(DOWNLOAD_STATE)
            self._json(200, payload)
            return
        if path == "/api/rescan":
            self._json(200, rebuild())
            return
        qs = urllib.parse.parse_qs(parsed.query)
        rel = (qs.get("path") or [""])[0]
        target = safe_join(MUSIC_ROOT, rel) if rel else None
        if path == "/cover":
            if not target or not target.is_file():
                self._send(404, b"", "text/plain")
                return
            if target.suffix.lower() in AUDIO_EXTS:
                data, ctype = extract_cover(target)
                if not data:
                    self._send(404, b"", "text/plain")
                    return
                self._send(200, data, ctype, {"Cache-Control": "private, max-age=86400"})
                return
            data = target.read_bytes()
            ctype = mimetypes.guess_type(target.name)[0] or "image/jpeg"
            self._send(200, data, ctype, {"Cache-Control": "private, max-age=86400"})
            return
        if path == "/stream":
            self._stream(target)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if parsed.path == "/api/login":
            try:
                body = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "bad json"})
                return
            supplied = str(body.get("password", ""))
            if PASSWORD and not hmac.compare_digest(supplied, PASSWORD):
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"token": TOKEN})
            return
        if parsed.path == "/api/download":
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            try:
                body = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "bad json"})
                return
            url = str(body.get("url", "")).strip()
            if not url:
                self._json(400, {"error": "missing url"})
                return
            with DOWNLOAD_LOCK:
                if DOWNLOAD_STATE.get("status") == "running":
                    self._json(409, {"error": "already running"})
                    return
                DOWNLOAD_STATE.update({
                    "status": "running", "message": "Looking up tracks…",
                    "url": url, "done": 0, "total": 0, "failed": [], "attempt": 1,
                })

            def on_progress(done, total, failed, attempt):
                if attempt > 1:
                    message = f"Retrying missing tracks ({attempt}/{MAX_ATTEMPTS})"
                elif total:
                    message = "Downloading"
                else:
                    message = "Looking up tracks…"
                with DOWNLOAD_LOCK:
                    DOWNLOAD_STATE.update({
                        "status": "running",
                        "done": done,
                        "total": total,
                        "failed": list(failed),
                        "attempt": attempt,
                        "message": message,
                    })

            def job():
                try:
                    ensure_deps()
                    code, done, total, failed, _newly = spotify_download_with_retries(
                        url, MUSIC_ROOT, on_progress=on_progress,
                    )
                    info = rebuild()
                    count = info.get("count", 0)
                    if failed:
                        shown = ", ".join(failed[:8])
                        extra = f" (+{len(failed) - 8} more)" if len(failed) > 8 else ""
                        msg = f"{len(failed)} still missing after retries: {shown}{extra}. Library has {count} tracks."
                    elif code == 0:
                        msg = f"Done. Library has {count} tracks."
                    else:
                        msg = f"Finished with errors (code {code}). Library has {count} tracks."
                    with DOWNLOAD_LOCK:
                        DOWNLOAD_STATE.update({
                            "status": "done", "message": msg,
                            "done": max(0, (total or done) - len(failed)),
                            "total": total or done,
                            "failed": list(failed),
                        })
                except Exception as e:
                    sys.stderr.write(f"download failed: {e}\n")
                    with DOWNLOAD_LOCK:
                        DOWNLOAD_STATE.update({"status": "error", "message": str(e)})

            threading.Thread(target=job, daemon=True).start()
            self._json(202, {"status": "started", "url": url})
            return
        self._send(404, b"not found", "text/plain")

    def do_HEAD(self):
        self.do_GET()

    def _stream(self, target: Path | None):
        if not target or not target.is_file() or target.suffix.lower() not in AUDIO_EXTS:
            self._send(404, b"not found", "text/plain")
            return
        size = target.stat().st_size
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if range_header and range_header.startswith("bytes="):
            spec = range_header.split("=", 1)[1].split(",")[0].strip()
            try:
                if "-" in spec:
                    a, b = spec.split("-", 1)
                    if a == "":
                        length = int(b or 0)
                        start = max(0, size - length)
                    else:
                        start = int(a)
                        end = int(b) if b else size - 1
                    end = min(end, size - 1)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "private, max-age=3600")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with target.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)


def main():
    global PASSWORD, TOKEN, MUSIC_ROOT
    parser = argparse.ArgumentParser(description="Shelf music streamer")
    parser.add_argument("--music", default=str(Path.home() / "Music"), help="Folder of music files")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--password", default=os.environ.get("SHELF_PASSWORD", ""))
    parser.add_argument("--https", action="store_true", help="Serve HTTPS using cert.pem and key.pem")
    args = parser.parse_args()
    MUSIC_ROOT = Path(args.music).expanduser().resolve()
    PASSWORD = args.password
    TOKEN = token_for(PASSWORD or "open")
    if not PASSWORD:
        print("Warning: no password set. Fine on Tailscale, not fine on the open internet.", file=sys.stderr)
    info = rebuild()
    scheme = "https" if args.https else "http"
    print(f"Shelf indexed {info['count']} tracks in {MUSIC_ROOT}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    if args.https:
        here = Path(__file__).resolve().parent
        cert = here / "cert.pem"
        key = here / "key.pem"
        if not cert.is_file() or not key.is_file():
            cert.write_text(FALLBACK_CERT, encoding="ascii")
            key.write_text(FALLBACK_KEY, encoding="ascii")
            print(f"Wrote {cert} and {key}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"Listening on {scheme}://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


FALLBACK_CERT = """-----BEGIN CERTIFICATE-----
MIIDMjCCAhqgAwIBAgIUXVW9CzctcRRulpJaRNTetKqza2kwDQYJKoZIhvcNAQEL
BQAwEDEOMAwGA1UEAwwFU2hlbGYwHhcNMjYxMDA2MDI1MzEyWhcNMjkwMTA4MDI1
MzEyWjAQMQ4wDAYDVQQDDAVTaGVsZjCCASIwDQYJKoZIhvcNAQEBBQADggEPADCC
AQoCggEBAKLnV0K/l6sP2Do0qPbE+SVZOa8AGbPkkIvhaBtDcs2K9wJ30a9gom6Z
lUMFbB9YvCfVvI37fHv90MtoChvmMrqjDcfFSt3nBwnd9fkboDHxepSoiTGmD3n9
Vc12oplNB7ndcqj/iklmB0or8zXN3xFXkyG1hG7mJIkKJvD00fOztLlD9KluKzz9
C2WlyiMgPf2+8vQiHJzhvDSrvXO7AJWFWOPOTed7HceWblgAOvCfadvJ6BbJldRy
V0O+z7dzQMpDSAyaokFSa8MlCktz6y8OM+OiuFeCSv3yRwmfadxc2VLhfhiev1FK
ptu0di9VXUDmf0g2Z42eGd6y2ZmFmGcCAwEAAaOBgzCBgDAdBgNVHQ4EFgQUuzMb
jH4MmPFUNpSRrIKhakpiw+kwHwYDVR0jBBgwFoAUuzMbjH4MmPFUNpSRrIKhakpi
w+kwDwYDVR0TAQH/BAUwAwEB/zAtBgNVHREEJjAkgglsb2NhbGhvc3SCC3NoZWxm
LmxvY2FshwQKAADFhwR/AAABMA0GCSqGSIb3DQEBCwUAA4IBAQBdfhZEoA2/lqh/
+OwGwU50UZ+MljnxiHuiA7lJGrewOHR03nleryRMuEqNEZrkkoAtBUO+7u8HMz8d
L94RdDhMt0SyJvgFl09KU0MCn8MNoPK5lBkZ8doKv6wAQQiJs7lfSHyuG7oXbBaq
dp/QLMPsMgBPnhdxfo6Jgka3JB0EDYgbrWV/x528eiE1OJnBQtJud2+//EetX8HF
XbaUfYBX/FFj6t4kU/oZadxU7bZiH+oLCZtsT+lQhiQsY8xcOSw9KGBrFnv3wehQ
LI4pm02bpNjY8+z6TtcxYPEnF1aeSmiLRYkH8EaaOpPxX4Ez7KiR7M36JE6A9X44
3joOOfmQ
-----END CERTIFICATE-----
"""

FALLBACK_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCi51dCv5erD9g6
NKj2xPklWTmvABmz5JCL4WgbQ3LNivcCd9GvYKJumZVDBWwfWLwn1byN+3x7/dDL
aAob5jK6ow3HxUrd5wcJ3fX5G6Ax8XqUqIkxpg95/VXNdqKZTQe53XKo/4pJZgdK
K/M1zd8RV5MhtYRu5iSJCibw9NHzs7S5Q/Spbis8/QtlpcojID39vvL0Ihyc4bw0
q71zuwCVhVjjzk3nex3Hlm5YADrwn2nbyegWyZXUcldDvs+3c0DKQ0gMmqJBUmvD
JQpLc+svDjPjorhXgkr98kcJn2ncXNlS4X4Ynr9RSqbbtHYvVV1A5n9INmeNnhne
stmZhZhnAgMBAAECggEATYNmsgwYTv0rFgcYntL6+hqKqAqUMFUBso1+y/o8FjGZ
d0MsMxb4M0hBDbL2z7OZVBsPrT+M7DLJtv+6T6wpV0VeuT/Xb98OKlPDRtUuVJVi
jnnjTgyGMLWqrilVFTziAD6u4Fs3odU6gsCG6YMcMVRKXi/Q5DP+DncWMHFNtXd6
nKu9mxoDXeRK8qd/hStv/BJT9lTD1tN2R0WoffM/R5zfilJx65JD5A0osdPaksDG
iO+oqQ+/yRAdrp/6EtHBzb/PjSUWHHachYLZPQppJ7qzc6uLSXXboTCsyggrxG58
kFfFHNauscQPO129kFCbSJk2zcG+VMwQCuTVXul+gQKBgQDOUwcnj7S54VgUY2T1
2qVh04UeLN/XBuzX16GM3mHAz3l7CthAYoy+cgvR7vA0fCa55aOTmIxaVgsb2KVR
m83sk4tgqSFmtP8eDq4gXFY5GYOcN1qku0zl4HWJdnITQAcyw+OfRr/ksovyi9rY
/JzCm7bIPim1MdRpBvHWz6Kz9QKBgQDKIAt19awOx/Hc8dCB9FOLUss8FEoRvSuO
orA3pYoiQx9hAanFB9omJTI9Ul4TiSGoZRbRVbbrsTx8+NhFmtOGp+2l3sXIV4zC
xONrLspeh0IgjWw0zO4HzJs67Pq4oA89Odd4uTYbTlYkW5CkTpULZhoMGLDIw/50
Gi09mjc9awKBgAXybLVeNgZZkXFdAA76E2RDdYXOI1GOWItMItLtogGtuUGrqVoJ
vSbMIL0XVwMcRP18uf9KGVmNO1q9Ht7AT+ouwoixt02/j87s+2XhY3bX3mxFSC6R
W4EQvDKPIHCfmtWCwe/YWTWdU91U3KT+ewI8/MK+z/LyEb5JPfVEHNt1AoGAPJ5l
sYfvhTNUB0aCmSb0k1lOWINhP3azhICoRtgf2Gy3EXnVN6FGPKB1M8zrKAK9lGpU
8I07Ye5yzKw4+I8Zl03vyQpJ2O7g6VzxM6kByl3YaDjh1egMD0y+1WpTO2HNkDgz
tiId5wyUay0G8TAcJSIG98/D3UJiouFi5hHFNWcCgYEAsm65uyezuGDU04bZYy6Q
aRIAnn/VeA9rjSpOuNEkvvwFF/GN/cedxPl7dlXMWvpqd+AVnbLQOfOeEghbCgcP
aymkcFd+edoVYlW0mKN51OwN1iXpQASLjVjwduLniOjLYIBG/XfsCHhkLytDS8Zw
pOMh9wcbSkmpZGMlZBmbKnU=
-----END PRIVATE KEY-----
"""


if __name__ == "__main__":
    main()

