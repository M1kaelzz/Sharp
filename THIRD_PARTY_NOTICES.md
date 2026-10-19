# Third-Party Notices

Sharp is licensed under AGPL-3.0 (see `LICENSE`). It bundles or downloads third-party components
that keep their own licenses. This file lists them so downstream users can comply.

> **Accuracy note**: license identifiers below are the ones these projects publish upstream. Where
> a component is not redistributed by us, we point at the upstream project rather than restating
> its terms. If you spot a mismatch, open an issue — we will fix the notice.

## 1. Redistributed with this repository

### Frontend libraries (`runtime/src/sharp/server/static/vendor/`)

The web UI has no build step: these files are served as-is.

| File | Component | License (upstream) |
|---|---|---|
| `alpine.min.js` | Alpine.js | MIT |
| `tailwindcss.js` | Tailwind CSS (Play CDN runtime) | MIT |
| `cytoscape.min.js` | Cytoscape.js | MIT |
| `cytoscape-dagre.js` | cytoscape-dagre | MIT |
| `cytoscape-cola.js` | cytoscape-cola | MIT |
| `cytoscape-klay.js` | cytoscape-klay | MIT |
| `cytoscape-elk.js` | cytoscape-elk | MIT |
| `cytoscape-navigator.js` | cytoscape-navigator | MIT |
| `dagre.min.js` | dagre | MIT |
| `cola.min.js` | WebCola | MIT |
| `klay.js` | KLay Layered (KIELER) | EPL-1.0 |
| `elk.bundled.js` | Eclipse Layout Kernel (ELK) | EPL-1.0 |
| `purify.min.js` | DOMPurify | Apache-2.0 OR MPL-2.0 |

### Fonts (`runtime/src/sharp/server/static/fonts/`)

| File | Component | License (upstream) |
|---|---|---|
| `ark-pixel-12px-monospaced-zh_cn.ttf.woff2` | Ark Pixel Font (方舟像素字体) | SIL OFL-1.1 |

> Note on the layout engines: `klay.js` and `elk.bundled.js` are Eclipse Public License 1.0
> components. They are used unmodified as vendored assets and are not linked into Sharp's own code.
> If your distribution requires stricter separation, drop the corresponding layout option in
> `static/app.graph.js` and remove the file — the other layouts are MIT.

## 2. Downloaded at build time, **not** redistributed by us

`container/fetch_vendor.sh` downloads the following into `container/vendor/` when you build the
worker image. Sharp does not ship these binaries in the source package: they are fetched from
upstream releases at pinned versions, and each keeps its own license. Versions are pinned in the
script; consult each upstream project for terms and attribution requirements.

| Tool | Upstream | Version pinned |
|---|---|---|
| katana | projectdiscovery/katana | 1.5.0 |
| nuclei | projectdiscovery/nuclei | 3.7.1 |
| nuclei-templates | projectdiscovery/nuclei-templates | `main` (rolling) |
| httpx | projectdiscovery/httpx | 1.6.9 |
| naabu | projectdiscovery/naabu | 2.3.3 (amd64 only — upstream publishes no arm64 build) |
| dalfox | hahwul/dalfox | 2.12.0 |
| ffuf | ffuf/ffuf | 2.1.0 |
| ripgrep | BurntSushi/ripgrep | 15.1.0 (Debian package, amd64) |
| fd | sharkdp/fd | 10.4.2 |
| yq | mikefarah/yq | 4.44.3 |
| gitleaks | gitleaks/gitleaks | 8.21.2 |
| ysoserial | frohoff/ysoserial | 0.0.6 |
| jwt_tool | ticarpi/jwt_tool | `master` (rolling) |
| nikto | sullo/nikto | `master` (rolling) |
| jadx | skylot/jadx | 1.5.1 |
| dex2jar | ThexXTURBOXx/dex2jar | 2.4.38 |
| vineflower | Vineflower/vineflower | 1.11.0 |

Additional tools come from the container base image via `apt` (`container/Dockerfile`) and keep the
licenses of their Debian packages.

> For reproducible releases, pin the three rolling dependencies (`nuclei-templates`, `jwt_tool`,
> `nikto`) to tags or commit SHAs in `fetch_vendor.sh`.

## 3. Excluded from the open-source package

| Component | Why it is not shipped |
|---|---|
| `container/device-tools/panda-dex-dumper` | third-party prebuilt ELF binary; upstream redistribution terms are not stated. Fetch it yourself if you need Android DEX analysis. |
| `container/vendor/` (≈283 MB) | downloaded third-party binaries; use `fetch_vendor.sh` |
| `packaging/` | build tooling for the separately licensed desktop distribution (open-core boundary, see `docs/OPENSOURCE_RELEASE.md`) |

## 4. Runtime dependencies (Python)

Sharp's Python dependencies are declared in `runtime/pyproject.toml` and locked in
`runtime/uv.lock` (`fastapi`, `uvicorn`, `click`, `pyyaml`, `docker`, `requests`, `cryptography`,
and their transitive deps). They are installed from PyPI at install time and keep their own
licenses — check the lock file for exact versions.
