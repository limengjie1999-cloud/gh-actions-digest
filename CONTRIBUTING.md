# Contributing

Thanks for helping keep this action useful for other maintainers.

## Development

The digest is a single Python script in `scripts/digest.py`. It uses the
standard library only and needs Python 3.9 or newer. GitHub-hosted runners
already provide that.

Run the tests from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

`tests/fixtures/sample.md` is the digest produced from
`tests/fixtures/window.json`. The README embeds that sample. When you change
the markdown, regenerate the sample and update both places. The tests fail if
they drift.

A local preview against a real repository needs a token that can read Actions.
Fine-grained tokens need the Actions repository permission. Do not commit the
token.

```bash
INPUT_REPOSITORY=owner/name \
INPUT_TOKEN="$GITHUB_TOKEN" \
INPUT_LOOKBACK_HOURS=24 \
python3 scripts/digest.py
```

The script writes `digest-output/digest.md`. That directory is gitignored.

## Pull requests

- Keep the action usable with the defaults. A daily run should produce a short
  failure digest without extra configuration.
- Pin new third-party actions to a commit SHA and note the version in a comment.
- Do not add dependencies, secrets, or network calls beyond the GitHub API.
- Add or update a unit test for behavior changes.
