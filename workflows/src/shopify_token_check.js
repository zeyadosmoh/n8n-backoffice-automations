// Token endpoint answer -> {token, expiresIn}. Errors carry the HTTP status and Shopify's short error code only:
// never the response body as a whole, the request, or anything that could contain the client secret or a token.
// Shopify documents a form-encoded body; this request sends JSON (n8n's Custom Auth credential can only add JSON
// body fields). Shopify accepts it today; if that ever stops, this fails loudly and the failure email fires.
const r = $json;
const code = Number(r.statusCode || 0);
const body = (r.body && typeof r.body === 'object') ? r.body : {};
if (code === 200 && typeof body.access_token === 'string' && body.access_token !== '') {
  const expiresIn = Number(body.expires_in);
  if (!Number.isFinite(expiresIn) || expiresIn <= 0) {
    throw new Error(`Shopify token endpoint returned no usable expires_in (got ${JSON.stringify(body.expires_in)})`);
  }
  return [{ json: { token: body.access_token, expiresIn } }];
}
const reason = typeof body.error === 'string' ? body.error.replace(/[^A-Za-z0-9_ .:-]/g, '').slice(0, 80) : '';
const detail = code ? `HTTP ${code}${reason ? ` (${reason})` : ''}`
  : `network error: ${String((r.error && (r.error.code || r.error.name)) || 'no response').slice(0, 40)}`;
throw new Error(`Shopify token request failed: ${detail}. Check SHOPIFY_SHOP, the app install and the ` +
  '"P8 Shopify app" credential. If the endpoint rejects the JSON body, see README "Real Shopify store".');
