#!/usr/bin/env python3
"""
Spotify Downloader for Jellyfin (Python port of downloader.bat)
Requires: Python 3.11+, spotdl, yt-dlp, and ffmpeg on PATH.
"""

import os
import sys
import subprocess
from pathlib import Path

def main():
    print("=" * 51)
    print("  CHECKING FOR DOWNLOADER UPDATES (PLEASE WAIT)")
    print("=" * 51)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp", "spotdl", "--quiet"],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Warning: Failed to upgrade packages: {e}")
        # Continue anyway

    print("\n" + "=" * 51)
    print("             SPOTIFY DOWNLOADER FOR JELLYFIN")
    print("=" * 51)
    print()

    spoturl = input("Paste your Spotify Playlist or Album URL: ").strip()
    if not spoturl:
        print("Error: You must paste a valid URL.")
        input("Press Enter to exit...")
        sys.exit(1)

    # Output path: Music folder next to this script
    script_dir = Path(__file__).resolve().parent
    outpath = script_dir / "Music"
    outpath.mkdir(parents=True, exist_ok=True)

    print(f"\nStarting download straight to: {outpath}")
    print("Optimization Active: 4 concurrent tracks.")
    print("Saving locally to script directory.\n")

    # Build the spotdl command (matches the original bat + intended --lyrics none)
    # Prefer the module form so it works even if the Scripts path differs
    cmd = [
        sys.executable, "-m", "spotdl", "download", spoturl,
        "--output", str(outpath / "{artists}/{album}/{track-number} - {title}.{output-ext}"),
        "--format", "m4a",
        "--bitrate", "320k",
        "--threads", "4",
        "--lyrics", "none",   # skip lyric providers for speed (as intended in the bat comments)
    ]

    print("Running:", " ".join(cmd))
    print()

    try:
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            print(f"\nspotdl exited with code {result.returncode}")
    except FileNotFoundError:
        print("Error: spotdl not found. Make sure it is installed and Python can run it.")
        print("Try: python -m pip install --upgrade spotdl")
    except Exception as e:
        print(f"Unexpected error: {e}")

    print("\n" + "=" * 51)
    print("Process finished! Check your local Music folder.")
    print("=" * 51)
    input("Press Enter to exit...")

if __name__ == "__main__":
    main()