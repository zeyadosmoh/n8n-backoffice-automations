// Start of a Shopify sync run (STORE_SOURCE=shopify): one state object that travels through the GraphQL page loop.
// Orders first (sortKey UPDATED_AT, committed page by page), then inventory levels at the primary location
// (no sort key in the API, so all pages are collected and applied in one transaction at the end).
// The token is NOT copied into this state; the GraphQL node reads it from "Get Shopify token".
const c = $('Load cursors').first().json;
const since = (ts) => (ts ? `updated_at:>='${new Date(ts).toISOString()}'` : null);
const ORDERS = `query P8Orders($first: Int!, $after: String, $q: String) {
  orders(first: $first, after: $after, sortKey: UPDATED_AT, query: $q) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id name createdAt processedAt updatedAt cancelledAt displayFinancialStatus displayFulfillmentStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 50) {
        pageInfo { hasNextPage }
        nodes { id sku name quantity originalUnitPriceSet { shopMoney { amount } } }
      }
    }
  }
}`;
const INVENTORY = `query P8Inventory($first: Int!, $after: String, $q: String) {
  location {
    id
    inventoryLevels(first: $first, after: $after, query: $q) {
      pageInfo { hasNextPage endCursor }
      nodes { updatedAt quantities(names: ["available"]) { name quantity } item { id sku } }
    }
  }
}`;
const streams = [
  { stream: 'orders', query: ORDERS, first: 50, q: since(c.orders_since), since: c.orders_since || null },
  { stream: 'inventory_levels', query: INVENTORY, first: 250, q: since(c.inventory_since),
    since: c.inventory_since || null },
];
return [{
  json: {
    source: 'shopify', streams, idx: 0, after: null, buffer: [],
    attempt: 0, throttled: 0, retries: 0, rateLimited: 0, totals: {}, startedAt: new Date().toISOString(),
  },
}];
