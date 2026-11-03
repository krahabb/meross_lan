"""Publish the checked fork commit without moving tags or replacing releases."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import zipfile

from home_common import FORK, GitHub, git, quote, read_json, validate_home_tree


def upload(api, release_id, path):
    request = urllib.request.Request(
        f"https://uploads.github.com/repos/{FORK}/releases/{release_id}/assets?name={quote(path.name)}",
        data=path.read_bytes(),
        headers={
            "Authorization": f"Bearer {api.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/zip",
            "User-Agent": "meross-lan-home-automation",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def archive(path, sha):
    files = git(
        "ls-tree", "-r", "--name-only", sha, "--", "custom_components/meross_lan"
    ).stdout.splitlines()
    if not files:
        raise ValueError("No integration files found in the tested commit")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for filename in files:
            data = subprocess.run(
                ["git", "show", f"{sha}:{filename}"], check=True, capture_output=True
            ).stdout
            bundle.writestr(filename, data)


def publish(api):
    marker = validate_home_tree()
    sha = git("rev-parse", "HEAD").stdout.strip()
    if sha != os.environ.get("TESTED_SHA", sha):
        raise ValueError("Checkout does not match the commit that passed checks")
    git("fetch", "--no-tags", "origin", "+refs/heads/master:refs/remotes/origin/master")
    if git("merge-base", "--is-ancestor", sha, "origin/master", check=False).returncode:
        raise ValueError("Only commits already merged into master can be published")
    if git("merge-base", "--is-ancestor", marker["sha"], sha, check=False).returncode:
        raise ValueError("The upstream release is missing from this commit's ancestry")
    tag = marker["tag"]
    release = api.request(
        "GET", f"repos/{FORK}/releases/tags/{quote(tag)}", missing_ok=True
    )
    ref = api.request("GET", f"repos/{FORK}/git/ref/tags/{quote(tag)}", missing_ok=True)
    if release and not release["draft"]:
        if not ref or ref["object"]["type"] != "commit":
            raise ValueError(
                "Published home release is missing its expected lightweight tag"
            )
        tagged_sha = ref["object"]["sha"]
        try:
            tagged_marker = validate_home_tree(ref=tagged_sha)
        except (ValueError, RuntimeError, KeyError) as error:
            raise ValueError(
                "Published tag is not a valid home release; it will not be replaced"
            ) from error
        if (
            tagged_marker != marker
            or git(
                "merge-base", "--is-ancestor", tagged_sha, sha, check=False
            ).returncode
        ):
            raise ValueError(
                "Published tag has unexpected upstream metadata or ancestry; it will not be replaced"
            )
        print(f"{tag} is already published and immutable; leaving it unchanged.")
        return
    if validate_home_tree(ref="origin/master") != marker:
        raise ValueError(
            "Master already tracks another upstream release; refusing to publish an older run as latest"
        )
    if ref:
        if ref["object"]["type"] != "commit" or ref["object"]["sha"] != sha:
            raise ValueError(
                f"Existing tag {tag} does not point to the tested fork commit; refusing to move it"
            )
    else:
        api.request(
            "POST", f"repos/{FORK}/git/refs", {"ref": f"refs/tags/{tag}", "sha": sha}
        )
    if release is None:
        body = (
            f"Based on [{marker['repository']} {tag}](https://github.com/{marker['repository']}/releases/tag/{tag}) "
            f"at `{marker['sha']}`.\n\n"
            f"This release contains the tested home fork commit `{sha}`.\n\n"
            "Home changes:\n"
            "- Device schedules are exposed through HVAC mode AUTO instead of an auto preset, across supported MTS models.\n"
            "- MTS100/MTS150 and MTS200-compatible thermostats report the standard none preset instead of custom; MTS300 has no presets.\n"
            f"- Requires Home Assistant {read_json('hacs.json')['homeassistant']} or newer.\n"
            "- HACS displays Meross LAN (home version).\n\n"
            "The upstream tag and manifest version are retained without a fork suffix. "
            "HACS installs source from this fork's tag; meross_lan.zip also contains the integration "
            "under custom_components/meross_lan/."
        )
        release = api.request(
            "POST",
            f"repos/{FORK}/releases",
            {
                "tag_name": tag,
                "target_commitish": sha,
                "name": f"{tag} (home version)",
                "body": body,
                "draft": True,
                "prerelease": False,
            },
        )
    assets = api.request("GET", f"repos/{FORK}/releases/{release['id']}/assets")
    # A failed upload can leave a zero-byte "starter" asset. Drafts can be repaired;
    # a published release has already returned above and is never touched.
    for asset in assets:
        if asset["name"] == "meross_lan.zip":
            api.request("DELETE", f"repos/{FORK}/releases/assets/{asset['id']}")
    with tempfile.TemporaryDirectory(prefix="meross-home-release-") as directory:
        path = Path(directory) / "meross_lan.zip"
        archive(path, sha)
        upload(api, release["id"], path)
    api.request(
        "PATCH",
        f"repos/{FORK}/releases/{release['id']}",
        {"draft": False, "make_latest": "true"},
    )
    print(f"Published {tag} from tested fork commit {sha}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Validate home metadata without API calls"
    )
    if parser.parse_args().check:
        validate_home_tree()
    else:
        publish(GitHub())
