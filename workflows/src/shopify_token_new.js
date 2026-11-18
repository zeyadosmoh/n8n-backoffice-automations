// New token stored in Postgres: hand it back to the calling workflow (with its expiry, never logged anywhere).
const t = $('Check token response').first().json;
return [{ json: { token: t.token, cached: false, expiresAt: $json.expires_at } }];
