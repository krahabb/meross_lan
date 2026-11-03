# Home fork maintenance

The working repository and HACS source is
[nilvanis/meross_lan](https://github.com/nilvanis/meross_lan).

## Normal updates

`sync-upstream.yml` checks the latest **stable GitHub Release** from
`krahabb/meross_lan` every six hours, at minute 17 UTC. It can also be started from
Actions → Import stable upstream releases → Run workflow. Prereleases and commits
that have not been released are not selected.

The release's tag and full upstream commit SHA are recorded in
`.github/upstream-release.json`. For example, the initial upstream tag is
`v5.8.0`, while its integration manifest version is `5.8.0`. Both keep their
original upstream spelling; the fork adds no version suffix.

The importer starts from `master`, performs a normal Git merge of that upstream
release and updates one branch, `automation/upstream-release`, and one open PR.
Your AUTO implementation, HACS name, README and automation remain ordinary Git
changes and are merged along with future upstream changes. Unchanged candidates
keep their existing commits. Commits added manually to an open update PR are
preserved, and new changes on `master` are merged into that history. Only the
bot's update branch can be replaced, using
`--force-with-lease`; `master` and release tags are never reset.

CI tests the candidate, including `tests/test_climate_schedule.py`, automation
tests, metadata, HACS and hassfest. Formatting is checked, without rewriting files,
for Python files maintained by this fork. Existing formatting differences in
unrelated upstream files are outside that check.

The integration tests use stable Home Assistant **2026.10.4** with its matching
`pytest-homeassistant-custom-component` **0.13.375**, both pinned in the requirements
files. All Python jobs read **3.14.10** from `.python-version`; the development
container uses the verified `3.14-bookworm` image pinned by digest to that patch.
These pins keep a future install from silently selecting a beta. Update the
Home Assistant/test helper pair together and align the Python file and container
image when changing the interpreter version. See [test setup](../tests/README.md).

Review the PR and select **Create a merge commit**. Do not squash or rebase an
upstream update: preserving upstream ancestry lets Git recognize changes already
imported on subsequent updates. Automatic merging is intentionally disabled.

After merge, `release-home.yml` tests the exact commit on `master` again. Only
after all checks succeed does it create the original upstream tag in **this
fork**, pointing to the tested fork commit. It then creates a draft GitHub
Release, uploads `meross_lan.zip` and publishes the Release in the same job.
The ZIP contains `custom_components/meross_lan/`. HACS uses the normal source
archive of this fork's tag; `zip_release` is not needed.

Published versions are immutable. Re-running a release leaves an already
published version unchanged. An existing tag pointing elsewhere causes a failure,
never a tag move. Older unfinished runs cannot publish themselves as latest after
`master` has imported a newer release. Retry a failed draft/asset upload with
**Re-run all jobs** on the original failed workflow run, so the same commit is
tested and published.

The release notes list the fork's home changes from `scripts/home_publish.py` and
the minimum Home Assistant version from `hacs.json`. Update that list whenever
fork behavior changes.

## Home patch releases

Changes merged after a version has been published do not have to wait for the
next upstream release. Once `release-home.yml` has passed on the merged `master`
commit, create a GitHub Release by hand: a new tag on that commit that appends a
fourth number to the upstream tag (`v5.8.0.1`, `v5.8.0.2`, …), the tag as the
release name, and **Set as the latest release** selected. Describe the home
changes and any breaking changes for automations in the notes. HACS installs the
tag's source archive; the `meross_lan.zip` asset is optional.

The integration manifest keeps the upstream version, so Home Assistant shows
`5.8.0` for every `v5.8.0.N` release. The automation neither creates nor moves
these tags; the next imported upstream release is published under its own tag
as usual.

## Initial repository settings

1. Enable GitHub Actions in the fork's Actions tab.
2. In Settings → Actions → General, allow GitHub Actions to create pull requests.
   The default token permission can remain read-only; individual jobs declare
   the permissions they need.
3. Enable Issues and add repository topics, for example `home-assistant`, `hacs`,
   `meross`, `custom-component`, `thermostat`. HACS validates that metadata; Issues
   also provide visible synchronization failure reports.
4. Protect `master`: require a PR and the `home/checks` status, and keep the branch
   up to date before merge. Allow merge commits for upstream PRs. Configure the
   required context after its first successful run has appeared.
5. Merge the home behavior changes and automation. The first successful
   `release-home.yml` run publishes `v5.8.0` from the fork, provided that tag is not
   already occupied. Add this GitHub fork as a custom HACS integration repository.

## Authentication and CI

By default the importer uses the repository's automatic `GITHUB_TOKEN`; no secret
is required for ordinary updates. Its write credentials exist only in the
trusted import job. Candidate tests run in separate read-only jobs, with checkout
credential persistence disabled and no inherited secrets.

The importer explicitly dispatches `pull.yml` **from trusted `master`**, passing
the exact candidate SHA as an input. A separate job records `home/checks` on that
SHA. It does not execute candidate scripts with write credentials. This avoids
depending on workflows triggered by token-created pushes or PRs; GitHub currently
places PR workflows created by `GITHUB_TOKEN` in an approval-required state.
An additional waiting PR workflow can be ignored when the explicitly dispatched
run has supplied `home/checks`.

If upstream changes Actions workflow files, `GITHUB_TOKEN` may not have permission
to push them. The importer stops with a visible issue instead of dropping those
changes. For fully automatic imports of workflow changes, create the optional
repository secret `UPSTREAM_SYNC_TOKEN` using a dedicated fine-grained PAT scoped
only to this fork, with **Contents**, **Pull requests** and **Workflows** set to
read and write. The built-in token still handles issue reports and explicit CI
dispatches. A GitHub App installation token with the same permissions can also be
provided. Do not store a broad personal token just to run this automation.

## Conflicts and recovery

Conflicting merges, changed upstream tags, non-descendant releases and invalid
metadata fail closed. The importer does not choose `ours` or `theirs` globally,
does not update `master`, and does not publish an untested candidate. It creates
or updates the issue **Upstream synchronization needs attention**, including the
workflow log and conflicting paths. An existing PR remains available for review.

Resolve the merge on a working branch while preserving the home contract, review
and test it, then merge it normally. Update the release marker only to the exact
upstream release tag/SHA actually imported. Re-run the importer after configuring
a missing token or fixing the cause of the failure.

GitHub disables scheduled workflows in public repositories after **60 days with
no repository activity**. Scheduled runs alone do not guarantee continued
activity. There are no artificial keepalive commits here. Re-enable a disabled
workflow in Actions and use Run workflow; for unattended operation through long
quiet periods, an optional external timer can enable the workflow and call its
`workflow_dispatch` endpoint. Cron schedules may also be delayed by GitHub load.

References: [workflow triggers and token behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow),
[scheduled workflow limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[enabling workflows](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows).
