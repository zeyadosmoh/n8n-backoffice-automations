// One GraphQL Admin API answer -> what to do next, and the page mapped into the same staging shape as the mock
// store's REST pages (so p8_sync_orders_page / p8_sync_inventory_page, the cursor and idempotency stay unchanged).
//   apply  : rows ready to commit (an orders page, or all inventory pages collected)
//   next   : inventory page collected, fetch the next one
//   retry  : THROTTLED / 429 -> wait until the bucket has the points; 5xx / network -> backoff 1, 2, 4, 8, 16 s
//   reauth : 401, the cached token was rejected -> forget it and fail (the next run fetches a new one)
// Every success also reads extensions.cost.throttleStatus and asks for a pause when points run low.
const MAX_RETRIES = 5;
const MAX_THROTTLED = 20;
const st = $('Next GraphQL request').item.json;
const s = st.streams[st.idx];
const r = $json;
const code = Number(r.statusCode || 0);
const body = (r.body && typeof r.body === 'object') ? r.body : {};
const errors = Array.isArray(body.errors) ? body.errors : [];
const cost = (body.extensions && body.extensions.cost) || null;

// Seconds until the bucket holds `need` points again (+0.5 s margin), 0 if it already does.
const waitForPoints = (need) => {
  const ts = cost && cost.throttleStatus;
  if (!ts || !ts.restoreRate) return 0;
  const missing = need - ts.currentlyAvailable;
  return missing > 0 ? Math.round((missing / ts.restoreRate + 0.5) * 10) / 10 : 0;
};
const numId = (gid) => Number(String(gid).split('?')[0].split('/').pop());
const lower = (v) => (v == null ? null : String(v).toLowerCase());
const FULFILLMENT = { FULFILLED: 'fulfilled', UNFULFILLED: null, PARTIALLY_FULFILLED: 'partial' };

const mapOrder = (o) => {
  if (o.lineItems.pageInfo.hasNextPage) {
    throw new Error(`Shopify order ${o.name} has more than 50 line items; raise lineItems(first:) in ` +
      '"Start Shopify streams". Nothing was written for this page and the cursor was not moved.');
  }
  return {
    id: numId(o.id),
    name: o.name,
    created_at: o.processedAt || o.createdAt,   // the order date (backdated imports keep theirs)
    updated_at: o.updatedAt,
    cancelled_at: o.cancelledAt,
    financial_status: lower(o.displayFinancialStatus),
    fulfillment_status: o.displayFulfillmentStatus in FULFILLMENT ? FULFILLMENT[o.displayFulfillmentStatus]
      : lower(o.displayFulfillmentStatus),
    currency: o.totalPriceSet.shopMoney.currencyCode,
    total_price: o.totalPriceSet.shopMoney.amount,
    line_items: o.lineItems.nodes.map((li) => ({
      id: numId(li.id),
      sku: li.sku || '',
      title: li.name,
      quantity: li.quantity,
      price: li.originalUnitPriceSet.shopMoney.amount,
    })),
  };
};
const mapLevel = (l) => {
  const q = (l.quantities || []).find((x) => x.name === 'available');
  return { inventory_item_id: numId(l.item.id), sku: l.item.sku || '', available: q ? q.quantity : 0,
           updated_at: l.updatedAt };
};

if (code === 200 && body.data && errors.length === 0) {
  const pause = waitForPoints(cost ? cost.requestedQueryCost : 0);
  const base = { ...st, stream: s.stream, attempt: 0, throttled: 0, pauseSeconds: pause };
  if (s.stream === 'orders') {
    const conn = body.data.orders;
    return { json: { ...base, action: 'apply', payload: { orders: conn.nodes.map(mapOrder) },
                     nextAfter: conn.pageInfo.hasNextPage ? conn.pageInfo.endCursor : null } };
  }
  if (!body.data.location) throw new Error('Shopify store has no primary location (read_locations scope?)');
  const conn = body.data.location.inventoryLevels;
  const buffer = st.buffer.concat(conn.nodes.map(mapLevel));
  if (conn.pageInfo.hasNextPage) {
    return { json: { ...base, action: 'next', buffer, nextAfter: conn.pageInfo.endCursor } };
  }
  return { json: { ...base, action: 'apply', buffer: [], payload: { inventory_levels: buffer }, nextAfter: null } };
}

if (code === 401) {
  return { json: { ...st, stream: s.stream, action: 'reauth' } };
}
const throttledError = errors.some((e) => e.extensions && e.extensions.code === 'THROTTLED');
if (code === 429 || (code === 200 && throttledError)) {
  const throttled = st.throttled + 1;
  if (throttled > MAX_THROTTLED) {
    throw new Error(`Shopify (${s.stream}) still THROTTLED after ${MAX_THROTTLED} waits. ` +
      'Nothing was written for this page and the cursor was not moved.');
  }
  const retryAfter = Number((r.headers && r.headers['retry-after']) || 0);
  const need = cost ? cost.requestedQueryCost : 0;
  const wait = Math.min(60, Math.max(1, retryAfter, waitForPoints(need)));
  return { json: { ...st, stream: s.stream, action: 'retry', throttled, rateLimited: st.rateLimited + 1,
                   waitSeconds: wait, lastError: 'THROTTLED' } };
}
if (!code || code >= 500) {
  const attempt = st.attempt + 1;
  const detail = code ? `HTTP ${code}` : `network error: ${(r.error && (r.error.code || r.error.message)) || 'no response'}`;
  if (attempt > MAX_RETRIES) {
    throw new Error(`Shopify (${s.stream}) still failing after ${MAX_RETRIES} retries: ${detail}. ` +
      'Nothing was written for this page and the cursor was not moved.');
  }
  const wait = Math.min(30, 2 ** (attempt - 1)) + Math.random() * 0.5;
  return { json: { ...st, stream: s.stream, action: 'retry', attempt, retries: st.retries + 1,
                   waitSeconds: Math.round(wait * 10) / 10, lastError: detail } };
}
// Anything else is not transient: GraphQL errors (bad query, missing scope), 4xx.
const msg = errors.length ? errors.map((e) => e.message).join('; ')
  : (typeof body.errors === 'string' ? body.errors : JSON.stringify(body).slice(0, 200));
throw new Error(`Shopify (${s.stream}) refused the request: HTTP ${code}: ${String(msg).slice(0, 300)}`);
