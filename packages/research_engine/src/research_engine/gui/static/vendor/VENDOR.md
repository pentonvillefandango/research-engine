# Vendored front-end assets

Written by `scripts/vendor_assets.sh`, which downloads each npm tarball, verifies it against the
registry's `dist.integrity` (sha512) before extracting, and copies one file out unchanged.
`scripts/vendor_assets.sh --check` re-downloads, re-verifies and compares the bytes; it exits
non-zero on any drift. The files are byte-exact copies (no trailing newline), so the pre-commit
whitespace hooks exclude this directory.

Vendored on 2026-10-05.

| File | Package | Tarball member | Source tarball | npm `dist.integrity` (tarball) | File SRI (sha384) |
|---|---|---|---|---|---|
| `htmx.min.js` | `htmx.org@2.0.11` | `package/dist/htmx.min.js` | https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz | `sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==` | `sha384-2OatzQy1H+Zd/IIrjr1TcuDGqLXeHhbooAyJY1KdQMKnr4LZ22k31GBLdYKHmVjg` |
| `sse.js` | `htmx-ext-sse@2.2.4` | `package/dist/sse.min.js` | https://registry.npmjs.org/htmx-ext-sse/-/htmx-ext-sse-2.2.4.tgz | `sha512-LJmxVhykyflBWgh5PvbRidcyuqMHlgfajmmzumvKctv9puvsufeH6OaejSMZTNTnEI8O2wXXn4ZZtBdpEMqmEQ==` | `sha384-A986SAtodyH8eg8x8irJnYUk7i9inVQqYigD6qZ9evobksGNIXfeFvDwLSHcp31N` |

Licences: both are BSD Zero Clause (0BSD): `htmx.org` declares it in its npm metadata; `htmx-ext-sse` ships it as `package/LICENSE`.
