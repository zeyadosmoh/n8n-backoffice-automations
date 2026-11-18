// All extracted rows -> one item for the validation query. Empty objects (blank lines) are dropped.
const f = $('Detect file type').first().json;
const rows = f.fileError ? null : $input.all().map((i) => i.json).filter((r) => r && Object.keys(r).length > 0);
const ai = String($env.AI_MATCH_ENABLED || 'false').toLowerCase() === 'true';
return [{ json: { fileName: f.fileName, rows, ai, fileError: f.fileError || null } }];
