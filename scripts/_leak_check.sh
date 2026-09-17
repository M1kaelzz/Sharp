#!/usr/bin/env bash
# Shared leak self-check for all packaging scripts.
# Source this file AFTER rsync has staged the tree, then call `run_leak_check "$STAGE"`.
#
# Design: a release artifact you cannot trust is worse than no artifact.
# This function FAILS (exit 1) if the staged tree contains anything that
# looks like a credential, private key, runtime DB, or real report.
#
# Usage:
#   source "$(dirname "$0")/_leak_check.sh"
#   run_leak_check "$STAGE"

run_leak_check() {
  local STAGE="$1"
  local LEAKS=0

  # 1) Forbidden paths — files that must never be in any package
  while IFS= read -r hit; do
    [ -n "$hit" ] && { echo "  ✗ 禁止路径: ${hit#$STAGE/}"; LEAKS=1; }
  done < <(find "$STAGE" \( \
        -name "secrets.env" -o -name "*.key" -o -name "license_signing_key*" \
        -o -name "*.db" -o -name "*.db-wal" -o -name "*.db-shm" -o -name "*.sqlite*" \
        -o -name ".DS_Store" -o -name "*.pyc" -o -name "id_rsa*" \) -print)

  # 2) Forbidden directories
  for d in runtime/.venv container/vendor container/device-tools packaging reports .git; do
    [ -e "$STAGE/$d" ] && { echo "  ✗ 禁止目录: $d"; LEAKS=1; }
  done

  # 3) Content patterns (skip binaries)
  local CONTENT_PATTERNS='sk-ant-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}'
  while IFS= read -r hit; do
    [ -n "$hit" ] && { echo "  ✗ 疑似密钥内容: ${hit#$STAGE/}"; LEAKS=1; }
  done < <(grep -rIlE "$CONTENT_PATTERNS" "$STAGE" 2>/dev/null || true)

  # 4) Non-empty credential assignments (template secrets.env.example must have empty values)
  while IFS= read -r hit; do
    [ -n "$hit" ] && { echo "  ✗ 非空凭据赋值: ${hit#$STAGE/}"; LEAKS=1; }
  done < <(grep -rIlE '^(SHARP_ANTHROPIC_AUTH_TOKEN|ANTHROPIC_AUTH_TOKEN|OPENAI_API_KEY|PI_BASE_URL)=[^[:space:]]+' "$STAGE" 2>/dev/null || true)

  # 5) Maintenance deny-list (local only, not shipped in the package)
  local DENY_FILE
  DENY_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.oss-deny.txt"
  if [ -f "$DENY_FILE" ]; then
    local DENY_HITS=0
    while IFS= read -r pat; do
      [ -z "$pat" ] && continue
      case "$pat" in \#*) continue ;; esac
      while IFS= read -r hit; do
        [ -n "$hit" ] && { echo "  ✗ 命中本地 deny 清单 [${pat}]: ${hit#$STAGE/}"; LEAKS=1; DENY_HITS=1; }
      done < <(grep -rIlE "$pat" "$STAGE" 2>/dev/null || true)
    done < "$DENY_FILE"
    [ "$DENY_HITS" -eq 0 ] && printf '  ✓ 本地 deny 清单通过（%s 条模式）\n' "$(grep -cvE '^\s*(#|$)' "$DENY_FILE")"
  else
    echo "  ! 未找到 .oss-deny.txt —— 跳过自用端点检查（建议创建，见 docs/OPENSOURCE_RELEASE.md §2.3）"
  fi

  if [ "$LEAKS" -ne 0 ]; then
    printf '\n  ✗ 泄漏自检未通过 —— 已中止，未产出任何包（见上方 ✗ 行）\n' >&2
    exit 1
  fi
  printf '  ✓ 未发现密钥 / 私钥 / 运行库 / 报告 / 第三方二进制\n'
}

# Check the final zip archive for forbidden entries.
# Usage: run_zip_check "$OUT_ZIP"
run_zip_check() {
  local OUT_ZIP="$1"
  local ZIP_BAD
  ZIP_BAD=$(unzip -l "$OUT_ZIP" | grep -cE "\.venv/|__pycache__|\.pyc$|license_signing|datas/sharp/secrets\.env|reports/|container/vendor|panda-dex|issue_license|packaging/|\.oss-deny" || true)
  if [ "${ZIP_BAD:-0}" -ne 0 ]; then
    echo "  ✗ 成品包内含禁止条目（${ZIP_BAD} 行）："
    unzip -l "$OUT_ZIP" | grep -E "\.venv/|__pycache__|\.pyc$|license_signing|datas/sharp/secrets\.env|reports/|container/vendor|panda-dex|issue_license|packaging/|\.oss-deny" | head -5
    printf '\n  ✗ 成品包自检未通过\n' >&2
    exit 1
  fi
  printf '  ✓ 成品包自检通过\n'
}
