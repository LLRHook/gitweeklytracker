#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CLIPS_DIR="$SCRIPT_DIR/clips"
WORK_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

if ! command -v vhs >/dev/null 2>&1; then
  echo "ERROR: vhs is required. Install with: brew install charmbracelet/tap/vhs"
  exit 1
fi

mkdir -p "$CLIPS_DIR"

DEMO_DIR="$WORK_DIR/git-weekly-demo"
REPO_DIR="$DEMO_DIR/demo-repos/demo-api"
mkdir -p "$REPO_DIR"
cp "$ROOT/git-weekly-report.sh" "$DEMO_DIR/git-weekly-report.sh"

cd "$REPO_DIR"
git init -q
git -c user.name="Demo User" -c user.email="demo@example.com" commit --allow-empty -m "Initialize demo project" --date="$(date -u -v-2d '+%Y-%m-%dT10:00:00Z')" >/dev/null
printf "alpha\n" > app.txt
git add app.txt
GIT_AUTHOR_DATE="$(date -u -v-1d '+%Y-%m-%dT12:00:00Z')" \
GIT_COMMITTER_DATE="$(date -u -v-1d '+%Y-%m-%dT12:00:00Z')" \
git -c user.name="Demo User" -c user.email="demo@example.com" commit -m "Add app text" >/dev/null
printf "beta\n" >> app.txt
GIT_AUTHOR_DATE="$(date -u '+%Y-%m-%dT12:00:00Z')" \
GIT_COMMITTER_DATE="$(date -u '+%Y-%m-%dT12:00:00Z')" \
git -c user.name="Demo User" -c user.email="demo@example.com" commit -am "Expand app text" >/dev/null
cd "$DEMO_DIR"

THEME='{ "name": "weekly-report", "black": "#0b1020", "red": "#ff5d73", "green": "#37d39a", "yellow": "#ffd166", "blue": "#4f8cff", "magenta": "#a78bfa", "cyan": "#5eead4", "white": "#f7fbff", "brightBlack": "#6b7f99", "brightRed": "#ff8495", "brightGreen": "#7fffc5", "brightYellow": "#ffe199", "brightBlue": "#91b8ff", "brightMagenta": "#c4b5fd", "brightCyan": "#99f6e4", "brightWhite": "#ffffff", "background": "#0b1020", "foreground": "#f7fbff", "selection": "#26395c", "cursor": "#37d39a" }'

SETTINGS="Set Theme $THEME
Set FontFamily \"Menlo\"
Set LetterSpacing 0
Set LineHeight 1.2
Set FontSize 18
Set Width 1320
Set Height 860
Set Padding 22
Set WindowBar \"Colorful\"
Set BorderRadius 12
Set Framerate 30
Set TypingSpeed 26ms"

write_prelude() {
  cat <<TAPE
$SETTINGS

Hide
Type "cd '$DEMO_DIR'"
Enter
Sleep 200ms
Type "export PS1='\$ '"
Enter
Sleep 200ms
Type "clear"
Enter
Sleep 300ms
Show
TAPE
}

cat > "$WORK_DIR/01-scope.tape" <<TAPE
$(write_prelude)
Output "$CLIPS_DIR/01-scope.mp4"

Type "GIT_REPORT_EMAILS=demo@example.com"
Sleep 300ms
Enter
Type "GIT_REPORT_REPOS=./demo-repos"
Sleep 300ms
Enter
Type "git -C demo-repos/demo-api log --oneline --author demo@example.com -3"
Sleep 300ms
Enter
Sleep 2.8s
TAPE

cat > "$WORK_DIR/02-generate.tape" <<TAPE
$(write_prelude)
Output "$CLIPS_DIR/02-generate.mp4"

Type "GIT_REPORT_EMAILS=demo@example.com GIT_REPORT_REPOS=./demo-repos bash git-weekly-report.sh"
Sleep 400ms
Enter
Sleep 5.8s
TAPE

REPORT_FILE="weekly-report-$(date -u '+%Y-%m-%d').md"

cat > "$WORK_DIR/03-markdown.tape" <<TAPE
$(write_prelude)
Output "$CLIPS_DIR/03-markdown.mp4"

Type "GIT_REPORT_EMAILS=demo@example.com GIT_REPORT_REPOS=./demo-repos bash git-weekly-report.sh >/dev/null"
Enter
Sleep 700ms
Type "sed -n '1,42p' $REPORT_FILE"
Sleep 300ms
Enter
Sleep 4.2s
TAPE

cat > "$WORK_DIR/04-evidence.tape" <<TAPE
$(write_prelude)
Output "$CLIPS_DIR/04-evidence.mp4"

Type "grep -n 'Top Changed Files' -A8 $REPORT_FILE"
Sleep 300ms
Enter
Sleep 2.2s
Type "grep -n 'Daily Activity' -A9 $REPORT_FILE"
Sleep 300ms
Enter
Sleep 2.4s
TAPE

for tape in "$WORK_DIR"/*.tape; do
  echo "Recording $(basename "$tape")"
  vhs "$tape"
done

echo "Recorded clips in $CLIPS_DIR"
