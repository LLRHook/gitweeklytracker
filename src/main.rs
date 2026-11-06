mod analytics;
mod git;

use analytics::{Analytics, Options};
use std::env;
use std::path::PathBuf;
use std::time::Duration;

fn options(args: impl IntoIterator<Item = String>) -> Result<Options, String> {
    let mut args = args.into_iter();
    let mut emails = Vec::new();
    let mut paths = Vec::new();
    let (mut since, mut until) = (None, None);
    let mut timeout = Duration::from_secs(10);
    while let Some(flag) = args.next() {
        let value = args
            .next()
            .ok_or_else(|| format!("Missing value for {flag}"))?;
        match flag.as_str() {
            "--email" => emails.push(value),
            "--repo" => paths.push(PathBuf::from(value)),
            "--since" => since = Some(value.parse::<i64>().map_err(|_| "Invalid --since")?),
            "--until" => until = Some(value.parse::<i64>().map_err(|_| "Invalid --until")?),
            "--timeout-ms" => {
                let ms = value.parse::<u64>().map_err(|_| "Invalid --timeout-ms")?;
                if !(1..=60_000).contains(&ms) {
                    return Err("--timeout-ms must be between 1 and 60000".into());
                }
                timeout = Duration::from_millis(ms);
            }
            _ => return Err(format!("Unknown option: {flag}")),
        }
    }
    let since = since.ok_or("Missing --since (Unix seconds)")?;
    let until = until.ok_or("Missing --until (Unix seconds)")?;
    // Match Python datetime's supported years and keep date arithmetic bounded.
    if since < -62_135_596_800 || until > 253_402_300_799 || since > until {
        return Err("Invalid UTC report interval".into());
    }
    if emails.is_empty() || emails.iter().any(|e| e.trim().is_empty()) {
        return Err("At least one nonempty --email is required".into());
    }
    Ok(Options {
        emails,
        paths,
        since,
        until,
        timeout,
        home: env::var_os("HOME").map(PathBuf::from),
    })
}

fn main() {
    match options(env::args().skip(1)) {
        Ok(options) => println!("{}", Analytics::collect(&options).json()),
        Err(error) => {
            eprintln!("gitweeklytracker-core: {error}");
            std::process::exit(2);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Options, String> {
        options(args.iter().map(|s| s.to_string()))
    }

    #[test]
    fn validates_options_without_starting_git() {
        for args in [
            vec![],
            vec!["--email"],
            vec!["--email", "", "--since", "0", "--until", "1"],
            vec!["--email", "a", "--since", "2", "--until", "1"],
            vec!["--email", "a", "--since", "no", "--until", "1"],
            vec![
                "--email",
                "a",
                "--since",
                "0",
                "--until",
                "1",
                "--timeout-ms",
                "0",
            ],
            vec![
                "--email",
                "a",
                "--since",
                "0",
                "--until",
                "1",
                "--unknown",
                "1",
            ],
        ] {
            assert!(parse(&args).is_err(), "{args:?}");
        }
        let result = parse(&[
            "--email",
            "a",
            "--email",
            "b",
            "--repo",
            "path with spaces",
            "--since",
            "0",
            "--until",
            "1",
        ])
        .unwrap();
        assert_eq!(result.emails, ["a", "b"]);
        assert_eq!(result.paths, [PathBuf::from("path with spaces")]);
    }
}
