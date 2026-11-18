// Which Africa/Cairo day to export: the webhook may pass {"day": "YYYY-MM-DD"}; the schedule uses today.
const body = ($json.body && typeof $json.body === 'object') ? $json.body : {};
const day = body.day || $now.setZone('Africa/Cairo').toISODate();
if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) throw new Error(`Invalid day "${day}", expected YYYY-MM-DD`);
return [{ json: { day, file: `/files/exports/orders_${day}.xlsx` } }];
