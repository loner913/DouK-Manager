from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build-windows.yml"


class DevelopCiWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_develop_pull_requests_and_pushes_are_ci_inputs(self) -> None:
        self.assertIn("pull_request:", self.workflow)
        self.assertRegex(
            self.workflow,
            r"pull_request:\s*\n\s*branches: \[develop, main\]",
        )
        self.assertRegex(
            self.workflow,
            r"push:\s*\n\s*branches: \[develop\]",
        )
        self.assertNotRegex(
            self.workflow,
            r"push:\s*\n\s*branches: \[main\]",
        )
        self.assertIn("workflow_dispatch:", self.workflow)

        pull_request_block = self.workflow.split("  pull_request:", 1)[1].split(
            "  push:", 1
        )[0]
        self.assertNotIn("paths:", pull_request_block)

    def test_test_job_has_stable_name_and_read_only_permissions(self) -> None:
        self.assertRegex(self.workflow, r"(?m)^name: Develop CI$")
        self.assertRegex(
            self.workflow,
            r"(?ms)^  test:\s*\n    name: Test\s*$",
        )
        self.assertRegex(
            self.workflow,
            r"(?ms)^permissions:\s*\n  contents: read\s*$",
        )
        self.assertNotRegex(self.workflow, r"(?m)^\s*(contents|actions): write\s*$")

    def test_package_is_gated_and_artifact_is_identifiable(self) -> None:
        self.assertRegex(
            self.workflow,
            r"(?ms)^  package:\s*\n    name: Windows portable package\s*$",
        )
        self.assertIn("needs: test", self.workflow)
        self.assertIn("github.event_name != 'pull_request'", self.workflow)
        self.assertIn("github.run_number", self.workflow)
        self.assertIn("github.sha", self.workflow)
        self.assertIn("steps.identity.outputs.ref", self.workflow)
        self.assertIn("steps.identity.outputs.sha", self.workflow)
        self.assertIn("retention-days: 30", self.workflow)
        self.assertIn('"Version: 0.1.3"', self.workflow)

    def test_official_actions_do_not_use_node20_generations(self) -> None:
        self.assertIn("actions/checkout@v7", self.workflow)
        self.assertIn("actions/setup-python@v7", self.workflow)
        self.assertIn("actions/upload-artifact@v7", self.workflow)
        self.assertIsNone(re.search(r"actions/checkout@v4", self.workflow))
        self.assertIsNone(re.search(r"actions/setup-python@v5", self.workflow))


if __name__ == "__main__":
    unittest.main()
