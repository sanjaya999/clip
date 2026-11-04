/**
 * Plays a video source in the editor's <video>. A downloaded file plays directly. A stream is an HLS playlist served
 * by this server, played through hls.js, which is loaded only then. Browsers that play HLS themselves are used only
 * where hls.js cannot run (no Media Source Extensions, as on iPhone): Chrome's built-in HLS player can stop loading
 * for good after a seek.
 */
const HLS_MODULE = 'https://cdn.jsdelivr.net/npm/hls.js@1.5.20/dist/hls.mjs';

let hls = null;
// Bumped by every play and stop, so a slow hls.js load never attaches to a video the editor has moved on from.
let round = 0;

async function loadHls() {
  try {
    const { default: Hls } = await import(HLS_MODULE);
    return Hls.isSupported() ? Hls : null;
  } catch (err) {
    console.error(err);
    return null;
  }
}

/**
 * @param {HTMLVideoElement} video
 * @param {string} src
 * @param {() => void} onFatal called when the stream cannot play, in the cases where the <video> fires no error
 */
export async function playSource(video, src, onFatal) {
  stopSource(video);
  const mine = round;
  const nativeHls = Boolean(video.canPlayType('application/vnd.apple.mpegurl'));
  const Hls = src.endsWith('.m3u8') ? await loadHls() : null;
  if (mine !== round) return;
  if (!Hls) {
    if (src.endsWith('.m3u8') && !nativeHls) {
      onFatal();
      return;
    }
    video.src = src;
    video.load();
    return;
  }
  // The server fetches each segment from the platform, or cuts it with ffmpeg, so allow longer than a CDN would need.
  hls = new Hls({ fragLoadPolicy: { default: { maxTimeToFirstByteMs: 60000, maxLoadTimeMs: 120000,
    timeoutRetry: { maxNumRetry: 2, retryDelayMs: 0, maxRetryDelayMs: 0 },
    errorRetry: { maxNumRetry: 2, retryDelayMs: 1000, maxRetryDelayMs: 8000 } } } });
  hls.on(Hls.Events.ERROR, (_, data) => {
    if (data.fatal && mine === round) onFatal();
  });
  hls.loadSource(src);
  hls.attachMedia(video);
}

/** @param {HTMLVideoElement} video */
export function stopSource(video) {
  round++;
  hls?.destroy();
  hls = null;
  video.removeAttribute('src');
  video.load();
}
