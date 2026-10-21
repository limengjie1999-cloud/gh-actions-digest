"""Behavior tests for the workflow-run digest."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import digest  # noqa: E402

NOW = datetime(2026, 10, 21, 9, 48, tzinfo=timezone.utc)
FIXTURES = ROOT / "tests" / "fixtures"
WINDOW = json.loads((FIXTURES / "window.json").read_text(encoding="utf-8"))
SAMPLE = (FIXTURES / "sample.md").read_text(encoding="utf-8")


def make_options(**overrides: object) -> digest.Options:
    values: dict[str, object] = {
        "lookback_hours": 24,
        "include_successes": False,
        "include_cancelled": True,
        "include_timed_out": True,
        "max_items": 30,
        "repository": "octo-org/octo-repo",
        "server_url": "https://github.com",
        "fail_on_failures": False,
        "now": NOW,
    }
    values.update(overrides)
    return digest.Options(**values)  # type: ignore[arg-type]


def make_env(root: Path, **overrides: str) -> dict[str, str]:
    env = {
        "INPUT_REPOSITORY": "octo-org/octo-repo",
        "INPUT_TOKEN": "super-secret-token",
        "INPUT_LOOKBACK_HOURS": "24",
        "INPUT_API_URL": "https://api.github.com",
        "INPUT_SERVER_URL": "https://github.com",
        "GITHUB_WORKSPACE": str(root),
        "GITHUB_STEP_SUMMARY": str(root / "summary.md"),
        "GITHUB_OUTPUT": str(root / "output.txt"),
    }
    env.update(overrides)
    return env


class Response:
    def __init__(self, payload: object) -> None:
        if isinstance(payload, bytes):
            self._raw = payload
        else:
            self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class FakeOpener:
    def __init__(self, handler) -> None:  # noqa: ANN001
        self.handler = handler
        self.requests: list[object] = []
        self.timeout: object = None

    def open(self, request, timeout=None):  # noqa: ANN001
        self.requests.append(request)
        self.timeout = timeout
        result = self.handler(request)
        if isinstance(result, BaseException):
            raise result
        return result


def query_of(request) -> dict[str, list[str]]:  # noqa: ANN001
    return urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)


def headers_of(request) -> dict[str, str]:  # noqa: ANN001
    return {key.lower(): value for key, value in request.header_items()}


def run_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "CI",
        "display_title": "Broken build",
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "failure",
        "html_url": "https://github.com/octo-org/octo-repo/actions/runs/9",
        "run_number": 9,
        "created_at": "2026-10-21T08:00:00Z",
        "triggering_actor": {"login": "octocat"},
    }
    payload.update(overrides)
    return payload


class DigestTests(TestCase):
    def test_window_fixture_matches_sample(self) -> None:
        result = digest.build_digest(WINDOW, make_options())
        self.assertEqual(result.markdown, SAMPLE)
        self.assertNotIn("Quiet success", result.markdown)
        self.assertNotIn("Ancient failure", result.markdown)
        self.assertNotIn("HIDDENBODY", result.markdown)
        self.assertNotIn("Still going", result.markdown)
        self.assertNotIn("Skip docs", result.markdown)
        self.assertEqual(result.counts["failure"], 1)
        self.assertEqual(result.counts["success"], 0)
        self.assertEqual(result.attention_count, 4)
        self.assertEqual(result.matched_count, 4)
        self.assertEqual(result.listed_count, 4)
        self.assertEqual(result.in_progress_count, 1)
        self.assertTrue(result.has_failures)

    def test_successes_are_optional_and_old_runs_stay_out(self) -> None:
        result = digest.build_digest(WINDOW, make_options(include_successes=True))
        self.assertIn("Quiet success", result.markdown)
        self.assertIn("## Successes", result.markdown)
        self.assertNotIn("Ancient failure", result.markdown)
        self.assertEqual(result.counts["success"], 1)
        self.assertEqual(result.matched_count, 5)

    def test_disabled_categories_are_omitted_from_counts(self) -> None:
        result = digest.build_digest(
            WINDOW,
            make_options(include_cancelled=False, include_timed_out=False),
        )
        self.assertNotIn("Refresh docs site", result.markdown)
        self.assertNotIn("Publish 1.4.0", result.markdown)
        self.assertNotIn("| cancelled |", result.markdown)
        self.assertNotIn("| timed_out |", result.markdown)
        self.assertEqual(result.attention_count, 2)
        self.assertEqual(result.counts["cancelled"], 0)
        self.assertEqual(result.counts["timed_out"], 0)

    def test_max_items_keeps_failures_ahead_of_successes(self) -> None:
        result = digest.build_digest(
            WINDOW,
            make_options(include_successes=True, max_items=1),
        )
        self.assertIn("Update the README badges", result.markdown)
        self.assertNotIn("Quiet success", result.markdown)
        self.assertNotIn("Publish 1.4.0", result.markdown)
        self.assertIn("Showing 1 of 5 matching runs.", result.markdown)
        self.assertEqual(result.attention_count, 4)
        self.assertEqual(result.counts["success"], 1)

    def test_window_boundary_uses_utc(self) -> None:
        raw = [
            run_payload(
                display_title="On the boundary",
                created_at="2026-10-20T09:48:00Z",
                run_number=1,
            ),
            run_payload(
                display_title="Just outside",
                created_at="2026-10-20T09:47:59Z",
                run_number=2,
            ),
            run_payload(
                display_title="Offset inside",
                created_at="2026-10-20T11:48:00+02:00",
                run_number=3,
            ),
        ]
        text = digest.build_digest(raw, make_options()).markdown
        self.assertIn("On the boundary", text)
        self.assertIn("Offset inside", text)
        self.assertNotIn("Just outside", text)

    def test_table_cells_are_escaped_and_unsafe_urls_are_not_linked(self) -> None:
        raw = [
            run_payload(
                name="CI <Build>",
                display_title="Fix | pipe & more",
                head_branch="feat/a|b",
                html_url="http://insecure.example/runs/1",
                run_number=4,
                triggering_actor={"login": "octo`cat"},
            )
        ]
        text = digest.build_digest(raw, make_options()).markdown
        self.assertIn("Fix \\| pipe &amp; more", text)
        self.assertIn("CI &lt;Build&gt;", text)
        self.assertIn("`feat/a\\|b`", text)
        self.assertIn("octo'cat", text)
        self.assertIn("| #4 |", text)
        self.assertNotIn("http://insecure.example", text)
        self.assertNotIn("](http://", text)

    def test_triggering_actor_wins_and_blank_branch_is_a_dash(self) -> None:
        raw = [
            run_payload(
                head_branch="",
                triggering_actor={"login": "trigger-user"},
                actor={"login": "other-user"},
            )
        ]
        text = digest.build_digest(raw, make_options()).markdown
        self.assertIn("trigger-user", text)
        self.assertNotIn("other-user", text)
        self.assertIn("| — |", text)

    def test_schedule_title_falls_back_to_commit_subject(self) -> None:
        raw = [
            run_payload(
                name="Nightly",
                display_title="Nightly",
                event="schedule",
                head_commit={"message": "Subject line\n\nHIDDENBODY"},
            )
        ]
        text = digest.build_digest(raw, make_options()).markdown
        self.assertIn("Subject line", text)
        self.assertNotIn("HIDDENBODY", text)

    def test_in_progress_skipped_and_unreadable_runs_are_notes(self) -> None:
        raw = [
            run_payload(status="queued", conclusion=None, display_title="Queued"),
            run_payload(status="in_progress", conclusion=None, display_title="Running"),
            run_payload(conclusion="skipped", display_title="Skipped job"),
            run_payload(conclusion="action_required", display_title="Waiting on approval"),
            {},
            "not-a-run",
        ]
        result = digest.build_digest(raw, make_options())
        self.assertEqual(result.in_progress_count, 2)
        self.assertIn("2 runs are still in progress", result.markdown)
        self.assertIn("1 action_required, 1 skipped", result.markdown)
        self.assertIn("2 runs could not be read", result.markdown)
        self.assertNotIn("Queued", result.markdown)
        self.assertNotIn("Skipped job", result.markdown)

    def test_scan_truncation_is_reported(self) -> None:
        text = digest.build_digest(
            [run_payload()],
            make_options(),
            scan_truncated=True,
            scanned=1000,
            total_matched=1400,
        ).markdown
        self.assertIn("Read 1000 of 1400 runs returned for this window.", text)

    def test_all_clear_copy(self) -> None:
        text = digest.build_digest([], make_options()).markdown
        self.assertIn("No runs need attention in this window.", text)
        self.assertNotIn("## Needs attention", text)

    def test_input_parsing(self) -> None:
        self.assertTrue(digest.parse_bool("YES", "include-successes"))
        self.assertFalse(digest.parse_bool("No", "include-successes"))
        with self.assertRaises(digest.DigestError):
            digest.parse_bool("maybe", "include-successes")
        with self.assertRaises(digest.DigestError):
            digest.parse_int("24h", "lookback-hours", 1, 336)
        with self.assertRaises(digest.DigestError):
            digest.parse_int("0", "lookback-hours", 1, 336)
        with self.assertRaises(digest.DigestError):
            digest.parse_int("337", "lookback-hours", 1, 336)
        for bad in ("", "octo-org", "octo-org/../repo", "https://github.com/a/b", "a/b/c"):
            with self.assertRaises(digest.DigestError):
                digest.parse_repository(bad)
        self.assertEqual(digest.parse_repository("octo-org/my.repo"), "octo-org/my.repo")
        with self.assertRaises(digest.DigestError):
            digest.parse_https_url("http://api.github.com", "API URL", "https://api.github.com")
        with self.assertRaises(digest.DigestError):
            digest.parse_https_url(
                "https://user:token@api.github.com", "API URL", "https://api.github.com"
            )

    def test_options_from_env_defaults(self) -> None:
        options = digest.options_from_env({"INPUT_REPOSITORY": "octo-org/octo-repo"}, now=NOW)
        self.assertEqual(options.lookback_hours, 24)
        self.assertEqual(options.max_items, 30)
        self.assertFalse(options.include_successes)
        self.assertTrue(options.include_cancelled)
        self.assertTrue(options.include_timed_out)
        self.assertFalse(options.fail_on_failures)
        self.assertEqual(options.server_url, "https://github.com")
        self.assertEqual(options.since, datetime(2026, 10, 20, 9, 48, tzinfo=timezone.utc))

    def test_main_writes_summary_output_and_file(self) -> None:
        opener = FakeOpener(
            lambda request: Response({"total_count": len(WINDOW), "workflow_runs": WINDOW})
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = root / "summary.md"
            summary.write_text("earlier step\n", encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = digest.main(make_env(root), opener=opener, now=NOW)
            digest_file = (root / "digest-output" / "digest.md").read_text(encoding="utf-8")
            output = (root / "output.txt").read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(digest_file, SAMPLE)
            self.assertEqual(summary.read_text(encoding="utf-8"), "earlier step\n\n" + SAMPLE)
            self.assertIn("failure-count=1\n", output)
            self.assertIn("startup-failure-count=1\n", output)
            self.assertIn("timed-out-count=1\n", output)
            self.assertIn("cancelled-count=1\n", output)
            self.assertIn("success-count=0\n", output)
            self.assertIn("attention-count=4\n", output)
            self.assertIn("has-failures=true\n", output)
            self.assertIn(f"digest-path={root / 'digest-output' / 'digest.md'}\n", output)
            self.assertNotIn("super-secret-token", digest_file)
            self.assertNotIn("super-secret-token", stdout.getvalue())
            self.assertEqual(opener.timeout, 30)
            self.assertEqual(headers_of(opener.requests[0])["authorization"], "Bearer super-secret-token")

    def test_fail_on_failures_still_publishes_the_digest(self) -> None:
        opener = FakeOpener(
            lambda request: Response({"total_count": 1, "workflow_runs": [run_payload()]})
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = digest.main(
                    make_env(root, INPUT_FAIL_ON_FAILURES="true"),
                    opener=opener,
                    now=NOW,
                )
            summary = (root / "summary.md").read_text(encoding="utf-8")
            output = (root / "output.txt").read_text(encoding="utf-8")
            self.assertEqual(code, 1)
            self.assertIn("Broken build", summary)
            self.assertIn("has-failures=true", output)
            self.assertIn("1 failed run(s)", stderr.getvalue())
            self.assertNotIn("super-secret-token", stderr.getvalue())

    def test_cancellations_alone_do_not_fail_the_step(self) -> None:
        opener = FakeOpener(
            lambda request: Response(
                {
                    "total_count": 1,
                    "workflow_runs": [run_payload(conclusion="cancelled", display_title="Stopped")],
                }
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = digest.main(
                    make_env(root, INPUT_FAIL_ON_FAILURES="true"),
                    opener=opener,
                    now=NOW,
                )
            output = (root / "output.txt").read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertIn("has-failures=false", output)
            self.assertIn("attention-count=1", output)
            self.assertIn("Stopped", (root / "summary.md").read_text(encoding="utf-8"))

    def test_startup_failure_counts_as_a_failure(self) -> None:
        opener = FakeOpener(
            lambda request: Response(
                {
                    "total_count": 1,
                    "workflow_runs": [
                        run_payload(conclusion="startup_failure", display_title="Did not start")
                    ],
                }
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = digest.main(
                    make_env(root, INPUT_FAIL_ON_FAILURES="true"),
                    opener=opener,
                    now=NOW,
                )
            self.assertEqual(code, 1)
            self.assertIn("startup-failure-count=1", (root / "output.txt").read_text(encoding="utf-8"))
            self.assertIn("failed run(s)", stderr.getvalue())

    def test_missing_token_does_not_call_the_api(self) -> None:
        opener = FakeOpener(lambda request: Response({"total_count": 0, "workflow_runs": []}))
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = digest.main(
                    make_env(Path(tmp), INPUT_TOKEN="  "),
                    opener=opener,
                    now=NOW,
                )
            self.assertEqual(code, 1)
            self.assertEqual(opener.requests, [])
            self.assertIn("token", stderr.getvalue())

    def test_insecure_api_url_is_rejected(self) -> None:
        opener = FakeOpener(lambda request: Response({"total_count": 0, "workflow_runs": []}))
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = digest.main(
                    make_env(Path(tmp), INPUT_API_URL="http://127.0.0.1:9"),
                    opener=opener,
                    now=NOW,
                )
            self.assertEqual(code, 1)
            self.assertEqual(opener.requests, [])
            self.assertIn("https", stderr.getvalue())

    def test_fetch_paginates_with_the_created_filter(self) -> None:
        def handler(request):  # noqa: ANN001
            parsed = query_of(request)
            page = int(parsed["page"][0])
            self.assertEqual(parsed["per_page"], ["100"])
            self.assertEqual(parsed["created"], [">=2026-10-20T09:48:00Z"])
            self.assertIn("/repos/octo-org/octo-repo/actions/runs?", request.full_url)
            headers = headers_of(request)
            self.assertEqual(headers["authorization"], "Bearer super-secret-token")
            self.assertEqual(headers["accept"], "application/vnd.github+json")
            self.assertEqual(headers["x-github-api-version"], digest.API_VERSION)
            self.assertIn("gh-actions-digest", headers["user-agent"])
            if page == 1:
                return Response(
                    {"total_count": 120, "workflow_runs": [{"id": index} for index in range(100)]}
                )
            if page == 2:
                return Response(
                    {"total_count": 120, "workflow_runs": [{"id": index} for index in range(20)]}
                )
            raise AssertionError(f"unexpected page {page}")

        opener = FakeOpener(handler)
        fetched = digest.fetch_runs(
            "octo-org/octo-repo",
            "super-secret-token",
            NOW - timedelta(hours=24),
            "https://api.github.com",
            opener,
        )
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(fetched.scanned, 120)
        self.assertFalse(fetched.truncated)
        self.assertEqual(fetched.total, 120)

    def test_fetch_reports_a_short_scan(self) -> None:
        opener = FakeOpener(
            lambda request: Response(
                {"total_count": 250, "workflow_runs": [{"id": index} for index in range(100)]}
            )
        )
        fetched = digest.fetch_runs(
            "octo-org/octo-repo",
            "token",
            NOW,
            "https://api.github.com",
            opener,
            max_pages=1,
        )
        self.assertTrue(fetched.truncated)
        self.assertEqual(fetched.scanned, 100)
        self.assertEqual(fetched.total, 250)

    def test_fetch_http_and_parse_errors(self) -> None:
        forbidden = urllib.error.HTTPError(
            "https://api.github.com/repos/octo-org/octo-repo/actions/runs",
            403,
            "Forbidden",
            None,
            io.BytesIO(b'{"message":"resource not accessible by integration"}'),
        )
        opener = FakeOpener(lambda request: forbidden)
        with self.assertRaises(digest.DigestError) as caught:
            digest.fetch_runs(
                "octo-org/octo-repo",
                "super-secret-token",
                NOW,
                "https://api.github.com",
                opener,
            )
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("actions:read", str(caught.exception))
        self.assertNotIn("super-secret-token", str(caught.exception))

        opener = FakeOpener(lambda request: Response(b"not-json"))
        with self.assertRaises(digest.DigestError) as caught:
            digest.fetch_runs("octo-org/octo-repo", "token", NOW, "https://api.github.com", opener)
        self.assertIn("not JSON", str(caught.exception))

        opener = FakeOpener(lambda request: urllib.error.URLError("name resolution failed"))
        with self.assertRaises(digest.DigestError) as caught:
            digest.fetch_runs("octo-org/octo-repo", "token", NOW, "https://api.github.com", opener)
        self.assertIn("Could not reach", str(caught.exception))

    def test_redirects_are_refused(self) -> None:
        with self.assertRaises(digest.DigestError) as caught:
            digest._RefuseRedirects().redirect_request(
                None, None, 302, "Found", {}, "https://example.com/other"
            )
        self.assertIn("Refusing to follow the redirect", str(caught.exception))

    def test_utf8_titles_round_trip(self) -> None:
        opener = FakeOpener(
            lambda request: Response(
                {
                    "total_count": 1,
                    "workflow_runs": [run_payload(display_title="Café deploy")],
                }
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = digest.main(make_env(root), opener=opener, now=NOW)
            text = (root / "digest-output" / "digest.md").read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertIn("Café deploy", text)
