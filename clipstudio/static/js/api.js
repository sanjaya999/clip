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
