# Clip Studio desktop app

The desktop app is the same Clip Studio in its own window, with nothing to install first: no Python, uv or ffmpeg.
It runs on Windows, macOS (Apple silicon and Intel) and Linux.

## How it works

- `main.js` is the Electron main process. It starts the Clip Studio server on `127.0.0.1`, waits for it to answer,
  and loads its pages in a window. It uses port 47800 whenever that is free, so the redirect URI you register for
  YouTube or TikTok sign-in stays the same between launches, and any free port otherwise. Quitting the app stops the
  server and any ffmpeg it started.
- In a packaged app the server is `clipstudio-server`, a PyInstaller build of `backend.py` made by
  `clipstudio-server.spec`. It holds Python, every dependency and the pages in `clipstudio/static`.
- The app ships its own `ffmpeg` and `ffprobe` from the `ffmpeg-static` and `ffprobe-static` npm packages, and puts
  them first on the server's `PATH`.
- The clip library, downloaded videos, saved AI keys and upload accounts live in the app-data folder, which the app
  passes to the server as `CLIP_STUDIO_STORAGE`:
  - Windows: `%APPDATA%\Clip Studio\storage`
  - macOS: `~/Library/Application Support/Clip Studio/storage`
  - Linux: `~/.config/Clip Studio/storage`
- The server's log is `logs/server.log` in the same app-data folder. Read it when the app says the server stopped.
- Links that leave the app, such as a platform sign-in on the Publish page or "Open on YouTube", open in the system
  browser. Google refuses to sign in inside an embedded browser, and the sign-in comes back to the local server,
  which the system browser can reach.

The app still needs the internet for what the browser version needs it for: reading videos, AI providers, uploads,
the Archivo font and the hls.js player for Stream mode.

## Run it from the repo

You need Node.js 20 or newer, plus what the browser version needs (`./start.sh` once, so `.venv` exists).

```bash
cd desktop
npm install
npm start
```

Without a packaged server, `npm start` runs `python -m clipstudio` from the repo's `.venv`. Set
`CLIP_STUDIO_PYTHON` to use another Python. It uses the ffmpeg on your `PATH`, and the npm copies only if there is
none.

## Build installers on your machine

Each OS builds its own installers, so build on the OS you are packaging for.

```bash
# From the repo root
python -m pip install . pyinstaller
pyinstaller desktop/clipstudio-server.spec --noconfirm --distpath desktop/backend-dist --workpath build/pyinstaller
desktop/backend-dist/clipstudio-server/clipstudio-server --check   # add .exe on Windows

cd desktop
npm install
npm run dist
```

The installers land in `desktop/dist`:

| OS | Files |
| --- | --- |
| Windows | `Clip-Studio-<version>-win-x64.exe` (installer) |
| macOS | `Clip-Studio-<version>-mac-<arch>.dmg` and `.zip` |
| Linux | `Clip-Studio-<version>-linux-x86_64.AppImage` and `.tar.gz` |

## Release on GitHub

`.github/workflows/desktop.yml` builds all four installers on GitHub's runners. To release:

1. Set the new version in `desktop/package.json` (and `pyproject.toml`, to keep them together), for example `0.2.0`.
   Commit it to `master`.
2. Tag that commit with the same version and push the tag:
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```
3. The workflow checks the tag matches `desktop/package.json`, builds on Windows, macOS Apple silicon, macOS Intel and
   Linux, and uploads every installer to a **draft** release named after the tag.
4. Open the repo's Releases page, check the draft has the `.exe`, both `.dmg` files and the `.AppImage`, write the
   release notes, and choose **Publish release**.

To try a build without releasing, run the workflow by hand from the Actions tab (Desktop app, Run workflow). The
installers are then kept as workflow artifacts only.

## Before the first public release

- **Signing.** The builds are unsigned, so Windows SmartScreen warns about an unknown publisher and macOS refuses to
  open the app until the user right-clicks it and chooses Open. To sign, add these repository secrets and remove
  `CSC_IDENTITY_AUTO_DISCOVERY: "false"` from the workflow:
  - macOS: `CSC_LINK` (a base64 Developer ID Application `.p12`), `CSC_KEY_PASSWORD`, and for notarization
    `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD` and `APPLE_TEAM_ID`. Pass them as `env` on the Package step.
  - Windows: `WIN_CSC_LINK` and `WIN_CSC_KEY_PASSWORD` for a code-signing certificate.
- **Icon.** Add `desktop/build/icon.png` (1024×1024). electron-builder makes every platform's icon from it. Until then
  the app uses Electron's default icon.
- **Licence.** The bundled ffmpeg is GPL. Shipping it is fine for an open-source release; pick a licence for this repo
  and keep ffmpeg's licence file, which the build copies next to it.
- **Updates.** The builds do not update themselves. To add that, install `electron-updater` and call
  `autoUpdater.checkForUpdatesAndNotify()` in `main.js`. The GitHub publish settings already in `package.json` are the
  ones it reads.
