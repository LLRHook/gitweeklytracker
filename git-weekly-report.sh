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
    echo "Or create a .env file (see .env.example) with:"
    echo '  GIT_REPORT_EMAILS="user1@example.com,user2@example.com"'
    echo ""
    echo "Repos are auto-discovered from your machine. To limit the scan, set:"
    echo '  GIT_REPORT_REPOS="~/projects,~/work/api-server"'
    exit 1
fi

# Split comma-separated emails into array, trimming whitespace
IFS=',' read -ra RAW_EMAILS <<< "$GIT_REPORT_EMAILS"
EMAILS=()
for e in "${RAW_EMAILS[@]}"; do
    trimmed="$(echo "$e" | xargs)"
    [[ -n "$trimmed" ]] && EMAILS+=("$trimmed")
done

if [[ ${#EMAILS[@]} -eq 0 ]]; then
    echo "Error: No valid emails found in GIT_REPORT_EMAILS."
    exit 1
fi

# ── Discover repositories ──────────────────────────────────
# Priority:
#   1. GIT_REPORT_REPOS env var (comma-separated paths / parent dirs)
#   2. Auto-discover all git repos on the machine, filtered to those
#      with commits from the configured emails in the past year

REPOS=()

add_repo_path() {
    local dir="$1"
    dir="${dir/#\~/$HOME}"
    dir="$(cd "$dir" 2>/dev/null && pwd)" || return
    if [[ -d "$dir/.git" ]] || git -C "$dir" rev-parse --is-inside-work-tree &>/dev/null 2>&1; then
        REPOS+=("$dir")
    else
        local child
        for child in "$dir"/*/; do
            [[ -d "$child/.git" ]] && REPOS+=("$(cd "$child" && pwd)")
        done
    fi
}

if [[ -n "${GIT_REPORT_REPOS:-}" ]]; then
    # Explicit repo list provided
    IFS=',' read -ra RAW_REPOS <<< "$GIT_REPORT_REPOS"
    for r in "${RAW_REPOS[@]}"; do
        trimmed="$(echo "$r" | xargs)"
        [[ -n "$trimmed" ]] && add_repo_path "$trimmed"
    done
else
    # Auto-discover: find all git repos under $HOME, then filter by author email
    echo "Auto-discovering repositories under $HOME..."

    # Search common project locations (depth-limited to stay fast)
    ALL_GIT_DIRS=()
    while IFS= read -r gitdir; do
        [[ -n "$gitdir" ]] && ALL_GIT_DIRS+=("$(dirname "$gitdir")")
    done < <(find "$HOME" -maxdepth 5 -name .git -type d \
        -not -path "*/node_modules/*" \
        -not -path "*/.cache/*" \
        -not -path "*/Library/*" \
        -not -path "*/.Trash/*" \
        -not -path "*/.cargo/*" \
        -not -path "*/.rustup/*" \
        2>/dev/null)

    echo "  Found ${#ALL_GIT_DIRS[@]} git repo(s), filtering by author emails..."

    for repo_dir in "${ALL_GIT_DIRS[@]+"${ALL_GIT_DIRS[@]}"}"; do
        for email in "${EMAILS[@]}"; do
            # Quick check: does this repo have any commits from this email?
            hit="$(git -C "$repo_dir" log --author="$email" --since="1 year ago" \
                --oneline -1 2>/dev/null || true)"
            if [[ -n "$hit" ]]; then
                REPOS+=("$repo_dir")
                break  # no need to check other emails for this repo
            fi
        done
    done
fi

if [[ ${#REPOS[@]} -eq 0 ]]; then
    echo "Error: No git repositories found with commits from the configured emails."
    echo "  Emails: ${EMAILS[*]}"
    echo ""
    echo "You can also set GIT_REPORT_REPOS to specify paths manually:"
    echo '  GIT_REPORT_REPOS="~/projects,~/work/api-server"'
    exit 1
fi

# Deduplicate repos
UNIQUE_REPOS=()
for r in "${REPOS[@]}"; do
    dupe=0
    for u in "${UNIQUE_REPOS[@]+"${UNIQUE_REPOS[@]}"}"; do
        [[ "$r" == "$u" ]] && dupe=1 && break
    done
    [[ $dupe -eq 0 ]] && UNIQUE_REPOS+=("$r")
done
REPOS=("${UNIQUE_REPOS[@]}")

echo "Scanning ${#REPOS[@]} repository(ies)..."
for r in "${REPOS[@]}"; do
    echo "  → $r"
done
echo ""

# Compute date range (UTC) — handle macOS vs Linux
if date -v-7d +%s &>/dev/null; then
    END_DATE="$(date -u '+%Y-%m-%d %H:%M')"
    END_ISO="$(date -u '+%Y-%m-%dT%H:%M:%S')"
    START_DATE="$(date -u -v-7d '+%Y-%m-%d %H:%M')"
    START_ISO="$(date -u -v-7d '+%Y-%m-%dT%H:%M:%S')"
    TODAY="$(date -u '+%Y-%m-%d')"
    IS_MACOS=1
else
    END_DATE="$(date -u '+%Y-%m-%d %H:%M')"
    END_ISO="$(date -u '+%Y-%m-%dT%H:%M:%S')"
    START_DATE="$(date -u -d '7 days ago' '+%Y-%m-%d %H:%M')"
    START_ISO="$(date -u -d '7 days ago' '+%Y-%m-%dT%H:%M:%S')"
    TODAY="$(date -u '+%Y-%m-%d')"
    IS_MACOS=0
fi

REPORT_FILE="weekly-report-${TODAY}.md"

# ── Data collection ─────────────────────────────────────────
# Parallel indexed arrays — index i corresponds to EMAILS[i]

A_COMMITS=()
A_INSERTIONS=()
A_DELETIONS=()
A_FILES=()
A_LOG=()        # log entries include repo name

TOTAL_COMMITS=0
TOTAL_INSERTIONS=0
TOTAL_DELETIONS=0

# Daily activity counters — 0=Mon .. 6=Sun
DAY_NAMES=(Mon Tue Wed Thu Fri Sat Sun)
DAY_COUNTS=(0 0 0 0 0 0 0)

day_index() {
    case "$1" in
        Mon) echo 0 ;; Tue) echo 1 ;; Wed) echo 2 ;; Thu) echo 3 ;;
        Fri) echo 4 ;; Sat) echo 5 ;; Sun) echo 6 ;; *) echo -1 ;;
    esac
}

ALL_CHANGED_FILES=""

# Track which repos had activity
ACTIVE_REPOS=""

# Initialize per-author accumulators
for i in "${!EMAILS[@]}"; do
    A_COMMITS+=("0")
    A_INSERTIONS+=("0")
    A_DELETIONS+=("0")
    A_FILES+=("0")
    A_LOG+=("")
done

# Iterate over every repo × every author
for repo in "${REPOS[@]}"; do
    repo_name="$(basename "$repo")"

    for i in "${!EMAILS[@]}"; do
        email="${EMAILS[$i]}"
        commits=0
        insertions=0
        deletions=0
        files_changed=0
        log_entries=""

        while IFS= read -r line; do
            [[ -z "$line" ]] && continue

            if [[ "$line" == COMMIT:* ]]; then
                line="${line#COMMIT:}"
                IFS='|' read -r hash cdate subject <<< "$line"
                commits=$((commits + 1))
                log_entries="${log_entries}${hash:0:7}|${cdate}|${subject}|${repo_name}"$'\n'

                commit_date_part="${cdate%% *}"
                if [[ "$IS_MACOS" -eq 1 ]]; then
                    dow="$(date -j -f '%Y-%m-%d' "$commit_date_part" '+%a' 2>/dev/null || echo "")"
                else
                    dow="$(date -d "$commit_date_part" '+%a' 2>/dev/null || echo "")"
                fi
                if [[ -n "$dow" ]]; then
                    di="$(day_index "$dow")"
                    if [[ "$di" -ge 0 ]]; then
                        DAY_COUNTS[$di]=$(( ${DAY_COUNTS[$di]} + 1 ))
                    fi
                fi
            elif [[ "$line" =~ ([0-9]+)\ file ]]; then
                if [[ "$line" =~ ([0-9]+)\ file.*changed ]]; then
                    files_changed=$((files_changed + BASH_REMATCH[1]))
                fi
                if [[ "$line" =~ ([0-9]+)\ insertion ]]; then
                    insertions=$((insertions + BASH_REMATCH[1]))
                fi
                if [[ "$line" =~ ([0-9]+)\ deletion ]]; then
                    deletions=$((deletions + BASH_REMATCH[1]))
                fi
            fi
        done < <(git -C "$repo" log --author="$email" --since="$START_ISO" --until="$END_ISO" \
            --format="COMMIT:%H|%ai|%s" --shortstat 2>/dev/null || true)

        A_COMMITS[$i]=$(( ${A_COMMITS[$i]} + commits ))
        A_INSERTIONS[$i]=$(( ${A_INSERTIONS[$i]} + insertions ))
        A_DELETIONS[$i]=$(( ${A_DELETIONS[$i]} + deletions ))
        A_FILES[$i]=$(( ${A_FILES[$i]} + files_changed ))
        A_LOG[$i]="${A_LOG[$i]}${log_entries}"

        TOTAL_COMMITS=$((TOTAL_COMMITS + commits))
        TOTAL_INSERTIONS=$((TOTAL_INSERTIONS + insertions))
        TOTAL_DELETIONS=$((TOTAL_DELETIONS + deletions))

        if [[ $commits -gt 0 ]]; then
            ACTIVE_REPOS="${ACTIVE_REPOS}${repo_name}"$'\n'
        fi

        # Collect changed file names (prefixed with repo name)
        changed="$(git -C "$repo" log --author="$email" --since="$START_ISO" --until="$END_ISO" \
            --name-only --format="" 2>/dev/null || true)"
        if [[ -n "$changed" ]]; then
            # Prefix each file with repo name for clarity
            while IFS= read -r f; do
                [[ -n "$f" ]] && ALL_CHANGED_FILES="${ALL_CHANGED_FILES}${repo_name}/${f}"$'\n'
            done <<< "$changed"
        fi
    done
done

NET_CHANGE=$((TOTAL_INSERTIONS - TOTAL_DELETIONS))

# Count unique active repos
ACTIVE_REPO_COUNT=0
if [[ -n "$ACTIVE_REPOS" ]]; then
    ACTIVE_REPO_COUNT="$(echo "$ACTIVE_REPOS" | grep -v '^$' | sort -u 2>/dev/null | wc -l | xargs)"
fi

# Top 10 most-changed files
TOP_FILES=""
if [[ -n "$ALL_CHANGED_FILES" ]]; then
    TOP_FILES="$(echo "$ALL_CHANGED_FILES" | grep -v '^$' | sort 2>/dev/null | uniq -c | sort -rn 2>/dev/null | head -10 || true)"
fi

# Longest commit streak
ALL_COMMIT_DATES=""
for repo in "${REPOS[@]}"; do
    for email in "${EMAILS[@]}"; do
        dates="$(git -C "$repo" log --author="$email" --since="$START_ISO" --until="$END_ISO" \
            --format="%ai" 2>/dev/null | awk '{print $1}' || true)"
        [[ -n "$dates" ]] && ALL_COMMIT_DATES="${ALL_COMMIT_DATES}${dates}"$'\n'
    done
done

LONGEST_STREAK=0
if [[ -n "$ALL_COMMIT_DATES" ]]; then
    UNIQUE_DATES="$(echo "$ALL_COMMIT_DATES" | grep -v '^$' | sort -u)"
    streak=1
    max_streak=1
    prev=""
    while IFS= read -r d; do
        [[ -z "$d" ]] && continue
        if [[ -n "$prev" ]]; then
            if [[ "$IS_MACOS" -eq 1 ]]; then
                prev_epoch="$(date -j -f '%Y-%m-%d' "$prev" '+%s' 2>/dev/null || echo 0)"
                curr_epoch="$(date -j -f '%Y-%m-%d' "$d" '+%s' 2>/dev/null || echo 0)"
            else
                prev_epoch="$(date -d "$prev" '+%s' 2>/dev/null || echo 0)"
                curr_epoch="$(date -d "$d" '+%s' 2>/dev/null || echo 0)"
            fi
            diff_days=$(( (curr_epoch - prev_epoch) / 86400 ))
            if [[ $diff_days -eq 1 ]]; then
                streak=$((streak + 1))
            else
                streak=1
            fi
            [[ $streak -gt $max_streak ]] && max_streak=$streak
        fi
        prev="$d"
    done <<< "$UNIQUE_DATES"
    LONGEST_STREAK=$max_streak
fi

# ── Helper: bar chart ───────────────────────────────────────

bar_chart() {
    local count=$1
    local max=$2
    local width=20
    if [[ $max -eq 0 ]]; then
        echo ""
        return
    fi
    local filled=$(( (count * width) / max ))
    local bar=""
    for ((j = 0; j < filled; j++)); do
        bar="${bar}█"
    done
    echo "$bar"
}

MAX_DAY=0
for di in 0 1 2 3 4 5 6; do
    [[ ${DAY_COUNTS[$di]} -gt $MAX_DAY ]] && MAX_DAY=${DAY_COUNTS[$di]}
done

# ── Terminal output ─────────────────────────────────────────

print_terminal() {
    echo "═══════════════════════════════════════════"
    echo " Git Weekly Report"
    echo " ${START_DATE} UTC → ${END_DATE} UTC"
    echo " ${#REPOS[@]} repo(s) scanned, ${ACTIVE_REPO_COUNT} with activity"
    echo "═══════════════════════════════════════════"
    echo ""

    for i in "${!EMAILS[@]}"; do
        echo "Author: ${EMAILS[$i]}"
        echo "─────────────────────────────"
        printf "  %-18s %s\n" "Commits:" "${A_COMMITS[$i]}"
        printf "  %-18s %s\n" "Files Changed:" "${A_FILES[$i]}"
        printf "  %-18s +%s\n" "Insertions:" "${A_INSERTIONS[$i]}"
        printf "  %-18s -%s\n" "Deletions:" "${A_DELETIONS[$i]}"
        echo ""
    done

    echo "── Summary ──────────────────"
    printf "  %-18s %s\n" "Total Commits:" "$TOTAL_COMMITS"
    printf "  %-18s +%s\n" "Total Insertions:" "$TOTAL_INSERTIONS"
    printf "  %-18s -%s\n" "Total Deletions:" "$TOTAL_DELETIONS"
    if [[ $NET_CHANGE -ge 0 ]]; then
        printf "  %-18s +%s\n" "Net Change:" "$NET_CHANGE"
    else
        printf "  %-18s %s\n" "Net Change:" "$NET_CHANGE"
    fi
    printf "  %-18s %s day(s)\n" "Longest Streak:" "$LONGEST_STREAK"
    echo ""

    if [[ -n "$TOP_FILES" ]]; then
        echo "── Top Changed Files ────────"
        while IFS= read -r line; do
            count="$(echo "$line" | awk '{print $1}')"
            file="$(echo "$line" | awk '{$1=""; print $0}' | xargs)"
            printf "  %4s  %s\n" "$count" "$file"
        done <<< "$TOP_FILES"
        echo ""
    fi

    echo "── Daily Activity ───────────"
    for di in 0 1 2 3 4 5 6; do
        c=${DAY_COUNTS[$di]}
        b="$(bar_chart "$c" "$MAX_DAY")"
        printf "  %s: %-20s %s\n" "${DAY_NAMES[$di]}" "$b" "$c"
    done
    echo ""

    echo "Report saved to: ${REPORT_FILE}"
}

# ── Markdown output ─────────────────────────────────────────

generate_markdown() {
    {
        echo "# Git Weekly Report"
        echo ""
        echo "**Period:** ${START_DATE} UTC → ${END_DATE} UTC"
        echo ""
        echo "**Repositories scanned:** ${#REPOS[@]} (${ACTIVE_REPO_COUNT} with activity)"
        echo ""

        # List repos
        echo "<details>"
        echo "<summary>Repository list</summary>"
        echo ""
        for r in "${REPOS[@]}"; do
            echo "- \`$r\`"
        done
        echo ""
        echo "</details>"
        echo ""

        echo "## Per-Author Breakdown"
        echo ""
        echo "| Author | Commits | Files Changed | Insertions | Deletions |"
        echo "|--------|---------|---------------|------------|-----------|"
        for i in "${!EMAILS[@]}"; do
            echo "| ${EMAILS[$i]} | ${A_COMMITS[$i]} | ${A_FILES[$i]} | +${A_INSERTIONS[$i]} | -${A_DELETIONS[$i]} |"
        done
        echo ""

        echo "## Summary"
        echo ""
        echo "| Metric | Value |"
        echo "|--------|-------|"
        echo "| Total Commits | ${TOTAL_COMMITS} |"
        echo "| Total Insertions | +${TOTAL_INSERTIONS} |"
        echo "| Total Deletions | -${TOTAL_DELETIONS} |"
        if [[ $NET_CHANGE -ge 0 ]]; then
            echo "| Net Change | +${NET_CHANGE} |"
        else
            echo "| Net Change | ${NET_CHANGE} |"
        fi
        echo "| Longest Streak | ${LONGEST_STREAK} day(s) |"
        echo ""

        if [[ -n "$TOP_FILES" ]]; then
            echo "## Top Changed Files"
            echo ""
            echo "| Changes | File |"
            echo "|---------|------|"
            while IFS= read -r line; do
                count="$(echo "$line" | awk '{print $1}')"
                file="$(echo "$line" | awk '{$1=""; print $0}' | xargs)"
                echo "| ${count} | \`${file}\` |"
            done <<< "$TOP_FILES"
            echo ""
        fi

        echo "## Daily Activity"
        echo ""
        echo "| Day | Commits |"
        echo "|-----|---------|"
        for di in 0 1 2 3 4 5 6; do
            echo "| ${DAY_NAMES[$di]} | ${DAY_COUNTS[$di]} |"
        done
        echo ""

        echo "## Commit Log"
        echo ""
        for i in "${!EMAILS[@]}"; do
            log="${A_LOG[$i]}"
            if [[ -n "$log" ]]; then
                echo "### ${EMAILS[$i]}"
                echo ""
                echo "| Hash | Date | Message | Repo |"
                echo "|------|------|---------|------|"
                while IFS= read -r entry; do
                    [[ -z "$entry" ]] && continue
                    IFS='|' read -r hash cdate subject repo_name <<< "$entry"
                    echo "| \`${hash}\` | ${cdate} | ${subject} | ${repo_name} |"
                done <<< "$log"
                echo ""
            fi
        done

        echo "---"
        echo "*Generated on $(date -u '+%Y-%m-%d %H:%M UTC')*"
    } > "$REPORT_FILE"
}

# ── Main ────────────────────────────────────────────────────

print_terminal
generate_markdown
