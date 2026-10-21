"""Checks that the action, workflow, and docs stay in sync."""

from __future__ import annotations

import re
from pathlib import Path
from unittest import TestCase

TOKEN_SHAPES = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
)

ROOT = Path(__file__).resolve().parents[1]


def section_keys(text: str, section: str) -> list[str]:
    keys: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line == f"{section}:":
            in_section = True
            continue
        if in_section and line and not line.startswith(" "):
            break
        if in_section:
            match = re.match(r"^  ([a-z0-9-]+):", line)
            if match:
                keys.append(match.group(1))
    return keys


class ProjectTests(TestCase):
    def test_readme_documents_the_action_and_sample(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        action = (ROOT / "action.yml").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "digest.yml").read_text(encoding="utf-8")
        sample = (ROOT / "tests" / "fixtures" / "sample.md").read_text(encoding="utf-8")
        self.assertIn(sample.strip(), readme)
        self.assertIn(workflow.strip(), readme)
        for name in section_keys(action, "inputs"):
            self.assertIn(f"`{name}`", readme)
        for name in section_keys(action, "outputs"):
            self.assertIn(f"`{name}`", readme)
        self.assertIn("using: composite", action)
        self.assertIn("scripts/digest.py", action)
        self.assertIn("actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9", action)
        self.assertIn('default: "24"', action)
        self.assertIn('default: "30"', action)
        self.assertIn('default: "false"', action)
        self.assertIn('default: "true"', action)

    def test_example_and_test_workflows(self) -> None:
        digest_workflow = (ROOT / ".github" / "workflows" / "digest.yml").read_text(encoding="utf-8")
        test_workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", digest_workflow)
        self.assertIn('cron: "0 13 * * *"', digest_workflow)
        self.assertIn("uses: ./", digest_workflow)
        self.assertIn("actions: write", digest_workflow)
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", digest_workflow)
        self.assertIn("python3 -m unittest discover -s tests -v", test_workflow)
        self.assertIn("contents: read", test_workflow)

    def test_license_contributing_and_gitignore(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2026 limengjie1999-cloud", license_text)
        self.assertIn("Permission is hereby granted", license_text)
        self.assertIn("python3 -m unittest discover -s tests -v", contributing)
        self.assertIn("digest-output/", gitignore)
        self.assertIn("standard library", contributing.lower())

    def test_repository_does_not_contain_tokens(self) -> None:
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in TOKEN_SHAPES:
                self.assertIsNone(pattern.search(text), str(path))
