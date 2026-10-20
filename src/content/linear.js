// Linear: issue view + board view.
//
// Issue view: the Status property is a <button data-detail-button> whose icon is the
// 14x14 workflow-state glyph (priority/labels/etc. use 16x16 icons).
// Board: cards are <a data-board-item href=".../issue/KEY-1/...">, inside
// <div data-board-column="N"> (N = column index). Column names are <h3>s in a separate
// header row, in the same order; if the counts ever disagree we match by x-position.
(() => {
  // Issues are linked by key (/issue/OPS-15/slug) or, for ones created this session, by UUID.
  const KEY_RE = /\/issue\/([A-Z][A-Z0-9]*-\d+|[0-9a-f]{8}-[0-9a-f-]{27})/i;

  function currentItemKey() {
    const m = location.pathname.match(KEY_RE);
    return m ? `item:ln:${m[1].toUpperCase()}` : null;
  }

  function itemState() {
    const buttons = [...document.querySelectorAll('button[data-detail-button]')];
    const status = buttons.find((b) => b.querySelector('svg[viewBox="0 0 14 14"]')) || buttons[0];
    return status ? status.textContent : null;
  }

  function columnHeaders() {
    return [...document.querySelectorAll('[data-contextual-menu] h3')].filter((h) => !h.closest('a[data-board-item], [data-board-column]'));
  }

  function columnNamer() {
    const headers = columnHeaders();
    const indices = new Set([...document.querySelectorAll('[data-board-column]')].map((c) => c.getAttribute('data-board-column')));
    if (headers.length && headers.length === indices.size) {
      return (column) => headers[Number(column.getAttribute('data-board-column'))]?.textContent;
    }
    const boxes = headers.map((h) => ({ name: h.textContent, rect: (h.closest('[data-contextual-menu]') || h).getBoundingClientRect() }));
    return (column) => {
      const r = column.getBoundingClientRect();
      const cx = r.left + r.width / 2;
      return boxes.find((b) => b.rect.left <= cx && cx <= b.rect.right)?.name;
    };
  }

  GangstaCloser.start({
    site: 'linear',
    watchAttributes: ['class', 'href', 'data-board-column'],
    snapshot() {
      const out = [];
      const key = currentItemKey();
      if (key) out.push({ key, state: itemState() });
      const cards = document.querySelectorAll('[data-board-column] a[data-board-item][href*="/issue/"]');
      if (cards.length) {
        const nameOf = columnNamer();
        for (const a of cards) {
          const m = a.getAttribute('href').match(KEY_RE);
          const column = a.closest('[data-board-column]');
          if (m && column) out.push({ key: `card:ln:${m[1].toUpperCase()}`, state: nameOf(column), el: a });
        }
      }
      return out;
    },
  });
})();
