#!/bin/bash

set -euo pipefail

# Sharp one-click start script (CyberStrikeAI-style facade).
#
# This is a THIN SHELL over the cross-platform Python launcher (./sharp):
# it adds a friendly banner, colored output, pre-flight environment checks
# with per-OS install guidance, and a China mirror default for uv — then
# `exec`s into ./sharp so the signal chain reaches the process supervisor
# directly (Ctrl+C reaches server+dispatcher without an extra shell layer).
#
# The heavy lifting stays in ./sharp (uv discovery/sync, secrets validation,
# docker worker-image build, server+dispatcher supervision, Windows support).
# Windows users keep using ./sharp directly.

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"
LAUNCHER="$ROOT_DIR/sharp"

# ---- Colors -----------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()    { echo -e "${BLUE}ℹ️  $1${NC}"; }
success() { echo -e "${GREEN}✅ $1${NC}"; }
warning() { echo -e "${YELLOW}⚠️  $1${NC}"; }
error()   { echo -e "${RED}❌ $1${NC}"; }
note()    { echo -e "${CYAN}ℹ️  $1${NC}"; }

# ---- Mirror (this script run only; never touches system config) -------------
# External value wins: a user who already exports UV_DEFAULT_INDEX keeps theirs.
UV_INDEX_DEFAULT="${SHARP_UV_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

print_banner() {
    echo ""
    echo "=========================================="
    echo "  Sharp Deploy & Start Script"
    echo "  (thin facade; heavy lifting in ./sharp)"
    echo ""
    echo "  default: desktop app (./run.sh web for web UI)"
    echo "=========================================="
    echo ""
    info "uv mirror (this run only): ${UV_INDEX_DEFAULT}"
    echo ""
}

# ---- Pre-flight checks -------------------------------------------------------
# python3 >= 3.12 (matches runtime/pyproject.toml requires-python)
check_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        error "python3 not found"
        echo ""
        info "Install Python 3.12 or later first:"
        echo "  macOS:   brew install python3"
        echo "  Ubuntu:  sudo apt-get install python3 python3-venv"
        echo "  CentOS:  sudo yum install python3"
        exit 1
    fi
    PYTHON_VERSION="$(python3 -c 'import sys; print("{}.{}".format(sys.version_info[0], sys.version_info[1]))')"
    if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
        success "Python check passed: $PYTHON_VERSION (>= 3.12)"
    else
        error "Python version too old: $PYTHON_VERSION (requires 3.12+)"
        echo ""
        info "Upgrade Python first (see runtime/pyproject.toml requires-python)."
        exit 1
    fi
}

# uv — the launcher drives everything through uv; install guidance per OS.
check_uv() {
    if command -v uv >/dev/null 2>&1; then
        success "uv found: $(uv --version 2>/dev/null || echo ok)"
        return 0
    fi
    if [ -n "${UV_BIN:-}" ] && [ -x "$UV_BIN" ]; then
        success "uv found via UV_BIN: $UV_BIN"
        return 0
    fi
    error "uv not found"
    echo ""
    info "Sharp drives all dependency handling through uv. Install it first:"
    echo "  macOS:   brew install uv"
    echo "  Linux:   curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  Windows: use ./sharp directly (winget install astral-sh.uv)"
    echo ""
    info "Then reopen the terminal (or re-source the PATH) and retry."
    exit 1
}

# Docker — WARNING only, never fatal: `run.sh web --no-dispatch` still works
# for initial configuration, and the operator may start Docker later.
check_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        warning "docker CLI not found — worker containers cannot run in this state"
        echo ""
        info "The web/API server still starts (web --no-dispatch); to fully run"
        info " Sharp install Docker:"
        echo "  macOS:   https://www.docker.com/products/docker-desktop/"
        echo "  Ubuntu:  sudo apt-get install docker.io"
        echo "  CentOS:  sudo yum install docker"
        echo ""
        return 0
    fi
    if ! docker info >/dev/null 2>&1; then
        warning "Docker daemon is not running — the launcher will fail on worker-image setup."
        info "Start Docker Desktop (or 'sudo systemctl start docker'), then retry."
        return 0
    fi
    success "Docker daemon is running"
}

# ---- Main --------------------------------------------------------------------
main() {
    # No args = desktop mode (same default as ./sharp, zero surprise).
    # Unknown/other args pass straight through to ./sharp.
    print_banner
    info "Checking runtime environment..."
    if command -v python3 >/dev/null 2>&1; then
        check_python
    else
        error "python3 not found"
        exit 1
    fi

    check_uv
    check_docker
    echo ""

    export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-$UV_INDEX_DEFAULT}"

    if [ ! -x "$LAUNCHER" ]; then
        chmod +x "$LAUNCHER" 2>/dev/null || true
    fi
    if [ ! -x "$LAUNCHER" ]; then
        error "launcher not found or not executable: ./sharp"
        exit 1
    fi

    success "environment OK — handing off to ./sharp"
    echo ""
    # exec: signal chain reaches ./sharp directly; it supervises the server +
    # dispatcher pair (Ctrl+C tears both down cleanly).
    exec "$LAUNCHER" "$@"
}

main "$@"
