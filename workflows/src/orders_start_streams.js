// Start of a sync run: one state object that travels through the page loop.
// Streams are synced one after the other: orders, then inventory levels.
const base = $env.STORE_API_BASE;
const c = $input.first().json;
const streams = [
  { stream: 'orders', path: 'orders.json', since: c.orders_since, extra: { status: 'any' } },
  { stream: 'inventory_levels', path: 'inventory_levels.json', since: c.inventory_since, extra: {} },
].map((s) => {
  // (URLSearchParams is not available in the Code-node sandbox)
  const q = { limit: '250', ...s.extra };
  if (s.since) q.updated_at_min = new Date(s.since).toISOString();
  const qs = Object.entries(q).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');
  return { stream: s.stream, since: s.since || null, firstUrl: `${base}/${s.path}?${qs}` };
});
return [{
  json: {
    streams, idx: 0, url: streams[0].firstUrl, nextUrl: null,
    attempt: 0, retries: 0, rateLimited: 0, totals: {}, startedAt: new Date().toISOString(),
  },
}];
