#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$ROOT_DIR/dist}"
PACKAGE_NAME="${2:-Sharp-portable}"

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sharp-package.XXXXXX")"
cleanup() {
  rm -rf "$STAGE_DIR"
}
trap cleanup EXIT

mkdir -p "$STAGE_DIR/$PACKAGE_NAME"

rsync -a "$ROOT_DIR/" "$STAGE_DIR/$PACKAGE_NAME/" \
  --exclude ".git/" \
  --exclude ".DS_Store" \
  --exclude "dist/" \
  --exclude "runtime/.venv/" \
  --exclude "tests/" \
  --exclude "__pycache__/" \
  --exclude ".pytest_cache/" \
  --exclude "._*" \
  --exclude ".secrets" \
  --exclude ".oss-deny.txt" \
  --exclude "container/vendor/" \
  --exclude "container/device-tools/" \
  --exclude "container/.agents/skills/" \
  --exclude "packaging/" \
  --exclude "datas/" \
  --exclude "license.key" \
  --exclude "*.key" \
  --exclude "tools/issue_license.py" \
  --exclude "tools/__pycache__/" \
  --exclude "reports/"

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
elif command -v zip >/dev/null 2>&1; then
  (cd "$STAGE_DIR" && zip -qr "$ZIP_PATH" "$PACKAGE_NAME")
else
  echo "Neither ditto nor zip is available." >&2
  exit 1
fi

echo "Created: $ZIP_PATH"
echo "Note: datas/sharp/secrets.env is intentionally excluded."

echo "[4/4] 成品包自检"
run_zip_check "$ZIP_PATH"
