import {
  accountCallbackUrl, clearUploads, connectAccountUrl, deleteUpload, forgetAccount, listAccounts, listClips,
  listUploads, retryUpload, saveAccount, startUpload,
} from './api.js';
import { escapeHtml, renderNav, showMessage } from './ui.js';
import { clipName } from './widgets.js';

/**
 * @typedef {import('./api.js').Account} Account
 * @typedef {import('./api.js').Upload} Upload
 * @typedef {import('./api.js').Clip} Clip
 */

const POLL_MS = 1500;
const STATUS_LABELS = { queued: 'Queued', uploading: 'Uploading', done: 'Done', failed: 'Failed' };
const PRIVACY_LABELS = { private: 'Private', unlisted: 'Unlisted', public: 'Public' };

const pageError = document.getElementById('pageError');
const uploadForm = document.getElementById('uploadForm');
const uploadClip = document.getElementById('uploadClip');
const uploadPreview = document.getElementById('uploadPreview');
const platformChecks = document.getElementById('platformChecks');
const uploadTitle = document.getElementById('uploadTitle');
const uploadDescription = document.getElementById('uploadDescription');
const uploadTags = document.getElementById('uploadTags');
const uploadPrivacy = document.getElementById('uploadPrivacy');
const privacyNotes = document.getElementById('privacyNotes');
const uploadBtn = document.getElementById('uploadBtn');
const uploadStatus = document.getElementById('uploadStatus');
const uploadsList = document.getElementById('uploadsList');
const clearUploadsBtn = document.getElementById('clearUploadsBtn');
const accountsList = document.getElementById('accountsList');

/** @type {Account[]} */
let accounts = [];
/** @type {Clip[]} */
let clips = [];
let pollTimer = null;
let pollRound = 0;
// The title box follows the chosen clip until the person types their own.
let titleTyped = false;

function showError(text) {
  pageError.textContent = text || '';
  pageError.hidden = !text;
}

function setStatus(el, text, isError = false) {
  el.textContent = text;
  el.classList.toggle('is-error', isError);
}

const clipById = (id) => clips.find((c) => c.clip_id === id);
const accountLabel = (id) => accounts.find((a) => a.id === id)?.label || id;

function defaultTitle(clip) {
  return clip ? (clip.hook || clipName(clip).replace(/\.mp4$/, '').replace(/_/g, ' ')).slice(0, 100) : '';
}

// --------------------------------------------------------------------------------------------- upload form

function fillClips(selected) {
  if (!clips.length) {
    uploadClip.replaceChildren(new Option('No clips yet. Export one in the Editor.', ''));
    return;
  }
  uploadClip.replaceChildren(
    new Option('Pick a clip…', ''),
    ...clips.map((c) => new Option(`${clipName(c)} · ${c.duration} s`, c.clip_id)),
  );
  uploadClip.value = selected && clipById(selected) ? selected : '';
  showClip();
}

function showClip() {
  const clip = clipById(uploadClip.value);
  uploadPreview.hidden = !clip;
  if (clip) {
    if (!uploadPreview.src.endsWith(clip.preview_url)) uploadPreview.src = clip.preview_url;
  } else {
    uploadPreview.removeAttribute('src');
  }
  if (!titleTyped) uploadTitle.value = defaultTitle(clip);
}

function renderPlatformChecks() {
  const checked = new Set([...platformChecks.querySelectorAll('input:checked')].map((i) => i.value));
  const connected = accounts.filter((a) => a.connected);
  if (connected.length === 1) checked.add(connected[0].id);
  platformChecks.innerHTML = accounts.map((a) => `
    <label class="check">
      <input type="checkbox" name="platform" value="${escapeHtml(a.id)}"
        ${a.connected ? '' : 'disabled'}${a.connected && checked.has(a.id) ? ' checked' : ''}>
      ${escapeHtml(a.label)}${a.connected ? '' : ' <span class="muted">(not connected)</span>'}
    </label>`).join('') || '<p class="hint">No platforms.</p>';
  renderPrivacyNotes();
}

function chosenPlatforms() {
  return [...platformChecks.querySelectorAll('input:checked')].map((i) => i.value);
}

function renderPrivacyNotes() {
  privacyNotes.innerHTML = chosenPlatforms()
    .map((id) => accounts.find((a) => a.id === id))
    .filter((a) => a?.privacy_note)
    .map((a) => `<li><strong>${escapeHtml(a.label)}:</strong> ${escapeHtml(a.privacy_note)}</li>`)
    .join('');
}

uploadClip.addEventListener('change', () => {
  history.replaceState(null, '', uploadClip.value ? `?clip=${encodeURIComponent(uploadClip.value)}` : location.pathname);
  showClip();
});
uploadTitle.addEventListener('input', () => {
  titleTyped = uploadTitle.value.trim() !== '';
});
platformChecks.addEventListener('change', renderPrivacyNotes);

uploadForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const clip = clipById(uploadClip.value);
  const platforms = chosenPlatforms();
  if (!clip) {
    setStatus(uploadStatus, 'Pick a clip first.', true);
    return;
  }
  if (!platforms.length) {
    setStatus(uploadStatus, 'Tick a connected platform. Connect one under Accounts.', true);
    return;
  }
  uploadBtn.disabled = true;
  setStatus(uploadStatus, 'Starting…');
  const tags = uploadTags.value.split(',').map((t) => t.trim()).filter(Boolean);
  const failed = [];
  for (const platform of platforms) {
    try {
      await startUpload({
        clip_id: clip.clip_id, platform, title: uploadTitle.value.trim(), description: uploadDescription.value,
        tags, privacy: uploadPrivacy.value,
      });
    } catch (err) {
      failed.push(`${accountLabel(platform)}: ${err.message}`);
    }
  }
  setStatus(uploadStatus, failed.length ? failed.join(' ') : 'Started. Follow it below.', failed.length > 0);
  uploadBtn.disabled = false;
  refreshUploads();
});

// ------------------------------------------------------------------------------------------------- uploads

/** @param {Upload} u */
function uploadHtml(u) {
  const clip = clipById(u.clip_id);
  const active = u.status === 'queued' || u.status === 'uploading';
  const spinner = active ? '<span class="spinner" aria-hidden="true"></span> ' : '';
  const facts = [
    accountLabel(u.platform), PRIVACY_LABELS[u.privacy] || u.privacy,
    clip ? clipName(clip) : 'deleted clip', u.automatic ? 'automatic' : null,
    new Date(u.created_at * 1000).toLocaleString(),
  ].filter(Boolean);
  return `
    <article class="upload-card">
      <header class="upload-head">
        <h3 class="upload-title">${escapeHtml(u.title)}</h3>
        <span class="upload-state" data-status="${u.status}">${spinner}${STATUS_LABELS[u.status]}</span>
      </header>
      <p class="upload-facts">${facts.map(escapeHtml).join(' · ')}</p>
      ${u.detail ? `<p class="upload-detail" data-status="${u.status}">${escapeHtml(u.detail)}</p>` : ''}
      ${active ? '' : `
        <div class="upload-actions">
          ${u.remote_url ? `<a class="btn btn-sm" href="${escapeHtml(u.remote_url)}" target="_blank" rel="noopener">Open on ${escapeHtml(accountLabel(u.platform))}</a>` : ''}
          <button type="button" class="btn btn-sm" data-action="retry" data-upload-id="${escapeHtml(u.upload_id)}">${u.status === 'failed' ? 'Retry' : 'Upload again'}</button>
          <button type="button" class="btn btn-sm btn-icon" data-action="delete" data-upload-id="${escapeHtml(u.upload_id)}" title="Remove from this list" aria-label="Remove from this list">✕</button>
        </div>`}
    </article>`;
}

async function refreshUploads() {
  clearTimeout(pollTimer);
  const round = ++pollRound;
  try {
    const list = await listUploads();
    if (list.some((u) => !clipById(u.clip_id))) clips = await listClips();
    clearUploadsBtn.hidden = !list.some((u) => u.status === 'done' || u.status === 'failed');
    if (list.length) uploadsList.innerHTML = list.map(uploadHtml).join('');
    else showMessage(uploadsList, 'Nothing uploaded yet.');
    if (list.some((u) => u.status === 'queued' || u.status === 'uploading') && round === pollRound) {
      pollTimer = setTimeout(refreshUploads, POLL_MS);
    }
  } catch (err) {
    showError(`${err.message}. Trying again…`);
    if (round === pollRound) pollTimer = setTimeout(refreshUploads, 3 * POLL_MS);
  }
}

uploadsList.addEventListener('click', async (e) => {
  const button = e.target.closest('[data-action]');
  if (!button) return;
  button.disabled = true;
  try {
    if (button.dataset.action === 'retry') await retryUpload(button.dataset.uploadId);
    else await deleteUpload(button.dataset.uploadId);
    showError(null);
  } catch (err) {
    showError(err.message);
  }
  refreshUploads();
});

clearUploadsBtn.addEventListener('click', async () => {
  try {
    await clearUploads();
  } catch (err) {
    showError(err.message);
  }
  refreshUploads();
});

// ------------------------------------------------------------------------------------------------ accounts

/** @param {Account} a */
function accountHtml(a) {
  const fields = a.fields.map((f) => {
    const saved = a.values[f.name];
    const value = f.secret ? '' : saved || '';
    const placeholder = f.secret ? (saved ? `Saved ${saved}` : '') : '';
    return `
      <label class="control">
        <span class="field-label">${escapeHtml(f.label)}</span>
        <input type="${f.secret ? 'password' : 'text'}" class="field field-sm" name="${escapeHtml(f.name)}"
          value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}" autocomplete="off" spellcheck="false">
        ${f.help ? `<span class="hint">${escapeHtml(f.help)}</span>` : ''}
      </label>`;
  }).join('');
  const privacy = Object.entries(PRIVACY_LABELS).map(([value, label]) =>
    `<option value="${value}"${a.default_privacy === value ? ' selected' : ''}>${label}</option>`).join('');
  return `
    <article class="account-card" data-platform="${escapeHtml(a.id)}">
      <header class="account-head">
        <h3 class="account-title">${escapeHtml(a.label)}</h3>
        <span class="account-state" data-connected="${a.connected}">${a.connected ? 'Connected' : 'Not connected'}</span>
      </header>
      <p class="hint">${escapeHtml(a.help)}</p>
      <form class="account-form">
        ${fields}
        ${a.uses_oauth ? `
          <div class="callback-url">
            <span class="field-label">Redirect URI to register</span>
            <code>${escapeHtml(accountCallbackUrl(a.id))}</code>
          </div>` : ''}
        <label class="check"><input type="checkbox" name="auto_upload"${a.auto_upload ? ' checked' : ''}>
          Upload every finished clip automatically</label>
        <span class="hint">Finished clips are Studio exports and pipeline results. They upload with the settings below.</span>
        <label class="control">
          <span class="field-label">Who can see automatic uploads</span>
          <select class="field field-sm" name="default_privacy">${privacy}</select>
        </label>
        <label class="control">
          <span class="field-label">Description for automatic uploads</span>
          <textarea class="field field-sm" name="default_description" rows="2" maxlength="4000">${escapeHtml(a.default_description)}</textarea>
        </label>
        ${a.privacy_note ? `<p class="hint">${escapeHtml(a.privacy_note)}</p>` : ''}
        <div class="account-actions">
          <button type="submit" class="btn btn-sm btn-primary">Save</button>
          ${a.uses_oauth ? `<button type="button" class="btn btn-sm" data-action="connect"${a.can_connect ? '' : ' disabled title="Save the app details first"'}>${a.connected ? 'Connect again' : 'Connect'}</button>` : ''}
          <button type="button" class="btn btn-sm btn-danger" data-action="forget">Forget</button>
        </div>
        <p class="send-status" role="status"></p>
      </form>
    </article>`;
}

function applyAccounts(list) {
  accounts = list;
  accountsList.innerHTML = accounts.map(accountHtml).join('');
  renderPlatformChecks();
}

async function refreshAccounts() {
  try {
    applyAccounts(await listAccounts());
  } catch (err) {
    showMessage(accountsList, err.message, true);
  }
}

accountsList.addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target.closest('.account-form');
  const card = form.closest('.account-card');
  const status = form.querySelector('.send-status');
  const data = new FormData(form);
  const account = accounts.find((a) => a.id === card.dataset.platform);
  const credentials = {};
  for (const f of account.fields) credentials[f.name] = String(data.get(f.name) || '');
  setStatus(status, 'Saving…');
  try {
    applyAccounts(await saveAccount(account.id, {
      credentials,
      auto_upload: data.get('auto_upload') === 'on',
      default_privacy: data.get('default_privacy'),
      default_description: String(data.get('default_description') || ''),
    }));
    const saved = accountsList.querySelector(`[data-platform="${account.id}"] .send-status`);
    const now = accounts.find((a) => a.id === account.id);
    setStatus(saved, now.connected ? 'Saved.' : now.can_connect ? 'Saved. Now choose Connect.' : 'Saved.');
  } catch (err) {
    setStatus(status, err.message, true);
  }
});

accountsList.addEventListener('click', async (e) => {
  const button = e.target.closest('[data-action]');
  if (!button) return;
  const card = button.closest('.account-card');
  const status = card.querySelector('.send-status');
  const id = card.dataset.platform;
  if (button.dataset.action === 'connect') {
    // The sign-in ends on a page of its own; this page picks up the result when it is shown again.
    window.open(connectAccountUrl(id), '_blank', 'noopener');
    setStatus(status, 'Finish signing in in the new tab, then come back here.');
    return;
  }
  if (button.dataset.action === 'forget') {
    if (!confirm(`Forget the saved ${accountLabel(id)} details? Uploads to it stop until you connect again.`)) return;
    try {
      applyAccounts(await forgetAccount(id));
    } catch (err) {
      setStatus(status, err.message, true);
    }
  }
});

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  refreshAccounts();
  refreshUploads();
});

async function start() {
  renderNav();
  try {
    [clips] = await Promise.all([listClips(), refreshAccounts()]);
  } catch (err) {
    showError(err.message);
  }
  const wanted = new URLSearchParams(location.search).get('clip');
  fillClips(wanted);
  if (wanted && !clipById(wanted)) showError('That clip is no longer in the library.');
  refreshUploads();
}

start();
