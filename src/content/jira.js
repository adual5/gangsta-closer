// Jira Cloud (*.atlassian.net): issue view (full page or board modal) + boards.
(() => {
  const KEY_RE = /\b([A-Z][A-Z0-9_]+-\d+)\b/;

  function currentItemKey() {
    const selected = new URLSearchParams(location.search).get('selectedIssue');
    if (selected) return `item:jira:${selected}`;
    const m = location.pathname.match(/\/browse\/([A-Z][A-Z0-9_]+-\d+)/);
    return m ? `item:jira:${m[1]}` : null;
  }

  function itemState() {
    const el =
      document.querySelector('[data-testid="issue-field-status.ui.status-view.status-button.status-button"]') ||
      document.querySelector('[data-testid="issue.views.issue-base.foundation.status.status-field-wrapper"] button') ||
      document.querySelector('#issue\\.fields\\.status-view\\.status-button') ||
      document.querySelector('[data-testid*="status-view.status-button"], [data-testid*="status-field-wrapper"] button');
    return el ? el.textContent : null;
  }

  function cardKey(card) {
    const id = card.getAttribute('id') || '';
    const m =
      id.match(/^card-(.+)$/) ||
      (card.getAttribute('data-issue-key') || '').match(/(.+)/) ||
      (card.querySelector('a[href*="/browse/"]')?.getAttribute('href') || '').match(KEY_RE) ||
      (card.getAttribute('aria-label') || '').match(KEY_RE) ||
      (card.querySelector('[data-testid*="key"]')?.textContent || '').match(KEY_RE);
    return m ? m[1] : null;
  }

  function columnName(column) {
    const title =
      column.querySelector('[data-testid*="column-name"]') ||
      column.querySelector('[data-testid*="column-title"]') ||
      column.querySelector('[data-testid*="column-header"] h2, [data-testid*="column-header"]');
    return title ? title.textContent.replace(/\d+\s*(of\s*\d+)?\s*$/, '') : null;
  }

  const CARD_SEL = '[data-testid="platform-board-kit.ui.card.card"], [data-testid^="platform-card.ui.card"][id^="card-"], [id^="card-"][data-testid*="card"]';
  const COLUMN_SEL = '[data-testid*="platform-board-kit.ui.column.draggable-column"], [data-component-selector="platform-board-kit.ui.column.draggable-column"], [data-testid*="board.column"]';

  GangstaCloser.start({
    site: 'jira',
    watchAttributes: ['class', 'aria-label', 'data-testid'],
    snapshot() {
      const out = [];
      const key = currentItemKey();
      if (key) out.push({ key, state: itemState() });
      for (const card of document.querySelectorAll(CARD_SEL)) {
        const k = cardKey(card);
        const column = card.closest(COLUMN_SEL);
        if (k && column) out.push({ key: `card:jira:${k}`, state: columnName(column) });
      }
      return out;
    },
  });
})();
