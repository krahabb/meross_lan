"""Merge the latest stable upstream Release and maintain one reviewable PR."""

import json
import os
import sys

from home_common import (
    BASE,
    BRANCH,
    FORK,
    GitHub,
    MARKER,
    UPSTREAM,
    git,
    output,
    quote,
    validate_home_tree,
    validate_marker,
    write_json,
)

REPORT_TITLE = "Upstream synchronization needs attention"


def tag_commit(api, tag):
    obj = api.request("GET", f"repos/{UPSTREAM}/git/ref/tags/{quote(tag)}")["object"]
    for _ in range(10):
        if obj["type"] == "commit":
            return obj["sha"]
        if obj["type"] != "tag":
            break
        obj = api.request("GET", f"repos/{UPSTREAM}/git/tags/{obj['sha']}")["object"]
    raise ValueError("Upstream release tag does not resolve to a commit")


def report(api, body):
    issues = api.request("GET", f"repos/{FORK}/issues?state=open&per_page=100")
    issue = next(
        (
            item
            for item in issues
            if item["title"] == REPORT_TITLE and "pull_request" not in item
        ),
        None,
    )
    if issue:
        api.request("PATCH", f"repos/{FORK}/issues/{issue['number']}", {"body": body})
    else:
        api.request(
            "POST", f"repos/{FORK}/issues", {"title": REPORT_TITLE, "body": body}
        )


def close_report(api):
    issues = api.request("GET", f"repos/{FORK}/issues?state=open&per_page=100")
    for issue in issues:
        if issue["title"] == REPORT_TITLE and "pull_request" not in issue:
            run_url = f"https://github.com/{FORK}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
            api.request(
                "POST",
                f"repos/{FORK}/issues/{issue['number']}/comments",
                {
                    "body": f"The importer completed in [this run]({run_url}). "
                    "Any remaining conflicts are listed in the open update PR."
                },
            )
            api.request(
                "PATCH",
                f"repos/{FORK}/issues/{issue['number']}",
                {"state": "closed", "state_reason": "completed"},
            )


def is_ancestor(ancestor, descendant):
    return (
        git("merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode
        == 0
    )


def conflict_body(marker, release, paths):
    listed = "\n".join(f"- `{path}`" for path in paths)
    return (
        f"Import stable upstream release [{marker['tag']}]({release['html_url']}) "
        f"at `{marker['sha']}`.\n\n"
        "**Git could not merge this release into `master` automatically.** This branch "
        "points at the upstream release commit itself, so GitHub lists the conflicts and "
        "shows the upstream changes. It cannot be merged until they are resolved.\n\n"
        f"Conflicting paths:\n\n{listed}\n\n"
        "Resolve them while preserving AUTO HVAC mode, the HACS name and the home test pins:\n\n"
        "```bash\n"
        "git fetch origin\n"
        f"git switch -C {BRANCH} origin/{BASE}\n"
        f"git merge {marker['sha']}\n"
        "# resolve, test and commit the merge\n"
        f"git push --force-with-lease origin {BRANCH}\n"
        "```\n\n"
        "Simple text conflicts can also be resolved with GitHub's **Resolve conflicts** "
        "button. Then run **Actions → Import stable upstream releases → Run workflow**: "
        "it keeps your resolution, merges newer `master` commits, records the release in "
        f"`{MARKER}` and starts the checks. Review and use **Create a merge commit**; "
        "do not squash or rebase.\n\n"
        f"Upstream release notes:\n\n{release.get('body') or '(No upstream release notes.)'}"
    )


def dispatch_checks(api, sha):
    # The workflow definition comes from trusted master; candidate code gets read-only jobs.
    api.request(
        "POST",
        f"repos/{FORK}/actions/workflows/pull.yml/dispatches",
        {"ref": BASE, "inputs": {"ref": sha}},
    )


def sync(api):
    automation_api = (
        GitHub(os.environ.get("AUTOMATION_TOKEN"))
        if os.environ.get("AUTOMATION_TOKEN")
        else api
    )
    release = api.request("GET", f"repos/{UPSTREAM}/releases/latest")
    if release["draft"] or release["prerelease"]:
        raise ValueError("The selected upstream Release is not stable")
    marker = validate_marker(
        {
            "repository": UPSTREAM,
            "tag": release["tag_name"],
            "sha": tag_commit(api, release["tag_name"]),
        }
    )
    git(
        "fetch", "--no-tags", "origin", f"+refs/heads/{BASE}:refs/remotes/origin/{BASE}"
    )
    previous = validate_marker(
        json.loads(git("show", f"origin/{BASE}:{MARKER}").stdout)
    )
    if previous["tag"] == marker["tag"]:
        if previous["sha"] != marker["sha"]:
            raise ValueError(
                "Upstream moved an already imported release tag; refusing to replace it"
            )
        output(changed="false")
        print(f"Already tracking {marker['tag']} ({marker['sha']}).")
        return

    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    ref = "refs/remotes/home-upstream/release"
    git(
        "fetch",
        "--no-tags",
        f"https://github.com/{UPSTREAM}.git",
        f"+refs/tags/{marker['tag']}:{ref}",
    )
    if git("rev-parse", f"{ref}^{{commit}}").stdout.strip() != marker["sha"]:
        raise ValueError(
            "Fetched tag differs from the release SHA resolved through GitHub"
        )
    if git(
        "merge-base", "--is-ancestor", previous["sha"], marker["sha"], check=False
    ).returncode:
        raise ValueError(
            "New release is not a descendant of the imported release; manual review required"
        )
    workflows = git(
        "diff", "--name-only", previous["sha"], marker["sha"], "--", ".github/workflows"
    ).stdout
    if workflows and os.environ.get("SYNC_CUSTOM_TOKEN") != "true":
        raise ValueError(
            "Upstream changes Actions workflows. Configure UPSTREAM_SYNC_TOKEN with Contents, "
            "Pull requests and Workflows write permissions before retrying.\n\n"
            + workflows
        )

    old_branch = api.request(
        "GET", f"repos/{FORK}/git/ref/heads/{quote(BRANCH)}", missing_ok=True
    )
    owner = FORK.split("/")[0]
    pulls = api.request(
        "GET",
        f"repos/{FORK}/pulls?state=open&head={quote(owner + ':' + BRANCH)}&base={BASE}",
    )
    existing_sha = old_branch["object"]["sha"] if old_branch and pulls else None
    if existing_sha:
        git(
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}",
        )
        existing_marker = git("show", f"{existing_sha}:{MARKER}", check=False)
        if (
            existing_marker.returncode == 0
            and json.loads(existing_marker.stdout) == marker
            and is_ancestor(f"origin/{BASE}", existing_sha)
        ):
            dispatch_checks(automation_api, existing_sha)
            output(changed="false", sha=existing_sha, tag=marker["tag"])
            print(
                "Existing PR already contains this release and current master; keeping its commits."
            )
            return
    # Keep any human conflict resolutions already committed to an open update PR.
    # New master commits are merged into that history instead of rebuilding it.
    # A branch holding only upstream commits is an unresolved conflict PR: nothing
    # on it needs preserving, so the merge is retried from master.
    preserve = existing_sha and not is_ancestor(existing_sha, marker["sha"])
    git("checkout", "-B", BRANCH, existing_sha if preserve else f"origin/{BASE}")
    if preserve:
        merged_base = git(
            "merge", "--no-ff", "--no-edit", f"origin/{BASE}", check=False
        )
        if merged_base.returncode:
            paths = git("diff", "--name-only", "--diff-filter=U").stdout
            git("merge", "--abort", check=False)
            raise ValueError(
                f"Merge conflict updating the existing PR with master. No candidate was pushed.\n\n{paths}"
            )
    merged = git("merge", "--no-ff", "--no-edit", marker["sha"], check=False)
    conflicts = []
    if merged.returncode:
        conflicts = git("diff", "--name-only", "--diff-filter=U").stdout.split()
        git("merge", "--abort", check=False)
        if preserve or not conflicts:
            target = " into the existing PR commits" if preserve else ""
            raise ValueError(
                f"Merge conflict importing {marker['tag']}{target}. "
                "No candidate was pushed.\n\n" + ("\n".join(conflicts) or merged.stderr)
            )
        # Publish the release commit itself so the PR shows the conflicts on GitHub.
        git("checkout", "-B", BRANCH, marker["sha"])
    else:
        write_json(MARKER, marker)
        validate_home_tree(marker)
        git("add", str(MARKER))
        if git("diff", "--cached", "--quiet", check=False).returncode:
            git("commit", "-m", f"Track upstream release {marker['tag']}")
    sha = git("rev-parse", "HEAD").stdout.strip()
    expected = old_branch["object"]["sha"] if old_branch else ""
    git(
        "push",
        f"--force-with-lease=refs/heads/{BRANCH}:{expected}",
        "origin",
        f"HEAD:refs/heads/{BRANCH}",
    )

    body = (
        f"Import stable upstream release [{marker['tag']}]({release['html_url']}) "
        f"at `{marker['sha']}` using a Git merge.\n\n"
        "Home changes remain in the merge history. CI protects AUTO HVAC mode, the HACS name, "
        "and the integration. Review and use **Create a merge commit**; do not squash or rebase. "
        "After merging, the tested fork commit is published under the original upstream tag.\n\n"
        "Automatic merging is disabled.\n\n"
        f"Upstream release notes:\n\n{release.get('body') or '(No upstream release notes.)'}"
    )
    if conflicts:
        body = conflict_body(marker, release, conflicts)
    values = {"title": f"Update upstream to {marker['tag']}", "body": body}
    if pulls:
        pull = api.request("PATCH", f"repos/{FORK}/pulls/{pulls[0]['number']}", values)
    else:
        pull = api.request(
            "POST", f"repos/{FORK}/pulls", {**values, "head": BRANCH, "base": BASE}
        )
    if conflicts:
        output(changed="true", conflict="true", sha=sha, tag=marker["tag"])
        print(
            f"::warning::{marker['tag']} conflicts with {BASE} in "
            f"{', '.join(conflicts)}. Resolve them in PR #{pull['number']}."
        )
        return
    dispatch_checks(automation_api, sha)
    output(changed="true", sha=sha, tag=marker["tag"])
    print(f"Prepared {marker['tag']} at fork commit {sha}.")


def main():
    api = GitHub()
    automation_api = (
        GitHub(os.environ.get("AUTOMATION_TOKEN"))
        if os.environ.get("AUTOMATION_TOKEN")
        else api
    )
    try:
        sync(api)
    except Exception as error:
        message = str(error)
        run_url = f"https://github.com/{FORK}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
        try:
            report(
                automation_api,
                f"Synchronization stopped safely.\n\n```text\n{message}\n```\n\n[Workflow log]({run_url})",
            )
        except Exception as report_error:
            print(f"Could not create GitHub issue: {report_error}", file=sys.stderr)
        print(message, file=sys.stderr)
        return 1
    try:
        close_report(automation_api)
    except Exception as close_error:
        print(f"Could not close GitHub issue: {close_error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
