# Shelf

A passworded music player that runs on the computer where your files already live. Your phone is just a browser (or a home-screen icon). Nothing phones home. No account. No subscription.

Organize files like this if you can. Shelf uses folders, not ID3 tags, so it stays dependency-free:

```
Music/
  Artist Name/
    Album Name/
      01 - Song.mp3
      cover.jpg
```

`cover.jpg`, `folder.jpg`, or `front.png` in the album folder becomes the artwork.

## Windows

`python3` on a new Windows install is a Store stub, not Python. That is the error in the screenshot.

1. Install Python from https://www.python.org/downloads/windows/
2. On the first installer screen, check "Add python.exe to PATH".
3. Put your files in `C:\Users\zjarr\Music`, or edit `MUSIC` in `start-shelf.bat`.
4. Edit `PASS` in `start-shelf.bat`, then double-click it.

Or, after Python is installed, in the Shelf folder:

```bat
py -3 shelf.py --music "%USERPROFILE%\Music" --password "a-long-password"
```

`~/Music` is a Mac/Linux path. On this PC the folder is `C:\Users\zjarr\Music`.


```bash
python3 shelf.py --music ~/Music --password 'a-long-password'
```

Open `http://127.0.0.1:8787` on that computer and confirm songs play. Seeking works. Then stop exposing it any other way than the steps below.

Python 3.9+ is enough. No pip install.

## Install it on the phone

Shelf is an installable app, not a store download. The icon, lock screen, and Bluetooth buttons come from the page itself.

Android, Chrome:

1. Open the Tailscale address.
2. Log in.
3. Tap Install app, or the Chrome menu, then Install app / Add to Home screen.
4. Open Shelf from the home screen, not the browser tab.

iPhone, Safari:

1. Open the Tailscale address in Safari.
2. Share, then Add to Home Screen.
3. Open that icon. iOS will not offer an install button.

Lock the phone or pocket it after playback has started. The track should keep going, and a Cardo or Sena helmet should get play, pause, and next from the media session. If the helmet only has a volume rocker, that still works through the phone's audio output.

Before a ride, build a queue and tap Save queue offline. Those files stay on the phone, so a dead zone does not stop the music. Streaming still needs Tailscale and a signal. Offline files do not.

Android is the reliable one for screen-off playback. iOS will usually continue audio after you lock the phone, but it is stricter than a native app and can drop a home-screen player if the system is low on memory. If that happens on an iPhone, start the ride from the Safari tab instead of the home-screen icon.

Do not port-forward 8787 to the internet. You were already burned by an account that someone else could charge. An open music port is the same class of mistake.

Use Tailscale. It is free for personal use, and the phone and the computer join a private network. Nothing is on the public internet.

1. Install Tailscale on the computer and the phone. Sign in with the same account.
2. On the computer, start Shelf bound to all interfaces (the default).
3. In the Tailscale app, copy the computer's tailnet IP (100.x.y.z) or MagicDNS name.
4. On the phone, open `http://100.x.y.z:8787`, enter the password, then Share → Add to Home Screen.

That home-screen icon is the app. Playback, queue, shuffle, and repeat stay in the browser. Lock-screen controls depend on the phone: iOS Safari is decent for one track, Android Chrome is better.

If Tailscale is not an option, a WireGuard VPN you run yourself is the other sane choice. Cloudflare Tunnel also works, but only with an access rule in front of it.

## Rescan

Shelf indexes on startup. After you add files, open `http://127.0.0.1:8787/api/rescan` while logged in, or restart the process.

## Formats

Phones play mp3 and m4a reliably. Opus and ogg work in Chrome. Flac often fails in Safari. If a file will not play, convert that one to mp3. The server does not transcode, which is why it stays light.

## Downloading from Spotify

Everything is in the single `shelf.py` file (no separate downloader module needed).



Shelf can pull Spotify playlists, albums, or tracks straight into its Music folder.

Requirements (one-time):
```bash
py -m pip install spotdl yt-dlp
```
FFmpeg must also be on PATH (spotdl uses it).

In the web UI, paste a Spotify URL into the new field and click **Download**.  
The job runs in the background; the library rescans automatically when it finishes.

Files are saved as:
```
Music/Artist Name/Album Name/01 - Song Title.m4a
```
which matches Shelf’s folder-based scanner.

You can also run the helper standalone:
```bash
python downloader.py
```
