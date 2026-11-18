// Decide what to do with one HTTP response: apply the page, or back off and retry.
// 429 -> wait Retry-After. 5xx / network error -> exponential backoff (1, 2, 4, 8, 16 s + jitter).
// After MAX_RETRIES failed attempts (5xx / network) or MAX_429 rate-limit answers in a row for the same page,
// the run fails and the error workflow sends an email. 429s have their own budget: they are expected, not errors.
const MAX_RETRIES = 5;
const MAX_429 = 20;
const st = $('Next request').item.json;
const r = $json;
const stream = st.streams[st.idx].stream;
const code = Number(r.statusCode || 0);

if (code === 200) {
  const link = (r.headers && r.headers.link) || '';
  const m = link.match(/<([^>]+)>;\s*rel="next"/);
  return { json: { ...st, action: 'apply', stream, payload: r.body, nextUrl: m ? m[1] : null, throttled: 0 } };
}

const detail = code ? `HTTP ${code}` : `network error: ${(r.error && (r.error.message || r.error)) || 'no response'}`;
if (code && code !== 429 && code < 500) {
  throw new Error(`Store API (${stream}) refused the request: ${detail} ${JSON.stringify(r.body || '').slice(0, 200)}`);
}
const limited = code === 429;
const attempt = st.attempt + (limited ? 0 : 1);
const throttled = (st.throttled || 0) + (limited ? 1 : 0);
if (throttled > MAX_429) {
  throw new Error(`Store API (${stream}) rate limit did not clear after ${MAX_429} waits. ` +
    'Nothing was written for this page and the cursor was not moved.');
}
if (attempt > MAX_RETRIES) {
  throw new Error(`Store API (${stream}) still failing after ${MAX_RETRIES} retries: ${detail}. ` +
    'Nothing was written for this page and the cursor was not moved.');
}
const retryAfter = Number((r.headers && r.headers['retry-after']) || 0);
const wait = limited ? Math.min(60, retryAfter || 2) : Math.min(30, 2 ** (attempt - 1)) + Math.random() * 0.5;
return {
  json: {
    ...st, action: 'retry', stream, attempt, throttled,
    retries: st.retries + (limited ? 0 : 1), rateLimited: st.rateLimited + (limited ? 1 : 0),
    waitSeconds: Math.round(wait * 10) / 10, lastError: detail,
  },
};
