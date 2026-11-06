//! Bounded, shell-free Git commands. Failures remain empty best-effort results.
use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::process::CommandExt;

#[cfg(unix)]
unsafe extern "C" {
    fn kill(pid: i32, signal: i32) -> i32;
}

fn stop_group(child: &mut std::process::Child) {
    #[cfg(unix)]
    if let Ok(pid) = i32::try_from(child.id()) {
        // SAFETY: every command starts in its own process group. A negative PID
        // targets only that owned group, including helpers holding stdout open.
        unsafe { kill(-pid, 9) };
    }
    let _ = child.kill();
}

pub fn run(repo: &Path, args: &[String], timeout: Duration) -> Vec<u8> {
    let mut command = Command::new("git");
    command
        .args(["--no-pager", "-C"])
        .arg(repo)
        .args(args)
        .env("LC_ALL", "C")
        .env("TZ", "UTC")
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR")
        .env_remove("GIT_INDEX_FILE")
        .env_remove("GIT_OBJECT_DIRECTORY")
        .env_remove("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(unix)]
    command.process_group(0);
    let Ok(mut child) = command.spawn() else {
        return Vec::new();
    };
    let mut stdout = child.stdout.take().expect("piped stdout");
    // Drain concurrently: a large history must not fill the pipe and deadlock
    // the timeout monitor. There is one reader per Git command, not per commit.
    let start = Instant::now();
    let (completed, output) = mpsc::sync_channel(1);
    thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout.read_to_end(&mut bytes).map(|_| bytes);
        let _ = completed.send(result);
    });
    let success = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status.success(),
            Ok(None) if start.elapsed() < timeout => thread::sleep(Duration::from_millis(5)),
            _ => break false,
        }
    };
    stop_group(&mut child);
    let _ = child.wait();
    // A detached descendant can retain stdout after the Git leader exits. The
    // pipe belongs to the same deadline; never join its reader without a bound.
    // Only the owned process group is terminated. Any late reader result is
    // discarded when this receiver/CLI exits, without killing unrelated peers.
    if success && start.elapsed() < timeout {
        output
            .recv_timeout(timeout.saturating_sub(start.elapsed()))
            .ok()
            .and_then(Result::ok)
            .unwrap_or_default()
    } else {
        Vec::new()
    }
}
