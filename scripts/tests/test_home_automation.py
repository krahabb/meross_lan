"""Exercise release automation with real local Git repositories and a fake API."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import home_common
import home_publish
import home_sync


class FakeGitHub:
    token = "test-token"

    def __init__(self, case):
        self.case = case
        self.calls = []
        self.pulls = []
        self.issues = []
        self.release = None
        self.tag_sha = None
        self.assets = []

    def request(self, method, path, data=None, missing_ok=False):
        self.calls.append((method, path, data))
        if path.endswith("/releases/latest"):
            return {
                "tag_name": "v1.1.0",
                "draft": False,
                "prerelease": False,
                "html_url": "https://github.com/upstream/releases/tag/v1.1.0",
                "body": "Upstream improvements",
            }
        if path.startswith(f"repos/{home_common.UPSTREAM}/git/ref/tags/"):
            return {"object": {"type": "commit", "sha": self.case.upstream_sha}}
        if "/git/ref/heads/" in path:
            result = self.case.run_git(
                "--git-dir",
                str(self.case.origin),
                "rev-parse",
                f"refs/heads/{home_common.BRANCH}",
                check=False,
            )
            return (
                {"object": {"sha": result.stdout.strip()}}
                if result.returncode == 0
                else None
            )
        if method == "GET" and "/pulls?" in path:
            return self.pulls
        if method == "POST" and path.endswith("/pulls"):
            self.pulls.append({"number": 1, **data})
            return self.pulls[-1]
        if method == "PATCH" and "/pulls/" in path:
            self.pulls[0].update(data)
            return self.pulls[0]
        if path.endswith("/dispatches"):
            return None
        if method == "GET" and "/issues?" in path:
            return self.issues
        if method == "POST" and path.endswith("/issues"):
            self.issues.append({"number": 9, **data})
            return self.issues[-1]
        if method == "PATCH" and "/issues/" in path:
            self.issues[0].update(data)
            return self.issues[0]
        if "/releases/tags/" in path:
            return self.release
        if "/git/ref/tags/" in path:
            return (
                {"object": {"type": "commit", "sha": self.tag_sha}}
                if self.tag_sha
                else None
            )
        if method == "POST" and path.endswith("/git/refs"):
            self.tag_sha = data["sha"]
            return {"object": {"sha": self.tag_sha}}
        if method == "POST" and path.endswith("/releases"):
            self.release = {"id": 42, **data}
            return self.release
        if method == "GET" and path.endswith("/assets"):
            return self.assets
        if method == "DELETE" and "/releases/assets/" in path:
            self.assets.clear()
            return None
        if method == "PATCH" and path.endswith("/releases/42"):
            self.release.update(data)
            return self.release
        raise AssertionError(f"Unexpected API call: {method} {path}")


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="meross-home-tests-")
        self.addCleanup(self.directory.cleanup)
        self.previous_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.previous_cwd)
        root = Path(self.directory.name)
        self.work = root / "work"
        self.work.mkdir()
        self.origin = root / "origin.git"
        self.upstream = root / "upstream.git"
        os.chdir(self.work)
        self.env = patch.dict(
            os.environ,
            {
                "GH_TOKEN": "",
                "AUTOMATION_TOKEN": "",
                "SYNC_CUSTOM_TOKEN": "false",
                "TESTED_SHA": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.run_git("init", "-b", "master")
        self.run_git("config", "user.name", "Automation test")
        self.run_git("config", "user.email", "test@example.invalid")
        home_common.MANIFEST.parent.mkdir(parents=True)
        home_common.write_json(home_common.MANIFEST, {"version": "1.0.0"})
        home_common.write_json("hacs.json", {"name": "Meross LAN"})
        Path("control.txt").write_text("base\n")
        self.commit("Initial upstream")
        self.initial_sha = self.head()
        home_common.MARKER.parent.mkdir()
        home_common.write_json(
            home_common.MARKER,
            {
                "repository": home_common.UPSTREAM,
                "tag": "v1.0.0",
                "sha": self.initial_sha,
            },
        )
        home_common.write_json(
            "hacs.json", {"name": home_common.HACS_NAME, "homeassistant": "2026.8.0"}
        )
        Path("control.txt").write_text("home\n")
        Path("home.txt").write_text("local changes must survive\n")
        self.commit("Home changes")
        self.home_sha = self.head()
        self.run_git("clone", "--bare", str(self.work), str(self.origin))
        self.run_git("remote", "add", "origin", str(self.origin))
        self.api = FakeGitHub(self)

    def run_git(self, *args, check=True):
        return subprocess.run(
            ["git", *args], text=True, capture_output=True, check=check
        )

    def commit(self, message):
        self.run_git("add", ".")
        self.run_git("commit", "-m", message)

    def head(self):
        return self.run_git("rev-parse", "HEAD").stdout.strip()

    def new_release(self, conflict=False, workflows=False):
        self.run_git("checkout", "-b", "upstream-work", self.initial_sha)
        home_common.write_json(home_common.MANIFEST, {"version": "1.1.0"})
        Path("upstream.txt").write_text("new upstream behavior\n")
        if conflict:
            Path("control.txt").write_text("upstream\n")
        if workflows:
            Path(".github/workflows").mkdir(parents=True)
            Path(".github/workflows/new.yml").write_text("name: upstream\n")
        self.commit("New stable upstream release")
        self.upstream_sha = self.head()
        self.run_git("tag", "v1.1.0")
        self.run_git("clone", "--bare", str(self.work), str(self.upstream))
        self.run_git("checkout", "master")

    def sync(self):
        actual_git = home_common.git

        def local_git(*args, **kwargs):
            args = tuple(
                (
                    str(self.upstream)
                    if value == f"https://github.com/{home_common.UPSTREAM}.git"
                    else value
                )
                for value in args
            )
            return actual_git(*args, **kwargs)

        with patch.object(home_sync, "git", side_effect=local_git):
            home_sync.sync(self.api)

    def prepare_publish(self):
        self.new_release()
        self.sync()
        self.run_git("push", "origin", "HEAD:master")
        os.environ["TESTED_SHA"] = self.head()

    def test_merge_preserves_both_histories_and_rerun_keeps_one_pr(self):
        self.new_release()
        self.sync()
        candidate = self.head()
        for ancestor in (self.home_sha, self.upstream_sha):
            self.run_git("merge-base", "--is-ancestor", ancestor, candidate)
        self.assertEqual(Path("control.txt").read_text(), "home\n")
        self.assertTrue(Path("upstream.txt").exists())
        self.assertTrue(Path("home.txt").exists())
        self.assertEqual(
            home_common.read_json(home_common.MARKER)["sha"], self.upstream_sha
        )
        self.sync()
        self.assertEqual(self.head(), candidate)
        self.assertEqual(len(self.api.pulls), 1)
        dispatch = [call for call in self.api.calls if call[1].endswith("/dispatches")][
            -1
        ]
        self.assertEqual(dispatch[2], {"ref": "master", "inputs": {"ref": candidate}})

    def test_conflict_does_not_push_a_candidate(self):
        self.new_release(conflict=True)
        with self.assertRaisesRegex(ValueError, "Merge conflict"):
            self.sync()
        self.assertEqual(self.api.pulls, [])
        self.assertNotEqual(
            self.run_git(
                "--git-dir",
                str(self.origin),
                "rev-parse",
                f"refs/heads/{home_common.BRANCH}",
                check=False,
            ).returncode,
            0,
        )
        self.assertEqual(
            home_common.read_json(home_common.MARKER)["sha"], self.initial_sha
        )

    def test_existing_pr_keeps_human_commits_when_master_moves(self):
        self.new_release()
        self.sync()
        Path("manual-fix.txt").write_text("Human conflict resolution\n")
        self.commit("Keep this manual PR fix")
        manual_sha = self.head()
        self.run_git("push", "origin", f"HEAD:{home_common.BRANCH}")
        self.run_git("checkout", "master")
        Path("master-update.txt").write_text("New home change\n")
        self.commit("Master advanced")
        self.run_git("push", "origin", "master")
        self.sync()
        self.run_git("merge-base", "--is-ancestor", manual_sha, self.head())
        self.assertTrue(Path("manual-fix.txt").exists())
        self.assertTrue(Path("master-update.txt").exists())
        self.assertEqual(len(self.api.pulls), 1)

    def test_workflow_changes_require_optional_token(self):
        self.new_release(workflows=True)
        with self.assertRaisesRegex(ValueError, "UPSTREAM_SYNC_TOKEN"):
            self.sync()
        os.environ["SYNC_CUSTOM_TOKEN"] = "true"
        self.sync()
        self.assertTrue(Path(".github/workflows/new.yml").exists())

    def test_same_release_after_merge_is_noop(self):
        self.prepare_publish()
        self.api.calls.clear()
        self.sync()
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))

    def test_moved_imported_tag_fails_closed(self):
        self.prepare_publish()
        self.api.case.upstream_sha = "a" * 40
        with self.assertRaisesRegex(ValueError, "moved an already imported"):
            self.sync()

    def test_reports_reuse_one_issue(self):
        home_sync.report(self.api, "First conflict")
        home_sync.report(self.api, "Updated conflict")
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(self.api.issues[0]["body"], "Updated conflict")

    def test_release_tags_tested_fork_commit_and_publishes_zip(self):
        self.prepare_publish()

        def inspect_upload(api, release_id, path):
            self.assertEqual(release_id, 42)
            self.assertTrue(api.release["draft"])
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read(str(home_common.MANIFEST)))
                self.assertEqual(manifest["version"], "1.1.0")
            return {"id": 7}

        with patch.object(
            home_publish, "upload", side_effect=inspect_upload
        ) as uploaded:
            home_publish.publish(self.api)
        self.assertEqual(uploaded.call_count, 1)
        self.assertEqual(self.api.tag_sha, self.head())
        self.assertNotEqual(self.api.tag_sha, self.upstream_sha)
        self.assertEqual(self.api.release["tag_name"], "v1.1.0")
        self.assertIn(
            "- Requires Home Assistant 2026.8.0 or newer.", self.api.release["body"]
        )
        self.assertFalse(self.api.release["draft"])
        self.api.calls.clear()
        home_publish.publish(self.api)
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))

    def test_existing_tag_is_never_moved(self):
        self.prepare_publish()
        self.api.tag_sha = self.upstream_sha
        with self.assertRaisesRegex(ValueError, "refusing to move"):
            home_publish.publish(self.api)
        self.assertEqual(self.api.tag_sha, self.upstream_sha)
        self.assertIsNone(self.api.release)

    def test_published_raw_upstream_tag_is_reported_instead_of_silently_accepted(self):
        self.prepare_publish()
        self.api.tag_sha = self.upstream_sha
        self.api.release = {"id": 42, "tag_name": "v1.1.0", "draft": False}
        self.api.calls.clear()
        with self.assertRaisesRegex(ValueError, "not a valid home release"):
            home_publish.publish(self.api)
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))

    def test_upload_failure_leaves_an_unpublished_draft(self):
        self.prepare_publish()
        with patch.object(
            home_publish, "upload", side_effect=RuntimeError("Upload interrupted")
        ):
            with self.assertRaisesRegex(RuntimeError, "Upload interrupted"):
                home_publish.publish(self.api)
        self.assertTrue(self.api.release["draft"])
        self.assertEqual(self.api.tag_sha, self.head())

    def test_rerun_of_older_unpublished_version_cannot_replace_latest(self):
        self.prepare_publish()
        tested_sha = self.head()
        marker = home_common.read_json(home_common.MARKER)
        marker["tag"] = "v1.2.0"
        home_common.write_json(home_common.MARKER, marker)
        home_common.write_json(home_common.MANIFEST, {"version": "1.2.0"})
        self.commit("Master now tracks a newer upstream release")
        self.run_git("push", "origin", "HEAD:master")
        self.run_git("checkout", "--detach", tested_sha)
        self.api.calls.clear()
        with self.assertRaisesRegex(ValueError, "another upstream release"):
            home_publish.publish(self.api)
        self.assertIsNone(self.api.tag_sha)
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))

    def test_draft_retry_replaces_incomplete_upload_before_publishing(self):
        self.prepare_publish()
        self.api.tag_sha = self.head()
        self.api.release = {"id": 42, "tag_name": "v1.1.0", "draft": True}
        self.api.assets = [
            {"id": 99, "name": "meross_lan.zip", "state": "starter", "size": 0}
        ]
        with patch.object(home_publish, "upload", return_value={"id": 7}) as uploaded:
            home_publish.publish(self.api)
        self.assertEqual(uploaded.call_count, 1)
        self.assertIn(
            ("DELETE", f"repos/{home_common.FORK}/releases/assets/99", None),
            self.api.calls,
        )
        self.assertFalse(self.api.release["draft"])

    def test_manifest_and_hacs_contract_are_validated(self):
        self.new_release()
        self.sync()
        home_common.write_json("hacs.json", {"name": "Meross LAN"})
        with self.assertRaisesRegex(ValueError, "hacs.json name"):
            home_common.validate_home_tree()
        home_common.write_json("hacs.json", {"name": home_common.HACS_NAME})
        with self.assertRaisesRegex(ValueError, "minimum Home Assistant"):
            home_common.validate_home_tree()
        home_common.write_json(
            "hacs.json", {"name": home_common.HACS_NAME, "homeassistant": "2026.8.0"}
        )
        home_common.write_json(home_common.MANIFEST, {"version": "1.1.0+home.1"})
        with self.assertRaisesRegex(ValueError, "manifest version"):
            home_common.validate_home_tree()

    def test_untested_checkout_cannot_be_published(self):
        self.prepare_publish()
        os.environ["TESTED_SHA"] = "0" * 40
        self.api.calls.clear()
        with self.assertRaisesRegex(ValueError, "passed checks"):
            home_publish.publish(self.api)
        self.assertEqual(self.api.calls, [])


if __name__ == "__main__":
    unittest.main()
