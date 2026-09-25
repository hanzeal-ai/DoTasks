// One snapshot in flight; reconnects and explicit actions re-read authoritative state.
export function subscribeTeamBoard({ url, load, onData, onError, document = globalThis.document,
  EventSource = globalThis.EventSource, setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout }) {
  let closed = false, source = null, timer = null, pending = null, dirty = false;
  const schedule = () => {
    clearTimeout(timer);
    if (!closed && !document.hidden) timer = setTimeout(() => { void refresh(); }, 30_000);
  };
  async function refresh() {
    if (closed) return;
    dirty = true;
    if (pending) return pending;
    pending = (async () => {
      while (dirty && !closed) {
        dirty = false;
        try { const board = await load(); if (!closed) onData(board); }
        catch (error) { if (!closed) onError(error); }
      }
    })();
    try { await pending; }
    finally { pending = null; if (!source || source.readyState !== 1) schedule(); }
  }
  function connect() {
    clearTimeout(timer);
    if (closed || document.hidden) return;
    if (EventSource) {
      source = new EventSource(url);
      source.addEventListener('board_changed', () => { void refresh(); });
      source.addEventListener('error', schedule);
      source.addEventListener('open', () => { clearTimeout(timer); });
    } else void refresh();
  }
  function visibility() {
    source?.close(); source = null; clearTimeout(timer);
    if (!document.hidden) { void refresh(); connect(); }
  }
  document.addEventListener('visibilitychange', visibility);
  // Fetch immediately, even if a proxy delays opening the event stream.
  if (!document.hidden) { void refresh(); connect(); }
  return { refresh, close() { closed = true; dirty = false; clearTimeout(timer); source?.close(); document.removeEventListener('visibilitychange', visibility); } };
}
