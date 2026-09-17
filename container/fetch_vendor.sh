#!/usr/bin/env bash
# Run this ON YOUR MAC (where your proxy/VPN works) to download all GitHub
# artifacts the Sharp worker image needs into ./vendor/. The Docker build then only
# COPYs them in — zero GitHub access during build.
#
# Usage:
#   cd container
#   ./fetch_vendor.sh                 # direct (follows this machine's arch)
#   ARCH=arm64 ./fetch_vendor.sh      # 显式指定目标架构（交叉构建用）
#   PRINT_ONLY=1 ARCH=arm64 ./fetch_vendor.sh   # 只打印将下载的 URL，不下载
#   GH=https://ghfast.top/ ./fetch_vendor.sh          # via a github proxy prefix
#   https_proxy=http://127.0.0.1:7890 ./fetch_vendor.sh   # via your local proxy
#
# Re-runnable: already-downloaded files are skipped. If one fails, fix network
# and re-run; only the missing ones are retried.
set -u

GH="${GH:-}"
VENDOR="$(cd "$(dirname "$0")" && pwd)/vendor"
mkdir -p "$VENDOR"

# ── 目标架构 ──────────────────────────────────────────────────────────────────
# 二进制产物必须与目标镜像架构一致，否则容器里会 exec format error（且极难排查）。
case "${ARCH:-$(uname -m)}" in
  x86_64|amd64) ARCH=amd64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) echo "[FAIL] 不支持的架构：${ARCH:-$(uname -m)}（支持 amd64 / arm64）"; exit 2 ;;
esac

# 防混架构：vendor/ 是平面目录，换了架构必须清空重下
if [ -f "$VENDOR/.arch" ] && [ "$(cat "$VENDOR/.arch")" != "$ARCH" ]; then
  if [ "${FORCE:-0}" = "1" ]; then
    echo "[warn] 清理旧架构（$(cat "$VENDOR/.arch")）产物，切换为 $ARCH"
    rm -f "$VENDOR"/*.zip "$VENDOR"/*.tar.gz "$VENDOR"/*.deb "$VENDOR"/yq "$VENDOR"/.arch
  else
    echo "[FAIL] vendor/ 里已有 $(cat "$VENDOR/.arch") 的产物，当前目标 $ARCH"
    echo "       混架构二进制会在容器里 exec format error。"
    echo "       重下：rm -rf vendor && ARCH=$ARCH ./fetch_vendor.sh   或   FORCE=1 ARCH=$ARCH ./fetch_vendor.sh"
    exit 2
  fi
fi

gitleaks_arch="$ARCH"; [ "$ARCH" = "amd64" ] && gitleaks_arch=x64

fail=0

fetch() {
  # fetch <output-name> <github-url>
  local out="$VENDOR/$1"; local url="${GH}$2"
  if [ "${PRINT_ONLY:-0}" = "1" ]; then echo "  $1  <-  $url"; return 0; fi
  if [ -s "$out" ]; then echo "[skip] $1 (already present)"; return 0; fi
  echo "[get ] $1"
  # --speed-limit/--speed-time：下载停滞（<1KB/s 持续 45 秒）时自动放弃并重试，
# 否则一次网络抽风就会像这次一样把整个流程挂死几十分钟。
  if curl -fL --retry 3 --retry-delay 2 --connect-timeout 20 \
       --speed-limit 1024 --speed-time 45 -o "$out.part" "$url"; then
    mv "$out.part" "$out"; echo "[ ok ] $1"
  else
    rm -f "$out.part"; echo "[FAIL] $1  <- $url"; fail=1
  fi
}

echo "[arch] 目标架构：$ARCH"

# --- Go web scanners (release archives) ---
fetch katana.zip   "https://github.com/projectdiscovery/katana/releases/download/v1.5.0/katana_1.5.0_linux_${ARCH}.zip"
fetch nuclei.zip   "https://github.com/projectdiscovery/nuclei/releases/download/v3.7.1/nuclei_3.7.1_linux_${ARCH}.zip"
fetch httpx.zip    "https://github.com/projectdiscovery/httpx/releases/download/v1.6.9/httpx_1.6.9_linux_${ARCH}.zip"
# naabu：上游只发布 linux/amd64（没有 linux/arm64）—— arm64 镜像里会缺席，
# Dockerfile 已容错跳过；端口扫描可用 nmap/masscan 等价替代。
if [ "$ARCH" = "amd64" ]; then
  fetch naabu.zip  "https://github.com/projectdiscovery/naabu/releases/download/v2.3.3/naabu_2.3.3_linux_amd64.zip"
else
  echo "[skip] naabu.zip（上游未提供 linux/arm64 构建）"
fi
fetch dalfox.tar.gz "https://github.com/hahwul/dalfox/releases/download/v2.12.0/dalfox-linux-${ARCH}.tar.gz"
fetch ffuf.tar.gz  "https://github.com/ffuf/ffuf/releases/download/v2.1.0/ffuf_2.1.0_linux_${ARCH}.tar.gz"

# --- small utilities ---
# ripgrep：上游 deb 只发 amd64，arm64 由 Dockerfile 改走 Debian 仓库（apt）
if [ "$ARCH" = "amd64" ]; then
  fetch ripgrep.deb "https://github.com/BurntSushi/ripgrep/releases/download/15.1.0/ripgrep_15.1.0-1_amd64.deb"
else
  echo "[skip] ripgrep.deb（arm64 走 apt 安装）"
fi
fetch fd.deb       "https://github.com/sharkdp/fd/releases/download/v10.4.2/fd_10.4.2_${ARCH}.deb"
fetch yq           "https://github.com/mikefarah/yq/releases/download/v4.44.3/yq_linux_${ARCH}"
fetch gitleaks.tar.gz "https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_${gitleaks_arch}.tar.gz"

# --- web attack helpers ---
fetch ysoserial.jar "https://github.com/frohoff/ysoserial/releases/download/v0.0.6/ysoserial-all.jar"
fetch jwt_tool.tar.gz "https://github.com/ticarpi/jwt_tool/archive/refs/heads/master.tar.gz"
fetch nikto.tar.gz  "https://github.com/sullo/nikto/archive/refs/heads/master.tar.gz"

# --- Android static analysis (APK -> source -> APIs) ---
# jadx: primary APK/dex decompiler. dex2jar: DEX->JAR for vineflower.
# vineflower: high-quality Java decompiler (maintained fernflower fork).
fetch jadx.zip     "https://github.com/skylot/jadx/releases/download/v1.5.1/jadx-1.5.1.zip"
fetch dex2jar.zip  "https://github.com/ThexXTURBOXx/dex2jar/releases/download/2.4.38/dex-tools-2.4.38.zip"
fetch vineflower.jar "https://github.com/Vineflower/vineflower/releases/download/1.11.0/vineflower-1.11.0.jar"

# --- nuclei templates (large; main branch tarball) ---
fetch nuclei-templates.tar.gz "https://github.com/projectdiscovery/nuclei-templates/archive/refs/heads/main.tar.gz"

echo
# PRINT_ONLY 只打印 URL，不能写标记（否则会把 .arch 污染成与实际内容不符的架构）
if [ "${PRINT_ONLY:-0}" != "1" ]; then
  echo "$ARCH" > "$VENDOR/.arch"
fi

if [ "$fail" = 0 ]; then
  echo "All artifacts present in $VENDOR (arch=$ARCH)"
  if [ "$ARCH" = "arm64" ]; then
    echo "Next: docker build --platform linux/arm64 -t sharp-worker:latest ."
  else
    echo "Next: docker build -t sharp-worker:latest ."
  fi
else
  echo "Some downloads FAILED. Fix network (set GH= proxy or https_proxy=) and re-run."
  exit 1
fi
