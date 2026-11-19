/** Every call the pages make to the server. Pages import these and never call fetch themselves. */

/**
 * @typedef {{start: number, end: number, text: string}} CaptionSegment
 * @typedef {{video_id: string, platform: string, title: string, duration: number}} VideoInfo
 * @typedef {{id: string, label: string, accepts_key: boolean, needs_key: boolean, has_server_key: boolean,
 *   saved_key_hint: string|null, has_saved_settings: boolean, default_model: string|null,
 *   uses_base_url: boolean, default_base_url: string|null}} ProviderInfo
 * @typedef {{providers: ProviderInfo[], auto: string|null}} ProviderList
 * @typedef {{start: number, end: number, title: string, reason: string, score: number}} Highlight
 * @typedef {{clip_id: string, video_id: string, start_time: number, end_time: number, duration: number,
 *   file_name: string, download_url: string, preview_url: string, captions: CaptionSegment[],
 *   hook: string|null, made_by_run: string|null, created_at: number}} Clip
 */

export async function sendJson(method, path, body, fallbackMessage) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === null ? undefined : JSON.stringify(body)
  });
  if (!res.ok) throw new Error(await errorDetail(res, fallbackMessage));
  return res.json();
}

async function getJson(path, fallbackMessage) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(await errorDetail(res, fallbackMessage));
  return res.json();
}

// FastAPI sends `detail` as a string for HTTPException but as a list of objects for 422s.
export async function errorDetail(res, fallbackMessage) {
  try {
    const { detail } = await res.json();
    if (typeof detail === 'string' && detail) return detail;
  } catch {}
  return fallbackMessage;
}

/** @returns {Promise<VideoInfo>} */
export const getVideoInfo = (url) => sendJson('POST', '/api/info', { url }, 'Could not read this link');

/** @returns {Promise<{video_id: string, stream_url: string}>} */
export const downloadVideo = (url) => sendJson('POST', '/api/download', { url }, 'Download failed');

/**
 * Open a video for streaming instead of downloading it. `stream_url` is an HLS playlist the server cuts on request.
 * @returns {Promise<{video_id: string, stream_url: string, duration: number}>}
 */
export const openStream = (url) => sendJson('POST', '/api/stream', { url }, 'Could not open the stream');

/** @returns {Promise<{video_id: string, captions: CaptionSegment[]}>} */
export const getCaptions = (url) => sendJson('POST', '/api/captions', { url }, 'Could not fetch subtitles');

/** @returns {Promise<ProviderList>} */
export const listProviders = () => getJson('/api/ai/providers', 'Could not load AI providers');

/** @returns {Promise<ProviderList>} */
export const saveProviderSettings = (id, settings) =>
  sendJson('PUT', `/api/ai/providers/${id}`, settings, 'Could not save these settings');

/** @returns {Promise<ProviderList>} */
export const forgetProviderSettings = (id) =>
  sendJson('DELETE', `/api/ai/providers/${id}`, null, 'Could not forget these settings');

/** @returns {Promise<{provider: string, highlights: Highlight[]}>} */
export const findHighlights = (body) => sendJson('POST', '/api/ai-highlights', body, 'Could not find moments');

/** @returns {Promise<Clip>} */
export const createClip = (body) => sendJson('POST', '/api/clip', body, 'Export failed');

/**
 * @typedef {'queued'|'running'|'done'|'failed'|'skipped'} StepState
 * @typedef {{id: string, label: string, state: StepState, started_at: number|null, finished_at: number|null,
 *   detail: string|null}} StepRecord
 * @typedef {{run_id: string, pipeline: string, input_clip_id: string, steps: StepRecord[],
 *   output_clip_id: string|null, created_at: number, status: 'queued'|'running'|'done'|'failed'}} Run
 * @typedef {{id: string, label: string, description: string, steps: {id: string, label: string}[],
 *   needs_captions: boolean}} Pipeline
 * @typedef {{provider: string, api_key?: string, model?: string, base_url?: string}} AIChoice
 */

/** @returns {Promise<Clip[]>} newest first */
export const listClips = () => getJson('/api/clips', 'Could not load the clips');

/** @param {string} clipId */
export const deleteClip = (clipId) =>
  sendJson('DELETE', `/api/clips/${encodeURIComponent(clipId)}`, null, 'Could not delete this clip');

/** Delete every clip that no active run is reading. @returns {Promise<{status: string, count: number, kept: number}>} */
export const clearClips = () => sendJson('DELETE', '/api/clips', null, 'Could not delete the clips');

/** @returns {Promise<Pipeline[]>} */
export const getPipelines = () => getJson('/api/pipelines', 'Could not load the pipelines');

/**
 * @param {{clip_id: string, pipeline: string, ai: AIChoice}} body
 * @returns {Promise<Run>}
 */
export const createRun = (body) => sendJson('POST', '/api/runs', body, 'Could not send the clip');

/**
 * @param {string} runId
 * @param {{ai?: AIChoice}} [body]
 * @returns {Promise<Run>}
 */
export const rerunRun = (runId, body = {}) =>
  sendJson('POST', `/api/runs/${encodeURIComponent(runId)}/rerun`, body, 'Could not retry the run');

/** @returns {Promise<Run[]>} newest first */
export const listRuns = () => getJson('/api/runs', 'Could not load the runs');

/** @param {string} runId */
export const deleteRun = (runId) =>
  sendJson('DELETE', `/api/runs/${encodeURIComponent(runId)}`, null, 'Could not delete this run');

/** @returns {Promise<{status: string, count: number}>} */
export const clearRuns = () =>
  sendJson('DELETE', '/api/runs', null, 'Could not clear runs');

/** @param {string} clipId @returns {Promise<Clip>} */
export const getClip = (clipId) => getJson(`/api/clips/${encodeURIComponent(clipId)}`, 'Could not load this clip');

/**
 * @typedef {{font: string, size: number, color: string, outline_color: string, outline: number, box: boolean,
 *   box_color: string, box_opacity: number, position: 'bottom'|'middle'|'top', margin: number, bold: boolean,
 *   italic: boolean, uppercase: boolean}} CaptionStyle
 * @typedef {{trim_start: number, trim_end: number|null, aspect: string, fit: 'crop'|'fit', focus_x: number,
 *   focus_y: number, background: string, border: number, border_color: string, brightness: number, contrast: number,
 *   saturation: number, volume: number, fade: boolean, title: string, title_style: CaptionStyle,
 *   captions: CaptionSegment[], caption_style: CaptionStyle}} EditSpec
 */

/** @returns {Promise<{fonts: string[], defaults: EditSpec}>} */
export const getStudioOptions = () => getJson('/api/studio', 'Could not load the Studio settings');

/**
 * The clip's subtitles on its own clock, fetched from its video first if the clip was saved without them.
 * @returns {Promise<{captions: CaptionSegment[], fetched: boolean}>}
 */
export const autoCaptions = (clipId) =>
  sendJson('POST', `/api/clips/${encodeURIComponent(clipId)}/auto-captions`, null, 'Could not get captions');

/** Export the Studio's edit as a new clip. @returns {Promise<Clip>} */
export const renderClip = (clipId, spec) =>
  sendJson('POST', `/api/clips/${encodeURIComponent(clipId)}/render`, spec, 'The export failed');

/**
 * @typedef {{name: string, label: string, secret: boolean, help: string}} AccountField
 * @typedef {{id: string, label: string, help: string, fields: AccountField[], values: Record<string, string|null>,
 *   connected: boolean, uses_oauth: boolean, can_connect: boolean, auto_upload: boolean, default_privacy: string,
 *   default_description: string, privacy_note: string}} Account
 * @typedef {{upload_id: string, clip_id: string, platform: string, title: string, description: string,
 *   tags: string[], privacy: string, status: 'queued'|'uploading'|'done'|'failed', detail: string|null,
 *   remote_url: string|null, automatic: boolean, created_at: number, finished_at: number|null}} Upload
 */

/** @returns {Promise<Account[]>} */
export const listAccounts = () => getJson('/api/accounts', 'Could not load the accounts');

/** Blank credential fields keep their saved values. @returns {Promise<Account[]>} */
export const saveAccount = (platform, settings) =>
  sendJson('PUT', `/api/accounts/${encodeURIComponent(platform)}`, settings, 'Could not save this account');

/** @returns {Promise<Account[]>} */
export const forgetAccount = (platform) =>
  sendJson('DELETE', `/api/accounts/${encodeURIComponent(platform)}`, null, 'Could not forget this account');

/** The page that starts a platform sign-in. Open it in a new tab: it ends on a page of its own. */
export const connectAccountUrl = (platform) => `/api/accounts/${encodeURIComponent(platform)}/connect`;

/** The address a platform sends the user back to after sign-in. Register it in the platform's developer console. */
export const accountCallbackUrl = (platform) =>
  `${location.origin}/api/accounts/${encodeURIComponent(platform)}/callback`;

/**
 * @param {{clip_id: string, platform: string, title: string, description: string, tags: string[], privacy: string}} body
 * @returns {Promise<Upload>}
 */
export const startUpload = (body) => sendJson('POST', '/api/uploads', body, 'Could not start the upload');

/** @returns {Promise<Upload[]>} newest first */
export const listUploads = () => getJson('/api/uploads', 'Could not load the uploads');

/** @returns {Promise<Upload>} */
export const retryUpload = (uploadId) =>
  sendJson('POST', `/api/uploads/${encodeURIComponent(uploadId)}/retry`, null, 'Could not retry the upload');

export const deleteUpload = (uploadId) =>
  sendJson('DELETE', `/api/uploads/${encodeURIComponent(uploadId)}`, null, 'Could not delete this upload');

/** @returns {Promise<{status: string, count: number}>} */
export const clearUploads = () => sendJson('DELETE', '/api/uploads', null, 'Could not clear the uploads');
