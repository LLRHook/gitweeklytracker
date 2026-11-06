#!/usr/bin/env bash
set -euo pipefail
trap '' PIPE  # ignore SIGPIPE so piped output doesn't kill the script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if it exists
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
fi

# Validate GIT_REPORT_EMAILS
if [[ -z "${GIT_REPORT_EMAILS:-}" ]]; then
    echo "Error: GIT_REPORT_EMAILS is not set."
    echo ""
    echo "Usage:"
    echo "  GIT_REPORT_EMAILS=\"user@example.com\" bash $0"
    echo ""
    echo "Or create a .env file next to the script with:"
    echo '  GIT_REPORT_EMAILS="user1@example.com,user2@example.com"'
    echo ""
    echo "Repos are auto-discovered from your machine. To limit the scan, set:"
    echo '  GIT_REPORT_REPOS="~/projects,~/work/api-server"'
    exit 1
fi

# Python presents the report; discovery, Git collection and aggregation run once
# in the Rust processor. Export config loaded from .env for the child process.
export GIT_REPORT_EMAILS
export GIT_REPORT_REPOS="${GIT_REPORT_REPOS:-}"
if [[ -n "${GIT_TRACKER_BIN:-}" ]]; then
    export GIT_TRACKER_BIN
fi
exec python3 "$SCRIPT_DIR/git_report.py" --now "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
