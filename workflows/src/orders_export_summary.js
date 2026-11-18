const d = $('Export day').first().json;
const rows = $('Load order lines').all().filter((i) => i.json.order_id !== undefined).length;
return [{ json: { ok: true, synthetic_data: true, day: d.day, file: d.file.replace('/files/', ''), rows } }];
