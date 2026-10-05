#!/usr/bin/env bash
# Vendor htmx + its SSE extension from the npm registry. Each tarball's sha512 is PINNED below
# (recorded in gui/static/vendor/VENDOR.md); the registry's dist.integrity and the downloaded
# tarball must both equal the pin before anything is extracted. Changing a version means
# changing its pin deliberately, in review.
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
fetch() {  # pkg version member outname pinned-sha512
  local pkg=$1 ver=$2 member=$3 out=$4 pinned=$5 meta tarball integrity actual
  meta=$(curl -fsS "https://registry.npmjs.org/$pkg/$ver")
  tarball=$(python3 -c 'import json,sys;print(json.load(sys.stdin)["dist"]["tarball"])' <<<"$meta")
  integrity=$(python3 -c 'import json,sys;print(json.load(sys.stdin)["dist"]["integrity"])' <<<"$meta")
  case "$tarball" in
    https://registry.npmjs.org/*) ;;
    *) echo "unexpected tarball URL for $pkg@$ver: $tarball" >&2; exit 1 ;;
  esac
  [ "$integrity" = "$pinned" ] || {
    echo "registry integrity for $pkg@$ver differs from the pin: $integrity" >&2; exit 1; }
  curl -fsS "$tarball" -o "$tmp/$out.tgz"
  actual="sha512-$(openssl dgst -sha512 -binary "$tmp/$out.tgz" | base64 -w0)"
  [ "$actual" = "$pinned" ] || { echo "tarball for $pkg@$ver does not match the pin: $actual" >&2; exit 1; }
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
fetch htmx.org 2.0.11 dist/htmx.min.js htmx.min.js sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==
fetch htmx-ext-sse 2.2.4 dist/sse.min.js sse.js sha512-LJmxVhykyflBWgh5PvbRidcyuqMHlgfajmmzumvKctv9puvsufeH6OaejSMZTNTnEI8O2wXXn4ZZtBdpEMqmEQ==
echo "vendor ok"
