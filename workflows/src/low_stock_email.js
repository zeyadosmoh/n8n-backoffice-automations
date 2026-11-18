// One HTML email for all SKUs claimed today, sorted by days of stock left (fewest first, "no recent sales" last).
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const rows = $input.all().map((i) => i.json).filter((r) => r.sku);
const day = $now.setZone('Africa/Cairo').toISODate();
if (rows.length === 0) {
  return [{ json: { send: false, count: 0, day, ids: [] } }];
}
const fmtDays = (d) => (d === null || d === undefined ? 'no recent sales' : Number(d) === 0 ? 'out of stock' : `${Number(d).toFixed(1)} days`);
const color = (d) => (d === null || d === undefined ? '#57606a' : Number(d) < 3 ? '#b42318' : Number(d) < 7 ? '#b54708' : '#1f2328');
const tr = rows.map((r) => `
    <tr>
      <td style="padding:6px 10px;border-bottom:1px solid #eaeef2;font-family:monospace">${esc(r.sku)}</td>
      <td style="padding:6px 10px;border-bottom:1px solid #eaeef2">${esc(r.name)}</td>
      <td style="padding:6px 10px;border-bottom:1px solid #eaeef2;text-align:right">${esc(r.stock)}</td>
      <td style="padding:6px 10px;border-bottom:1px solid #eaeef2;text-align:right">${esc(r.reorder_point)}</td>
      <td style="padding:6px 10px;border-bottom:1px solid #eaeef2;text-align:right">${esc(r.units_14d)}</td>
      <td style="padding:6px 10px;border-bottom:1px solid #eaeef2;text-align:right;font-weight:bold;color:${color(r.days_left)}">${fmtDays(r.days_left)}</td>
    </tr>`).join('');
const html = `
<div style="font-family:Arial,sans-serif;font-size:14px;color:#1f2328;max-width:760px">
  <h2 style="margin:0 0 6px">Low stock: ${rows.length} SKU${rows.length === 1 ? '' : 's'} below reorder point</h2>
  <p style="margin:0 0 14px;color:#57606a">${esc(day)} · sorted by days of stock left (stock ÷ average daily sales over the last 14 days)</p>
  <table style="border-collapse:collapse;width:100%">
    <thead><tr style="background:#f6f8fa;text-align:left">
      <th style="padding:6px 10px">SKU</th><th style="padding:6px 10px">Product</th>
      <th style="padding:6px 10px;text-align:right">Stock</th><th style="padding:6px 10px;text-align:right">Reorder point</th>
      <th style="padding:6px 10px;text-align:right">Sold (14 d)</th><th style="padding:6px 10px;text-align:right">Days left</th>
    </tr></thead>
    <tbody>${tr}
    </tbody>
  </table>
  <p style="color:#57606a;font-size:12px;margin-top:16px">Each SKU is alerted at most once per day. P8 back-office automations · synthetic data</p>
</div>`;
const text = [`Low stock ${day}: ${rows.length} SKU(s) below reorder point (synthetic data)`,
  ...rows.map((r) => `${r.sku}  ${r.name}  stock ${r.stock}/${r.reorder_point}  sold14d ${r.units_14d}  ${fmtDays(r.days_left)}`)].join('\n');
return [{
  json: {
    send: true, count: rows.length, day, ids: rows.map((r) => Number(r.alert_id)), skus: rows.map((r) => r.sku),
    subject: `[P8] Low stock: ${rows.length} SKU${rows.length === 1 ? '' : 's'} below reorder point (${day})`, html, text,
  },
}];
