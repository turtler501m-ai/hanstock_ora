#!/usr/bin/env bash
set -euo pipefail

profile="${1:-a}"
if [[ "$#" -gt 0 ]]; then
  shift
fi

case "$profile" in
  a|b|c) ;;
  *)
    echo "Usage: $0 {a|b|c} [codex args...]" >&2
    exit 2
    ;;
esac

if ! command -v codex >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Codex CLI was not found in PATH.

Install it on this VM with:
  curl -fsSL https://chatgpt.com/codex/install.sh | sh

Then open a new shell or reload your PATH and run this command again.
EOF
  exit 127
fi

export CODEX_HOME="${CODEX_HOME:-$HOME/.codex-$profile}"
mkdir -p "$CODEX_HOME"
exec codex --dangerously-bypass-approvals-and-sandbox "$@"
