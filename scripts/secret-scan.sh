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
  'shp(at|ss|ca|pa)_[0-9A-Za-z]{16,}'         # Shopify tokens (shpat_/shpca_) and client secrets (shpss_)
  '"(encryptionKey|accessToken|apiKey)"[[:space:]]*:[[:space:]]*"[^"]{6,}'
  '(password|passwd|secret|api_key)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{6,}'
)
EXISTING=()
for f in "${FILES[@]}"; do [ -f "$f" ] && [ "$f" != "scripts/secret-scan.sh" ] && [ "$f" != "scripts/check_workflows_clean.py" ] && EXISTING+=("$f"); done

if [ ${#EXISTING[@]} -gt 0 ]; then
  for p in "${PATTERNS[@]}"; do
    # file:line only: a matched line would echo the token/secret itself
    if out=$(grep -HnEI -e "$p" -- "${EXISTING[@]}" 2>/dev/null | cut -d: -f1,2); then
      report "pattern '$p' matched at (line not shown):"; echo "$out"
    fi
  done

  # 3) The literal values of the secrets in .env (Shopify client secret, tokens, passwords). Only file:line is
  #    printed, never the matching line, so a hit does not echo the secret.
  if [ -f .env ]; then
    VALS=$(mktemp); chmod 600 "$VALS"
    python3 - "$VALS" <<'PY'
import sys
keys = {"SHOPIFY_CLIENT_SECRET", "STORE_API_TOKEN", "MOCK_ADMIN_TOKEN", "N8N_WEBHOOK_TOKEN", "POSTGRES_PASSWORD",
        "GEMINI_API_KEY"}
skip = {"", "change-me", "shpat_change-me"}
vals = []
for line in open(".env"):
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        v = v.strip()
        if k.strip() in keys and v not in skip and len(v) >= 8:
            vals.append(v)
open(sys.argv[1], "w").write("\n".join(vals) + ("\n" if vals else ""))
PY
    if [ -s "$VALS" ] && out=$(grep -HnFI -f "$VALS" -- "${EXISTING[@]}" 2>/dev/null | cut -d: -f1,2); then
      report "value of a secret from .env found at (line not shown):"; echo "$out"
    fi
    rm -f "$VALS"
  fi

  # 4) Real-looking email addresses. Ignores samples/ and placeholder domains.
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

# 5) Exported n8n workflows: credentials by reference only, no .env secret values (incl. the webhook token).
if ! python3 scripts/check_workflows_clean.py; then fail=1; fi

if [ $fail -eq 0 ]; then echo "SECRET-SCAN: clean (${#EXISTING[@]} files checked)"; fi
exit $fail
