/**
 * Clip Studio's desktop shell. It starts the Python server on a free local port, waits for it to answer, and shows
 * the same pages the browser version serves. The packaged app ships the server as a PyInstaller folder and its own
 * ffmpeg and ffprobe, and keeps the library and saved keys in the user's app-data folder.
 */
const { app, BrowserWindow, dialog, shell } = require('electron');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');

const IS_WINDOWS = process.platform === 'win32';
const STARTUP_TIMEOUT_MS = 90_000;
const PREFERRED_PORT = 47800;

/** @type {import('node:child_process').ChildProcess|null} */
let server = null;
/** @type {BrowserWindow|null} */
let mainWindow = null;
let quitting = false;
let logStream = null;
// Set once the server answers. Pages from anywhere else open in the system browser.
let serverOrigin = null;

function listenOn(port) {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.unref();
    probe.on('error', reject);
    probe.listen(port, '127.0.0.1', () => {
      const { port: chosen } = probe.address();
      probe.close(() => resolve(chosen));
    });
  });
}

/**
 * The same port every launch when it is free, so a platform sign-in's registered redirect URI keeps working
 * (TikTok matches it exactly). Any free port otherwise.
 */
async function choosePort() {
  try {
    return await listenOn(PREFERRED_PORT);
  } catch {
    return listenOn(0);
  }
}

/** The server command: the bundled executable when packaged, else the repo's Python for development. */
function serverCommand(port) {
  const args = ['--host', '127.0.0.1', '--port', String(port)];
  if (app.isPackaged) {
    const dir = path.join(process.resourcesPath, 'backend');
    return { command: path.join(dir, IS_WINDOWS ? 'clipstudio-server.exe' : 'clipstudio-server'), args, cwd: dir };
  }
  const repo = path.resolve(__dirname, '..');
  const venvPython = path.join(repo, '.venv', IS_WINDOWS ? 'Scripts/python.exe' : 'bin/python');
  const python = process.env.CLIP_STUDIO_PYTHON
    || (fs.existsSync(venvPython) ? venvPython : IS_WINDOWS ? 'python' : 'python3');
  return { command: python, args: ['-m', 'clipstudio', ...args], cwd: repo };
}

/** Folders holding ffmpeg and ffprobe: the bundled copies when packaged, the npm copies as a fallback in development. */
function ffmpegDirs() {
  if (app.isPackaged) return [path.join(process.resourcesPath, 'ffmpeg')];
  const dirs = [];
  try {
    dirs.push(path.dirname(require('ffmpeg-static')));
  } catch {}
  try {
    dirs.push(path.dirname(require('ffprobe-static').path));
  } catch {}
  return dirs;
}

function serverEnv() {
  const env = { ...process.env, PYTHONUNBUFFERED: '1', CLIP_STUDIO_STORAGE: path.join(app.getPath('userData'), 'storage') };
  // Windows spells it Path. Setting a second PATH key would leave the child with two and an unpredictable winner.
  const key = Object.keys(env).find((k) => k.toUpperCase() === 'PATH') || 'PATH';
  const bundled = ffmpegDirs();
  // The packaged app always uses its own ffmpeg; development prefers the one already installed.
  const parts = app.isPackaged ? [...bundled, env[key]] : [env[key], ...bundled];
  env[key] = parts.filter(Boolean).join(path.delimiter);
  return env;
}

function logFile() {
  const dir = path.join(app.getPath('userData'), 'logs');
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, 'server.log');
}

function startServer(port) {
  const { command, args, cwd } = serverCommand(port);
  const env = serverEnv();
  fs.mkdirSync(env.CLIP_STUDIO_STORAGE, { recursive: true });
  logStream = fs.createWriteStream(logFile(), { flags: 'w' });
  server = spawn(command, args, { cwd, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  server.stdout.pipe(logStream, { end: false });
  server.stderr.pipe(logStream, { end: false });
  server.on('error', (err) => fail(`Clip Studio could not start its server: ${err.message}`));
  server.on('exit', (code) => {
    server = null;
    if (!quitting) fail(`Clip Studio's server stopped (exit code ${code}).`);
  });
}

function stopServer() {
  if (!server) return;
  const { pid } = server;
  server = null;
  if (IS_WINDOWS) {
    // Kill the whole tree, so an ffmpeg the server started does not outlive it.
    spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true });
  } else {
    try {
      process.kill(pid, 'SIGTERM');
    } catch {}
  }
}

function answers(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.setTimeout(2000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForServer(origin) {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!server) return false;
    if (await answers(`${origin}/api/pipelines`)) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

function fail(message) {
  if (quitting) return;
  quitting = true;
  dialog.showErrorBox('Clip Studio', `${message}\n\nDetails are in ${logFile()}`);
  stopServer();
  app.quit();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#2c2d30',
    title: 'Clip Studio',
    autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false },
  });
  const contents = mainWindow.webContents;
  // New windows (platform sign-in, "Open on YouTube") go to the system browser. Google refuses sign-in inside an
  // embedded browser, and the sign-in returns to this app's local server, which the system browser can reach too.
  contents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  contents.on('will-navigate', (event, url) => {
    if (serverOrigin && url.startsWith(serverOrigin)) return;
    event.preventDefault();
    if (/^https?:/.test(url)) shell.openExternal(url);
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  return mainWindow;
}

async function launch() {
  const window = createWindow();
  window.loadFile(path.join(__dirname, 'loading.html'));
  const port = await choosePort();
  const origin = `http://127.0.0.1:${port}`;
  startServer(port);
  if (!(await waitForServer(origin))) {
    fail('Clip Studio\'s server did not start in time.');
    return;
  }
  serverOrigin = origin;
  window.loadURL(`${origin}/`);
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });
  app.whenReady().then(launch).catch((err) => fail(err.message));
  // The server belongs to the window, so closing it quits on every platform, macOS included.
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', () => {
    quitting = true;
    stopServer();
  });
}
