// Asana: task details pane + list rows (My tasks, project list) + board cards.
//
// In Asana "done" is the completion checkmark, not a status name, so a task's state is
// "Completed" when its checkmark is ticked. Otherwise it's the board column it sits in,
// which also lets a drag into a "Done" section count.
//
// Selectors are from the live app:
//   List row:  .SpreadsheetRow > [role=checkbox].TaskRowCompletionStatus-checkbox, and the task
//              name <textarea id="Pot.<project>_<section>_<task>">.
//   Board:     .BoardCardLayout[data-task-id] with the same checkbox, wrapped in
//              [role=group][aria-label="column <name>"].
//   Task pane: .TaskPane[data-task-id]. Wide, it shows a "Mark complete" button
//              (.TaskCompletionToggleButton[aria-pressed]); narrow, a checkmark (the same checkbox as
//              list rows). The toolbar swaps between the two, keeping the unused one in an
//              [aria-hidden=true][inert] row.
(() => {
  // Task URLs: /1/<workspace>/project/<p>/task/<task>, /1/<workspace>/task/<task>, /0/<project>/<task>.
  const URL_TASK_RE = /\/task\/(\d+)|^\/0\/\d+\/(\d+)(?:\/|$)/;
  const CHECKBOX = '.TaskRowCompletionStatus-checkbox';

  function ticked(el, attr) {
    return el?.getAttribute(attr) === 'true' ? 'Completed' : null;
  }

  function paneState(pane) {
    const visible = (el) => !el.closest('[aria-hidden="true"], [inert]');
    const button = [...pane.querySelectorAll('.TaskCompletionToggleButton')].find(visible);
    if (button) return ticked(button, 'aria-pressed') || 'Incomplete';
    const box = [...pane.querySelectorAll(CHECKBOX)].find(visible);
    return box ? ticked(box, 'aria-checked') || 'Incomplete' : null;
  }

  function paneTaskId(pane) {
    if (pane.dataset.taskId) return pane.dataset.taskId;
    const m = location.pathname.match(URL_TASK_RE);
    return m ? m[1] || m[2] : null;
  }

  function rowTaskId(row) {
    const name = row.querySelector('textarea[id]');
    const m = name?.id.match(/_(\d+)$/);
    return m ? m[1] : null;
  }

  function columnName(card) {
    const label = card.closest('[role="group"][aria-label^="column "]')?.getAttribute('aria-label');
    return label ? label.slice('column '.length) : null;
  }

  GangstaCloser.start({
    site: 'asana',
    watchAttributes: ['class', 'aria-checked', 'aria-pressed', 'aria-label'],
    snapshot() {
      const out = [];
      for (const pane of document.querySelectorAll('.TaskPane')) {
        const id = paneTaskId(pane);
        if (id) out.push({ key: `item:asana:${id}`, state: paneState(pane) });
      }
      for (const row of document.querySelectorAll('.SpreadsheetRow')) {
        const box = row.querySelector(CHECKBOX);
        const id = box && rowTaskId(row);
        if (id) out.push({ key: `card:asana:${id}`, state: ticked(box, 'aria-checked') || 'Incomplete', el: row });
      }
      for (const card of document.querySelectorAll('[data-task-id]:not(.TaskPane)')) {
        const box = card.querySelector(CHECKBOX);
        if (box) out.push({ key: `card:asana:${card.dataset.taskId}`, state: ticked(box, 'aria-checked') || columnName(card) || 'Incomplete', el: card });
      }
      return out;
    },
  });
})();
