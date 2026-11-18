// Error Trigger payload -> alert email fields. The execution link uses N8N_EDITOR_BASE_URL (default
// http://localhost:5680), never a hard-coded host.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const e = $json;
const exec = e.execution || {};
const wf = e.workflow || {};
const err = exec.error || (e.trigger && e.trigger.error) || {};
const node = exec.lastNodeExecuted || (err.node && err.node.name) || (e.trigger ? 'trigger' : 'unknown');
const message = err.message || 'unknown error';
const base = String($env.N8N_EDITOR_BASE_URL || 'http://localhost:5680').replace(/\/+$/, '');
const link = exec.id ? `${base}/workflow/${wf.id}/executions/${exec.id}` : null;
const when = $now.setZone('Africa/Cairo').toFormat('yyyy-LL-dd HH:mm');
const html = `
<div style="font-family:Arial,sans-serif;font-size:14px;color:#1f2328">
  <h2 style="color:#b42318;margin:0 0 12px">n8n workflow failed</h2>
  <table cellpadding="6" style="border-collapse:collapse">
    <tr><td><b>Workflow</b></td><td>${esc(wf.name)} (${esc(wf.id)})</td></tr>
    <tr><td><b>Node</b></td><td>${esc(node)}</td></tr>
    <tr><td><b>Error</b></td><td>${esc(message)}</td></tr>
    <tr><td><b>Mode</b></td><td>${esc(exec.mode || 'trigger')}</td></tr>
    <tr><td><b>When</b></td><td>${esc(when)} (Africa/Cairo)</td></tr>
    <tr><td><b>Execution</b></td><td>${link ? `<a href="${esc(link)}">${esc(link)}</a>` : 'n/a (failed before an execution was stored)'}</td></tr>
  </table>
  <p style="color:#57606a;font-size:12px">P8 back-office automations · synthetic data</p>
</div>`;
const text = `n8n workflow failed\nWorkflow: ${wf.name} (${wf.id})\nNode: ${node}\nError: ${message}\nExecution: ${link || 'n/a'}\n(synthetic data)`;
return [{ json: { subject: `[P8 alert] ${wf.name || 'workflow'} failed at "${node}"`, html, text, workflow: wf.name, node, message, link } }];
