// After a page was committed (orders page, or all inventory pages in one transaction) or collected (inventory):
// follow endCursor, move on to the next stream, or finish. Passes on any pause the throttle status asked for.
const st = $('Route Shopify page').item.json;
const totals = { ...st.totals };
const t = totals[st.stream] || { pages: 0, received: 0, changed: 0, cursor: null };
if (st.action === 'apply') {
  const res = $json.result;
  totals[st.stream] = {
    pages: t.pages + 1, received: t.received + res.received, changed: t.changed + res.changed,
    cursor: res.cursor ? res.cursor.cursor_updated_at : t.cursor,
  };
} else {
  totals[st.stream] = { ...t, pages: t.pages + 1 };
}
const next = { ...st, totals, attempt: 0, throttled: 0, waitSeconds: st.pauseSeconds || 0 };
for (const k of ['payload', 'action', 'nextAfter', 'pauseSeconds', 'lastError']) delete next[k];
if (st.nextAfter) return { json: { ...next, after: st.nextAfter, done: false } };
if (st.idx + 1 < st.streams.length) {
  return { json: { ...next, idx: st.idx + 1, after: null, buffer: [], done: false } };
}
return { json: { ...next, done: true } };
