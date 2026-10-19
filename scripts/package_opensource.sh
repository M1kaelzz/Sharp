#!/usr/bin/env bash
# Package the OPEN-SOURCE snapshot of Sharp.
#
#   bash scripts/package_opensource.sh [PACKAGE_NAME]
#
# Policy (single source of truth): docs/OPENSOURCE_RELEASE.md
#   - never ship: real credentials, the license signing private key, runtime DBs, real reports
#   - don't ship: third-party binaries (283MB vendor dir), venvs, build output, caches
#   - ship: server + dispatcher + worker image definition + frontend + docs + tests + OSS files
#
# The script FAILS (exit 1) if the staged tree contains anything that looks like a leak, instead of
# producing a plausible-looking but unsafe archive. A release artifact you cannot trust is worse
# than no artifact.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d)"
PACKAGE_NAME="${1:-Sharp-opensource-$STAMP}"
OUT_DIR="$ROOT_DIR/dist"
OUT_ZIP="$OUT_DIR/$PACKAGE_NAME.zip"

fail() { printf '\n  ✗ %s\n' "$*" >&2; exit 1; }
ok()   { printf '  ✓ %s\n' "$*"; }

STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sharp-oss.XXXXXX")"
trap 'rm -rf "$STAGE_DIR"' EXIT
mkdir -p "$STAGE_DIR/$PACKAGE_NAME" "$OUT_DIR"

echo "=============================================================="
echo "  Sharp 开源版打包：$PACKAGE_NAME"
echo "=============================================================="
echo "[1/5] 复制源码（按 docs/OPENSOURCE_RELEASE.md 的边界）"

rsync -a "$ROOT_DIR/" "$STAGE_DIR/$PACKAGE_NAME/" \
  --exclude ".git/" \
  --exclude ".DS_Store" \
  --exclude "._*" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude ".pytest_cache/" \
  --exclude ".coverage" \
  --exclude "dist/" \
  --exclude "build/" \
  --exclude ".buildenv/" \
  --exclude "runtime/.venv/" \
  --exclude "container/vendor/" \
  --exclude "container/device-tools/" \
  --exclude "container/.agents/skills/" \
  --exclude "packaging/" \
  --exclude "reports/" \
  --exclude ".secrets" \
  --exclude ".oss-deny.txt" \
  --exclude "datas/" \
  --exclude "license.key" \
  --exclude "*.key" \
  --exclude "tools/issue_license.py" \
  --exclude "tools/__pycache__/" \
  --exclude ".github/workflows/build-windows-exe.yml"

# 运行数据目录保留为空占位（解开即可跑）
mkdir -p "$STAGE_DIR/$PACKAGE_NAME/datas/sharp"
cat > "$STAGE_DIR/$PACKAGE_NAME/datas/sharp/.gitkeep" <<'EOF'
Runtime data lives here (SQLite DB, secrets.env, license key) and is never committed.
EOF

STAGE="$STAGE_DIR/$PACKAGE_NAME"

echo "[2/5] 泄漏自检（命中即失败）"
source "$ROOT_DIR/scripts/_leak_check.sh"
run_leak_check "$STAGE"

echo "[3/5] 必需文件自检（缺一即失败）"
REQUIRED="LICENSE README.md README.en.md SECURITY.md CONTRIBUTING.md THIRD_PARTY_NOTICES.md
secrets.env.example .gitignore dispatch.yaml docker-compose.yaml Dockerfile
docs/OPENSOURCE_RELEASE.md docs/ARCHITECTURE.md docs/USAGE.md docs/CHANGELOG.md docs/GLOSSARY.md docs/ARCHITECTURE.en.md docs/USAGE.en.md
.github/workflows/tests.yml .github/ISSUE_TEMPLATE/bug_report.md .github/pull_request_template.md
scripts/check_docs.py scripts/check_methods.py scripts/package_opensource.sh
runtime/pyproject.toml runtime/uv.lock runtime/src/sharp/cli.py
container/Dockerfile container/fetch_vendor.sh"
MISSING=""
for f in $REQUIRED; do
  [ -e "$STAGE/$f" ] || MISSING="$MISSING $f"
done
[ -n "$MISSING" ] && fail "缺少必需文件:$MISSING"
ok "必需文件齐备（$(echo "$REQUIRED" | wc -w | tr -d ' ') 项）"

echo "[4/5] 打包"
rm -f "$OUT_ZIP"
if command -v ditto >/dev/null 2>&1; then
  (cd "$STAGE_DIR" && ditto -c -k --norsrc --keepParent "$PACKAGE_NAME" "$OUT_ZIP")
else
  (cd "$STAGE_DIR" && zip -qr "$OUT_ZIP" "$PACKAGE_NAME")
fi

# 成品包自检：装配树干净不代表 zip 本体干净（打包器自身也要被验证）。
# 教训：曾用 `find -path "*\.venv*"` 做「独立复核」——BSD find 不把反斜杠当转义，
# 该模式永不匹配，等于假阴性；另外在解压副本里跑 pytest 会凭空造出 .venv/__pycache__，
# 让人误以为包里有脏东西。所以这里直接查 **zip 条目清单**。
echo "[5/5] 成品包自检（查 zip 本体，不查装配树）"
run_zip_check "$OUT_ZIP"

FILES=$(find "$STAGE" -type f | wc -l | tr -d ' ')
SIZE=$(du -sh "$STAGE" | awk '{print $1}')
SHA=$(shasum -a 256 "$OUT_ZIP" | awk '{print $1}')
{
  echo "package: $PACKAGE_NAME"
  echo "built_at: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "files: $FILES"
  echo "uncompressed: $SIZE"
  echo "sha256: $SHA"
  echo "excluded: secrets.env / license_signing_key.b64 / *.db / reports/ / container-vendor / device-tools / packaging / venv / caches"
} > "$OUT_DIR/$PACKAGE_NAME.manifest.txt"

echo
printf '  ✓ %s\n' "Created: $OUT_ZIP"
printf '  ✓ %s\n' "Manifest: $OUT_DIR/$PACKAGE_NAME.manifest.txt"
printf '      files=%s  size=%s  sha256=%s\n' "$FILES" "$SIZE" "${SHA:0:16}…"
echo
echo "  下一步（见 docs/OPENSOURCE_RELEASE.md §4）："
echo "    cd /tmp && unzip -q '$OUT_ZIP' && cd $PACKAGE_NAME"
echo "    uv run --project runtime --with pytest --with httpx pytest -q"
