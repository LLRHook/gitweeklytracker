#!/usr/bin/env bash
set -euo pipefail

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
    echo "Usage: GIT_REPORT_EMAILS=\"user1@example.com,user2@example.com\" bash $0"
    echo ""
    echo "Or create a .env file with:"
    echo '  GIT_REPORT_EMAILS="user1@example.com,user2@example.com"'
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

# Compute date range (UTC) — handle macOS vs Linux
if date -v-7d +%s &>/dev/null; then
    # macOS
    END_DATE="$(date -u '+%Y-%m-%d %H:%M')"
    END_ISO="$(date -u '+%Y-%m-%dT%H:%M:%S')"
    START_DATE="$(date -u -v-7d '+%Y-%m-%d %H:%M')"
    START_ISO="$(date -u -v-7d '+%Y-%m-%dT%H:%M:%S')"
    TODAY="$(date -u '+%Y-%m-%d')"
    IS_MACOS=1
else
    # Linux
    END_DATE="$(date -u '+%Y-%m-%d %H:%M')"
    END_ISO="$(date -u '+%Y-%m-%dT%H:%M:%S')"
    START_DATE="$(date -u -d '7 days ago' '+%Y-%m-%d %H:%M')"
    START_ISO="$(date -u -d '7 days ago' '+%Y-%m-%dT%H:%M:%S')"
    TODAY="$(date -u '+%Y-%m-%d')"
    IS_MACOS=0
fi

REPORT_FILE="weekly-report-${TODAY}.md"

# Verify we're in a git repo
if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    echo "Error: Not inside a git repository."
    exit 1
fi

# ── Data collection ─────────────────────────────────────────
# Use parallel indexed arrays instead of associative arrays (Bash 3 compat)
# Index i corresponds to EMAILS[i]

A_COMMITS=()    # per-author commit count
A_INSERTIONS=() # per-author insertions
A_DELETIONS=()  # per-author deletions
A_FILES=()      # per-author files changed
A_LOG=()        # per-author commit log entries

TOTAL_COMMITS=0
TOTAL_INSERTIONS=0
TOTAL_DELETIONS=0

# Daily activity counters — indexed 0=Mon 1=Tue ... 6=Sun
DAY_NAMES=(Mon Tue Wed Thu Fri Sat Sun)
DAY_COUNTS=(0 0 0 0 0 0 0)

# Map 3-letter day abbreviation to index
day_index() {
    case "$1" in
        Mon) echo 0 ;; Tue) echo 1 ;; Wed) echo 2 ;; Thu) echo 3 ;;
        Fri) echo 4 ;; Sat) echo 5 ;; Sun) echo 6 ;; *) echo -1 ;;
    esac
}

# Collect all changed files across authors for "top changed files"
ALL_CHANGED_FILES=""

for i in "${!EMAILS[@]}"; do
    email="${EMAILS[$i]}"
    commits=0
    insertions=0
    deletions=0
    files_changed=0
    log_entries=""

    # Read git log with shortstat — prefix commit lines with COMMIT: marker
    while IFS= read -r line; do
        if [[ -z "$line" ]]; then
            continue
        fi

        if [[ "$line" == COMMIT:* ]]; then
            # Strip marker and parse: hash|date|subject
            line="${line#COMMIT:}"
            IFS='|' read -r hash cdate subject <<< "$line"
            commits=$((commits + 1))
            log_entries="${log_entries}${hash:0:7}|${cdate}|${subject}"$'\n'

            # Track daily activity from commit date
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
            # shortstat line
            if [[ "$line" =~ ([0-9]+)\ file.*changed ]]; then
                fc="${BASH_REMATCH[1]}"
                files_changed=$((files_changed + fc))
            fi
            if [[ "$line" =~ ([0-9]+)\ insertion ]]; then
                ins="${BASH_REMATCH[1]}"
                insertions=$((insertions + ins))
            fi
            if [[ "$line" =~ ([0-9]+)\ deletion ]]; then
                del="${BASH_REMATCH[1]}"
                deletions=$((deletions + del))
            fi
        fi
    done < <(git log --author="$email" --since="$START_ISO" --until="$END_ISO" \
        --format="COMMIT:%H|%ai|%s" --shortstat 2>/dev/null || true)

    A_COMMITS+=("$commits")
    A_INSERTIONS+=("$insertions")
    A_DELETIONS+=("$deletions")
    A_FILES+=("$files_changed")
    A_LOG+=("$log_entries")

    TOTAL_COMMITS=$((TOTAL_COMMITS + commits))
    TOTAL_INSERTIONS=$((TOTAL_INSERTIONS + insertions))
    TOTAL_DELETIONS=$((TOTAL_DELETIONS + deletions))

    # Collect changed file names
    changed="$(git log --author="$email" --since="$START_ISO" --until="$END_ISO" \
        --name-only --format="" 2>/dev/null || true)"
    if [[ -n "$changed" ]]; then
        ALL_CHANGED_FILES="${ALL_CHANGED_FILES}${changed}"$'\n'
    fi
done

NET_CHANGE=$((TOTAL_INSERTIONS - TOTAL_DELETIONS))

# Top 10 most-changed files
TOP_FILES=""
if [[ -n "$ALL_CHANGED_FILES" ]]; then
    TOP_FILES="$(echo "$ALL_CHANGED_FILES" | grep -v '^$' | sort | uniq -c | sort -rn | head -10)"
fi

# Longest commit streak (consecutive days with commits in the week)
ALL_COMMIT_DATES=""
for email in "${EMAILS[@]}"; do
    dates="$(git log --author="$email" --since="$START_ISO" --until="$END_ISO" \
        --format="%ai" 2>/dev/null | awk '{print $1}' || true)"
    if [[ -n "$dates" ]]; then
        ALL_COMMIT_DATES="${ALL_COMMIT_DATES}${dates}"$'\n'
    fi
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
            if [[ $streak -gt $max_streak ]]; then
                max_streak=$streak
            fi
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

# Find max day count for bar scaling
MAX_DAY=0
for di in 0 1 2 3 4 5 6; do
    [[ ${DAY_COUNTS[$di]} -gt $MAX_DAY ]] && MAX_DAY=${DAY_COUNTS[$di]}
done

# ── Terminal output ─────────────────────────────────────────

print_terminal() {
    echo "═══════════════════════════════════════════"
    echo " Git Weekly Report"
    echo " ${START_DATE} UTC → ${END_DATE} UTC"
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
                echo "| Hash | Date | Message |"
                echo "|------|------|---------|"
                while IFS= read -r entry; do
                    [[ -z "$entry" ]] && continue
                    IFS='|' read -r hash cdate subject <<< "$entry"
                    echo "| \`${hash}\` | ${cdate} | ${subject} |"
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
