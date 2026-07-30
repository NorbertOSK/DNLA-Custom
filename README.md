# DNLA Custom

https://github.com/user-attachments/assets/eb9a253c-d7e9-4663-9559-ea777e1c3678

Turn your Mac into a wireless screen for your phone. **DNLA Custom** makes
your Mac show up as a DLNA cast target on your local network — pick it from
any DLNA-capable app on your phone and the video plays on your Mac through
VLC, with play/pause/seek/volume controlled from the phone.

> **Platform**: macOS only (Apple Silicon and Intel). Windows/Linux are not
> supported at this time.

Inspired by [Macast](https://github.com/xfangfang/Macast) and
[minicast](https://github.com/freedomNTD/minicast), but fully
self-contained: the renderer is a single Python file with **zero runtime
dependencies** — everything uses the Python standard library and the VLC
you already have installed.

## How it works

```
Phone (DLNA sender app)          Mac (DNLA Custom)
┌──────────────────────┐         ┌────────────────────────────┐
│  video app / gallery │  SSDP   │ discovery responder (UDP)  │
│  "cast to device" ───┼────────▶│ UPnP/SOAP server (HTTP) ───┼──▶ VLC plays
│  play/pause/seek ────┼────────▶│ AVTransport control        │    the stream
└──────────────────────┘         └────────────────────────────┘
```

The app announces itself as a UPnP **MediaRenderer** via SSDP. When your
phone sends a video URL, DNLA Custom launches VLC and drives it through
VLC's local HTTP interface.

## Requirements

| Requirement | Notes |
| --- | --- |
| macOS | Tested on macOS 15+ (Apple Silicon) |
| [VLC media player](https://www.videolan.org/vlc/download-macosx.html) | Must be installed at `/Applications/VLC.app` — this is the playback engine |
| [Python 3](https://www.python.org/downloads/macos/) (3.10+) | Preinstalled with Homebrew or Xcode Command Line Tools; only needed to run from source or build |
| Same Wi-Fi network | Phone and Mac must be on the same LAN |

No `pip install` is required to **run** the renderer — dependencies are only
needed to **build** the GUI app (see below).

## Option 1 — Run from source (CLI)

```sh
git clone <this-repo>
cd dlna
./dlnacast
```

Output:

```
▶ dlnacast 1.0 is running
  Device name : dlnacast (your-hostname)
  Listening   : http://192.168.x.x:8895
  Player      : VLC (starts automatically on first cast)
```

Keep it running and cast from your phone. `Ctrl-C` stops it. Useful flags:
`--name "Living Room"`, `--port 8896`, `--verbose`.

## Option 2 — Build the macOS app (GUI)

The GUI app shows a small window with **Start**, **Stop**, and **Quit**
buttons — no terminal needed once built.

### Build steps

```sh
cd dlna
./build.sh
```

The script:

1. Creates a local virtual environment in `.venv/` (your system Python is
   never modified).
2. Installs the build tools into it:
   [PyInstaller](https://pyinstaller.org/) (packager) and
   [PyObjC](https://pyobjc.readthedocs.io/) (native macOS UI bindings).
3. Produces two artifacts in `dist/`:
   - **`dist/DNLA Custom`** — single-file executable
   - **`dist/DNLA Custom.app`** — standard macOS app bundle (drag to
     `/Applications` if you like)

Rebuild any time the source changes by re-running `./build.sh`.

### Run it

Double-click **DNLA Custom.app** (or run `./dist/DNLA\ Custom`). The
single-file binary unpacks itself on launch, so the window can take a few
seconds to appear. Press **Start** to begin casting, **Stop** to go
offline, **Quit** to exit.

> **Why build a binary at all?** macOS network permissions are granted per
> app. With the packaged app you grant them to *DNLA Custom only*, instead
> of to the whole Python interpreter (which would cover any Python script
> on your machine).

## Using it from your phone

1. Start DNLA Custom (GUI **Start** button or `./dlnacast` in a terminal).
2. On your phone, open a DLNA-capable app:
   - **Android**: VLC for Android, BubbleUPnP, Web Video Cast, MX Player
   - **iPhone/iPad**: VLC for iOS, nPlayer, 8player
3. Tap the cast icon and pick the device (default name:
   `dlnacast (your-hostname)`).
4. Play — VLC opens on the Mac. Playback is controlled from the phone;
   press `f` in VLC for fullscreen.

## macOS permissions

**Local Network** — the first launch triggers the macOS prompt
*"would like to find and connect to devices on your local network"*. You
must **Allow** it, or discovery and playback silently fail. To fix a wrong
choice: **System Settings → Privacy & Security → Local Network**, enable
the app (or your terminal, when running from source), then relaunch.

For reference, the renderer listens on TCP `8895` (UPnP control) and UDP
`1900` (SSDP discovery).

## Development

```sh
# Run the test suite
python3 -m unittest

# Run the renderer with request logging
./dlnacast --verbose
```

### Project layout

| File | Purpose |
| --- | --- |
| `dlnacast.py` | The whole renderer: SSDP discovery, UPnP/SOAP services, GENA eventing, VLC control |
| `dnla_custom_gui.py` | Native macOS GUI (AppKit via PyObjC) wrapping the renderer |
| `dlnacast` | CLI launcher |
| `build.sh` | Builds `dist/DNLA Custom.app` with PyInstaller |
| `make_icon.py` | Generates the app icon (`assets/icon.icns`), invoked by `build.sh` |
| `test_dlnacast.py` | Unit tests |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Device not listed on the phone | Allow Local Network permission (above); confirm same Wi-Fi; disable router "AP/client isolation". |
| Device listed but playback fails | Try VLC mobile or BubbleUPnP as the sender; some apps send DRM or vendor-specific streams VLC cannot open. |
| `error: VLC.app not found` | Install [VLC](https://www.videolan.org/vlc/download-macosx.html) into `/Applications`. |
| Port already in use | CLI: `./dlnacast --port 8896`. |
| `pip` fails during `./build.sh` | Check your network connection; as a fallback, download the wheels manually and install with `pip install --no-index --find-links <dir>`. |
| Window is slow to appear | Expected with the single-file build — it unpacks on each launch. |

## Limitations

- macOS only (the player integration and GUI are Mac-specific).
- Plays what VLC can play — DRM-protected streams (Netflix, etc.) will not work.
- One stream at a time, by design.
