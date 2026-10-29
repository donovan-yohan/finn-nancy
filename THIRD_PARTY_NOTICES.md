# Third-party notices

## Bootstrap

- Files: `app/web/static/foundation/vendor/bootstrap.min.css` and `LICENSE.bootstrap`
- Version: 5.3.8
- Upstream: `https://github.com/twbs/bootstrap/tree/v5.3.8`
- License: MIT; full upstream notice is preserved in `LICENSE.bootstrap` and the CSS header.
- CSS SHA-256: `d85327d99c7a3ee1f9b5d0500d1370acea3ad2db39c163c2f51f232baedbdede`
- License SHA-256: `4620c84ad5ce8602ff65640ed6b7c8b78ebb9e036584f0ebc1ccc88206a4bb51`

Copied unchanged from Hotate's released local design foundation. No Bootstrap JavaScript or runtime network dependency is used. `app/web/static/foundation/SOURCE.json` pins that snapshot and the canonical token source/generated CSS hashes; `tokens.css` is generated upstream from `tokens.json`, not independently authored. `components.css` adapts the shared primitives and normal-flow shell; Connections alone adopts this new base in this slice.

## htmx

- File: `app/web/static/htmx.min.js`
- Version: 2.0.4 (the bundled file declares `version:"2.0.4"`)
- Upstream source: `https://github.com/bigskysoftware/htmx/tree/v2.0.4`
- Upstream distribution artifact: `https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/dist/htmx.min.js`
- SHA-256: `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`
- License: Zero-Clause BSD (0BSD)
- License source: `https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.4/LICENSE`

The vendored htmx artifact was compared with the upstream `v2.0.4` distribution artifact during public-release preparation. Its upstream license text follows:

```text
Zero-Clause BSD

Permission to use, copy, modify, and/or distribute this software for
any purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED “AS IS” AND THE AUTHOR DISCLAIMS ALL
WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES
OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE
FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY
DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN
AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```
