use crate::git;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant};

pub const DAYS: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

pub struct Options {
    pub emails: Vec<String>,
    pub paths: Vec<PathBuf>,
    pub since: i64,
    pub until: i64,
    pub timeout: Duration,
    pub home: Option<PathBuf>,
}

#[derive(Default)]
struct RepoStats {
    path: String,
    name: String,
    email: String,
    commits: u64,
    insertions: u64,
    deletions: u64,
    files_changed: u64,
}

pub struct Analytics {
    repos: Vec<PathBuf>,
    stats: Vec<RepoStats>,
    daily: [u64; 7],
    top_files: Vec<(u64, String)>,
    longest_streak: usize,
    since: i64,
    until: i64,
    emails: Vec<String>,
}

fn is_repo(path: &Path) -> bool {
    let dotgit = path.join(".git");
    dotgit.is_dir() || dotgit.is_file()
}

fn absolute_path(path: &Path) -> Option<PathBuf> {
    // Match os.path.abspath: normalize dots without resolving symlink names.
    let mut normalized = PathBuf::new();
    for component in std::path::absolute(path).ok()?.components() {
        match component {
            Component::ParentDir => {
                normalized.pop();
            }
            Component::CurDir => {}
            _ => normalized.push(component),
        }
    }
    Some(normalized)
}

fn expand_home(path: &Path, home: Option<&Path>) -> PathBuf {
    if let (Ok(tail), Some(home)) = (path.strip_prefix("~"), home) {
        home.join(tail)
    } else {
        path.to_path_buf()
    }
}

fn children(path: &Path) -> std::io::Result<Vec<fs::DirEntry>> {
    let mut entries: Vec<_> = fs::read_dir(path)?.filter_map(Result::ok).collect();
    entries.sort_by_key(|entry| entry.file_name());
    Ok(entries)
}

fn probe_known_subdirs(parent: &Path, options: &Options) -> Vec<PathBuf> {
    // Preserve the dashboard's macOS permission fallback. Only project directory
    // names are consulted; no session/history files or credentials are opened.
    let Some(home) = &options.home else {
        return Vec::new();
    };
    let prefix = parent.to_string_lossy().replace(['/', ' '], "-");
    let Ok(entries) = children(&home.join(".claude/projects")) else {
        return Vec::new();
    };
    entries
        .into_iter()
        .filter_map(|entry| {
            let name = entry.file_name();
            let suffix = name.to_str()?.strip_prefix(&prefix)?.strip_prefix('-')?;
            if suffix.is_empty() {
                return None;
            }
            let candidate = parent.join(suffix);
            let result = git::run(
                &candidate,
                &["rev-parse".into(), "--is-inside-work-tree".into()],
                options.timeout,
            );
            (result == b"true\n").then_some(candidate)
        })
        .collect()
}

fn walk_home(path: &Path, depth: usize, deadline: Instant, repos: &mut Vec<PathBuf>) {
    if depth >= 5 || Instant::now() >= deadline {
        return;
    }
    if is_repo(path) {
        repos.push(path.to_path_buf());
    }
    let Ok(entries) = children(path) else { return };
    for entry in entries {
        if Instant::now() >= deadline {
            break;
        }
        if [
            ".git",
            "node_modules",
            ".cache",
            "Library",
            ".Trash",
            ".cargo",
            ".rustup",
        ]
        .iter()
        .any(|name| entry.file_name() == *name)
        {
            continue;
        }
        // Like find's default, do not recurse through directory symlinks.
        if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
            walk_home(&entry.path(), depth + 1, deadline, repos);
        }
    }
}

fn discover(options: &Options) -> Vec<PathBuf> {
    let mut repos = Vec::new();
    if options.paths.is_empty() {
        if let Some(home) = &options.home {
            walk_home(
                home,
                0,
                Instant::now() + Duration::from_secs(60),
                &mut repos,
            );
        }
        repos.retain(|repo| {
            options.emails.iter().any(|email| {
                !git::run(
                    repo,
                    &[
                        "log".into(),
                        format!("--author={email}"),
                        "--since=1 year ago".into(),
                        "--format=%H".into(),
                        "--no-show-signature".into(),
                        "-1".into(),
                    ],
                    options.timeout,
                )
                .is_empty()
            })
        });
    } else {
        for path in &options.paths {
            let path = expand_home(path, options.home.as_deref());
            if !path.is_dir() {
                continue;
            }
            if is_repo(&path)
                || git::run(
                    &path,
                    &["rev-parse".into(), "--is-inside-work-tree".into()],
                    options.timeout,
                ) == b"true\n"
            {
                repos.push(path);
            } else {
                match children(&path) {
                    Ok(entries) => {
                        repos.extend(entries.into_iter().map(|e| e.path()).filter(|p| is_repo(p)))
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                        repos.extend(probe_known_subdirs(&path, options));
                    }
                    Err(_) => {}
                }
            }
        }
    }
    let mut seen = HashSet::new();
    repos
        .into_iter()
        .filter_map(|path| {
            let absolute = absolute_path(&path)?;
            let real = fs::canonicalize(&absolute).ok()?;
            seen.insert(real).then_some(absolute)
        })
        .collect()
}

fn collect_repo(repo: &Path, email: &str, options: &Options) -> (RepoStats, Vec<i64>, Vec<String>) {
    let mut stats = RepoStats {
        path: repo.to_string_lossy().into_owned(),
        name: repo
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned(),
        email: email.into(),
        ..RepoStats::default()
    };
    let base_args = vec![
        "log".into(),
        format!("--author={email}"),
        format!("--since=@{}", options.since),
        format!("--until=@{}", options.until),
        "--no-show-signature".into(),
        "--no-color".into(),
        "--no-ext-diff".into(),
    ];
    let mut args = base_args.clone();
    args.extend(["--format=COMMIT:%at".into(), "--shortstat".into()]);
    let output = git::run(repo, &args, options.timeout);
    let mut dates = Vec::new();
    for line in String::from_utf8_lossy(&output).lines().map(str::trim) {
        if let Some(timestamp) = line.strip_prefix("COMMIT:") {
            stats.commits += 1;
            if let Ok(timestamp) = timestamp.parse::<i64>() {
                dates.push(timestamp.div_euclid(86_400));
            }
        } else {
            for field in line.split(',') {
                let mut words = field.split_whitespace();
                let Some(count) = words.next().and_then(|s| s.parse::<u64>().ok()) else {
                    continue;
                };
                match words.next() {
                    Some("file" | "files") => stats.files_changed += count,
                    Some("insertion(+)" | "insertions(+)") => stats.insertions += count,
                    Some("deletion(-)" | "deletions(-)") => stats.deletions += count,
                    _ => {}
                }
            }
        }
    }
    let mut files = Vec::new();
    if stats.commits > 0 {
        let mut args = base_args;
        args.extend(["--name-only".into(), "-z".into(), "--format=".into()]);
        let output = git::run(repo, &args, options.timeout);
        for file in output.split(|b| *b == 0).filter(|name| !name.is_empty()) {
            files.push(format!("{}/{}", stats.name, String::from_utf8_lossy(file)));
        }
    }
    (stats, dates, files)
}

fn streak(dates: &BTreeSet<i64>) -> usize {
    let (mut longest, mut current, mut previous) = (0, 0, None);
    for &day in dates {
        current = if previous == Some(day - 1) {
            current + 1
        } else {
            1
        };
        longest = longest.max(current);
        previous = Some(day);
    }
    longest
}

// Proleptic Gregorian civil date from Unix days, including pre-epoch dates.
// The era decomposition keeps all intermediate arithmetic integral.
fn civil_date(day: i64) -> (i64, i64, i64) {
    let z = day + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    (year + i64::from(month <= 2), month, day)
}

fn display_time(timestamp: i64) -> String {
    let (year, month, day) = civil_date(timestamp.div_euclid(86_400));
    let seconds = timestamp.rem_euclid(86_400);
    format!(
        "{year:04}-{month:02}-{day:02} {:02}:{:02}",
        seconds / 3600,
        seconds % 3600 / 60
    )
}

fn quote(value: &str) -> String {
    let mut output = String::from("\"");
    for ch in value.chars() {
        match ch {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            '\u{00}'..='\u{1f}' => output.push_str(&format!("\\u{:04x}", ch as u32)),
            _ => output.push(ch),
        }
    }
    output.push('"');
    output
}

impl Analytics {
    pub fn collect(options: &Options) -> Self {
        let repos = discover(options);
        let mut result = Self {
            repos,
            stats: Vec::new(),
            daily: [0; 7],
            top_files: Vec::new(),
            longest_streak: 0,
            since: options.since,
            until: options.until,
            emails: options.emails.clone(),
        };
        let mut all_dates = BTreeSet::new();
        let mut file_indices = HashMap::new();
        for repo in &result.repos {
            for email in &options.emails {
                let (stats, dates, files) = collect_repo(repo, email, options);
                if stats.commits == 0 {
                    continue;
                }
                result.stats.push(stats);
                for date in dates {
                    result.daily[(date + 3).rem_euclid(7) as usize] += 1;
                    all_dates.insert(date);
                }
                for file in files {
                    let next_index = result.top_files.len();
                    let index = *file_indices.entry(file.clone()).or_insert(next_index);
                    if index == next_index {
                        result.top_files.push((0, file));
                    }
                    result.top_files[index].0 += 1;
                }
            }
        }
        result.longest_streak = streak(&all_dates);
        // Stable sort preserves first-seen ordering for ties, like the Python API.
        result.top_files.sort_by(|a, b| b.0.cmp(&a.0));
        result.top_files.truncate(10);
        result
    }

    pub fn json(&self) -> String {
        let commits: u64 = self.stats.iter().map(|s| s.commits).sum();
        let insertions: u64 = self.stats.iter().map(|s| s.insertions).sum();
        let deletions: u64 = self.stats.iter().map(|s| s.deletions).sum();
        let active: HashSet<_> = self.stats.iter().map(|s| &s.path).collect();
        let stats = self.stats.iter().map(|s| format!(
            "{{\"repo_path\":{},\"repo_name\":{},\"email\":{},\"commits\":{},\"insertions\":{},\"deletions\":{},\"files_changed\":{}}}",
            quote(&s.path), quote(&s.name), quote(&s.email), s.commits, s.insertions, s.deletions, s.files_changed,
        )).collect::<Vec<_>>().join(",");
        let daily = DAYS
            .iter()
            .zip(self.daily)
            .map(|(day, count)| format!("{}:{count}", quote(day)))
            .collect::<Vec<_>>()
            .join(",");
        let files = self
            .top_files
            .iter()
            .map(|(count, path)| format!("[{count},{}]", quote(path)))
            .collect::<Vec<_>>()
            .join(",");
        let emails = self
            .emails
            .iter()
            .map(|s| quote(s))
            .collect::<Vec<_>>()
            .join(",");
        let repos = self
            .repos
            .iter()
            .map(|p| quote(&p.to_string_lossy()))
            .collect::<Vec<_>>()
            .join(",");
        format!(
            "{{\"per_repo\":[{stats}],\"daily_activity\":{{{daily}}},\"top_files\":[{files}],\"longest_streak\":{},\"total_commits\":{commits},\"total_insertions\":{insertions},\"total_deletions\":{deletions},\"net_change\":{},\"active_repo_count\":{},\"repos_scanned\":{},\"start_date\":{},\"end_date\":{},\"emails\":[{emails}],\"repositories\":[{repos}]}}",
            self.longest_streak,
            i128::from(insertions) - i128::from(deletions),
            active.len(),
            self.repos.len(),
            quote(&display_time(self.since)),
            quote(&display_time(self.until)),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utc_dates_include_epoch_leap_days_and_centuries() {
        for (day, expected) in [
            (0, (1970, 1, 1)),
            (-1, (1969, 12, 31)),
            (11_016, (2000, 2, 29)),
            (20_165, (2025, 3, 18)),
            (-719_162, (1, 1, 1)),
            (2_932_896, (9999, 12, 31)),
        ] {
            assert_eq!(civil_date(day), expected);
        }
        assert_eq!(display_time(-1), "1969-12-31 23:59");
        assert_eq!(display_time(0), "1970-01-01 00:00");
    }

    #[test]
    fn consecutive_utc_days_survive_dst_year_and_leap_boundaries() {
        assert_eq!(streak(&BTreeSet::new()), 0);
        assert_eq!(streak(&BTreeSet::from([0])), 1);
        assert_eq!(streak(&BTreeSet::from([-1, 0, 1, 4, 5])), 3);
        assert_eq!(streak(&BTreeSet::from([20_519, 20_520, 20_521])), 3);
    }

    #[test]
    fn json_escapes_control_characters_without_losing_unicode() {
        assert_eq!(
            quote("a\"\\\n\r\t\0\u{08}é"),
            "\"a\\\"\\\\\\n\\r\\t\\u0000\\u0008é\""
        );
    }

    #[test]
    fn tilde_expansion_is_component_aware() {
        assert_eq!(
            expand_home(Path::new("~/a b"), Some(Path::new("/tmp/home"))),
            Path::new("/tmp/home/a b")
        );
        assert_eq!(
            expand_home(Path::new("~someone/a"), Some(Path::new("/tmp/home"))),
            Path::new("~someone/a")
        );
    }

    #[test]
    fn absolute_paths_normalize_dots_without_resolving_aliases() {
        assert_eq!(
            absolute_path(Path::new("/tmp/alias/../repo/./")),
            Some(PathBuf::from("/tmp/repo"))
        );
        assert_eq!(
            absolute_path(Path::new("/../../tmp")),
            Some(PathBuf::from("/tmp"))
        );
    }
}
