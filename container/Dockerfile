# Sharp worker image (vendored / offline-friendly build).
# Debian-slim base, a single agent CLI (claude-code), and core web scanners.
#
# All GitHub artifacts are pre-downloaded on the host into ./vendor/ by
# fetch_vendor.sh and COPYed in here, so the build itself never touches GitHub.
# apt uses a China mirror; pip/npm/node use China mirrors too.
# Run:  ./fetch_vendor.sh   then   docker build -t sharp-worker:latest ./container

# 目标平台（P2）：镜像架构由 `docker build --platform` 决定（默认跟随本机）。
#   Apple Silicon 原生 arm64（省掉 qemu，扫描器快数倍）：
#     cd container && ARCH=arm64 ./fetch_vendor.sh
#     docker build --platform linux/arm64 -t sharp-worker:latest .
#   amd64（部署到 x86 服务器或旧镜像）：
#     cd container && ARCH=amd64 ./fetch_vendor.sh
#     docker build --platform linux/amd64 -t sharp-worker:latest .
# ⚠️ vendor/ 里的二进制架构必须与 --platform 一致（脚本的 .arch 标记会拦住混用），
#    否则容器里会 exec format error。
#
# 注：不使用 BuildKit 的 TARGETPLATFORM/TARGETARCH 自动参数——本机 docker 没有
# buildx，经典 builder 不填充它们，会静默回落到默认值（踩过一次：构建出一半 amd64 层）。
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_TRUSTED_HOST=mirrors.aliyun.com
ENV npm_config_registry=https://registry.npmmirror.com
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV NODE_VERSION=20.18.0

# --- Layer 0: China Debian mirror ---------------------------------------------
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list; \
    fi

# --- Layer 1: engine runtime + process-control deps ---------------------------
# coreutils(timeout), util-linux(setsid), procps(pkill) are REQUIRED by the
# dispatcher exec-timeout wrapper and KILL-1 session-kill. Do NOT remove them.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl wget git unzip xz-utils jq \
      coreutils util-linux procps \
      nmap ncat perl \
      libpcap0.8 \
      default-jre-headless \
      adb apktool \
      sudo tzdata \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# node from npmmirror (China-fast; avoids NodeSource which is flaky in CN)
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      arm64) NODE_ARCH=arm64 ;; \
      amd64) NODE_ARCH=x64 ;; \
      *) echo "unsupported arch: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    echo "installing node linux-${NODE_ARCH}"; \
    curl -fL "https://cdn.npmmirror.com/binaries/node/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" -o /tmp/node.tar.xz; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \
    rm /tmp/node.tar.xz; \
    node --version; npm --version

RUN useradd -m -s /bin/bash kali && \
    echo "kali ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# --- Layer 2: vendored artifacts (pre-downloaded on host into ./vendor/) ------
# Nothing below touches GitHub; everything comes from the COPYed vendor dir.
COPY ./vendor /tmp/vendor

# Go/CLI tool binaries — extract each into its own dir, then install the largest
# executable file found (handles names like dalfox-linux-amd64, nested layouts).
RUN set -eux; cd /tmp/vendor; \
    for d in katana nuclei httpx naabu dalfox ffuf; do \
      # 上游未提供 linux/arm64 构建的产物（当前：naabu）在 arm64 下会缺席，
      # 这里显式跳过并留日志，而不是让整层构建失败。
      if [ ! -s "${d}.zip" ] && [ ! -s "${d}.tar.gz" ]; then \
        echo "warn: ${d} 不在 vendor 中（该架构上游可能未提供构建），跳过"; continue; \
      fi; \
      case "$d" in \
        katana|nuclei|httpx|naabu) mkdir -p "x_$d" && unzip -q "${d}.zip" -d "x_$d" ;; \
        dalfox|ffuf) mkdir -p "x_$d" && tar -xzf "${d}.tar.gz" -C "x_$d" ;; \
      esac; \
      bin="$(find "x_$d" -type f -perm -u+x ! -name '*.md' ! -name 'LICENSE*' ! -name '*.txt' | xargs -r ls -S 2>/dev/null | head -n1)"; \
      test -n "$bin"; \
      install -m 0755 "$bin" "/usr/local/bin/$d"; \
    done; \
    katana -version >/dev/null 2>&1 || true; \
    nuclei -version >/dev/null 2>&1 || true

# Small utilities: ripgrep, fd (debs), yq (bin), gitleaks (tar)
RUN set -eux; cd /tmp/vendor; \
    apt-get update; \
    if [ -s ripgrep.deb ]; then apt-get install -y --no-install-recommends ./ripgrep.deb; \
    else apt-get install -y --no-install-recommends ripgrep; fi; \
    if [ -s fd.deb ]; then apt-get install -y --no-install-recommends ./fd.deb; \
    else apt-get install -y --no-install-recommends fd-find && ln -sf "$(command -v fdfind)" /usr/local/bin/fd; fi; \
    apt-get clean && rm -rf /var/lib/apt/lists/*; \
    install -m 0755 yq /usr/local/bin/yq; \
    tar -xzf gitleaks.tar.gz gitleaks && mv gitleaks /usr/local/bin/ && chmod +x /usr/local/bin/gitleaks

# Web attack helpers: dirsearch + mitmproxy + httpie + sqlmap (pip, PyPI always
# accessible; pure-Python or deep-dep packages — not worth vendoring).
# jwt_tool + nikto + ysoserial are vendored (GitHub source tarballs / jar).
RUN set -eux; \
    pip3 install --no-cache-dir dirsearch mitmproxy httpie sqlmap pycryptodome gmssl; \
    mkdir -p /home/kali/tools; \
    tar -xzf /tmp/vendor/jwt_tool.tar.gz -C /home/kali/tools; \
    mv /home/kali/tools/jwt_tool* /home/kali/tools/jwt_tool; \
    pip3 install --no-cache-dir -r /home/kali/tools/jwt_tool/requirements.txt; \
    cp /tmp/vendor/ysoserial.jar /home/kali/tools/ysoserial.jar; \
    tar -xzf /tmp/vendor/nikto.tar.gz -C /home/kali/tools; \
    mv /home/kali/tools/nikto* /home/kali/tools/nikto; \
    printf '#!/bin/sh\nexec perl /home/kali/tools/nikto/program/nikto.pl "$@"\n' > /usr/local/bin/nikto; \
    chmod +x /usr/local/bin/nikto

# Agent CLI (claude-code) via npm + npmmirror (no GitHub)
RUN set -eux; \
    npm install -g @anthropic-ai/claude-code@2.1.98; \
    npm cache clean --force; \
    rm -rf /root/.npm

# nuclei templates (vendored archive) + config
RUN set -eux; \
    mkdir -p /home/kali/.local; \
    tar -xzf /tmp/vendor/nuclei-templates.tar.gz -C /home/kali/.local; \
    mv /home/kali/.local/nuclei-templates* /home/kali/.local/nuclei-templates; \
    mkdir -p /home/kali/.config/nuclei; \
    printf '%s\n' 'disable-update-check: true' 'update-template-dir: /home/kali/.local/nuclei-templates' >> /home/kali/.config/nuclei/config.yaml; \
    chown -R kali:kali /home/kali

# --- Layer 2b: Android app static-analysis chain (APK -> Java + DEX unpacking) -
# jadx (APK/DEX -> Java), dex2jar (DEX -> JAR for vineflower), vineflower
# (high-quality Java decompiler). All vendored; no GitHub access at build time.
# frida-tools (pip) is the CLIENT side for dynamic analysis — the frida-server
# and panda-dex-dumper binaries run ON THE DEVICE (ARM). USB is NOT visible to
# containers, so adb/frida must reach a rooted device over TCP:
#   host USB device:  ADB_SERVER_SOCKET=tcp:host.docker.internal:5037  (host runs `adb server`)
#   wireless adb:     adb connect <phone-ip>:<port>
#   frida remote:     frida -H <phone-ip-or-forwarded>:27042
ENV ANDROID_TOOLS=/home/kali/tools/android
RUN set -eux; \
    mkdir -p "$ANDROID_TOOLS"; \
    # jadx
    unzip -q /tmp/vendor/jadx.zip -d "$ANDROID_TOOLS/jadx"; \
    ln -sf "$ANDROID_TOOLS/jadx/bin/jadx" /usr/local/bin/jadx; \
    ln -sf "$ANDROID_TOOLS/jadx/bin/jadx-gui" /usr/local/bin/jadx-gui; \
    # dex2jar (accept both dex2jar.zip and versioned dex-tools-*.zip names)
    unzip -q "$(ls /tmp/vendor/dex2jar.zip /tmp/vendor/dex-tools*.zip 2>/dev/null | head -n1)" -d "$ANDROID_TOOLS"; \
    mv "$ANDROID_TOOLS"/dex-tools* "$ANDROID_TOOLS/dex2jar"; \
    chmod +x "$ANDROID_TOOLS"/dex2jar/*.sh; \
    for s in "$ANDROID_TOOLS"/dex2jar/d2j-*.sh; do \
      ln -sf "$s" "/usr/local/bin/$(basename "$s" .sh)"; \
    done; \
    # vineflower (accept both vineflower.jar and versioned vineflower-*.jar names)
    cp "$(ls /tmp/vendor/vineflower.jar /tmp/vendor/vineflower-*.jar 2>/dev/null | head -n1)" "$ANDROID_TOOLS/vineflower.jar"; \
    # frida client tooling (device-side frida-server pushed separately over adb)
    pip3 install --no-cache-dir frida-tools; \
    rm -rf /tmp/vendor; \
    chown -R kali:kali /home/kali
ENV FERNFLOWER_JAR_PATH=/home/kali/tools/android/vineflower.jar

# Device-side binaries pushed to a rooted phone over adb (NOT executed in-container;
# ARM aarch64). e.g. panda-dex-dumper for whole-APK unpacking / DEX memory dump.
COPY ./device-tools /home/kali/tools/device
RUN chown -R kali:kali /home/kali/tools/device

RUN rm -rf /tmp/vendor

# --- Layer 3: agent workspace + instructions ----------------------------------
COPY ./.agents /home/kali/workspace/.agents
COPY ./.agents /home/kali/workspace/.claude
COPY ./AGENTS.md /home/kali/workspace/AGENTS.md
COPY ./AGENTS.md /home/kali/workspace/CLAUDE.md
# Pre-built testing helpers available to all workers.
# session_manager.py: multi-account HTTP session management for IDOR/privilege-escalation testing.
#   Usage: import sys; sys.path.insert(0, '/home/kali/tools'); from session_manager import SessionManager
COPY ./session_manager.py /home/kali/tools/session_manager.py
# crypto_helper.py: AES/DES/3DES/SM4 decrypt-encrypt + SM3/MD5/HMAC/JWT/encoding helper
#   for targets with encrypted request bodies or signed params. Needs pycryptodome + gmssl.
#   Usage: python3 /home/kali/tools/crypto_helper.py aes --op dec --mode cbc --key-hex .. --iv-hex .. --in-b64 ..
COPY ./crypto_helper.py /home/kali/tools/crypto_helper.py
# Earlier root-owned COPY/RUN steps left /home/kali itself owned by root, so the
# unprivileged kali user could not create its HOME config dir (~/.claude), which
# made `claude` silently hang on startup. Fix ownership of the HOME root and
# pre-create the claude config/debug dirs so the agent CLI can initialise.
RUN mkdir -p /home/kali/.claude/debug && \
    chown -R kali:kali /home/kali

USER kali
RUN cd /home/kali/workspace && git init -q && \
    git config user.email "agent@sharp.local" && git config user.name "sharp-agent"
WORKDIR /home/kali/workspace

# --- OpenAI Codex CLI (codex worker) ---------------------------------------
# The codex driver runs `codex exec` with all provider settings passed as
# command-line config (-c ...); the API key is read from the OPENAI_API_KEY
# environment variable (docker exec injects worker.env). Pinned like claude.
USER root
RUN npm install -g @openai/codex@0.153.4 && \
    npm cache clean --force
USER kali
