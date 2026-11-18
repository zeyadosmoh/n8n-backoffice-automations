// Summary email + rejects.csv (one reason per row) + review.csv (AI suggestions, never applied).
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const csvCell = (v) => { const s = String(v ?? ''); return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
const toCsv = (header, rows) => [header.join(','), ...rows.map((r) => header.map((h) => csvCell(r[h])).join(','))].join('\r\n') + '\r\n';
const r = $json.report;
const base = String(r.source_file || 'upload').replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9_-]+/g, '_');
const binary = {};
const attachments = [];
if (r.rejects.length) {
  binary.rejects = { data: Buffer.from(toCsv(['row_no', 'sku', 'name', 'price', 'reason'], r.rejects)).toString('base64'),
    mimeType: 'text/csv', fileName: `rejects_${r.import_id}_${base}.csv`, fileExtension: 'csv' };
  attachments.push('rejects');
}
if (r.review.length) {
  binary.review = { data: Buffer.from(toCsv(['row_no', 'name', 'price', 'suggested_sku', 'suggested_name', 'confidence', 'current_price'], r.review)).toString('base64'),
    mimeType: 'text/csv', fileName: `review_${r.import_id}_${base}.csv`, fileExtension: 'csv' };
  attachments.push('review');
}
const money = (v) => (v === null || v === undefined ? '' : Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const LARGE = 30;
const large = r.changes.filter((c) => Math.abs(Number(c.pct)) >= LARGE);
const td = 'style="padding:5px 9px;border-bottom:1px solid #eaeef2"';
const tdr = 'style="padding:5px 9px;border-bottom:1px solid #eaeef2;text-align:right"';
const changeRows = r.changes.slice(0, 50).map((c) => `<tr><td ${td}><code>${esc(c.sku)}</code></td><td ${td}>${esc(c.name)}</td>` +
  `<td ${tdr}>${money(c.old_price)}</td><td ${tdr}>${money(c.new_price)}</td>` +
  `<td ${tdr}${Math.abs(Number(c.pct)) >= LARGE ? ' bgcolor="#fff4e5"' : ''}>${c.pct > 0 ? '+' : ''}${esc(c.pct)}%</td>` +
  `<td ${td}>${esc(c.match_method)}</td></tr>`).join('');
const rejectRows = r.rejects.slice(0, 50).map((x) => `<tr><td ${tdr}>${esc(x.row_no)}</td><td ${td}>${esc(x.sku)}</td><td ${td}>${esc(x.name)}</td><td ${td}>${esc(x.price)}</td><td ${td}>${esc(x.reason)}</td></tr>`).join('');
const reviewRows = r.review.map((x) => `<tr><td ${tdr}>${esc(x.row_no)}</td><td ${td}>${esc(x.name)}</td><td ${td}><code>${esc(x.suggested_sku)}</code> ${esc(x.suggested_name)}</td><td ${tdr}>${esc(x.confidence)}</td><td ${tdr}>${esc(x.price)}</td></tr>`).join('');
const applied = r.status === 'applied';
const headline = r.file_error
  ? `File rejected: ${esc(r.file_error)}`
  : applied
    ? `${r.rows_changed} price${r.rows_changed === 1 ? '' : 's'} updated, ${r.rows_rejected} row${r.rows_rejected === 1 ? '' : 's'} rejected`
    : `No prices updated: ${r.rows_rejected} row${r.rows_rejected === 1 ? '' : 's'} rejected`;
const html = `
<div style="font-family:Arial,sans-serif;font-size:14px;color:#1f2328;max-width:820px">
  <h2 style="margin:0 0 6px">Supplier price list: ${headline}</h2>
  <p style="margin:0 0 12px;color:#57606a">File <b>${esc(r.source_file)}</b> · import #${esc(r.import_id)} · status <b>${esc(r.status)}</b></p>
  <table cellpadding="4" style="border-collapse:collapse;margin-bottom:14px">
    <tr><td>Rows in file</td><td align="right"><b>${r.rows_total}</b></td></tr>
    <tr><td>Valid</td><td align="right"><b>${r.rows_valid}</b> (${r.rows_changed} changed, ${r.rows_unchanged} unchanged)</td></tr>
    <tr><td>Rejected</td><td align="right"><b>${r.rows_rejected}</b>${r.rejects.length ? ' (see the attached rejects file)' : ''}</td></tr>
    <tr><td>AI suggestions to review</td><td align="right"><b>${r.rows_review}</b>${r.ai_enabled ? '' : ' (AI matching off)'}</td></tr>
  </table>
  ${applied ? '<p style="color:#1a7f37">All valid rows were applied in one database transaction.</p>' : ''}
  ${large.length ? `<p style="background:#fff4e5;padding:8px">${large.length} change(s) of ${LARGE}% or more, please double-check them.</p>` : ''}
  ${r.changes.length ? `<h3>Price changes${r.changes.length > 50 ? ' (largest 50)' : ''}</h3><table style="border-collapse:collapse;width:100%"><tr style="background:#f6f8fa;text-align:left"><th ${td}>SKU</th><th ${td}>Product</th><th ${tdr}>Old</th><th ${tdr}>New</th><th ${tdr}>Change</th><th ${td}>Matched by</th></tr>${changeRows}</table>` : ''}
  ${r.review.length ? `<h3>AI suggestions to review (not applied)</h3><table style="border-collapse:collapse;width:100%"><tr style="background:#f6f8fa;text-align:left"><th ${tdr}>Row</th><th ${td}>Supplier name</th><th ${td}>Suggested product</th><th ${tdr}>Confidence</th><th ${tdr}>Price</th></tr>${reviewRows}</table>` : ''}
  ${r.rejects.length ? `<h3>Rejected rows${r.rejects.length > 50 ? ' (first 50)' : ''}</h3><table style="border-collapse:collapse;width:100%"><tr style="background:#f6f8fa;text-align:left"><th ${tdr}>Row</th><th ${td}>SKU</th><th ${td}>Name</th><th ${td}>Price</th><th ${td}>Reason</th></tr>${rejectRows}</table>` : ''}
  <p style="color:#57606a;font-size:12px;margin-top:16px">Row numbers are spreadsheet rows (header = row 1). P8 back-office automations · synthetic data</p>
</div>`;
const subject = `[P8] Price list ${r.source_file}: ${r.file_error ? 'file rejected' : `${r.rows_changed} updated, ${r.rows_rejected} rejected, ${r.rows_review} to review`}`;
return [{ json: { subject, html, attachments: attachments.join(','), report: r }, binary }];
