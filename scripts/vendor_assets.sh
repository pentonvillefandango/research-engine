#!/usr/bin/env bash
# Vendor htmx + its SSE extension from the npm registry, verifying each tarball against the
# registry's dist.integrity (sha512) before extracting anything.
# Usage: scripts/vendor_assets.sh           # (re)write the vendored files
#        scripts/vendor_assets.sh --check   # re-download, verify, compare bytes; non-zero on drift
set -euo pipefail
cd "$(dirname "$0")/.."
DEST=packages/research_engine/src/research_engine/gui/static/vendor
CHECK=0
case "${1:-}" in
  "") ;;
  --check) CHECK=1 ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac
mkdir -p "$DEST"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
fetch() {  # pkg version member outname
  local pkg=$1 ver=$2 member=$3 out=$4 meta tarball integrity actual
  meta=$(curl -fsS "https://registry.npmjs.org/$pkg/$ver")
  tarball=$(python3 -c 'import json,sys;print(json.load(sys.stdin)["dist"]["tarball"])' <<<"$meta")
  integrity=$(python3 -c 'import json,sys;print(json.load(sys.stdin)["dist"]["integrity"])' <<<"$meta")
  case "$tarball" in
    https://registry.npmjs.org/*) ;;
    *) echo "unexpected tarball URL for $pkg@$ver: $tarball" >&2; exit 1 ;;
  esac
  curl -fsS "$tarball" -o "$tmp/$out.tgz"
  actual="sha512-$(openssl dgst -sha512 -binary "$tmp/$out.tgz" | base64 -w0)"
  [ "$actual" = "$integrity" ] || { echo "integrity mismatch for $pkg@$ver" >&2; exit 1; }
  tar -xzf "$tmp/$out.tgz" -C "$tmp" "package/$member"
  if [ "$CHECK" = 1 ]; then
    [ -f "$DEST/$out" ] || { echo "missing: $DEST/$out" >&2; exit 1; }
    cmp -s "$tmp/package/$member" "$DEST/$out" || { echo "drift: $DEST/$out" >&2; exit 1; }
  else
    cp "$tmp/package/$member" "$DEST/$out"
  fi
  echo "$pkg@$ver $integrity -> $out sha384-$(openssl dgst -sha384 -binary "$DEST/$out" | base64 -w0)"
  rm -rf "$tmp/package"
}
fetch htmx.org 2.0.11 dist/htmx.min.js htmx.min.js
fetch htmx-ext-sse 2.2.4 dist/sse.min.js sse.js
echo "vendor ok"
