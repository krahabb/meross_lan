"""Small, standard-library helpers for the home fork's GitHub automation."""

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

UPSTREAM = "krahabb/meross_lan"
FORK = "nilvanis/meross_lan"
BASE = "master"
BRANCH = "automation/upstream-release"
MARKER = Path(".github/upstream-release.json")
MANIFEST = Path("custom_components/meross_lan/manifest.json")
HACS_NAME = "Meross LAN (home version)"


class GitHub:
    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN", "")

    def request(self, method, path, data=None, missing_ok=False):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "meross-lan-home-automation",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"https://api.github.com/{path}",
            data=json.dumps(data).encode() if data is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise RuntimeError(f"GitHub {method} {path}: HTTP {error.code}") from None


def git(*args, check=True):
    """Authenticate without putting a token in arguments, config files, or URLs."""
    env = os.environ.copy()
    if token := env.get("GH_TOKEN"):
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {credential}",
        )
    result = subprocess.run(
        ["git", *args], env=env, text=True, capture_output=True, check=False
    )
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed:\n{result.stderr or result.stdout}")
    return result


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def validate_marker(marker):
    if marker.get("repository") != UPSTREAM:
        raise ValueError("Unexpected upstream repository in release marker")
    if not re.fullmatch(r"[0-9a-f]{40}", marker.get("sha", "")):
        raise ValueError("Upstream release marker must contain a full commit SHA")
    tag = marker.get("tag", "")
    if (
        not tag
        or not git("check-ref-format", f"refs/tags/{tag}", check=False).returncode == 0
    ):
        raise ValueError("Invalid upstream release tag")
    return marker


def validate_home_tree(marker=None, ref=None):
    def tree_json(path):
        return (
            json.loads(git("show", f"{ref}:{path}").stdout) if ref else read_json(path)
        )

    marker = validate_marker(marker or tree_json(MARKER))
    version = tree_json(MANIFEST)["version"]
    if marker["tag"].removeprefix("v") != version:
        raise ValueError("Upstream tag and integration manifest version differ")
    hacs = tree_json("hacs.json")
    if hacs.get("name") != HACS_NAME:
        raise ValueError(f"hacs.json name must be {HACS_NAME!r}")
    if not hacs.get("homeassistant"):
        raise ValueError("hacs.json must declare the minimum Home Assistant version")
    return marker


def output(**values):
    if filename := os.environ.get("GITHUB_OUTPUT"):
        with open(filename, "a") as file:
            for key, value in values.items():
                file.write(f"{key}={value}\n")


def quote(value):
    return urllib.parse.quote(value, safe="")
