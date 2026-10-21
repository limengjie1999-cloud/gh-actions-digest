# gh-actions-digest

A composite GitHub Action that writes a short digest of recent workflow runs for the repository that calls it. Failures and startup failures are always included. Timeouts and cancellations are included by default. Successes stay out unless you ask for them.

The digest is appended to the job summary and, by default, uploaded as a workflow artifact named `actions-digest`.

## Why

On a small open-source repository, failed Actions runs are easy to miss. Mail is per workflow, badges only show the latest run, and a red `main` branch can sit there until someone happens to open the Actions tab.

This action asks the workflow-runs API what was created in the last day and puts the runs that need attention in one markdown page: the scheduled job's summary, with links to the runs. It is meant to be called once a day and then left alone.

## Quick start

1. Copy [`.github/workflows/digest.yml`](.github/workflows/digest.yml) into the repository you want to watch, or call this action from your own workflow.
2. Give the job `actions: write` so it can list runs and upload the digest. `actions: write` includes read. Use `actions: read` instead when you set `artifact` to `false`.
3. Merge the workflow to the default branch. GitHub only starts `schedule` events from that branch. Use **Run workflow** to produce a digest immediately.

Calling `uses: ./` requires a checkout, because the action files have to be in the workspace. A published copy does not:

```yaml
name: Daily Actions digest

on:
  schedule:
    - cron: "0 13 * * *"
  workflow_dispatch:

permissions:
  actions: write

jobs:
  digest:
    runs-on: ubuntu-latest
    steps:
      - uses: limengjie1999-cloud/gh-actions-digest@main
        with:
          lookback-hours: "24"
          include-successes: "false"
          max-items: "30"
```

`@main` follows the default branch. After you publish a release, pin a tag or a commit SHA (for example `limengjie1999-cloud/gh-actions-digest@v1`) so upgrades stay deliberate.

The action needs Python 3.9 or newer, which GitHub-hosted runners already provide. It uses only the Python standard library.

## Inputs

| Input | Type | Default | Description |
| --- | --- | --- | --- |
| `lookback-hours` | integer | `24` | Include runs created in the last N hours. Minimum 1, maximum 336 (14 days). |
| `include-successes` | boolean | `false` | Also list successful runs. |
| `include-cancelled` | boolean | `true` | List cancelled runs. Included runs count as needing attention. |
| `include-timed-out` | boolean | `true` | List timed-out runs. Included runs count as needing attention. |
| `max-items` | integer | `30` | Maximum runs to list, from 1 to 100. Summary counts still include every match. Failures are listed before startup failures, timeouts, cancellations, and successes. |
| `token` | string | job token | Token used to read Actions. It needs `actions: read`. |
| `repository` | string | calling repository | `owner/name` to summarize. |
| `artifact` | boolean | `true` | Upload the markdown file as a workflow artifact. |
| `artifact-name` | string | `actions-digest` | Artifact name. |
| `artifact-retention-days` | integer | `14` | How long GitHub keeps the artifact. `0` uses the repository default. |
| `fail-on-failures` | boolean | `false` | Fail the step when the window contains a `failure` or `startup_failure`. The digest is still written. Timeouts and cancellations do not fail the step. |

Booleans are the strings `true` and `false`.

Startup failures are always listed. They mean the workflow never started, which is a different conclusion from `failure` and is easy to overlook in the Actions UI.

## Outputs

| Output | Description |
| --- | --- |
| `failure-count` | Runs with conclusion `failure`. |
| `startup-failure-count` | Runs with conclusion `startup_failure`. |
| `timed-out-count` | Timed-out runs included in the digest. `0` when `include-timed-out` is `false`. |
| `cancelled-count` | Cancelled runs included in the digest. `0` when `include-cancelled` is `false`. |
| `success-count` | Successful runs included in the digest. `0` when `include-successes` is `false`. |
| `attention-count` | Failures, startup failures, and included timeouts and cancellations. |
| `has-failures` | `true` when `failure-count` or `startup-failure-count` is greater than zero. |
| `digest-path` | Workspace path of `digest-output/digest.md`. |

To fail the job when anything in the digest needs attention, including timeouts and cancellations:

```yaml
- name: Fail when runs need attention
  if: steps.digest.outputs.attention-count != '0'
  run: exit 1
```

## Example workflow

This is [`.github/workflows/digest.yml`](.github/workflows/digest.yml). It runs every day at 13:00 UTC and can be started with **Run workflow**. Manual inputs override the lookback, whether successes are included, and the list length. The schedule uses the defaults.

```yaml
name: Daily Actions digest

on:
  schedule:
    # 13:00 UTC every day. GitHub starts scheduled workflows from the default
    # branch and may delay the run when Actions is busy.
    - cron: "0 13 * * *"
  workflow_dispatch:
    inputs:
      lookback-hours:
        description: Hours of workflow runs to include
        default: "24"
        required: false
      include-successes:
        description: Include successful runs
        type: boolean
        default: false
        required: false
      max-items:
        description: Maximum runs to list
        default: "30"
        required: false

permissions:
  actions: write
  contents: read

jobs:
  digest:
    name: Publish digest
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Check out repository
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1

      - name: Summarize workflow runs
        id: digest
        uses: ./
        with:
          # workflow_dispatch inputs are unset for the daily schedule.
          lookback-hours: ${{ inputs.lookback-hours || '24' }}
          include-successes: ${{ inputs.include-successes == true && 'true' || 'false' }}
          max-items: ${{ inputs.max-items || '30' }}

      - name: Record counts in the log
        run: |
          echo "failure-count=${{ steps.digest.outputs.failure-count }}"
          echo "attention-count=${{ steps.digest.outputs.attention-count }}"
          echo "digest-path=${{ steps.digest.outputs.digest-path }}"
```

Open the finished run and expand the job summary. The same markdown is in the run's artifact as `digest.md`.

## Sample output

This is the job summary for a repository that, in the last 24 hours, had one failure, one startup failure, one timeout, and one cancellation. A successful run from the same window is omitted because `include-successes` is false. An in-progress run and a skipped run are counted in the notes instead of listed as rows.

The sample is generated from [`tests/fixtures/window.json`](tests/fixtures/window.json). When the workflow title is the same as the workflow name, as it is for many scheduled runs, the digest uses the commit subject.

```markdown
# Actions digest

**Repository:** [octo-org/octo-repo](https://github.com/octo-org/octo-repo)
**Window:** runs created in the last 24 hours (since 2026-10-20 09:48 UTC)
**Generated:** 2026-10-21 09:48 UTC

## Summary

| Conclusion | Runs |
| --- | ---: |
| failure | 1 |
| startup_failure | 1 |
| timed_out | 1 |
| cancelled | 1 |

4 runs need attention.

## Needs attention

| Created (UTC) | Conclusion | Workflow | Branch | Event | Actor | Title | Run |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-21 08:12 | failure | CI | `main` | push | octocat | Update the README badges | [#562](https://github.com/octo-org/octo-repo/actions/runs/562) |
| 2026-10-21 04:15 | startup_failure | Nightly | `main` | schedule | octocat | Fix nightly cron window | [#540](https://github.com/octo-org/octo-repo/actions/runs/540) |
| 2026-10-21 07:03 | timed_out | Release | `release` | workflow_dispatch | hubot | Publish 1.4.0 | [#561](https://github.com/octo-org/octo-repo/actions/runs/561) |
| 2026-10-21 02:41 | cancelled | Docs | `docs` | pull_request | octocat | Refresh docs site | [#558](https://github.com/octo-org/octo-repo/actions/runs/558) |

_1 run is still in progress and is not listed._

_Also in this window: 1 skipped._

---

Generated by [gh-actions-digest](https://github.com/limengjie1999-cloud/gh-actions-digest).
```

## How the window works

GitHub's list-runs API filters on `created_at`. The digest uses the same rule: a run is in the window when it was created at or after `now - lookback-hours`. A workflow that was created earlier and only finished inside the window is not included. Times in the digest are UTC.

The client reads at most 1,000 runs (10 pages of 100). If a repository produces more than that in the window, the digest says the scan stopped early. Shorten `lookback-hours` in that case.

Runs that are still queued or running are not listed. Completed runs with conclusions such as `skipped`, `neutral`, `stale`, or `action_required` are counted in one line under the tables.

## Security

The action sends `token` only to `github.api_url` (or `https://api.github.com` when you run the script locally) and does not follow redirects. The digest contains workflow names, branches, actors, and run titles returned by the API. Treat the artifact like any other Actions log. No other secrets are read or written.

## Contributing

The implementation is `scripts/digest.py` plus `action.yml`. Tests are standard-library `unittest`. From a checkout:

```bash
python3 -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for previews, the sample fixture, and what belongs in a pull request.

## License

[MIT](LICENSE). Copyright (c) 2026 limengjie1999-cloud.
