import { autoCaptions, getStudioOptions, listClips, renderClip } from './api.js';
import { countOf, escapeHtml, formatTime, renderNav } from './ui.js';
import { clipName, publishUrl, studioUrl } from './widgets.js';

/**
 * The Studio: captions, a title, the frame shape, a border and the look of one library clip, previewed live and
 * exported by the server as a new clip. The preview draws each setting the way the export will:
 * sizes are percents of the output frame, and the frame size follows clip_engine.edit_frame.
 *
 * @typedef {import('./api.js').Clip} Clip
 * @typedef {import('./api.js').EditSpec} EditSpec
 * @typedef {import('./api.js').CaptionStyle} CaptionStyle
 * @typedef {import('./api.js').CaptionSegment} CaptionSegment
 */

const DRAFT_PREFIX = 'clipStudio.studio.';
// Matches clip_engine.EDIT_MAX_SIDE.
const MAX_SIDE = 1920;
// libass sizes a font by its full line height, CSS by its em box, which is about 1.15 times smaller.
const ASS_TO_CSS_FONT = 0.87;
// Matches the box padding in captions._style_line, as a share of the font size.
const BOX_PADDING = 0.18;
const MIN_CUE = 0.2;

/** One-click caption looks. Each sets only the fields it names. */
const PRESETS = [
  { label: 'Clean', style: { font: 'Arial', size: 6.5, color: '#ffffff', outline_color: '#000000', outline: 12,
    box: false, bold: true, italic: false, uppercase: false } },
  { label: 'Bold pop', style: { font: 'Impact', size: 8.5, color: '#ffe14d', outline_color: '#000000', outline: 16,
    box: false, bold: false, italic: false, uppercase: true } },
  { label: 'Boxed', style: { font: 'Arial', size: 5.5, color: '#ffffff', box: true, box_color: '#000000',
    box_opacity: 0.65, bold: true, italic: false, uppercase: false } },
  { label: 'Subtle', style: { font: 'Georgia', size: 5, color: '#f5f5f5', outline_color: '#000000', outline: 6,
    box: false, bold: false, italic: true, uppercase: false } },
  { label: 'Playful', style: { font: 'Comic Sans MS', size: 7, color: '#ffffff', outline_color: '#6a2cff',
    outline: 20, box: false, bold: true, italic: false, uppercase: false } },
];

/** How each bound value reads next to its slider, by the last part of its path. */
const SHOW = {
  size: (v) => `${v}%`,
  outline: (v) => `${v}%`,
  margin: (v) => `${v}%`,
  border: (v) => `${v}%`,
  focus_x: (v) => `${v}%`,
  focus_y: (v) => `${v}%`,
  box_opacity: (v) => `${Math.round(v * 100)}%`,
  volume: (v) => `${Math.round(v * 100)}%`,
  brightness: (v) => (v > 0 ? `+${v.toFixed(2)}` : v.toFixed(2)),
  contrast: (v) => `×${v.toFixed(2)}`,
  saturation: (v) => `×${v.toFixed(2)}`,
};

const clipSelect = document.getElementById('clipSelect');
const exportBtn = document.getElementById('exportBtn');
const resetEditBtn = document.getElementById('resetEditBtn');
const statusEl = document.getElementById('studioStatus');
const errorEl = document.getElementById('studioError');
const frameInfo = document.getElementById('frameInfo');
const stageWrap = document.getElementById('stageWrap');
const stage = document.getElementById('stage');
const placeholder = document.getElementById('stagePlaceholder');
const video = document.getElementById('studioVideo');
const stageBorder = document.getElementById('stageBorder');
const stageTitle = document.getElementById('stageTitle');
const stageCaption = document.getElementById('stageCaption');
const playBtn = document.getElementById('playBtn');
const clockNow = document.getElementById('clockNow');
const clockLength = document.getElementById('clockLength');
const trimStartInput = document.getElementById('trimStart');
const trimEndInput = document.getElementById('trimEnd');
const trimFill = document.getElementById('trimFill');
const trimPlayhead = document.getElementById('trimPlayhead');
const captionTrack = document.getElementById('captionTrack');
const cueList = document.getElementById('cueList');
const cueHint = document.getElementById('cueHint');
const autoCaptionsBtn = document.getElementById('autoCaptionsBtn');
const addCueBtn = document.getElementById('addCueBtn');
const chunkBtn = document.getElementById('chunkBtn');
const chunkWords = document.getElementById('chunkWords');
const clearCuesBtn = document.getElementById('clearCuesBtn');
const boundInputs = [...document.querySelectorAll('[data-bind]')];

/** @type {EditSpec|null} */
let defaults = null;
/** @type {Clip[]} */
let clips = [];
/** @type {Clip|null} */
let clip = null;
/** @type {EditSpec|null} */
let spec = null;
let length = 0;
let srcW = 0;
let srcH = 0;
let frameRequest = 0;

// ------------------------------------------------------------------------------------------------ helpers

const getPath = (obj, path) => path.split('.').reduce((o, k) => o?.[k], obj);

function setPath(obj, path, value) {
  const keys = path.split('.');
  const last = keys.pop();
  keys.reduce((o, k) => o[k], obj)[last] = value;
}

const even = (v) => Math.max(2, Math.round(v / 2) * 2);
const round2 = (v) => Math.round(v * 100) / 100;
const trimEnd = () => spec?.trim_end ?? length;

function hexToRgba(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function outlineShadow(width, color) {
  if (width <= 0) return 'none';
  // Sixteen offsets read as a solid outline at the sizes captions use.
  return Array.from({ length: 16 }, (_, i) => {
    const a = (i / 16) * 2 * Math.PI;
    return `${(Math.cos(a) * width).toFixed(2)}px ${(Math.sin(a) * width).toFixed(2)}px 0 ${color}`;
  }).join(', ');
}

/** Deep-merge saved values over the defaults, so a draft from an older version still has every field. */
function merged(base, saved) {
  if (!saved || typeof saved !== 'object' || Array.isArray(base)) return saved ?? base;
  const out = { ...base };
  for (const [key, value] of Object.entries(saved)) {
    out[key] = base && typeof base[key] === 'object' && base[key] !== null && !Array.isArray(base[key])
      ? merged(base[key], value) : value;
  }
  return out;
}

function setStatus(content, loading = false) {
  if (loading) {
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    statusEl.replaceChildren(spinner, ` ${content}`);
  } else if (typeof content === 'string') {
    statusEl.textContent = content;
  } else {
    statusEl.replaceChildren(...content);
  }
}

function showError(text) {
  errorEl.textContent = text || '';
  errorEl.hidden = !text;
}

function link(text, href, download) {
  const a = document.createElement('a');
  a.href = href;
  a.textContent = text;
  if (download) a.download = download;
  return a;
}

// --------------------------------------------------------------------------------------------------- drafts

function readDraft(clipId) {
  try {
    return JSON.parse(localStorage.getItem(DRAFT_PREFIX + clipId));
  } catch {
    return null;
  }
}

function saveDraft() {
  if (!clip || !spec) return;
  try {
    localStorage.setItem(DRAFT_PREFIX + clip.clip_id, JSON.stringify(spec));
  } catch {}
}

function forgetDraft() {
  try {
    localStorage.removeItem(DRAFT_PREFIX + clip.clip_id);
  } catch {}
}

const freshSpec = () => structuredClone(defaults);

// ---------------------------------------------------------------------------------------------- the stage

/** The export's frame size. Mirrors clip_engine.edit_frame. */
function frameSize() {
  if (!srcW || !srcH) return [1280, 720];
  let w = srcW;
  let h = srcH;
  if (spec.aspect !== 'source') {
    const [a, b] = spec.aspect.split(':').map(Number);
    const ratio = a / b;
    const wider = srcW / srcH > ratio;
    if ((spec.fit === 'crop') === wider) {
      h = srcH;
      w = h * ratio;
    } else {
      w = srcW;
      h = w / ratio;
    }
  }
  const scale = Math.min(1, MAX_SIDE / Math.max(w, h));
  return [even(w * scale), even(h * scale)];
}

/** Size the stage to fit its space, and return how many screen pixels one output pixel takes. */
function layoutStage(outW, outH) {
  const scale = Math.min(stageWrap.clientWidth / outW, stageWrap.clientHeight / outH);
  stage.style.width = `${Math.floor(outW * scale)}px`;
  stage.style.height = `${Math.floor(outH * scale)}px`;
  return scale;
}

/** @param {HTMLElement} el @param {CaptionStyle} style */
function styleText(el, style, outH, scale) {
  const sizePx = (style.size / 100) * outH * scale;
  const margin = (style.margin / 100) * outH * scale;
  el.dataset.position = style.position;
  el.style.top = style.position === 'top' ? `${margin}px` : style.position === 'middle' ? '50%' : 'auto';
  el.style.bottom = style.position === 'bottom' ? `${margin}px` : 'auto';
  el.style.fontFamily = `"${style.font}", Arial, sans-serif`;
  el.style.fontSize = `${sizePx * ASS_TO_CSS_FONT}px`;
  el.style.fontWeight = style.bold ? '700' : '400';
  el.style.fontStyle = style.italic ? 'italic' : 'normal';
  el.style.textTransform = style.uppercase ? 'uppercase' : 'none';
  el.style.color = style.color;
  const span = el.firstElementChild;
  if (style.box) {
    span.style.background = hexToRgba(style.box_color, style.box_opacity);
    span.style.padding = `${sizePx * BOX_PADDING}px`;
    span.style.textShadow = 'none';
  } else {
    span.style.background = 'transparent';
    span.style.padding = '0';
    span.style.textShadow = outlineShadow((sizePx * style.outline) / 100, style.outline_color);
  }
}

function currentCue(t) {
  let found = -1;
  spec.captions.forEach((c, i) => {
    if (c.start <= t && t < c.end) found = i;
  });
  return found;
}

function renderOverlayText() {
  if (!spec) return;
  const index = currentCue(video.currentTime);
  const text = index >= 0 ? spec.captions[index].text : '';
  stageCaption.firstElementChild.textContent = text;
  stageCaption.hidden = !text.trim();
  for (const el of document.querySelectorAll('[data-cue]')) {
    el.classList.toggle('is-live', Number(el.dataset.cue) === index);
  }
}

function renderStage() {
  if (!spec || stage.hidden) return;
  const [outW, outH] = frameSize();
  const scale = layoutStage(outW, outH);
  const reshaped = spec.aspect !== 'source';
  stage.style.background = reshaped && spec.fit === 'fit' ? spec.background : '#000';
  video.style.objectFit = !reshaped ? 'fill' : spec.fit === 'crop' ? 'cover' : 'contain';
  video.style.objectPosition = spec.fit === 'crop' ? `${spec.focus_x}% ${spec.focus_y}%` : '50% 50%';
  // CSS brightness multiplies where ffmpeg's eq adds, so this is close rather than exact.
  video.style.filter = `brightness(${1 + spec.brightness}) contrast(${spec.contrast}) saturate(${spec.saturation})`;
  // A browser cannot play louder than the file, so the preview stops at 100%. The export applies the full volume.
  video.volume = Math.min(1, spec.volume);
  video.muted = spec.volume === 0;

  const border = (spec.border / 100) * Math.min(outW, outH) * scale;
  stageBorder.hidden = border <= 0;
  stageBorder.style.borderWidth = `${border}px`;
  stageBorder.style.borderColor = spec.border_color;

  styleText(stageTitle, spec.title_style, outH, scale);
  stageTitle.firstElementChild.textContent = spec.title;
  stageTitle.hidden = !spec.title.trim();
  styleText(stageCaption, spec.caption_style, outH, scale);
  renderOverlayText();

  const kept = Math.max(0, trimEnd() - spec.trim_start);
  frameInfo.textContent = `${outW}×${outH} · ${formatTime(kept)} long`;
}

// -------------------------------------------------------------------------------------------------- trimming

function renderTrim() {
  if (!length) return;
  trimStartInput.value = spec.trim_start;
  trimEndInput.value = trimEnd();
  trimFill.style.left = `${(spec.trim_start / length) * 100}%`;
  trimFill.style.width = `${((trimEnd() - spec.trim_start) / length) * 100}%`;
}

function setTrim(start, end) {
  const s = Math.max(0, Math.min(start, length - MIN_CUE));
  const e = Math.min(length, Math.max(end, s + MIN_CUE));
  spec.trim_start = round2(s);
  // At the very end, leave it open so the export keeps every last frame.
  spec.trim_end = e >= length - 0.01 ? null : round2(e);
  renderTrim();
  changed();
}

trimStartInput.addEventListener('input', () => {
  setTrim(parseFloat(trimStartInput.value), trimEnd());
  video.currentTime = spec.trim_start;
});
trimEndInput.addEventListener('input', () => {
  setTrim(spec.trim_start, parseFloat(trimEndInput.value));
  video.currentTime = trimEnd();
});
document.getElementById('setTrimInBtn').addEventListener('click', () => setTrim(video.currentTime, trimEnd()));
document.getElementById('setTrimOutBtn').addEventListener('click', () => setTrim(spec.trim_start, video.currentTime));

// -------------------------------------------------------------------------------------------------- playback

function tick() {
  const t = video.currentTime;
  if (!video.paused && t >= trimEnd()) {
    video.pause();
    video.currentTime = trimEnd();
  }
  clockNow.textContent = formatTime(t);
  if (length) trimPlayhead.style.left = `${(t / length) * 100}%`;
  captionTrack.style.setProperty('--playhead', `${length ? (t / length) * 100 : 0}%`);
  renderOverlayText();
  if (!video.paused) frameRequest = requestAnimationFrame(tick);
}

function togglePlay() {
  if (!spec || !length) return;
  if (video.paused) {
    if (video.currentTime < spec.trim_start || video.currentTime >= trimEnd() - 0.05) video.currentTime = spec.trim_start;
    video.play();
  } else {
    video.pause();
  }
}

video.addEventListener('play', () => {
  playBtn.textContent = 'Pause';
  cancelAnimationFrame(frameRequest);
  frameRequest = requestAnimationFrame(tick);
});
video.addEventListener('pause', () => {
  playBtn.textContent = 'Play';
  tick();
});
video.addEventListener('seeked', tick);
playBtn.addEventListener('click', togglePlay);
document.getElementById('backBtn').addEventListener('click', () => {
  video.currentTime = Math.max(0, video.currentTime - 1);
});
document.getElementById('forwardBtn').addEventListener('click', () => {
  video.currentTime = Math.min(length, video.currentTime + 1);
});
stage.addEventListener('click', togglePlay);
document.addEventListener('keydown', (e) => {
  if (e.key !== ' ' || e.target.closest('input, textarea, select, button')) return;
  e.preventDefault();
  togglePlay();
});

function seek(t) {
  video.currentTime = Math.max(0, Math.min(length, t));
}

// --------------------------------------------------------------------------------------------------- captions

function sortCues() {
  spec.captions.sort((a, b) => a.start - b.start || a.end - b.end);
}

function renderTrack() {
  captionTrack.replaceChildren(...spec.captions.map((c, i) => {
    const block = document.createElement('button');
    block.type = 'button';
    block.className = 'cue-block';
    block.dataset.cue = String(i);
    block.style.left = `${(c.start / length) * 100}%`;
    block.style.width = `${Math.max(0.4, ((c.end - c.start) / length) * 100)}%`;
    block.title = `${formatTime(c.start)} ${c.text}`;
    block.textContent = c.text;
    return block;
  }));
  if (length) {
    const shade = (left, width) => {
      const el = document.createElement('div');
      el.className = 'track-trimmed';
      el.style.left = `${left}%`;
      el.style.width = `${width}%`;
      return el;
    };
    captionTrack.prepend(
      shade(0, (spec.trim_start / length) * 100),
      shade((trimEnd() / length) * 100, ((length - trimEnd()) / length) * 100),
    );
  }
}

function cueHtml(c, i) {
  return `
    <div class="cue" data-index="${i}" data-cue="${i}">
      <div class="cue-times">
        <input type="number" class="field field-sm cue-time" data-field="start" min="0" step="0.05"
          value="${c.start.toFixed(2)}" aria-label="Caption ${i + 1} start (seconds)">
        <button type="button" class="btn btn-sm btn-icon" data-action="start-here" title="Start at the playhead">⇤</button>
        <span class="cue-dash">–</span>
        <input type="number" class="field field-sm cue-time" data-field="end" min="0" step="0.05"
          value="${c.end.toFixed(2)}" aria-label="Caption ${i + 1} end (seconds)">
        <button type="button" class="btn btn-sm btn-icon" data-action="end-here" title="End at the playhead">⇥</button>
        <span class="cue-spacer"></span>
        <button type="button" class="btn btn-sm btn-icon" data-action="play" title="Play this caption">▶</button>
        <button type="button" class="btn btn-sm btn-icon" data-action="split" title="Split at the playhead">Split</button>
        <button type="button" class="btn btn-sm btn-icon" data-action="merge" title="Join with the next caption"${i === spec.captions.length - 1 ? ' disabled' : ''}>Join</button>
        <button type="button" class="btn btn-sm btn-icon btn-danger" data-action="delete" title="Remove" aria-label="Remove caption ${i + 1}">✕</button>
      </div>
      <textarea class="field field-sm cue-text" rows="2" data-field="text" aria-label="Caption ${i + 1} text">${escapeHtml(c.text)}</textarea>
    </div>`;
}

function renderCues() {
  const has = spec.captions.length > 0;
  cueList.innerHTML = spec.captions.map(cueHtml).join('');
  cueHint.textContent = has
    ? `${countOf(spec.captions.length, 'caption')}. Edit the words and times, or click one on the timeline to jump to it.`
    : (clip?.captions.length
      ? 'This clip has subtitles. Choose Add captions automatically to place them, then edit them here.'
      : 'Choose Add captions automatically to fetch subtitles from the video, or Add caption to write your own.');
  for (const btn of [chunkBtn, clearCuesBtn, ...document.querySelectorAll('[data-shift]')]) btn.disabled = !has;
  renderTrack();
  renderOverlayText();
}

function cuesChanged() {
  sortCues();
  renderCues();
  changed();
}

/** Split each caption into lines of at most `words` words, timed by their share of the letters. */
function shortLines(cues, words) {
  return cues.flatMap((c) => {
    const all = c.text.split(/\s+/).filter(Boolean);
    if (all.length <= words) return [c];
    const groups = [];
    for (let i = 0; i < all.length; i += words) groups.push(all.slice(i, i + words).join(' '));
    const weights = groups.map((g) => g.length + 1);
    const total = weights.reduce((a, b) => a + b, 0);
    let t = c.start;
    return groups.map((text, i) => {
      const start = t;
      t = i === groups.length - 1 ? c.end : t + ((c.end - c.start) * weights[i]) / total;
      return { start: round2(start), end: round2(t), text };
    });
  });
}

cueList.addEventListener('input', (e) => {
  const field = e.target.dataset.field;
  if (field !== 'text') return;
  const i = Number(e.target.closest('.cue').dataset.index);
  spec.captions[i].text = e.target.value;
  const block = captionTrack.querySelector(`.cue-block[data-cue="${i}"]`);
  if (block) block.textContent = e.target.value;
  renderOverlayText();
  saveDraft();
});

cueList.addEventListener('change', (e) => {
  const field = e.target.dataset.field;
  if (field !== 'start' && field !== 'end') return;
  const c = spec.captions[Number(e.target.closest('.cue').dataset.index)];
  const value = Math.max(0, Math.min(length, parseFloat(e.target.value) || 0));
  c[field] = round2(value);
  if (c.end - c.start < MIN_CUE) {
    if (field === 'start') c.end = round2(Math.min(length, c.start + MIN_CUE));
    else c.start = round2(Math.max(0, c.end - MIN_CUE));
  }
  cuesChanged();
});

cueList.addEventListener('click', (e) => {
  const button = e.target.closest('[data-action]');
  if (!button) return;
  const i = Number(button.closest('.cue').dataset.index);
  const c = spec.captions[i];
  const t = round2(video.currentTime);
  switch (button.dataset.action) {
    case 'start-here':
      if (t < c.end - MIN_CUE / 2) c.start = t;
      break;
    case 'end-here':
      if (t > c.start + MIN_CUE / 2) c.end = t;
      break;
    case 'play':
      seek(c.start);
      video.play();
      return;
    case 'split': {
      if (t <= c.start + MIN_CUE / 2 || t >= c.end - MIN_CUE / 2) {
        setStatus('Move the playhead inside this caption to split it there.');
        return;
      }
      const words = c.text.split(/\s+/).filter(Boolean);
      const cut = Math.max(1, Math.min(words.length - 1, Math.round(((t - c.start) / (c.end - c.start)) * words.length)));
      spec.captions.splice(i, 1,
        { start: c.start, end: t, text: words.slice(0, cut).join(' ') },
        { start: t, end: c.end, text: words.slice(cut).join(' ') || '…' });
      break;
    }
    case 'merge': {
      const next = spec.captions[i + 1];
      if (!next) return;
      spec.captions.splice(i, 2, { start: c.start, end: Math.max(c.end, next.end), text: `${c.text} ${next.text}`.trim() });
      break;
    }
    case 'delete':
      spec.captions.splice(i, 1);
      break;
    default:
      return;
  }
  cuesChanged();
});

cueList.addEventListener('focusin', (e) => {
  const row = e.target.closest('.cue');
  if (!row || !e.target.matches('textarea')) return;
  const c = spec.captions[Number(row.dataset.index)];
  if (video.paused && (video.currentTime < c.start || video.currentTime >= c.end)) seek(c.start + 0.01);
});

captionTrack.addEventListener('click', (e) => {
  const block = e.target.closest('.cue-block');
  if (!block) return;
  const i = Number(block.dataset.cue);
  seek(spec.captions[i].start + 0.01);
  cueList.querySelector(`.cue[data-index="${i}"] textarea`)?.focus({ preventScroll: false });
});

addCueBtn.addEventListener('click', () => {
  const start = round2(Math.min(video.currentTime, length - MIN_CUE));
  const next = spec.captions.find((c) => c.start > start);
  const end = round2(Math.min(length, start + 2, next ? Math.max(next.start, start + MIN_CUE) : Infinity));
  spec.captions.push({ start, end, text: 'New caption' });
  cuesChanged();
  const i = spec.captions.findIndex((c) => c.start === start && c.text === 'New caption');
  const box = cueList.querySelector(`.cue[data-index="${i}"] textarea`);
  box?.focus();
  box?.select();
});

autoCaptionsBtn.addEventListener('click', async () => {
  if (spec.captions.length && !confirm('Replace your captions with the video\'s subtitles?')) return;
  autoCaptionsBtn.disabled = true;
  setStatus('Getting the subtitles…', true);
  showError(null);
  try {
    const { captions, fetched } = await autoCaptions(clip.clip_id);
    if (!captions.length) {
      setStatus('No subtitles are spoken inside this clip.');
      return;
    }
    spec.captions = captions.map((c) => ({ start: c.start, end: c.end, text: c.text }));
    if (fetched) clip.captions = [...clip.captions, ...captions];
    cuesChanged();
    setStatus(`Added ${countOf(captions.length, 'caption')}${fetched ? ' fetched from the video' : ''}`);
  } catch (err) {
    showError(err.message);
    setStatus('Captions not added');
  } finally {
    autoCaptionsBtn.disabled = false;
  }
});

chunkBtn.addEventListener('click', () => {
  spec.captions = shortLines(spec.captions, Number(chunkWords.value));
  cuesChanged();
});

for (const btn of document.querySelectorAll('[data-shift]')) {
  btn.addEventListener('click', () => {
    const by = Number(btn.dataset.shift);
    spec.captions = spec.captions
      .map((c) => ({ ...c, start: round2(Math.max(0, c.start + by)), end: round2(Math.min(length, c.end + by)) }))
      .filter((c) => c.end - c.start >= MIN_CUE / 2);
    cuesChanged();
  });
}

clearCuesBtn.addEventListener('click', () => {
  if (!confirm('Remove every caption from this edit?')) return;
  spec.captions = [];
  cuesChanged();
});

// --------------------------------------------------------------------------------------- style and video panels

function showValues() {
  for (const out of document.querySelectorAll('[data-show]')) {
    const value = getPath(spec, out.dataset.show);
    const format = SHOW[out.dataset.show.split('.').pop()];
    out.textContent = format ? format(value) : String(value);
  }
  const reshaped = spec.aspect !== 'source';
  for (const el of document.querySelectorAll('[data-when]')) el.hidden = !reshaped || el.dataset.when !== spec.fit;
  for (const btn of document.querySelectorAll('[data-preset]')) {
    const preset = PRESETS[Number(btn.dataset.preset)].style;
    const on = Object.entries(preset).every(([k, v]) => spec.caption_style[k] === v);
    btn.setAttribute('aria-pressed', String(on));
  }
}

function writeInputs() {
  for (const input of boundInputs) {
    const value = getPath(spec, input.dataset.bind);
    if (input.type === 'checkbox') input.checked = Boolean(value);
    else input.value = value ?? '';
  }
  showValues();
}

function readInput(input) {
  if (input.type === 'checkbox') return input.checked;
  if (input.type === 'range' || input.type === 'number') return parseFloat(input.value);
  return input.value;
}

for (const input of boundInputs) {
  input.addEventListener(input.type === 'checkbox' || input.tagName === 'SELECT' ? 'change' : 'input', () => {
    if (!spec) return;
    setPath(spec, input.dataset.bind, readInput(input));
    showValues();
    changed();
  });
}

function renderPresets() {
  const row = document.getElementById('presetRow');
  row.replaceChildren(...PRESETS.map((preset, i) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-sm preset';
    btn.dataset.preset = String(i);
    btn.textContent = preset.label;
    btn.style.fontFamily = `"${preset.style.font}", Arial, sans-serif`;
    return btn;
  }));
  row.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-preset]');
    if (!btn || !spec) return;
    Object.assign(spec.caption_style, PRESETS[Number(btn.dataset.preset)].style);
    writeInputs();
    changed();
  });
}

function changed() {
  renderStage();
  renderTrack();
  saveDraft();
}

// ------------------------------------------------------------------------------------------------- tabs

function switchTab(tab) {
  for (const panel of document.querySelectorAll('[data-tab-panel]')) panel.hidden = panel.dataset.tabPanel !== tab;
  for (const btn of document.querySelectorAll('[data-tab]')) btn.classList.toggle('active', btn.dataset.tab === tab);
}
for (const btn of document.querySelectorAll('[data-tab]')) btn.addEventListener('click', () => switchTab(btn.dataset.tab));

// --------------------------------------------------------------------------------------------- clip loading

function setEditing(on) {
  for (const btn of [exportBtn, playBtn, autoCaptionsBtn, addCueBtn]) btn.disabled = !on;
}

function fillClipSelect(selected) {
  if (!clips.length) {
    clipSelect.replaceChildren(new Option('No clips yet', ''));
    clipSelect.disabled = true;
    return;
  }
  clipSelect.disabled = false;
  clipSelect.replaceChildren(
    new Option('Pick a clip…', ''),
    ...clips.map((c) => new Option(`${clipName(c)} · ${c.duration} s`, c.clip_id)),
  );
  clipSelect.value = selected && clips.some((c) => c.clip_id === selected) ? selected : '';
}

function openClip(clipId) {
  showError(null);
  clip = clips.find((c) => c.clip_id === clipId) || null;
  video.pause();
  setEditing(false);
  if (!clip) {
    spec = null;
    stage.hidden = true;
    placeholder.hidden = false;
    frameInfo.textContent = '';
    cueList.replaceChildren();
    captionTrack.replaceChildren();
    setStatus(clips.length ? 'Pick a clip to edit' : 'Export a clip in the Editor first');
    return;
  }
  spec = merged(freshSpec(), readDraft(clip.clip_id));
  length = 0;
  srcW = 0;
  srcH = 0;
  setStatus('Loading the clip…', true);
  video.src = clip.preview_url;
  video.load();
}

video.addEventListener('loadedmetadata', () => {
  if (!clip || !spec) return;
  length = video.duration || clip.duration;
  srcW = video.videoWidth;
  srcH = video.videoHeight;
  for (const input of [trimStartInput, trimEndInput]) input.max = length;
  if (spec.trim_start >= length) spec.trim_start = 0;
  if (spec.trim_end !== null && spec.trim_end > length) spec.trim_end = null;
  clockLength.textContent = formatTime(length);
  placeholder.hidden = true;
  stage.hidden = false;
  writeInputs();
  renderTrim();
  renderCues();
  renderStage();
  seek(spec.trim_start);
  setEditing(true);
  setStatus(readDraft(clip.clip_id) ? 'Picked up your last edit of this clip' : 'Ready');
});

video.addEventListener('error', () => {
  if (!clip) return;
  showError('This clip\'s video could not be played. It may have been deleted.');
  setStatus('Clip not loaded');
});

clipSelect.addEventListener('change', () => {
  const id = clipSelect.value;
  history.replaceState(null, '', id ? studioUrl(id) : '/studio.html');
  openClip(id);
});

resetEditBtn.addEventListener('click', () => {
  if (!clip || !confirm('Start this clip\'s edit over? Your captions and settings for it are removed.')) return;
  forgetDraft();
  spec = freshSpec();
  writeInputs();
  renderTrim();
  renderCues();
  renderStage();
  setStatus('Started over');
});

exportBtn.addEventListener('click', async () => {
  if (!clip || !spec) return;
  video.pause();
  exportBtn.disabled = true;
  showError(null);
  setStatus('Exporting… the clip is re-encoded with your edits', true);
  const body = {
    ...spec,
    captions: spec.captions.filter((c) => c.text.trim() && c.end > c.start),
  };
  try {
    const made = await renderClip(clip.clip_id, body);
    clips.unshift(made);
    fillClipSelect(clip.clip_id);
    setStatus([
      'Exported.',
      link('Download', made.download_url, made.file_name),
      link('Upload', publishUrl(made.clip_id)),
      link('Open the result', studioUrl(made.clip_id)),
    ]);
  } catch (err) {
    showError(err.message);
    setStatus('Export failed');
  } finally {
    exportBtn.disabled = false;
  }
});

new ResizeObserver(() => renderStage()).observe(stageWrap);

async function start() {
  renderNav();
  renderPresets();
  try {
    const [options, list] = await Promise.all([getStudioOptions(), listClips()]);
    defaults = options.defaults;
    for (const select of document.querySelectorAll('[data-fonts]')) {
      select.replaceChildren(...options.fonts.map((f) => {
        const option = new Option(f, f);
        option.style.fontFamily = `"${f}"`;
        return option;
      }));
    }
    clips = list;
  } catch (err) {
    showError(err.message);
    setStatus('Studio not ready');
    return;
  }
  const wanted = new URLSearchParams(location.search).get('clip');
  fillClipSelect(wanted);
  if (wanted && !clips.some((c) => c.clip_id === wanted)) showError('That clip is no longer in the library.');
  openClip(clipSelect.value);
}

start();
