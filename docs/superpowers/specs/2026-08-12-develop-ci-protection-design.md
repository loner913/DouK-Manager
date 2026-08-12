# Develop CI And Branch Protection Design

## Purpose

Keep routine fixes and features on `develop` while preserving `main` and the
immutable `v0.1.0` release baseline. Every change proposed for `develop` must
have repeatable test evidence. Installable Windows packages must identify the
exact source commit that built them.

## Considered Approaches

1. Build every pull request and upload an artifact. This gives the fastest
   manual-install path but spends runner time and creates many packages that
   were never merged.
2. Test pull requests, build only after a change reaches `develop`, and retain
   a manual build path for explicit Windows acceptance. This preserves a clear
   candidate artifact for each integrated development commit without creating
   unnecessary packages. This is the selected approach.
3. Build only on `main`. This keeps Actions minimal but leaves `develop`
   changes without CI or a reproducible Windows package.

## Workflow Behaviour

The existing Windows workflow remains the sole build definition and will have
these triggers:

| Event | Branch | Result |
| --- | --- | --- |
| `pull_request` | target `develop` or `main` | install dependencies and run the full unit-test suite; no artifact |
| `push` | `develop` | run the same test suite, build the portable package, and upload it |
| `workflow_dispatch` | selected ref | run tests, build the portable package, and upload it for explicit acceptance |
| `push` | `main` | no routine workflow run; formal release automation will be designed separately for v0.2.0 |

Pull requests to `develop` or `main` do not use path filters because a skipped workflow would leave a
required check pending and make the pull request impossible to merge. Push
builds retain filters for source, resources, tests, package metadata, the spec,
and the workflow itself, so unrelated documentation changes do not create a
new Windows package.

Testing and packaging use separate jobs. The packaging job depends on the test
job and runs only for `develop` pushes or manual dispatches. The ZIP remains
`DouK-Manager_Windows_X64.zip`; the GitHub artifact is uniquely named
`DouK-Manager_Windows_X64-<branch>-run-<number>-<short-sha>` and is retained for
30 days. This avoids ambiguous duplicate downloads while keeping enough time
for Windows acceptance. `BUILD-INFO.txt` continues to include the application
version, commit short SHA, and UTC-offset build time; it is the binding between
an artifact and the audited source revision.

The workflow keeps `permissions: contents: read`. It does not write tags,
releases, branches, registry entries, or runtime configuration and does not
access the downloader's formal Volume.

## GitHub Protection Rules

GitHub-side protection is configuration, not repository source. Configure the
following after the CI PR has merged so the required check exists:

| Ref scope | Required controls |
| --- | --- |
| `main` | require a pull request, require the Develop CI test check, require the branch to be up to date and all conversations resolved, block direct pushes, force pushes, and deletion |
| `develop` | require a pull request, require the Develop CI test check, require the branch to be up to date and all conversations resolved, block direct pushes, force pushes, and deletion |
| `v*` tags | block creation, update, and deletion except by the repository owner during an explicitly approved formal release; `v0.1.0` remains untouched |

The required approval count is zero because the repository currently has one
maintainer and GitHub does not allow an author to approve their own pull
request. The PR, successful required check, up-to-date branch, and resolved
conversation gates still apply. Repository administrators are not granted a
routine bypass. No user outside the repository's authorized collaborators
gains write permission from this configuration.

## Out Of Scope

- No source feature changes.
- No release, tag, or merge into `main`.
- No edit to the existing `v0.1.0` tag or Release.
- No modification of runtime data, downloader binaries, or the formal
  `_internal\\Volume`.
- No network update, installer, registry, or local Python-environment change.

## Validation

1. Run `python -m unittest discover -s tests -v` locally on the CI branch.
2. Validate the workflow YAML and inspect its diff against `origin/develop`.
3. Push only the new branch and open a PR targeting `develop`.
4. Confirm the PR test run passes without uploading an artifact.
5. Merge only after user approval, then confirm the `develop` push run uploads
   one Windows package whose build information names the merged commit.
6. Apply GitHub rulesets, then read them back and verify `main` and `v0.1.0`
   still resolve to `161c20b9489bbe8f5a55d5777e45f803c79357a4`.
