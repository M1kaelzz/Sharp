#!/usr/bin/env bash
# Package a clean SOURCE snapshot (code + tests + docs), excluding build
# artifacts, virtualenvs, and — critically — secrets and the license signing
# private key. Produces a dated zip under dist/.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d)"
PACKAGE_NAME="${1:-Sharp-src-$STAMP}"
OUT_DIR="$ROOT_DIR/dist"
mkdir -p "$OUT_DIR"

STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sharp-src.XXXXXX")"
trap 'rm -rf "$STAGE_DIR"' EXIT
mkdir -p "$STAGE_DIR/$PACKAGE_NAME"

rsync -a "$ROOT_DIR/" "$STAGE_DIR/$PACKAGE_NAME/" \
  --exclude ".git/" \
  --exclude ".DS_Store" \
  --exclude "._*" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude ".pytest_cache/" \
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

# Keep an empty data dir so the tree is runnable after unzip.
mkdir -p "$STAGE_DIR/$PACKAGE_NAME/datas/sharp"

STAGE="$STAGE_DIR/$PACKAGE_NAME"

echo "[2/4] 泄漏自检（命中即失败）"
source "$ROOT_DIR/scripts/_leak_check.sh"
run_leak_check "$STAGE"

echo "[3/4] 打包"

ZIP_PATH="$OUT_DIR/$PACKAGE_NAME.zip"
rm -f "$ZIP_PATH"
if command -v ditto >/dev/null 2>&1; then
  (cd "$STAGE_DIR" && ditto -c -k --norsrc --keepParent "$PACKAGE_NAME" "$ZIP_PATH")
else
  (cd "$STAGE_DIR" && zip -qr "$ZIP_PATH" "$PACKAGE_NAME")
fi

echo "Created: $ZIP_PATH"
echo "Excluded: .venv, dist, build, secrets.env, license_signing_key.b64, *.key, *.db, caches"

echo "[4/4] 成品包自检"
run_zip_check "$ZIP_PATH"
