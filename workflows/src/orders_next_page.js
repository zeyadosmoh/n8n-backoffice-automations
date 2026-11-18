// After a page was committed (rows + cursor in one transaction): follow the Link header, move on to the next
// stream, or finish.
const st = $('Retry or apply?').item.json;
const res = $json.result;
const totals = { ...st.totals };
const t = totals[st.stream] || { pages: 0, received: 0, changed: 0 };
totals[st.stream] = {
  pages: t.pages + 1,
  received: t.received + res.received,
  changed: t.changed + res.changed,
  cursor: res.cursor ? res.cursor.cursor_updated_at : null,
};
const next = { ...st, totals, attempt: 0, throttled: 0 };
delete next.payload;
delete next.action;
if (st.nextUrl) return { json: { ...next, url: st.nextUrl, nextUrl: null, done: false } };
if (st.idx + 1 < st.streams.length) {
  const idx = st.idx + 1;
  return { json: { ...next, idx, url: st.streams[idx].firstUrl, nextUrl: null, done: false } };
}
return { json: { ...next, done: true } };
