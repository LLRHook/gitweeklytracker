"""Parse local Claude Code data files to compute token usage analytics."""

import json
import glob
import os
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, timezone


# Anthropic pricing per million tokens
PRICING = {
    "opus": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_create": 18.75},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_create": 3.75},
    "haiku": {"input": 0.25, "output": 1.25, "cache_read": 0.025, "cache_create": 0.3125},
}


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total(self) -> int:
        return self.active + self.cache

    @property
    def active(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache(self) -> int:
        return self.cache_read_input_tokens + self.cache_creation_input_tokens

    def __iadd__(self, other: "TokenUsage") -> "TokenUsage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        return self


@dataclass
class ProjectStats:
    project_path: str
    display_name: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    estimated_cost_usd: float = 0.0
    session_count: int = 0
    message_count: int = 0


@dataclass
class DailyUsage:
    date: date
    usage: TokenUsage = field(default_factory=TokenUsage)
    estimated_cost_usd: float = 0.0
    session_count: int = 0
    message_count: int = 0


@dataclass
class ModelBreakdown:
    model_name: str
    display_name: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    estimated_cost_usd: float = 0.0
    message_count: int = 0


@dataclass
class BillingPeriod:
    days_elapsed: int
    days_remaining: int
    period_start: date
    period_end: date


@dataclass
class ClaudeAnalytics:
    per_project: list[ProjectStats]
    daily_usage: list[DailyUsage]
    model_breakdown: list[ModelBreakdown]
    hourly_distribution: dict[int, int]
    billing_period: BillingPeriod
    total_tokens: TokenUsage
    total_cost_usd: float
    total_sessions: int
    total_messages: int
    busiest_hour: int
    most_active_project: str


def _get_model_tier(model: str) -> str:
    """Map a model name to its pricing tier."""
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "sonnet"  # default fallback


def _get_model_display_name(model: str) -> str:
    """Human-friendly model name."""
    m = model.lower()
    if "opus-4-6" in m:
        return "Opus 4.6"
    if "opus-4-5" in m:
        return "Opus 4.5"
    if "sonnet-4-6" in m:
        return "Sonnet 4.6"
    if "sonnet-4-5" in m:
        return "Sonnet 4.5"
    if "haiku-4-5" in m:
        return "Haiku 4.5"
    if "opus" in m:
        return "Opus"
    if "sonnet" in m:
        return "Sonnet"
    if "haiku" in m:
        return "Haiku"
    return model


def compute_cost(model: str, usage: TokenUsage) -> float:
    """Compute estimated cost in USD for a given model and token usage."""
    tier = _get_model_tier(model)
    prices = PRICING.get(tier, PRICING["sonnet"])
    cost = (
        usage.input_tokens * prices["input"] / 1_000_000
        + usage.output_tokens * prices["output"] / 1_000_000
        + usage.cache_read_input_tokens * prices["cache_read"] / 1_000_000
        + usage.cache_creation_input_tokens * prices["cache_create"] / 1_000_000
    )
    return cost


def _build_session_to_project(claude_dir: str) -> dict[str, str]:
    """Build session_id -> project_path mapping from history.jsonl."""
    mapping: dict[str, str] = {}
    history_path = os.path.join(claude_dir, "history.jsonl")
    if not os.path.exists(history_path):
        return mapping
    with open(history_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                sid = d.get("sessionId", "")
                proj = d.get("project", "")
                if sid and proj:
                    mapping[sid] = proj
            except json.JSONDecodeError:
                continue
    return mapping


def _build_session_to_project_from_sessions(claude_dir: str) -> dict[str, str]:
    """Build session_id -> cwd mapping from sessions/*.json."""
    mapping: dict[str, str] = {}
    sessions_dir = os.path.join(claude_dir, "sessions")
    if not os.path.isdir(sessions_dir):
        return mapping
    for fname in os.listdir(sessions_dir):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, fname), encoding="utf-8") as f:
                d = json.load(f)
                sid = d.get("sessionId", "")
                cwd = d.get("cwd", "")
                if sid and cwd:
                    mapping[sid] = cwd
        except (json.JSONDecodeError, KeyError):
            continue
    return mapping


def _extract_display_name(project_path: str) -> str:
    """Extract a short display name from a project path."""
    name = os.path.basename(project_path.rstrip("/"))
    return name or project_path


def _display_name_from_dirname(dirname: str) -> str:
    """Extract display name directly from encoded dirname as fallback.

    Strips the home directory prefix and common subdirectory patterns,
    returning the remaining project name portion.
    """
    # Build prefix from actual home directory
    home = os.path.expanduser("~").replace("/", "-").replace(" ", "-")
    home_prefix = "-" + home + "-"

    remaining = dirname
    if dirname.startswith(home_prefix):
        remaining = dirname[len(home_prefix):]

    # Strip common subdirectory prefixes (Documents-personal-projects-, etc.)
    subdir_prefixes = [
        "Documents-personal-projects-",
        "Documents-group-projects-",
        "Documents-School-",
        "Documents-CV-",
        "Documents-",
    ]
    for prefix in subdir_prefixes:
        if remaining.startswith(prefix) and len(remaining) > len(prefix):
            return remaining[len(prefix):]

    return remaining or dirname.rsplit("-", 1)[-1]


def _decode_project_dirname(dirname: str) -> str:
    """Decode an encoded project directory name back to a filesystem path.

    The encoding replaces / with - and space with -.
    Uses recursive search with filesystem existence checks to disambiguate.
    """
    raw = dirname.lstrip("-")
    parts = raw.split("-")

    def solve(idx: int, current_path: str) -> str | None:
        if idx == len(parts):
            return current_path

        # Try building increasingly longer segment names (greedy: longest first)
        for end in range(len(parts), idx, -1):
            segment_hyphen = "-".join(parts[idx:end])

            # Try as new subdirectory with hyphens preserved
            candidate = os.path.join(current_path, segment_hyphen)
            if os.path.exists(candidate):
                result = solve(end, candidate)
                if result:
                    return result

            # Try with spaces instead of hyphens
            if end > idx + 1:
                segment_space = " ".join(parts[idx:end])
                candidate = os.path.join(current_path, segment_space)
                if os.path.exists(candidate):
                    result = solve(end, candidate)
                    if result:
                        return result

        # Fallback: use single segment as directory name
        return solve(idx + 1, os.path.join(current_path, parts[idx]))

    result = solve(0, "/")
    return result or ("/" + raw.replace("-", "/"))


def _resolve_project_dir(
    dirname: str,
    session_ids: list[str],
    history_map: dict[str, str],
    sessions_map: dict[str, str],
) -> tuple[str, str]:
    """Resolve a project directory name to (project_path, display_name).

    Tries: history.jsonl mapping, sessions mapping, then filesystem decoding.
    """
    # Try history.jsonl first (most reliable)
    for sid in session_ids:
        if sid in history_map:
            path = history_map[sid]
            return path, _extract_display_name(path)

    # Try sessions/*.json
    for sid in session_ids:
        if sid in sessions_map:
            path = sessions_map[sid]
            return path, _extract_display_name(path)

    # Filesystem-based decoding
    path = _decode_project_dirname(dirname)
    display = _extract_display_name(path)

    # If the decoded display name looks like a generic fragment (too short,
    # or the decoded path doesn't exist), use the dirname-based extraction
    if not os.path.exists(path) or len(display) <= 3:
        display = _display_name_from_dirname(dirname)

    return path, display


def _parse_timestamp(ts: str) -> datetime:
    """Parse ISO 8601 timestamp from JSONL."""
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


def _compute_billing_period(today: date) -> BillingPeriod:
    """Calculate 7-day rolling billing window (Monday-based)."""
    days_since_monday = today.weekday()  # 0=Monday
    period_start = today - timedelta(days=days_since_monday)
    period_end = period_start + timedelta(days=6)
    days_elapsed = days_since_monday + 1
    days_remaining = 7 - days_elapsed
    return BillingPeriod(
        days_elapsed=days_elapsed,
        days_remaining=days_remaining,
        period_start=period_start,
        period_end=period_end,
    )


def get_claude_analytics(
    days: int = 7,
    claude_dir: str | None = None,
    progress_callback=None,
) -> ClaudeAnalytics:
    """Compute Claude Code analytics from local data files.

    Args:
        days: Number of days to look back.
        claude_dir: Path to ~/.claude directory.
        progress_callback: Optional callable(current, total, desc) for progress.
    """
    if claude_dir is None:
        claude_dir = os.path.expanduser("~/.claude")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    today = date.today()

    history_map = _build_session_to_project(claude_dir)
    sessions_map = _build_session_to_project_from_sessions(claude_dir)

    projects_dir = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects_dir):
        return _empty_analytics(today, days)

    project_dirs = [
        d for d in os.listdir(projects_dir)
        if os.path.isdir(os.path.join(projects_dir, d))
    ]

    # Collect all JSONL files grouped by project directory
    all_files: list[tuple[str, str]] = []  # (project_dirname, jsonl_path)
    for pdir in project_dirs:
        pdir_path = os.path.join(projects_dir, pdir)
        # Main session files
        for jsonl in glob.glob(os.path.join(pdir_path, "*.jsonl")):
            all_files.append((pdir, jsonl))
        # Subagent files
        for jsonl in glob.glob(os.path.join(pdir_path, "*", "subagents", "*.jsonl")):
            all_files.append((pdir, jsonl))

    # Per-project accumulators
    project_data: dict[str, dict] = {}  # dirname -> {path, display, usage, sessions, messages, models}
    daily_data: dict[date, dict] = {}  # date -> {usage, sessions, messages}
    model_data: dict[str, dict] = {}  # model -> {usage, messages}
    hourly_counts: dict[int, int] = {}  # local hour -> message count

    total_files = len(all_files)

    for file_idx, (pdir, jsonl_path) in enumerate(all_files):
        if progress_callback and file_idx % 50 == 0:
            progress_callback(file_idx, total_files, "Scanning JSONL files...")

        # Skip files not modified since the cutoff
        try:
            if os.path.getmtime(jsonl_path) < cutoff.timestamp():
                continue
        except OSError:
            continue

        session_id = os.path.splitext(os.path.basename(jsonl_path))[0]
        # For subagent files like agent-abc123.jsonl, use parent session dir
        if session_id.startswith("agent-"):
            parts = jsonl_path.split(os.sep)
            # .../projects/<pdir>/<session-uuid>/subagents/agent-xxx.jsonl
            for i, part in enumerate(parts):
                if part == "subagents" and i >= 2:
                    session_id = parts[i - 1]
                    break

        # Initialize project entry
        if pdir not in project_data:
            # Build session IDs from already-collected file list (avoid extra listdir)
            session_ids_in_dir = [
                os.path.splitext(os.path.basename(p))[0]
                for d, p in all_files
                if d == pdir and not os.path.basename(p).startswith("agent-")
            ]
            proj_path, display = _resolve_project_dir(
                pdir, session_ids_in_dir, history_map, sessions_map
            )
            project_data[pdir] = {
                "path": proj_path,
                "display": display,
                "usage": TokenUsage(),
                "sessions": set(),
                "messages": 0,
                "model_usage": {},  # model -> TokenUsage for accurate per-project cost
            }

        try:
            with open(jsonl_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if record.get("type") != "assistant":
                        continue
                    msg = record.get("message")
                    if not msg or not isinstance(msg, dict):
                        continue

                    usage = msg.get("usage")
                    if not usage:
                        continue

                    model = msg.get("model", "")
                    if not model or model == "<synthetic>":
                        continue

                    ts_str = record.get("timestamp", "")
                    if not ts_str:
                        continue
                    try:
                        ts = _parse_timestamp(ts_str)
                    except (ValueError, TypeError):
                        continue

                    if ts < cutoff:
                        continue

                    token_usage = TokenUsage(
                        input_tokens=usage.get("input_tokens", 0),
                        output_tokens=usage.get("output_tokens", 0),
                        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
                        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
                    )

                    # Accumulate per-project
                    project_data[pdir]["usage"] += token_usage
                    project_data[pdir]["sessions"].add(session_id)
                    project_data[pdir]["messages"] += 1
                    # Track per-project-per-model for accurate costing
                    if model not in project_data[pdir]["model_usage"]:
                        project_data[pdir]["model_usage"][model] = TokenUsage()
                    project_data[pdir]["model_usage"][model] += token_usage

                    # Accumulate per-day
                    msg_date = ts.date()
                    if msg_date not in daily_data:
                        daily_data[msg_date] = {
                            "usage": TokenUsage(),
                            "sessions": set(),
                            "messages": 0,
                            "model_usage": {},
                        }
                    daily_data[msg_date]["usage"] += token_usage
                    daily_data[msg_date]["sessions"].add(session_id)
                    daily_data[msg_date]["messages"] += 1
                    if model not in daily_data[msg_date]["model_usage"]:
                        daily_data[msg_date]["model_usage"][model] = TokenUsage()
                    daily_data[msg_date]["model_usage"][model] += token_usage

                    # Accumulate per-model
                    if model not in model_data:
                        model_data[model] = {"usage": TokenUsage(), "messages": 0}
                    model_data[model]["usage"] += token_usage
                    model_data[model]["messages"] += 1

                    # Hourly distribution (convert UTC to local time)
                    local_ts = ts.astimezone()
                    hour = local_ts.hour
                    hourly_counts[hour] = hourly_counts.get(hour, 0) + 1

        except (OSError, IOError):
            continue

    if progress_callback:
        progress_callback(total_files, total_files, "Done scanning.")

    # Build ProjectStats list
    per_project: list[ProjectStats] = []
    for pdir, pdata in project_data.items():
        if pdata["messages"] == 0:
            continue
        # Compute accurate per-project cost using per-model breakdown
        cost = sum(
            compute_cost(model, usage)
            for model, usage in pdata["model_usage"].items()
        )
        per_project.append(ProjectStats(
            project_path=pdata["path"],
            display_name=pdata["display"],
            usage=pdata["usage"],
            estimated_cost_usd=cost,
            session_count=len(pdata["sessions"]),
            message_count=pdata["messages"],
        ))
    per_project.sort(key=lambda p: p.usage.total, reverse=True)

    # Build DailyUsage list (last N days, fill gaps)
    daily_usage: list[DailyUsage] = []
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)
        dd = daily_data.get(d)
        if dd:
            usage = dd["usage"]
            cost = sum(
                compute_cost(m, u) for m, u in dd["model_usage"].items()
            )
            daily_usage.append(DailyUsage(
                date=d,
                usage=usage,
                estimated_cost_usd=cost,
                session_count=len(dd["sessions"]),
                message_count=dd["messages"],
            ))
        else:
            daily_usage.append(DailyUsage(date=d))

    # Build ModelBreakdown list
    model_breakdown: list[ModelBreakdown] = []
    for model, mdata in model_data.items():
        cost = compute_cost(model, mdata["usage"])
        model_breakdown.append(ModelBreakdown(
            model_name=model,
            display_name=_get_model_display_name(model),
            usage=mdata["usage"],
            estimated_cost_usd=cost,
            message_count=mdata["messages"],
        ))
    model_breakdown.sort(key=lambda m: m.usage.total, reverse=True)

    # Totals
    total_usage = TokenUsage()
    total_cost = 0.0
    total_messages = 0
    all_sessions: set[str] = set()
    for pdata in project_data.values():
        total_usage += pdata["usage"]
        total_messages += pdata["messages"]
        all_sessions.update(pdata["sessions"])
    for mb in model_breakdown:
        total_cost += mb.estimated_cost_usd

    # Busiest hour
    busiest_hour = max(hourly_counts, key=hourly_counts.get) if hourly_counts else 0

    # Most active project
    most_active = per_project[0].display_name if per_project else "N/A"

    billing_period = _compute_billing_period(today)

    return ClaudeAnalytics(
        per_project=per_project,
        daily_usage=daily_usage,
        model_breakdown=model_breakdown,
        hourly_distribution=hourly_counts,
        billing_period=billing_period,
        total_tokens=total_usage,
        total_cost_usd=total_cost,
        total_sessions=len(all_sessions),
        total_messages=total_messages,
        busiest_hour=busiest_hour,
        most_active_project=most_active,
    )



def _empty_analytics(today: date, days: int) -> ClaudeAnalytics:
    """Return empty analytics when no data is available."""
    daily = [DailyUsage(date=today - timedelta(days=days - 1 - i)) for i in range(days)]
    return ClaudeAnalytics(
        per_project=[],
        daily_usage=daily,
        model_breakdown=[],
        hourly_distribution={},
        billing_period=_compute_billing_period(today),
        total_tokens=TokenUsage(),
        total_cost_usd=0.0,
        total_sessions=0,
        total_messages=0,
        busiest_hour=0,
        most_active_project="N/A",
    )


# ── OAuth Usage Quota ──────────────────────────────────────

USAGE_BAR_CONFIG_DIR = os.path.expanduser("~/.config/claude-usage-bar")
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_BETA_HEADER = "oauth-2025-04-20"


@dataclass
class UsageBucket:
    utilization: float  # 0-100 percentage
    resets_at: str  # ISO 8601 timestamp

    @property
    def reset_datetime(self) -> datetime | None:
        if not self.resets_at:
            return None
        try:
            return datetime.fromisoformat(self.resets_at.replace("Z", "+00:00"))
        except ValueError:
            return None

    @property
    def reset_description(self) -> str:
        """Human-readable time until reset."""
        dt = self.reset_datetime
        if not dt:
            return ""
        delta = dt - datetime.now(timezone.utc)
        if delta.total_seconds() <= 0:
            return "now"
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


@dataclass
class UsageQuota:
    five_hour: UsageBucket | None = None
    seven_day: UsageBucket | None = None
    seven_day_opus: UsageBucket | None = None
    seven_day_sonnet: UsageBucket | None = None
    available: bool = False  # whether quota data was successfully fetched


def _load_oauth_credentials() -> dict | None:
    """Load OAuth credentials from claude-usage-bar config."""
    creds_path = os.path.join(USAGE_BAR_CONFIG_DIR, "credentials.json")
    if not os.path.exists(creds_path):
        return None
    try:
        with open(creds_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_oauth_credentials(creds: dict) -> None:
    """Save updated OAuth credentials back to disk."""
    creds_path = os.path.join(USAGE_BAR_CONFIG_DIR, "credentials.json")
    try:
        with open(creds_path, "w", encoding="utf-8") as f:
            json.dump(creds, f, indent=2, sort_keys=True)
    except OSError:
        pass


def _refresh_token_if_needed(creds: dict) -> dict | None:
    """Refresh the OAuth token if expired. Returns updated creds or None."""
    expires_at = creds.get("expiresAt", "")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < exp - timedelta(seconds=60):
                return creds  # still valid
        except ValueError:
            pass

    refresh_token = creds.get("refreshToken")
    if not refresh_token:
        return creds  # no refresh token, try access token as-is

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }).encode()

    req = urllib.request.Request(OAUTH_TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            creds["accessToken"] = data["access_token"]
            if "refresh_token" in data:
                creds["refreshToken"] = data["refresh_token"]
            if "expires_in" in data:
                exp = datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
                creds["expiresAt"] = exp.isoformat()
            _save_oauth_credentials(creds)
            return creds
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError):
        return creds  # refresh failed, try existing token


def _parse_usage_bucket(data: dict | None) -> UsageBucket | None:
    if not data or "utilization" not in data:
        return None
    return UsageBucket(
        utilization=data.get("utilization", 0.0),
        resets_at=data.get("resets_at", ""),
    )


def get_usage_quota() -> UsageQuota:
    """Fetch real usage quota from Anthropic OAuth API.

    Falls back to latest entry in history.json if API call fails.
    """
    creds = _load_oauth_credentials()
    if not creds:
        return UsageQuota()

    creds = _refresh_token_if_needed(creds)
    if not creds:
        return UsageQuota()

    token = creds.get("accessToken", "")
    if not token:
        return UsageQuota()

    # Try live API
    req = urllib.request.Request(USAGE_API_URL)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("anthropic-beta", USAGE_BETA_HEADER)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return UsageQuota(
                five_hour=_parse_usage_bucket(data.get("five_hour")),
                seven_day=_parse_usage_bucket(data.get("seven_day")),
                seven_day_opus=_parse_usage_bucket(data.get("seven_day_opus")),
                seven_day_sonnet=_parse_usage_bucket(data.get("seven_day_sonnet")),
                available=True,
            )
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        pass

    # Fallback: read latest from history.json
    history_path = os.path.join(USAGE_BAR_CONFIG_DIR, "history.json")
    if os.path.exists(history_path):
        try:
            with open(history_path, encoding="utf-8") as f:
                history = json.load(f)
            if history:
                latest = history[-1]
                return UsageQuota(
                    five_hour=UsageBucket(
                        utilization=latest.get("pct5h", 0) * 100,
                        resets_at="",
                    ),
                    seven_day=UsageBucket(
                        utilization=latest.get("pct7d", 0) * 100,
                        resets_at="",
                    ),
                    available=True,
                )
        except (json.JSONDecodeError, OSError):
            pass

    return UsageQuota()


if __name__ == "__main__":
    print("Computing Claude Code analytics (last 7 days)...")
    data = get_claude_analytics(days=7)
    print(f"Total sessions: {data.total_sessions}")
    print(f"Total messages: {data.total_messages}")
    print(f"Total tokens: {data.total_tokens.total:,}")
    print(f"Estimated cost: ${data.total_cost_usd:.2f}")
    print(f"Projects with activity: {len(data.per_project)}")
    print(f"Most active: {data.most_active_project}")
    print(f"Busiest hour: {data.busiest_hour}:00")
    print(f"\nPer-project breakdown:")
    for p in data.per_project[:10]:
        print(f"  {p.display_name}: {p.usage.total:,} tokens, ${p.estimated_cost_usd:.2f}, {p.session_count} sessions")
    print(f"\nDaily usage:")
    for d in data.daily_usage:
        print(f"  {d.date}: {d.usage.total:,} tokens, {d.message_count} messages")
    print(f"\nModel breakdown:")
    for m in data.model_breakdown:
        print(f"  {m.display_name}: {m.usage.total:,} tokens, ${m.estimated_cost_usd:.2f}")
