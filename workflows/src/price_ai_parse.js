// Gemini response -> [{row_no, sku, confidence}]. Any failure becomes an error text: the pending rows are then
// rejected with that reason instead of failing the whole import.
const req = $('Build AI prompt').first().json;
const r = $json;
let matches = [];
let error = null;
try {
  const code = Number(r.statusCode || 0);
  if (code !== 200) throw new Error(code ? `HTTP ${code}` : String((r.error && r.error.message) || 'no response'));
  const text = r.body.candidates[0].content.parts.map((p) => p.text || '').join('');
  const parsed = JSON.parse(text);
  if (!Array.isArray(parsed)) throw new Error('answer is not a JSON array');
  matches = parsed
    .filter((m) => m && Number.isInteger(Number(m.row_no)))
    .map((m) => ({ row_no: Number(m.row_no), sku: m.sku ? String(m.sku) : null, confidence: Math.max(0, Math.min(1, Number(m.confidence) || 0)) }));
} catch (e) {
  error = String(e.message || e).slice(0, 200);
}
const threshold = Number($env.AI_MATCH_MIN_CONFIDENCE || 0.85);
return [{ json: { importId: req.importId, matches, error, threshold } }];
