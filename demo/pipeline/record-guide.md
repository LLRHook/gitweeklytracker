# Recording Guide - Git Weekly Report Demo

This demo is fully automatable with VHS. It uses a temporary fixture repository
with synthetic commits from `demo@example.com`, so it does not read personal
repos, private `.env` values, or live work data.

## Automated Capture

```bash
cd "/Users/victorivanov/Documents/personal projects/gitweeklytracker"
bash demo/pipeline/setup.sh
bash demo/pipeline/record-clips.sh
bash demo/pipeline/make-demo.sh
```

Expected output:

```text
demo/pipeline/output/gitweeklytracker-demo.mp4
demo/pipeline/output/preview.png
```

## Demo Arc

1. `01-scope.mp4` - Show explicit `GIT_REPORT_EMAILS` and `GIT_REPORT_REPOS`
   scope, then inspect recent commits in the fixture repo.
2. `02-generate.mp4` - Run `git-weekly-report.sh` and show the terminal report:
   author totals, repo table, summary, top files, and daily activity.
3. `03-markdown.mp4` - Open the generated Markdown report and show the same data
   as a shareable artifact.
4. `04-evidence.mp4` - Pull out top changed files and daily activity, the two
   sections that make the weekly narrative quick to write.

## Manual Capture Fallback

Use a clean terminal at roughly 1320x860 px, Menlo 18pt, dark theme. Run the
same commands from `record-clips.sh` inside a temporary fixture directory. Name
the recordings exactly:

```text
clips/01-scope.mp4
clips/02-generate.mp4
clips/03-markdown.mp4
clips/04-evidence.mp4
```

Then run:

```bash
bash demo/pipeline/make-demo.sh
```

