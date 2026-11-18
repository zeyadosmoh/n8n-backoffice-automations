// Shopify answered 401 to a cached token (revoked, app reinstalled, ...). The cache row was just deleted, so the
// next run fetches a fresh token. Fail this run so the error workflow sends an email.
throw new Error('Shopify rejected the cached access token (HTTP 401). The cached token was deleted; the next run ' +
  'fetches a new one. If this repeats, check the app installation and its scopes. Nothing was written for this ' +
  'page and the cursor was not moved.');
