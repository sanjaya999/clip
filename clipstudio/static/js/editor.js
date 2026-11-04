import {
  createClip, createRun, findHighlights, forgetProviderSettings, getCaptions, getPipelines, getVideoInfo,
  downloadVideo, listProviders, listRuns, openStream, saveProviderSettings,
} from './api.js';
import { readAiPrefs, saveAiPrefs } from './ai-prefs.js';
import { forgetWork, pageWasReloaded, readWork, saveWork } from './editor-work.js';
import { countOf, escapeHtml, formatTime, renderNav, showMessage } from './ui.js';
import { pipelineOptions, showActiveRunCount, trackRunUrl } from './widgets.js';
import { playSource, stopSource } from './stream-player.js';

/**
 * `mode` is 'download' for a video saved in storage/media, or 'stream' for one read from the platform as it plays.
 * @type {{url: string, videoId: string, streamUrl: string, title: string, platform: string, duration: number,
 *   mode: 'download'|'stream'}|null}
 */
let currentVideo = null;
let videoDuration = 0;
let clipStart = 0;
let clipEnd = 0;
let allCaptions = [];
/** @type {'loading'|'ready'|'none'|'failed'|null} */
let captionsState = null;
let captionsError = '';
let highlights = [];
let answeredBy = null;
let activeTab = 'highlights';
// A restored range and pipeline choice wait here until the video's length and the pipeline list are known.
let pendingRange = null;
let pendingSendTo = null;
// Bumped by every subtitle fetch and by Clear, so a late answer for an old video is dropped.
let captionsRound = 0;

const videoPlayer = document.getElementById('videoPlayer');
const placeholderView = document.getElementById('placeholderView');
const urlInput = document.getElementById('urlInput');
const loadBtn = document.getElementById('loadBtn');
const loadModeSelect = document.getElementById('loadModeSelect');
const fetchCcTopBtn = document.getElementById('fetchCcTopBtn');
const loadStatus = document.getElementById('loadStatus');
const startSlider = document.getElementById('startSlider');
const endSlider = document.getElementById('endSlider');
const rangeFill = document.getElementById('rangeFill');
const playheadMarker = document.getElementById('playheadMarker');
const startTimeVal = document.getElementById('startTimeVal');
const endTimeVal = document.getElementById('endTimeVal');
const clipDurationPill = document.getElementById('clipDurationPill');
const setStartBtn = document.getElementById('setStartBtn');
const setEndBtn = document.getElementById('setEndBtn');
const stepBackBtn = document.getElementById('stepBackBtn');
const stepForwardBtn = document.getElementById('stepForwardBtn');
const previewClipBtn = document.getElementById('previewClipBtn');
const resetRangeBtn = document.getElementById('resetRangeBtn');
const exportBtn = document.getElementById('exportBtn');
const aiAnalyzeBtn = document.getElementById('aiAnalyzeBtn');
const highlightsList = document.getElementById('highlightsList');
const captionsList = document.getElementById('captionsList');
const searchCaptionsInput = document.getElementById('searchCaptionsInput');
const geminiApiKey = document.getElementById('geminiApiKey');
const aiProviderSelect = document.getElementById('aiProviderSelect');
const aiModelInput = document.getElementById('aiModelInput');
const aiBaseUrlInput = document.getElementById('aiBaseUrlInput');
const aiQueryInput = document.getElementById('aiQueryInput');
const aiProviderHint = document.getElementById('aiProviderHint');
const aiRememberRow = document.getElementById('aiRememberRow');
const aiRememberInput = document.getElementById('aiRememberInput');
const aiForgetBtn = document.getElementById('aiForgetBtn');
const aiAnswered = document.getElementById('aiAnswered');
const loadError = document.getElementById('loadError');
const sendToSelect = document.getElementById('sendToSelect');
const clearBtn = document.getElementById('clearBtn');
const videoMetaHeader = document.getElementById('videoMetaHeader');
const videoDurationDisplay = document.getElementById('videoDurationDisplay');

const PLATFORM_LABELS = { youtube: 'YouTube', twitch: 'Twitch', kick: 'Kick' };
const NO_SUBTITLES = 'This video has no subtitles, so the AI cannot search it. You can still trim the range by hand.';
const NO_VIDEO_MOMENTS = 'Load a video to find moments worth clipping.';
const NO_VIDEO_TRANSCRIPT = 'The transcript appears here after you load a video.';

let aiProviders = new Map();
let aiAuto = null;
let aiPrefs = readAiPrefs();
let pipelines = [];
let runCountTimer = null;
let runCountRound = 0;

function switchTab(tab) {
  activeTab = tab;
  for (const panel of document.querySelectorAll('[data-tab-panel]')) panel.hidden = panel.dataset.tabPanel !== tab;
  for (const btn of document.querySelectorAll('[data-tab]')) btn.classList.toggle('active', btn.dataset.tab === tab);
}

for (const btn of document.querySelectorAll('[data-tab]')) {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
}

function setStatus(text, loading = false) {
  if (loading) {
    loadStatus.innerHTML = `<span class="spinner"></span> <span>${text}</span>`;
  } else {
    loadStatus.textContent = text;
  }
}

async function loadVideo() {
  const url = urlInput.value.trim();
  if (!url) {
    showLoadError('Paste a YouTube, Twitch or Kick link first.');
    return;
  }

  showLoadError(null);
  setStatus('Fetching video info…', true);
  loadBtn.disabled = true;

  try {
    const info = await getVideoInfo(url);
    // A new link replaces everything the editor held for the last one.
    resetWork();
    forgetWork();
    showMessage(captionsList, 'Fetching subtitles…');
    showMessage(highlightsList, 'Fetching subtitles…');
    videoDuration = info.duration || 100;
    showVideoMeta(info.title, info.platform, info.duration);

    const mode = loadModeSelect.value === 'stream' ? 'stream' : 'download';
    setStatus(mode === 'stream' ? 'Opening stream…' : 'Downloading video…', true);
    const loaded = mode === 'stream' ? await openStream(url) : await downloadVideo(url);
    currentVideo = {
      url, videoId: info.video_id, streamUrl: loaded.stream_url,
      title: info.title, platform: info.platform, duration: info.duration, mode,
    };
    showVideo(currentVideo);
    rememberWork();
    fetchCaptions(url);
  } catch (err) {
    console.error(err);
    setStatus('Link not loaded');
    showLoadError(err.message);
  } finally {
    loadBtn.disabled = false;
  }
}

function showVideoMeta(title, platform, duration) {
  videoMetaHeader.hidden = false;
  document.getElementById('videoTitleDisplay').textContent = title;
  document.getElementById('videoPlatformDisplay').textContent = PLATFORM_LABELS[platform] || platform || '';
  videoDurationDisplay.textContent = duration ? formatTime(duration) : '';
}

function showVideo(video, restoring = false) {
  showVideoMeta(video.title, video.platform, video.duration);
  placeholderView.hidden = true;
  videoPlayer.hidden = false;
  const lost = () => {
    if (currentVideo !== video) return;
    resetWork();
    forgetWork();
    setStatus('Link not loaded');
    showLoadError(video.mode === 'stream'
      ? 'The stream stopped or is no longer open on the server. Choose Load video to open it again.'
      : 'The saved video is no longer on the server. Choose Load video to fetch it again.');
  };
  // A stream played through hls.js reports its own errors, so it always needs the handler.
  videoPlayer.onerror = restoring ? lost : null;
  playSource(videoPlayer, video.streamUrl, lost);

  videoPlayer.onloadedmetadata = () => {
    videoDuration = videoPlayer.duration || videoDuration;
    videoDurationDisplay.textContent = formatTime(videoDuration);
    initSliders();
    if (pendingRange) {
      setRange(pendingRange.start, pendingRange.end);
      videoPlayer.currentTime = Math.min(pendingRange.playhead ?? pendingRange.start, videoDuration);
      pendingRange = null;
    }
    exportBtn.disabled = false;
  };
}

/** Empty the editor: no video, subtitles, moments or range. The link box and the AI settings stay as they are. */
function resetWork() {
  currentVideo = null;
  captionsRound++;
  allCaptions = [];
  captionsState = null;
  captionsError = '';
  highlights = [];
  answeredBy = null;
  pendingRange = null;
  videoPlayer.pause();
  videoPlayer.onloadedmetadata = null;
  videoPlayer.onerror = null;
  stopSource(videoPlayer);
  videoPlayer.hidden = true;
  placeholderView.hidden = false;
  videoMetaHeader.hidden = true;
  exportBtn.disabled = true;
  aiAnswered.hidden = true;
  searchCaptionsInput.value = '';
  videoDuration = 100;
  startSlider.value = 0;
  endSlider.value = 0;
  clipStart = 0;
  clipEnd = 0;
  renderRange();
  showMessage(highlightsList, NO_VIDEO_MOMENTS);
  showMessage(captionsList, NO_VIDEO_TRANSCRIPT);
  updateCaptionControls();
}

function clearEditor() {
  resetWork();
  forgetWork();
  urlInput.value = '';
  aiQueryInput.value = '';
  showLoadError(null);
  setStatus('Ready');
}

function rememberWork() {
  const range = pendingRange || (currentVideo && videoPlayer.readyState >= HTMLMediaElement.HAVE_METADATA
    ? { start: clipStart, end: clipEnd, playhead: videoPlayer.currentTime } : null);
  const work = {
    url: urlInput.value,
    loadMode: loadModeSelect.value,
    video: currentVideo,
    range,
    captions: allCaptions,
    captionsState,
    captionsError,
    highlights,
    answeredBy,
    query: aiQueryInput.value,
    search: searchCaptionsInput.value,
    tab: activeTab,
    sendTo: pendingSendTo ?? sendToSelect.value,
  };
  // Hours of subtitles can be too big to store. Keep the rest and fetch the subtitles again on return.
  if (!saveWork(work)) saveWork({ ...work, captions: [], captionsState: 'loading', highlights: [], answeredBy: null });
}

function restoreWork() {
  if (pageWasReloaded()) {
    forgetWork();
    return;
  }
  const work = readWork();
  if (!work) return;
  urlInput.value = work.url || '';
  loadModeSelect.value = work.loadMode === 'stream' ? 'stream' : 'download';
  aiQueryInput.value = work.query || '';
  pendingSendTo = work.sendTo || null;
  switchTab(work.tab === 'captions' ? 'captions' : 'highlights');
  if (!work.video) {
    if (work.url) setStatus('Choose Load video to continue');
    return;
  }

  currentVideo = work.video;
  videoDuration = currentVideo.duration || 100;
  allCaptions = work.captions || [];
  captionsState = work.captionsState || 'loading';
  captionsError = work.captionsError || '';
  highlights = work.highlights || [];
  answeredBy = work.answeredBy || null;
  pendingRange = work.range || null;
  searchCaptionsInput.value = work.search || '';
  showVideo(currentVideo, true);
  if (captionsState === 'loading') {
    fetchCaptions(currentVideo.url);
  } else {
    showCaptionsState();
    setStatus('Picked up where you left off');
  }
}

function initSliders() {
  startSlider.max = videoDuration;
  endSlider.max = videoDuration;
  startSlider.value = 0;
  endSlider.value = Math.min(30, videoDuration);
  clipStart = 0;
  clipEnd = parseFloat(endSlider.value);
  updateTimelineUI();
}

function setRange(start, end) {
  startSlider.value = start;
  endSlider.value = end;
  // Sliders snap to their 0.1s step; the AI's timestamps must reach export unrounded.
  clipStart = start;
  clipEnd = end;
  renderRange();
}

function updateTimelineUI() {
  clipStart = Math.min(parseFloat(startSlider.value), parseFloat(endSlider.value) - 0.5);
  clipEnd = Math.max(parseFloat(endSlider.value), clipStart + 0.5);

  startSlider.value = clipStart;
  endSlider.value = clipEnd;
  renderRange();
}

function renderRange() {
  startTimeVal.textContent = formatTime(clipStart);
  endTimeVal.textContent = formatTime(clipEnd);
  const dur = Math.max(0, clipEnd - clipStart);
  clipDurationPill.textContent = formatTime(dur);

  const startPercent = (clipStart / videoDuration) * 100;
  const endPercent = (clipEnd / videoDuration) * 100;
  rangeFill.style.left = `${startPercent}%`;
  rangeFill.style.width = `${Math.max(0, endPercent - startPercent)}%`;
}

startSlider.addEventListener('input', () => {
  if (parseFloat(startSlider.value) >= parseFloat(endSlider.value)) {
    startSlider.value = parseFloat(endSlider.value) - 0.5;
  }
  updateTimelineUI();
  videoPlayer.currentTime = clipStart;
});

endSlider.addEventListener('input', () => {
  if (parseFloat(endSlider.value) <= parseFloat(startSlider.value)) {
    endSlider.value = parseFloat(startSlider.value) + 0.5;
  }
  updateTimelineUI();
  videoPlayer.currentTime = clipEnd;
});

videoPlayer.addEventListener('timeupdate', () => {
  if (videoDuration > 0) {
    const percent = (videoPlayer.currentTime / videoDuration) * 100;
    playheadMarker.style.left = `${percent}%`;
  }
});

setStartBtn.addEventListener('click', () => {
  const cur = videoPlayer.currentTime;
  if (cur < clipEnd) {
    startSlider.value = cur;
    updateTimelineUI();
  }
});

setEndBtn.addEventListener('click', () => {
  const cur = videoPlayer.currentTime;
  if (cur > clipStart) {
    endSlider.value = cur;
    updateTimelineUI();
  }
});

stepBackBtn.addEventListener('click', () => {
  videoPlayer.currentTime = Math.max(0, videoPlayer.currentTime - 0.1);
});

stepForwardBtn.addEventListener('click', () => {
  videoPlayer.currentTime = Math.min(videoDuration, videoPlayer.currentTime + 0.1);
});

previewClipBtn.addEventListener('click', () => {
  videoPlayer.currentTime = clipStart;
  videoPlayer.play();
  const checkLoop = () => {
    if (videoPlayer.currentTime >= clipEnd) {
      videoPlayer.pause();
      videoPlayer.removeEventListener('timeupdate', checkLoop);
    }
  };
  videoPlayer.addEventListener('timeupdate', checkLoop);
});

resetRangeBtn.addEventListener('click', () => {
  initSliders();
});

async function fetchCaptions(url = urlInput.value.trim()) {
  if (!url) return;

  const round = ++captionsRound;
  setStatus('Fetching subtitles…', true);
  captionsState = 'loading';
  highlights = [];
  answeredBy = null;
  try {
    const data = await getCaptions(url);
    if (round !== captionsRound) return;
    allCaptions = data.captions || [];
    captionsState = allCaptions.length ? 'ready' : 'none';
    setStatus(allCaptions.length ? `Loaded ${countOf(allCaptions.length, 'subtitle line')}` : 'No subtitles for this video');
  } catch (err) {
    if (round !== captionsRound) return;
    console.warn('Subtitles unavailable:', err);
    allCaptions = [];
    captionsState = 'failed';
    captionsError = err.message;
    setStatus('Subtitles failed');
  }
  showCaptionsState();
  rememberWork();
}

/** Draw the transcript and moments panels from the subtitles and moments the editor holds. */
function showCaptionsState() {
  updateCaptionControls();
  aiAnswered.hidden = true;
  if (captionsState === 'ready') {
    renderCaptions(filteredCaptions());
    if (answeredBy) {
      renderHighlights(highlights);
      aiAnswered.textContent = `Answered by ${providerLabel(answeredBy)}`;
      aiAnswered.hidden = false;
    } else {
      showMessage(highlightsList, `${countOf(allCaptions.length, 'subtitle line')} ready. Say what to look for, or leave it blank, and choose Find moments.`);
    }
  } else if (captionsState === 'none') {
    showMessage(captionsList, NO_SUBTITLES);
    showMessage(highlightsList, NO_SUBTITLES);
  } else if (captionsState === 'failed') {
    showMessage(captionsList, `Subtitles could not be loaded. ${captionsError}`, true);
    showMessage(highlightsList, 'Finding moments needs subtitles. You can still trim the range by hand.');
  }
}

function filteredCaptions() {
  const term = searchCaptionsInput.value.toLowerCase();
  return term ? allCaptions.filter((s) => s.text.toLowerCase().includes(term)) : allCaptions;
}

function renderCaptions(segments) {
  if (!segments || segments.length === 0) {
    showMessage(captionsList, allCaptions.length ? 'No lines match your search.' : NO_SUBTITLES);
    return;
  }
  captionsList.innerHTML = segments.map((s) => `
    <button class="transcript-line" data-seek="${s.start}">
      <span class="transcript-time">${formatTime(s.start)}</span>
      <span>${escapeHtml(s.text)}</span>
    </button>
  `).join('');
}

function seekVideo(time) {
  videoPlayer.currentTime = time;
  videoPlayer.play();
}

captionsList.addEventListener('click', (e) => {
  const line = e.target.closest('[data-seek]');
  if (line) seekVideo(parseFloat(line.dataset.seek));
});

searchCaptionsInput.addEventListener('input', () => renderCaptions(filteredCaptions()));

async function triggerAiHighlights() {
  if (allCaptions.length === 0) return;

  setStatus('Finding moments…', true);
  aiAnalyzeBtn.disabled = true;
  aiAnswered.hidden = true;
  highlightsList.innerHTML = `<p class="empty"><span class="spinner"></span> Reading the transcript…</p>`;

  try {
    await rememberAiSettings();
    const data = await findHighlights(aiRequestBody());
    highlights = data.highlights || [];
    answeredBy = data.provider;
    showCaptionsState();
    setStatus(`Found ${countOf(highlights.length, 'moment')}`);
    rememberWork();
  } catch (err) {
    console.error(err);
    showMessage(highlightsList, err.message, true);
    setStatus('Finding moments failed');
  } finally {
    updateCaptionControls();
  }
}

function aiRequestBody() {
  const body = { captions: allCaptions, provider: aiProviderSelect.value };
  const optional = [
    ['api_key', geminiApiKey, 'aiKeyRow'],
    ['model', aiModelInput, 'aiModelRow'],
    ['base_url', aiBaseUrlInput, 'aiBaseUrlRow'],
    ['query', aiQueryInput, null],
  ];
  for (const [name, input, rowId] of optional) {
    const value = input.value.trim();
    if (value && !(rowId && document.getElementById(rowId).hidden)) body[name] = value;
  }
  return body;
}

function updateCaptionControls() {
  aiAnalyzeBtn.disabled = allCaptions.length === 0;
  const chosen = pendingSendTo ?? sendToSelect.value;
  sendToSelect.innerHTML = '<option value="">Nothing, just download</option>'
    + pipelineOptions(pipelines, allCaptions.length > 0);
  const stillOpen = [...sendToSelect.options].some((o) => o.value === chosen && !o.disabled);
  sendToSelect.value = stillOpen ? chosen : '';
  if (pipelines.length && captionsState !== 'loading') pendingSendTo = null;
}

async function loadPipelines() {
  try {
    pipelines = await getPipelines();
  } catch (err) {
    console.warn(err);
  }
  updateCaptionControls();
}

async function watchRunCount() {
  clearTimeout(runCountTimer);
  const round = ++runCountRound;
  try {
    if (showActiveRunCount(await listRuns()) && round === runCountRound) runCountTimer = setTimeout(watchRunCount, 2000);
  } catch (err) {
    console.warn(err);
  }
}

function providerLabel(id) {
  return aiProviders.get(id)?.label || id;
}

async function loadAiProviders() {
  try {
    applyAiProviders(await listProviders());
  } catch (err) {
    console.warn(err);
    applyAiProviders({ providers: [], auto: null });
    aiProviderHint.textContent = 'AI providers could not be loaded. Automatic is the only choice.';
  }
}

function applyAiProviders(data) {
  const selected = aiProviderSelect.value === 'auto' ? aiPrefs.provider : aiProviderSelect.value;
  aiProviders = new Map(data.providers.map(p => [p.id, p]));
  aiAuto = data.auto;
  if (answeredBy && captionsState === 'ready') aiAnswered.textContent = `Answered by ${providerLabel(answeredBy)}`;
  aiProviderSelect.replaceChildren(new Option(aiAuto ? `Automatic (${providerLabel(aiAuto)})` : 'Automatic', 'auto'));
  for (const p of aiProviders.values()) aiProviderSelect.add(new Option(p.label, p.id));
  if (aiProviders.has(selected)) aiProviderSelect.value = selected;
  renderAiFields();
}

async function rememberAiSettings() {
  const p = aiProviders.get(aiProviderSelect.value);
  if (!p?.accepts_key || !aiRememberInput.checked) return;
  const settings = {
    api_key: geminiApiKey.value.trim(),
    model: aiModelInput.value.trim(),
    base_url: p.uses_base_url ? aiBaseUrlInput.value.trim() : '',
  };
  if (!settings.api_key && !settings.model && !settings.base_url) return;
  applyAiProviders(await saveProviderSettings(p.id, settings));
}

aiForgetBtn.addEventListener('click', async () => {
  const id = aiProviderSelect.value;
  try {
    applyAiProviders(await forgetProviderSettings(id));
    aiPrefs.models[id] = '';
    aiPrefs.baseUrls[id] = '';
    saveAiPrefs(aiPrefs);
    renderAiFields();
    aiProviderHint.textContent = `Forgot the saved ${providerLabel(id)} settings.`;
  } catch (err) {
    aiProviderHint.textContent = err.message;
  }
});

function renderAiFields() {
  const id = aiProviderSelect.value;
  const p = aiProviders.get(id);
  document.getElementById('aiModelRow').hidden = !p;
  document.getElementById('aiKeyRow').hidden = !p?.accepts_key;
  aiRememberRow.hidden = !p?.accepts_key;
  aiForgetBtn.hidden = !p?.has_saved_settings;
  document.getElementById('aiBaseUrlRow').hidden = !p?.uses_base_url;
  geminiApiKey.value = '';
  if (p) {
    geminiApiKey.placeholder = p.saved_key_hint ? `Saved key ${p.saved_key_hint}`
      : p.has_server_key ? 'Uses the server key' : (p.needs_key ? 'API key' : 'API key (optional)');
    aiModelInput.placeholder = p.default_model || 'Model name';
    aiModelInput.value = aiPrefs.models[id] || '';
    aiBaseUrlInput.placeholder = p.default_base_url || 'Base URL';
    aiBaseUrlInput.value = aiPrefs.baseUrls[id] || '';
  }
  renderAiHint();
}

function renderAiHint() {
  const p = aiProviders.get(aiProviderSelect.value);
  if (!p && aiAuto) {
    aiProviderHint.textContent = `Automatic uses ${providerLabel(aiAuto)} with the server's settings.`;
  } else if (p?.accepts_key) {
    aiProviderHint.textContent = aiRememberInput.checked
      ? 'Saved on this computer when you use it, so you only enter it once.'
      : 'Sent with this request only, never saved.';
  } else if (p) {
    aiProviderHint.textContent = '';
  }
}

aiProviderSelect.addEventListener('change', () => {
  aiPrefs.provider = aiProviderSelect.value;
  saveAiPrefs(aiPrefs);
  renderAiFields();
});
aiModelInput.addEventListener('input', () => {
  aiPrefs.models[aiProviderSelect.value] = aiModelInput.value.trim();
  saveAiPrefs(aiPrefs);
});
aiBaseUrlInput.addEventListener('input', () => {
  aiPrefs.baseUrls[aiProviderSelect.value] = aiBaseUrlInput.value.trim();
  saveAiPrefs(aiPrefs);
});
aiRememberInput.addEventListener('change', renderAiHint);
aiQueryInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !aiAnalyzeBtn.disabled) triggerAiHighlights();
});

function renderHighlights(highlights) {
  if (!highlights || highlights.length === 0) {
    showMessage(highlightsList, 'No moments found in this transcript.');
    return;
  }

  highlightsList.innerHTML = highlights.map(h => `
    <article class="highlight">
      <div class="highlight-head">
        <h3 class="highlight-title">${escapeHtml(h.title)}</h3>
        <span class="highlight-score" title="Highlight score">${Math.round(h.score * 100)}%</span>
      </div>
      <p class="highlight-reason">${escapeHtml(h.reason)}</p>
      <div class="highlight-foot">
        <span class="highlight-range">${formatTime(h.start)}–${formatTime(h.end)}</span>
        <button class="btn btn-sm" data-start="${h.start}" data-end="${h.end}">Use this range</button>
      </div>
    </article>
  `).join('');
}

highlightsList.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-start]');
  if (btn) applyClipRange(parseFloat(btn.dataset.start), parseFloat(btn.dataset.end));
});

function applyClipRange(start, end) {
  setRange(start, end);
  videoPlayer.currentTime = start;
  videoPlayer.play();
}

async function exportClip() {
  if (!currentVideo) {
    alert('Load a video before exporting.');
    return;
  }

  const pipeline = pipelines.find((p) => p.id === sendToSelect.value);
  exportBtn.disabled = true;
  const body = {
    video_id: currentVideo.videoId, start_time: clipStart, end_time: clipEnd, captions: allCaptions,
    mode: currentVideo.mode || 'download',
  };
  setStatus(currentVideo.mode === 'stream' ? 'Cutting the clip from the stream…' : 'Exporting clip…', true);

  let clip;
  try {
    clip = await createClip(body);
  } catch (err) {
    console.error(err);
    setStatus('Export failed: ' + err.message);
    alert(err.message);
    exportBtn.disabled = false;
    return;
  }

  try {
    if (pipeline) {
      await sendToPipeline(clip, pipeline);
    } else {
      setStatus('Clip exported');
      downloadClip(clip);
    }
  } finally {
    exportBtn.disabled = false;
  }
}

async function sendToPipeline(clip, pipeline) {
  setStatus(`Sending to ${pipeline.label}…`, true);
  try {
    await rememberAiSettings();
    const { captions, query, ...ai } = aiRequestBody();
    const run = await createRun({ clip_id: clip.clip_id, pipeline: pipeline.id, ai });
    const link = document.createElement('a');
    link.href = trackRunUrl(run.run_id);
    link.textContent = 'Track it';
    loadStatus.replaceChildren(`Sent to ${pipeline.label}.`, link);
    watchRunCount();
  } catch (err) {
    console.error(err);
    setStatus('Clip saved, but not sent');
    alert(`The clip was saved, but it was not sent to ${pipeline.label}. ${err.message}`);
  }
}

function downloadClip(clip) {
  const a = document.createElement('a');
  a.href = clip.download_url;
  a.download = clip.file_name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

function showLoadError(text) {
  loadError.textContent = text || '';
  loadError.hidden = !text;
  urlInput.setAttribute('aria-invalid', text ? 'true' : 'false');
}

loadBtn.addEventListener('click', loadVideo);
fetchCcTopBtn.addEventListener('click', () => fetchCaptions(currentVideo?.url));
clearBtn.addEventListener('click', clearEditor);
// Leaving for another page, or the tab going to the background, saves the work so it is here on return.
window.addEventListener('pagehide', rememberWork);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) rememberWork();
});
aiAnalyzeBtn.addEventListener('click', triggerAiHighlights);
exportBtn.addEventListener('click', exportClip);

urlInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') loadVideo();
});

renderNav();
restoreWork();
loadAiProviders();
loadPipelines();
watchRunCount();
