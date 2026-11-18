#!/usr/bin/env bash
# Lightweight secret scan. Uses gitleaks when installed; otherwise a grep fallback. Always runs the n8n workflow
# leak check (scripts/check_workflows_clean.py) as well.
# Scans files that are staged (or tracked, if nothing is staged).
# Usage: bash scripts/secret-scan.sh   -> exit 0 = clean, exit 1 = findings
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks protect --staged --redact --no-banner
  exec python3 scripts/check_workflows_clean.py
fi

mapfile -t FILES < <(git diff --cached --name-only --diff-filter=ACM)
if [ ${#FILES[@]} -eq 0 ]; then
  mapfile -t FILES < <(git ls-files)
fi

fail=0
report() { echo "SECRET-SCAN: $1"; fail=1; }

# 1) Files that must never be committed.
for f in "${FILES[@]}"; do
  case "$(basename "$f")" in
    .env.example) ;;
    *.cred.json|.env|.env.*|*.pem|*.p12|credentials.json|client_secret*.json)
      report "forbidden file staged: $f" ;;
  esac
done

# 2) Token / key patterns.
PATTERNS=(
  'AIza[0-9A-Za-z_-]{35}'                     # Google API key
  'ya29\.[0-9A-Za-z_-]{20,}'                  # Google OAuth access token
  '1//[0-9A-Za-z_-]{30,}'                     # Google OAuth refresh token
  '"refresh_token"[[:space:]]*:'              # clasp/OAuth token JSON
  '"client_secret"[[:space:]]*:'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'gh[pousr]_[0-9A-Za-z]{36}'                 # GitHub tokens
  'shp(at|ss|ca)_[0-9a-fA-F]{32}'             # Shopify-style access tokens
  '"(encryptionKey|accessToken|apiKey)"[[:space:]]*:[[:space:]]*"[^"]{6,}'
  '(password|passwd|secret|api_key)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{6,}'
)
EXISTING=()
for f in "${FILES[@]}"; do [ -f "$f" ] && [ "$f" != "scripts/secret-scan.sh" ] && [ "$f" != "scripts/check_workflows_clean.py" ] && EXISTING+=("$f"); done

if [ ${#EXISTING[@]} -gt 0 ]; then
  for p in "${PATTERNS[@]}"; do
    if out=$(grep -nEI -e "$p" -- "${EXISTING[@]}" 2>/dev/null); then
      report "pattern '$p' matched:"; echo "$out"
    fi
  done

  # 3) Real-looking email addresses. Ignores samples/ and placeholder domains.
  EMAIL_FILES=()
  for f in "${EXISTING[@]}"; do case "$f" in tests/fixtures/*) ;; *) EMAIL_FILES+=("$f") ;; esac; done
  if [ ${#EMAIL_FILES[@]} -gt 0 ]; then
    if out=$(grep -noEI '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' -- "${EMAIL_FILES[@]}" 2>/dev/null \
        | grep -viE '@([a-z0-9-]+\.)*(example\.(com|org|net)|example|test|invalid)$' \
        | grep -viE ':(noreply|no-reply)@' ); then
      report "email address found (use example.com placeholders):"; echo "$out"
    fi
  fi
fi

# 4) Exported n8n workflows: credentials by reference only, no .env secret values (incl. the webhook token).
if ! python3 scripts/check_workflows_clean.py; then fail=1; fi

if [ $fail -eq 0 ]; then echo "SECRET-SCAN: clean (${#EXISTING[@]} files checked)"; fi
exit $fail
