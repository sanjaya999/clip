/** Clip and run widgets shared by the pages. Each one is rendered by exactly one function here. */
import { escapeHtml, formatTime, setNavCount } from './ui.js';

/**
 * @typedef {import('./api.js').Clip} Clip
 * @typedef {import('./api.js').Pipeline} Pipeline
 * @typedef {import('./api.js').Run} Run
 */

const PIPELINE_PAGE = '/pipeline.html';

/** @param {Run} run */
export const isActive = (run) => run.status === 'queued' || run.status === 'running';

export const trackRunUrl = (runId) => `${PIPELINE_PAGE}?run=${encodeURIComponent(runId)}`;
export const studioUrl = (clipId) => `/studio.html?clip=${encodeURIComponent(clipId)}`;
export const publishUrl = (clipId) => `/publish.html?clip=${encodeURIComponent(clipId)}`;

/** @param {Run[]} runs */
export function showActiveRunCount(runs) {
  const active = runs.filter(isActive).length;
  setNavCount(PIPELINE_PAGE, active, `${active} active ${active === 1 ? 'run' : 'runs'}`);
  return active;
}

/** @param {Clip} clip */
export const clipName = (clip) => clip.file_name.replace(`${clip.clip_id}_`, '');

/** @param {Pipeline[]} pipelines */
export function pipelineOptions(pipelines, hasCaptions) {
  return pipelines.map((p) => {
    const blocked = p.needs_captions && !hasCaptions;
    return `<option value="${escapeHtml(p.id)}"${blocked ? ' disabled' : ''}>`
      + `${escapeHtml(p.label)}${blocked ? ' (needs subtitles)' : ''}</option>`;
  }).join('');
}

/**
 * The "Send to [pipeline] Send" control for one clip. Pages react to it with onSend.
 * @param {Clip} clip
 * @param {Pipeline[]} pipelines
 */
export function sendControl(clip, pipelines) {
  const hasCaptions = clip.captions.length > 0;
  const canSend = pipelines.some((p) => !p.needs_captions || hasCaptions);
  return `
    <div class="send-control" data-clip-id="${escapeHtml(clip.clip_id)}">
      <span class="send-label">Send to</span>
      <select class="field field-sm" aria-label="Pipeline to send ${escapeHtml(clipName(clip))} to">
        ${pipelineOptions(pipelines, hasCaptions)}
      </select>
      <button type="button" class="btn btn-sm" data-action="send"${canSend ? '' : ' disabled'}>Send</button>
      <p class="send-status" role="status"></p>
    </div>`;
}

/**
 * Call handler({clipId, pipeline, button, status}) when a Send button inside root is clicked.
 * @param {HTMLElement} root
 */
export function onSend(root, handler) {
  root.addEventListener('click', (e) => {
    const button = e.target.closest('[data-action="send"]');
    if (!button) return;
    const control = button.closest('.send-control');
    handler({
      clipId: control.dataset.clipId,
      pipeline: control.querySelector('select').value,
      button,
      status: control.querySelector('.send-status'),
    });
  });
}

export function setSendStatus(status, text, isError = false) {
  status.textContent = text;
  status.classList.toggle('is-error', isError);
}

/**
 * @param {Clip} clip
 * @param {Pipeline[]} pipelines
 */
export function clipCard(clip, pipelines) {
  const facts = [`${formatTime(clip.start_time)}–${formatTime(clip.end_time)}`, `${clip.duration} s`, clip.video_id];
  return `
    <article class="clip-card">
      <video class="clip-preview" src="${escapeHtml(clip.preview_url)}" controls preload="metadata"></video>
      <div class="clip-card-body">
        <p class="clip-name">${escapeHtml(clipName(clip))}</p>
        <p class="clip-facts">${facts.map(escapeHtml).join(' · ')}</p>
        ${clip.hook ? `<p class="clip-hook">Hook: ${escapeHtml(clip.hook)}</p>` : ''}
        <div class="clip-actions">
          <a class="btn btn-sm" href="${escapeHtml(clip.download_url)}" download="${escapeHtml(clip.file_name)}">Download</a>
          <a class="btn btn-sm" href="${escapeHtml(studioUrl(clip.clip_id))}">Edit in Studio</a>
          <a class="btn btn-sm" href="${escapeHtml(publishUrl(clip.clip_id))}">Upload</a>
          <button type="button" class="btn btn-sm btn-danger" data-action="delete-clip" data-clip-id="${escapeHtml(clip.clip_id)}">Delete</button>
          ${sendControl(clip, pipelines)}
        </div>
      </div>
    </article>`;
}
