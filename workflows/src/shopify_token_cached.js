// Cache hit: hand the cached token back to the calling workflow.
return [{ json: { token: $json.token, cached: true } }];
