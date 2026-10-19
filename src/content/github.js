// GitHub: issue/PR pages (and the issue side panel on lists/projects) + Projects boards.
(() => {
  const ITEM_PATH = /^\/([^/]+)\/([^/]+)\/(issues|pull)\/(\d+)/;

  // The issue being viewed: /owner/repo/issues/12, /owner/repo/pull/12,
  // or a side panel opened via ?issue=owner|repo|12.
  function currentItemKey() {
    const param = new URLSearchParams(location.search).get('issue');
    if (param) {
      const [owner, repo, num] = param.split('|');
      if (owner && repo && num) return `item:gh:${owner}/${repo}#${num}`.toLowerCase();
    }
    const m = location.pathname.match(ITEM_PATH);
    return m ? `item:gh:${m[1]}/${m[2]}#${m[4]}`.toLowerCase() : null;
  }

  function stateOf(el) {
    // Classic PR header: <span class="State" title="Status: Merged">.
    const title = el.getAttribute('title');
    let text = title && /^Status:/i.test(title) ? title.replace(/^Status:\s*/i, '') : el.textContent;
    // Belt and braces: the "not planned" / "duplicate" icons, even if the label is just "Closed".
    const icon = el.querySelector('svg')?.getAttribute('class') || '';
    if (/octicon-skip|octicon-duplicate/.test(icon)) text = 'Closed as not planned';
    return text;
  }

  function itemState() {
    const panel = document.querySelector('[data-testid="issue-viewer-container"], [data-testid="side-panel"]');
    const scope = new URLSearchParams(location.search).get('issue') && panel ? panel : document;
    const el =
      scope.querySelector('[data-testid="header-state"]') ||
      scope.querySelector('.gh-header-meta .State, #partial-discussion-header .State') ||
      scope.querySelector('span.State[title^="Status:"]') ||
      scope.querySelector('[class*="StateLabel-StateLabel"]');
    return el ? stateOf(el) : null;
  }

  GangstaCloser.start({
    site: 'github',
    watchAttributes: ['class', 'title', 'data-board-column'],
    snapshot() {
      const out = [];
      const key = currentItemKey();
      if (key) out.push({ key, state: itemState() });
      for (const card of document.querySelectorAll('[data-board-card-id]')) {
        const column = card.closest('[data-board-column]');
        if (column) out.push({ key: `card:gh:${card.getAttribute('data-board-card-id')}`, state: column.getAttribute('data-board-column'), el: card });
      }
      return out;
    },
  });
})();
