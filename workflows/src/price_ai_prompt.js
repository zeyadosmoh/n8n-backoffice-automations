// Prompt for the optional AI matching: only the rows the rules could not match, plus the catalog.
// The model must answer with JSON; anything it suggests only reaches the review list, never the prices.
const report = $('Validate + diff').first().json.report;
const catalog = $input.all().map((i) => ({ sku: i.json.sku, name: i.json.name }));
const rows = report.pending_ai.map((r) => ({ row_no: r.row_no, name: r.name }));
const prompt = [
  'You match product names from a supplier price list to SKUs in our catalog.',
  'For each row, return the single best catalog SKU and a confidence between 0 and 1.',
  'Confidence 0.9+ only when the names clearly describe the same product (same type, size and colour/variant).',
  'If no catalog product is the same item, return sku null and a low confidence. Never invent SKUs.',
  'Answer with a JSON array of {"row_no", "sku", "confidence"} and nothing else.',
  '',
  'ROWS_JSON:',
  JSON.stringify(rows),
  'CATALOG_JSON:',
  JSON.stringify(catalog),
].join('\n');
const model = $env.LLM_MODEL || 'gemini-3.5-flash-lite';
const base = String($env.GEMINI_API_BASE || 'https://generativelanguage.googleapis.com/v1beta').replace(/\/+$/, '');
return [{
  json: {
    url: `${base}/models/${encodeURIComponent(model)}:generateContent`,
    importId: report.import_id,
    body: {
      contents: [{ role: 'user', parts: [{ text: prompt }] }],
      generationConfig: {
        temperature: 0,
        responseMimeType: 'application/json',
        responseSchema: {
          type: 'ARRAY',
          items: {
            type: 'OBJECT',
            properties: { row_no: { type: 'INTEGER' }, sku: { type: 'STRING', nullable: true }, confidence: { type: 'NUMBER' } },
            required: ['row_no', 'confidence'],
          },
        },
      },
    },
  },
}];
