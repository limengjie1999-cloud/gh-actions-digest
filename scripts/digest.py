#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build a markdown digest of recent GitHub Actions workflow runs.

The composite action in action.yml calls this script. It lists workflow runs
from the GitHub API, appends a short digest to GITHUB_STEP_SUMMARY, and writes
the same markdown to digest-output/digest.md in the workspace.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

PER_PAGE = 100
MAX_PAGES = 10
API_VERSION = "2022-11-28"
USER_AGENT = "gh-actions-digest"
COUNT_KEYS = ("failure", "startup_failure", "timed_out", "cancelled", "success")
ATTENTION_KEYS = ("failure", "startup_failure", "timed_out", "cancelled")
OTHER_CONCLUSIONS = ("action_required", "skipped", "neutral", "stale")
RUN_HEADERS = (
    "Created (UTC)",
    "Conclusion",
    "Workflow",
    "Branch",
    "Event",
    "Actor",
    "Title",
    "Run",
)
PROJECT_URL = "https://github.com/limengjie1999-cloud/gh-actions-digest"
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TRUE = {"true", "1", "yes", "y"}
_FALSE = {"false", "0", "no", "n"}


class DigestError(Exception):
    """A user-facing failure that should fail the Actions step."""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect while a token is attached to the request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise DigestError(
            f"GitHub API redirected unexpectedly (HTTP {code}). "
            "Refusing to follow the redirect with the token."
        )


@dataclass(frozen=True)
class Options:
    lookback_hours: int
    include_successes: bool
    include_cancelled: bool
    include_timed_out: bool
    max_items: int
    repository: str
    server_url: str
    fail_on_failures: bool
    now: datetime

    @property
    def since(self) -> datetime:
        return self.now - timedelta(hours=self.lookback_hours)


@dataclass(frozen=True)
class Run:
    name: str
    conclusion: str | None
    status: str
    branch: str
    event: str
    title: str
    html_url: str
    run_number: int | None
    created_at: datetime
    actor: str


@dataclass(frozen=True)
class Digest:
    markdown: str
    counts: dict[str, int]
    attention_count: int
    matched_count: int
    listed_count: int
    in_progress_count: int

    @property
    def has_failures(self) -> bool:
        return self.counts["failure"] + self.counts["startup_failure"] > 0


@dataclass(frozen=True)
class FetchResult:
    runs: list[dict[str, Any]]
    truncated: bool
    scanned: int
    total: int


def parse_bool(value: str, name: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise DigestError(f"Input {name} must be true or false, got {value!r}.")


def parse_int(value: str, name: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value.strip())
    except ValueError:
        raise DigestError(f"Input {name} must be an integer, got {value!r}.") from None
    if number < minimum or number > maximum:
        raise DigestError(
            f"Input {name} must be between {minimum} and {maximum}, got {number}."
        )
    return number


def parse_repository(value: str) -> str:
    repository = value.strip()
    owner, separator, name = repository.partition("/")
    if (
        not separator
        or not _REPOSITORY.fullmatch(repository)
        or ".." in repository
        or owner in {".", ".."}
        or name in {".", ".."}
    ):
        raise DigestError(
            "Input repository must be owner/name, for example octo-org/octo-repo."
        )
    return repository


def parse_https_url(value: str, label: str, default: str) -> str:
    url = (value or default).strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or any(char in url for char in " \t\r\n")
    ):
        raise DigestError(f"{label} must be an https URL without credentials.")
    return url


def options_from_env(env: Mapping[str, str], now: datetime | None = None) -> Options:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return Options(
        lookback_hours=parse_int(
            env.get("INPUT_LOOKBACK_HOURS") or "24", "lookback-hours", 1, 336
        ),
        include_successes=parse_bool(
            env.get("INPUT_INCLUDE_SUCCESSES") or "false", "include-successes"
        ),
        include_cancelled=parse_bool(
            env.get("INPUT_INCLUDE_CANCELLED") or "true", "include-cancelled"
        ),
        include_timed_out=parse_bool(
            env.get("INPUT_INCLUDE_TIMED_OUT") or "true", "include-timed-out"
        ),
        max_items=parse_int(env.get("INPUT_MAX_ITEMS") or "30", "max-items", 1, 100),
        repository=parse_repository(env.get("INPUT_REPOSITORY") or ""),
        server_url=parse_https_url(
            env.get("INPUT_SERVER_URL") or "", "Server URL", "https://github.com"
        ),
        fail_on_failures=parse_bool(
            env.get("INPUT_FAIL_ON_FAILURES") or "false", "fail-on-failures"
        ),
        now=moment.astimezone(timezone.utc),
    )


def summary_keys(options: Options) -> list[str]:
    keys = ["failure", "startup_failure"]
    if options.include_timed_out:
        keys.append("timed_out")
    if options.include_cancelled:
        keys.append("cancelled")
    if options.include_successes:
        keys.append("success")
    return keys


def parse_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_display(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def format_api_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _login(raw: Mapping[str, Any]) -> str:
    for key in ("triggering_actor", "actor"):
        value = raw.get(key)
        if isinstance(value, dict) and value.get("login"):
            return str(value["login"])
    return ""


def _run_title(raw: Mapping[str, Any], name: str) -> str:
    display = str(raw.get("display_title") or "").strip()
    commit = ""
    head = raw.get("head_commit")
    if isinstance(head, dict):
        message = str(head.get("message") or "")
        if message.strip():
            commit = message.splitlines()[0].strip()
    if display and display != name:
        return display
    return commit or display or name


def _run_number(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def parse_run(raw: Mapping[str, Any]) -> Run | None:
    created = raw.get("created_at")
    if not created:
        return None
    try:
        created_at = parse_time(str(created))
    except ValueError:
        return None
    conclusion = raw.get("conclusion")
    conclusion_text = None if conclusion is None else str(conclusion)
    status = str(raw.get("status") or "")
    if not status:
        status = "completed" if conclusion_text else "in_progress"
    name = str(raw.get("name") or raw.get("path") or "workflow").strip() or "workflow"
    return Run(
        name=name,
        conclusion=conclusion_text,
        status=status,
        branch=str(raw.get("head_branch") or ""),
        event=str(raw.get("event") or ""),
        title=_run_title(raw, name),
        html_url=str(raw.get("html_url") or ""),
        run_number=_run_number(raw.get("run_number")),
        created_at=created_at,
        actor=_login(raw),
    )


def cell(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    text = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("`", "'")
    )
    return text or "—"


def branch_cell(branch: str) -> str:
    if not branch.strip():
        return "—"
    return f"`{cell(branch, 40)}`"


def run_link(run: Run) -> str:
    label = f"#{run.run_number}" if run.run_number is not None else "view"
    url = run.html_url
    if (
        url.startswith("https://")
        and len(url) <= 500
        and not any(char in url for char in " \t\r\n()<>")
    ):
        return f"[{label}]({url})"
    return label


def run_row(run: Run) -> list[str]:
    return [
        format_display(run.created_at),
        cell(run.conclusion or "—", 32),
        cell(run.name, 40),
        branch_cell(run.branch),
        cell(run.event, 24),
        cell(run.actor, 32),
        cell(run.title, 60),
        run_link(run),
    ]


def markdown_table(headers: list[str], rows: list[list[str]], right: set[int] | None = None) -> list[str]:
    aligned = right or set()
    separator = ["---:" if index in aligned else "---" for index in range(len(headers))]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return lines


def other_phrase(counts: dict[str, int]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for name in OTHER_CONCLUSIONS:
        seen.add(name)
        if counts.get(name):
            parts.append(f"{counts[name]} {name}")
    for name in sorted(counts):
        if name not in seen and counts[name]:
            parts.append(f"{counts[name]} {name}")
    return ", ".join(parts)


def render_markdown(
    options: Options,
    counts: dict[str, int],
    attention_count: int,
    shown_attention: list[Run],
    shown_success: list[Run],
    matched: int,
    listed: int,
    in_progress: int,
    other: dict[str, int],
    malformed: int,
    scan_truncated: bool,
    scanned: int,
    total_matched: int | None,
) -> str:
    lines = [
        "# Actions digest",
        "",
        f"**Repository:** [{options.repository}]({options.server_url}/{options.repository})",
        (
            f"**Window:** runs created in the last {options.lookback_hours} hours "
            f"(since {format_display(options.since)} UTC)"
        ),
        f"**Generated:** {format_display(options.now)} UTC",
        "",
        "## Summary",
        "",
    ]
    summary_rows = [[key, str(counts[key])] for key in summary_keys(options)]
    lines.extend(markdown_table(["Conclusion", "Runs"], summary_rows, right={1}))
    lines.append("")
    if attention_count == 0:
        lines.append("No runs need attention in this window.")
    elif attention_count == 1:
        lines.append("1 run needs attention.")
    else:
        lines.append(f"{attention_count} runs need attention.")
    lines.append("")
    if shown_attention:
        lines.append("## Needs attention")
        lines.append("")
        lines.extend(markdown_table(list(RUN_HEADERS), [run_row(run) for run in shown_attention]))
        lines.append("")
    if shown_success:
        lines.append("## Successes")
        lines.append("")
        lines.extend(markdown_table(list(RUN_HEADERS), [run_row(run) for run in shown_success]))
        lines.append("")

    notes: list[str] = []
    if matched > listed:
        notes.append(
            f"Showing {listed} of {matched} matching runs. Increase `max-items` to list more."
        )
    if in_progress == 1:
        notes.append("1 run is still in progress and is not listed.")
    elif in_progress > 1:
        notes.append(f"{in_progress} runs are still in progress and are not listed.")
    phrase = other_phrase(other)
    if phrase:
        notes.append(f"Also in this window: {phrase}.")
    if malformed == 1:
        notes.append("1 run could not be read and was skipped.")
    elif malformed > 1:
        notes.append(f"{malformed} runs could not be read and were skipped.")
    if scan_truncated:
        total_text = str(total_matched) if total_matched is not None else "more"
        notes.append(
            f"Read {scanned} of {total_text} runs returned for this window. "
            "Narrow `lookback-hours` if a busy repository looks incomplete."
        )
    for note in notes:
        lines.append(f"_{note}_")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"Generated by [gh-actions-digest]({PROJECT_URL}).")
    return "\n".join(lines).rstrip() + "\n"


def build_digest(
    raw_runs: list[Mapping[str, Any]],
    options: Options,
    *,
    scan_truncated: bool = False,
    scanned: int = 0,
    total_matched: int | None = None,
) -> Digest:
    enabled = set(summary_keys(options))
    buckets: dict[str, list[Run]] = {key: [] for key in COUNT_KEYS}
    other: dict[str, int] = {}
    in_progress = 0
    malformed = 0

    for raw in raw_runs:
        if not isinstance(raw, Mapping):
            malformed += 1
            continue
        run = parse_run(raw)
        if run is None:
            malformed += 1
            continue
        if run.created_at < options.since:
            continue
        if run.status != "completed":
            in_progress += 1
            continue
        conclusion = run.conclusion or "unknown"
        if conclusion in enabled and conclusion in buckets:
            buckets[conclusion].append(run)
            continue
        if conclusion in COUNT_KEYS:
            continue
        other[conclusion] = other.get(conclusion, 0) + 1

    for runs in buckets.values():
        runs.sort(key=lambda item: item.created_at, reverse=True)

    attention_keys = [key for key in ATTENTION_KEYS if key in enabled]
    attention_runs: list[Run] = []
    for key in attention_keys:
        attention_runs.extend(buckets[key])
    success_runs = buckets["success"] if "success" in enabled else []
    matched = len(attention_runs) + len(success_runs)
    budget = options.max_items
    shown_attention = attention_runs[:budget]
    shown_success = success_runs[: budget - len(shown_attention)]
    listed = len(shown_attention) + len(shown_success)
    counts = {key: len(buckets[key]) if key in enabled else 0 for key in COUNT_KEYS}
    attention_count = sum(counts[key] for key in attention_keys)
    markdown = render_markdown(
        options,
        counts,
        attention_count,
        shown_attention,
        shown_success,
        matched,
        listed,
        in_progress,
        other,
        malformed,
        scan_truncated,
        scanned,
        total_matched,
    )
    return Digest(
        markdown=markdown,
        counts=counts,
        attention_count=attention_count,
        matched_count=matched,
        listed_count=listed,
        in_progress_count=in_progress,
    )


def default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_RefuseRedirects)


def request_json(url: str, token: str, opener: Any) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read()
    except DigestError:
        raise
    except urllib.error.HTTPError as exc:
        detail = " ".join(exc.read().decode("utf-8", "replace").split())[:300]
        if exc.code in (401, 403, 404):
            raise DigestError(
                f"GitHub API returned HTTP {exc.code} for workflow runs. "
                "Check that the token can read Actions on this repository "
                f"(actions:read). {detail}".rstrip()
            ) from None
        raise DigestError(f"GitHub API returned HTTP {exc.code}. {detail}".rstrip()) from None
    except urllib.error.URLError as exc:
        raise DigestError(f"Could not reach the GitHub API: {exc.reason}") from None
    except TimeoutError:
        raise DigestError("Timed out talking to the GitHub API.") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DigestError("GitHub API returned a response that was not JSON.") from None
    if not isinstance(payload, dict):
        raise DigestError("GitHub API returned a response that was not a JSON object.")
    return payload


def fetch_runs(
    repository: str,
    token: str,
    since: datetime,
    api_url: str,
    opener: Any,
    max_pages: int = MAX_PAGES,
) -> FetchResult:
    if not token:
        raise DigestError("Input token is empty.")
    owner, name = repository.split("/")
    safe_repo = f"{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"
    stamp = format_api_time(since)
    runs: list[dict[str, Any]] = []
    total = 0
    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode(
            {
                "per_page": str(PER_PAGE),
                "page": str(page),
                "created": f">={stamp}",
            }
        )
        url = f"{api_url}/repos/{safe_repo}/actions/runs?{query}"
        payload = request_json(url, token, opener)
        if page == 1:
            try:
                total = int(payload.get("total_count") or 0)
            except (TypeError, ValueError):
                raise DigestError("GitHub API returned an invalid total_count.") from None
        batch = payload.get("workflow_runs", [])
        if not isinstance(batch, list):
            raise DigestError("GitHub API response did not include a workflow_runs list.")
        for item in batch:
            runs.append(item if isinstance(item, dict) else {})
        if len(batch) < PER_PAGE:
            break
        if total and len(runs) >= total:
            break
    return FetchResult(
        runs=runs,
        truncated=bool(total and len(runs) < total),
        scanned=len(runs),
        total=total,
    )


def write_digest_file(env: Mapping[str, str], markdown: str) -> str:
    workspace = env.get("GITHUB_WORKSPACE") or os.getcwd()
    directory = os.path.join(workspace, "digest-output")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "digest.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(markdown)
    return path


def append_summary(path: str, markdown: str) -> None:
    prefix = ""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        prefix = "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(prefix + markdown)


def write_outputs(path: str, values: Mapping[str, str]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise DigestError(f"Refusing to write multiline output {key}.")
            handle.write(f"{key}={value}\n")


def output_values(digest: Digest, path: str) -> dict[str, str]:
    return {
        "failure-count": str(digest.counts["failure"]),
        "startup-failure-count": str(digest.counts["startup_failure"]),
        "timed-out-count": str(digest.counts["timed_out"]),
        "cancelled-count": str(digest.counts["cancelled"]),
        "success-count": str(digest.counts["success"]),
        "attention-count": str(digest.attention_count),
        "has-failures": "true" if digest.has_failures else "false",
        "digest-path": path,
    }


def log_line(options: Options, digest: Digest, path: str) -> str:
    parts = [f"{digest.counts[key]} {key}" for key in summary_keys(options)]
    joined = ", ".join(parts)
    return (
        f"Actions digest for {options.repository}: {joined}. "
        f"Listed {digest.listed_count} of {digest.matched_count}. Wrote {path}."
    )


def main(
    env: Mapping[str, str] | None = None,
    opener: Any = None,
    now: datetime | None = None,
) -> int:
    environ: Mapping[str, str] = os.environ if env is None else env
    try:
        if sys.version_info < (3, 9):
            raise DigestError("Python 3.9+ is required.")
        options = options_from_env(environ, now=now)
        token = (environ.get("INPUT_TOKEN") or "").strip()
        if not token:
            raise DigestError(
                "Input token is empty. Pass a token with actions:read, usually the job GITHUB_TOKEN."
            )
        api_url = parse_https_url(
            environ.get("INPUT_API_URL") or environ.get("GITHUB_API_URL") or "",
            "API URL",
            "https://api.github.com",
        )
        print(
            f"Listing workflow runs for {options.repository} created since "
            f"{format_api_time(options.since)}."
        )
        fetched = fetch_runs(
            repository=options.repository,
            token=token,
            since=options.since,
            api_url=api_url,
            opener=opener or default_opener(),
        )
        digest = build_digest(
            fetched.runs,
            options,
            scan_truncated=fetched.truncated,
            scanned=fetched.scanned,
            total_matched=fetched.total,
        )
        path = write_digest_file(environ, digest.markdown)
        summary = environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            append_summary(summary, digest.markdown)
        output_path = environ.get("GITHUB_OUTPUT")
        if output_path:
            write_outputs(output_path, output_values(digest, path))
        print(log_line(options, digest, path))
        if options.fail_on_failures and digest.has_failures:
            failed = digest.counts["failure"] + digest.counts["startup_failure"]
            raise DigestError(
                f"{failed} failed run(s) in the last {options.lookback_hours} hours."
            )
        return 0
    except DigestError as exc:
        print(f"::error::{' '.join(str(exc).split())}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
