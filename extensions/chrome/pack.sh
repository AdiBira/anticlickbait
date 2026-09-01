#!/usr/bin/env bash
# Package the extension for the Chrome Web Store.
# Guardrail: refuses to build while MOCK is true, so fabricated demo scores
# can never ship to real users.
set -euo pipefail
cd "$(dirname "$0")"

if grep -Eq '^\s*export\s+const\s+MOCK\s*=\s*true' config.js; then
  echo "REFUSING TO PACK: config.js has MOCK = true. Set MOCK = false first." >&2
  exit 1
fi

OUT="../anticlickbait-extension.zip"
rm -f "$OUT"
# Ship only runtime files; exclude dev/test artifacts.
zip -r "$OUT" . \
  -x "QA.md" -x "pack.sh" -x "*.map" -x ".*" >/dev/null
echo "Packed -> $OUT"
