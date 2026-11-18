// Run summary (also the webhook response for "run now").
const st = $json;
const z = { pages: 0, received: 0, changed: 0, cursor: null };
return [{
  json: {
    ok: true,
    synthetic_data: true,
    source: st.source || 'mock',
    orders: st.totals.orders || z,
    inventory_levels: st.totals.inventory_levels || z,
    retries: st.retries,
    rate_limited: st.rateLimited,
    seconds: Math.round((Date.now() - new Date(st.startedAt).getTime()) / 100) / 10,
  },
}];
