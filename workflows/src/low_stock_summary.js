const e = $('Build email').first().json;
const marked = $input.first().json.marked;
return [{ json: { ok: true, synthetic_data: true, day: e.day, alerted: e.count, skus: e.skus || [], marked_sent: marked ?? 0 } }];
