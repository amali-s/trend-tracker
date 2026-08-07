"""Guard the CI workflow against typos.

GitHub Actions can't run here, so this is a config check, not an execution
test. It asserts the load-bearing lines are present and — only if PyYAML
happens to be installed — that the file parses and the key structure holds.
String-based by default so it needs no new dependency.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WORKFLOW = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".github", "workflows", "weekly-trend-scan.yml",
)


@pytest.fixture(scope="module")
def text() -> str:
    assert os.path.exists(WORKFLOW), "the weekly workflow file is missing"
    with open(WORKFLOW) as f:
        return f.read()


class TestSchedule:
    def test_sunday_1300_utc_cron(self, text):
        assert "cron: '0 13 * * 0'" in text

    def test_manual_dispatch_is_available(self, text):
        assert "workflow_dispatch:" in text

    def test_seed_and_dry_run_inputs_exist(self, text):
        assert "seed:" in text
        assert "dry_run:" in text


class TestPermissionsAndConcurrency:
    def test_has_write_access_for_commit_back(self, text):
        assert "contents: write" in text

    def test_serialized_to_avoid_racing_the_commit(self, text):
        assert "concurrency:" in text
        assert "group: trend-tracker" in text


class TestRunStep:
    def test_invokes_the_orchestrator(self, text):
        assert "python -m src.main" in text

    def test_maps_the_seed_and_dry_run_inputs_to_flags(self, text):
        assert "--seed" in text
        assert "--dry-run" in text

    @pytest.mark.parametrize("secret", [
        "ANTHROPIC_API_KEY", "EMAIL_TO", "GMAIL_USER", "GMAIL_APP_PASSWORD",
    ])
    def test_required_secrets_are_wired(self, text, secret):
        assert f"secrets.{secret}" in text

    def test_sendgrid_alternative_is_present(self, text):
        assert "secrets.SENDGRID_API_KEY" in text
        assert "secrets.EMAIL_FROM" in text


class TestCommitBack:
    def test_commits_the_three_state_files(self, text):
        for name in ("seen_posts.json", "seen_investments.json", "weekly_history.json"):
            assert f"data/{name}" in text

    def test_is_a_noop_when_nothing_changed(self, text):
        assert "git diff --cached --quiet" in text

    def test_pushes_on_change(self, text):
        assert "git push" in text

    def test_skipped_on_dry_run(self, text):
        assert "inputs.dry_run != 'true'" in text


class TestPlaywrightIsNotInstalled:
    def test_no_browser_download(self, text):
        """All sources are tier A/B; installing chromium would be pure waste.

        Checked against command lines only — the explanatory comment naming
        the skipped command is fine.
        """
        commands = [
            line for line in text.splitlines()
            if "playwright install" in line and not line.lstrip().startswith("#")
        ]
        assert commands == []


class TestStructureIfYamlAvailable:
    def test_parses_and_has_the_scan_job(self, text):
        yaml = pytest.importorskip("yaml")
        doc = yaml.safe_load(text)
        assert "scan" in doc["jobs"]
        # PyYAML parses the bare `on:` key as the boolean True.
        trigger = doc.get("on", doc.get(True))
        assert "schedule" in trigger
        assert doc["permissions"]["contents"] == "write"
