/** One row per page. Every page renders its nav from this list. */
export const PAGES = [
  { label: 'Editor', href: '/' },
  { label: 'Pipeline', href: '/pipeline.html' },
  { label: 'Studio', href: '/studio.html' },
  { label: 'Publish', href: '/publish.html' },
];

export function renderNav() {
  const here = location.pathname.replace(/\/index\.html$/, '/');
  for (const mount of document.querySelectorAll('[data-nav]')) {
    mount.replaceChildren(...PAGES.map(({ label, href }) => {
      const link = document.createElement('a');
      link.href = href;
      link.textContent = label;
      if (href === here) link.setAttribute('aria-current', 'page');
      return link;
    }));
  }
}

export function setNavCount(href, count, title) {
  for (const link of document.querySelectorAll(`[data-nav] a[href="${href}"]`)) {
    let badge = link.querySelector('.nav-count');
    if (!count) {
      badge?.remove();
      continue;
    }
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'nav-count';
      link.append(badge);
    }
    badge.textContent = count;
    badge.title = title;
  }
}

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** Safe in text and in quoted attribute values. */
export function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

export function formatTime(seconds) {
  if (isNaN(seconds) || seconds < 0) seconds = 0;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const ms = Math.floor((seconds % 1) * 10);
  const pad = (n) => String(n).padStart(2, '0');
  if (h > 0) {
    return `${pad(h)}:${pad(m)}:${pad(s)}.${ms}`;
  }
  return `${pad(m)}:${pad(s)}.${ms}`;
}

export function countOf(n, noun) {
  return `${n} ${noun}${n === 1 ? '' : 's'}`;
}

export function showMessage(container, text, isError = false) {
  const p = document.createElement('p');
  p.className = isError ? 'empty empty-error' : 'empty';
  p.textContent = text;
  container.replaceChildren(p);
}
