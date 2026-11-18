// Which uploaded file, and is it CSV or XLSX? Anything else is rejected as a whole file (summary email says why).
const item = $input.first();
const keys = Object.keys(item.binary || {});
if (keys.length === 0) {
  return [{ json: { kind: 'none', fileName: 'upload', fileError: 'no file was uploaded' } }];
}
const key = keys[0];
const bin = item.binary[key];
const fileName = bin.fileName || 'upload';
const ext = (fileName.match(/\.([a-z0-9]+)$/i) || [, ''])[1].toLowerCase();
const kind = ext === 'csv' ? 'csv' : ext === 'xlsx' ? 'xlsx' : 'unsupported';
const out = { json: { kind, fileName, binaryKey: key }, binary: { file: bin } };
if (kind === 'unsupported') out.json.fileError = `unsupported file type ${ext ? '.' + ext : '(no extension)'}: upload a .csv or .xlsx file`;
return [out];
