# Develop CI And Branch Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add repeatable CI and development artifacts on `develop`, then enforce GitHub rules that preserve `main`, `develop`, and release tags.

**Architecture:** Keep one Windows workflow with a stable test job and a dependent package job. A repository test locks down event triggers, permissions, build gating, artifact identity, and retention. GitHub rulesets enforce the server-side merge and ref-history boundaries after the CI check exists.

**Tech Stack:** GitHub Actions YAML, PowerShell, Python 3.12 `unittest`, PyInstaller, GitHub rulesets.

## Global Constraints

- `main` and `v0.1.0` must remain at `161c20b9489bbe8f5a55d5777e45f803c79357a4` throughout this work.
- Do not create, move, delete, or overwrite any tag or Release.
- Do not read or write the formal downloader `_internal\\Volume`.
- Do not install into or otherwise modify the local Python environment.
- Keep workflow permissions at `contents: read` and keep the Windows portable-package format.
- Routine development integrates through pull requests targeting `develop`, never directly into `main`.

---

### Task 1: Lock The Workflow Policy With A Regression Test

**Files:**
- Create: `tests/test_develop_ci_workflow.py`
- Inspect: `.github/workflows/build-windows.yml`

**Interfaces:**
- Consumes: repository-root workflow file at `.github/workflows/build-windows.yml`.
- Produces: `DevelopCiWorkflowTests`, a static policy suite that fails when triggers, permissions, build gating, or artifact identity drift.

- [ ] **Step 1: Write the failing test**

Create a `unittest.TestCase` that reads the workflow as UTF-8 and asserts:

```python
class DevelopCiWorkflowTests(unittest.TestCase):
    def test_develop_pull_requests_and_pushes_are_ci_inputs(self):
        self.assertIn("pull_request:", self.workflow)
        self.assertRegex(self.workflow, r"pull_request:\s*\n\s*branches: \[develop\]")
        self.assertRegex(self.workflow, r"push:\s*\n\s*branches: \[develop\]")
        self.assertNotRegex(self.workflow, r"push:\s*\n\s*branches: \[main\]")

    def test_package_is_gated_and_artifact_is_identifiable(self):
        self.assertIn("needs: test", self.workflow)
        self.assertIn("github.event_name != 'pull_request'", self.workflow)
        self.assertIn("github.run_number", self.workflow)
        self.assertIn("github.sha", self.workflow)
        self.assertIn("retention-days: 30", self.workflow)
```

Also assert a stable `Test` job name, `contents: read`, checkout/setup action versions that do not emit the recorded Node.js 20 deprecation warning, and absence of tag/release write permissions.

- [ ] **Step 2: Run test to verify it fails**

Run: `$env:PYTHONPATH='src'; python -m unittest tests.test_develop_ci_workflow -v`

Expected: FAIL because the old workflow pushes only on `main`, has no pull-request trigger, uses one combined job, uploads artifacts for every run, and retains them for 90 days.

- [ ] **Step 3: Commit the red test**

```powershell
git add -- tests/test_develop_ci_workflow.py
git commit -m "test: define develop CI policy"
```

### Task 2: Implement The Develop CI Workflow

**Files:**
- Modify: `.github/workflows/build-windows.yml`
- Test: `tests/test_develop_ci_workflow.py`

**Interfaces:**
- Consumes: GitHub event fields `event_name`, `ref_name`, `run_number`, and `sha`.
- Produces: stable required check `Develop CI / Test` and artifact `DouK-Manager_Windows_X64-<ref>-run-<number>-<short-sha>`.

- [ ] **Step 1: Change triggers and split jobs**

Set `pull_request.branches` to `[develop, main]`, set `push.branches` to
`[develop]`, and preserve `workflow_dispatch`. Do not apply path filters to pull requests because a
skipped required workflow would block merging indefinitely. Retain relevant
path filters for `develop` pushes so documentation-only merges do not create a
package. Rename the workflow to `Develop CI` and add a `test` job named `Test`
that installs the existing project dependencies and runs:

```powershell
python -m unittest discover -s tests -v
```

Add a dependent `package` job named `Windows portable package` with:

```yaml
needs: test
if: github.event_name != 'pull_request'
```

- [ ] **Step 2: Make artifacts uniquely auditable**

Calculate a seven-character SHA and artifact-safe ref in PowerShell, expose both through `$GITHUB_OUTPUT`, preserve `BUILD-INFO.txt`, and upload using:

```yaml
name: DouK-Manager_Windows_X64-${{ steps.identity.outputs.ref }}-run-${{ github.run_number }}-${{ steps.identity.outputs.sha }}
retention-days: 30
```

- [ ] **Step 3: Remove the Actions runtime warning**

Use `actions/checkout@v7`, `actions/setup-python@v7`, and
`actions/upload-artifact@v7`, which are the latest stable majors confirmed from
their official GitHub releases on 2026-08-12.

- [ ] **Step 4: Run focused and full tests**

Run:

```powershell
$env:PYTHONPATH='src'; python -m unittest tests.test_develop_ci_workflow -v
$env:PYTHONPATH='src'; python -m unittest discover -s tests -v
```

Expected: the policy test passes; the full suite reports 0 failures with only the existing optional skips.

- [ ] **Step 5: Commit the workflow**

```powershell
git add -- .github/workflows/build-windows.yml
git commit -m "ci: protect develop integration"
```

### Task 3: Publish And Verify The Pull Request

**Files:**
- No additional repository files.

**Interfaces:**
- Consumes: branch `chore/develop-ci-protection` based on `origin/develop`.
- Produces: a pull request targeting `develop` and a successful `Develop CI / Test` check with no PR artifact.

- [ ] **Step 1: Verify the complete branch**

Run the full test suite, `git diff --check`, inspect `git diff origin/develop...HEAD`, and confirm `git status --short` is clean.

- [ ] **Step 2: Push only the feature branch**

```powershell
git push -u origin chore/develop-ci-protection
```

- [ ] **Step 3: Open a pull request targeting develop**

The PR body records the trigger matrix, safety boundaries, local test count, and that `main`, `v0.1.0`, and Releases are untouched.

- [ ] **Step 4: Verify Actions**

Wait for `Develop CI / Test` to pass and confirm the PR run has no uploaded package artifact. Do not merge on a failed or missing check.

### Task 4: Enforce GitHub Rules And Integrate

**Files:**
- No repository file changes; GitHub repository rules only.

**Interfaces:**
- Consumes: successful required status context `Develop CI / Test` and approved PR.
- Produces: active protection for `main`, `develop`, and `v*`, then an integrated `develop` commit and development artifact.

- [ ] **Step 1: Create branch rulesets**

For `main` and `develop`, require PRs with zero approvals, successful `Develop CI / Test`, up-to-date branches, and resolved conversations. Block force pushes and deletion and do not provide a routine administrator bypass.

- [ ] **Step 2: Create release-tag protection**

Protect `refs/tags/v*` against update and deletion. Allow new formal version-tag creation only through an explicitly approved owner release action; do not operate on `v0.1.0` in this task.

- [ ] **Step 3: Read back and test the rules**

Read the effective rulesets from GitHub, confirm both branches and `v*` match, and verify the expected required status check name.

- [ ] **Step 4: Merge the approved PR into develop**

Use GitHub's merge control without changing `main`. If the repository supports a fast-forward/rebase merge through the protected PR flow, preserve the linear development history.

- [ ] **Step 5: Verify the develop push build**

Wait for both `Test` and `Windows portable package` jobs. Confirm one artifact exists with the expected ref, run number, and short SHA, and inspect its `BUILD-INFO.txt` identity.

- [ ] **Step 6: Prove immutable refs did not move**

Read remote refs and confirm:

```text
refs/heads/main = 161c20b9489bbe8f5a55d5777e45f803c79357a4
refs/tags/v0.1.0 = 161c20b9489bbe8f5a55d5777e45f803c79357a4
```

Confirm no new Tag or Release was created.
