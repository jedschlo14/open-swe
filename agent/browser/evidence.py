"""Publishes browser screenshots as pull request evidence on a repository evidence branch.

GitHub has no API for uploading images to a pull request, and a link that works
without signing in to GitHub outlives the reader's repository access. So
evidence lives on one orphan branch of the repository itself, and a pull request
links to it with ``blob/<branch>/<path>?raw=true``: GitHub decides who can see
the image, so readers need exactly the repository access they need to read the
pull request. The branch is a single parentless commit rewritten on every
publish, which is what makes retention real: files older than the retention
window are dropped from the tree and no history keeps them reachable.
"""

import base64
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote

import httpx2
from pydantic import BaseModel

from agent.github.http import GITHUB_API_BASE, github_request

logger = logging.getLogger(__name__)

EVIDENCE_BRANCH = "open-swe-evidence"
RETENTION = timedelta(days=30)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_ROOT = "evidence"
_DAY_PATH = re.compile(rf"^{_ROOT}/(\d{{4}}-\d{{2}}-\d{{2}})/")
_MAX_ATTEMPTS = 4

Label = Literal["before", "after", "other"]


class EvidenceError(RuntimeError):
    """The screenshot could not be published."""


class PublishedEvidence(BaseModel):
    path: str
    url: str
    markdown: str


def evidence_path(thread_id: str, label: str, data: bytes, now: datetime, extension: str) -> str:
    safe_thread = re.sub(r"[^A-Za-z0-9]", "", thread_id)[:8] or "thread"
    safe_label = re.sub(r"[^a-z0-9]", "", label.lower())[:16] or "other"
    digest = hashlib.sha256(data).hexdigest()[:8]
    return f"{_ROOT}/{now:%Y-%m-%d}/{safe_thread}-{safe_label}-{digest}.{extension}"


def stale_paths(paths: list[str], now: datetime) -> list[str]:
    """Files whose date folder is past retention, and anything else not laid out as evidence."""
    cutoff = (now - RETENTION).date()
    stale: list[str] = []
    for path in paths:
        match = _DAY_PATH.match(path)
        if match is None:
            stale.append(path)
            continue
        if datetime.strptime(match.group(1), "%Y-%m-%d").date() < cutoff:
            stale.append(path)
    return stale


def blob_url(full_name: str, path: str) -> str:
    return f"https://github.com/{full_name}/blob/{EVIDENCE_BRANCH}/{quote(path)}?raw=true"


async def _call(
    client: httpx2.AsyncClient, method: str, path: str, *, json: dict[str, object] | None = None
) -> httpx2.Response:
    url = f"{GITHUB_API_BASE}{path}"
    if json is None:
        return await github_request(client, method, url)
    return await github_request(client, method, url, json=json)


async def _fail(response: httpx2.Response, step: str) -> EvidenceError:
    detail = response.text[:300]
    return EvidenceError(f"GitHub refused to {step} ({response.status_code}): {detail}")


async def _branch_tip(client: httpx2.AsyncClient, full_name: str) -> str | None:
    response = await _call(client, "GET", f"/repos/{full_name}/git/ref/heads/{EVIDENCE_BRANCH}")
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise await _fail(response, "read the evidence branch")
    sha = response.json().get("object", {}).get("sha")
    if not isinstance(sha, str):
        raise EvidenceError("GitHub returned no commit for the evidence branch")
    return sha


async def _existing_tree(
    client: httpx2.AsyncClient, full_name: str, tip: str
) -> tuple[str, list[str]]:
    commit = await _call(client, "GET", f"/repos/{full_name}/git/commits/{tip}")
    if commit.status_code != 200:
        raise await _fail(commit, "read the evidence commit")
    tree_sha = commit.json()["tree"]["sha"]
    tree = await _call(client, "GET", f"/repos/{full_name}/git/trees/{tree_sha}?recursive=1")
    if tree.status_code != 200:
        raise await _fail(tree, "read the evidence tree")
    body = tree.json()
    if body.get("truncated"):
        raise EvidenceError("The evidence branch is too large to prune; clear it and retry.")
    return tree_sha, [item["path"] for item in body["tree"] if item.get("type") == "blob"]


async def publish_image(
    client: httpx2.AsyncClient,
    full_name: str,
    *,
    thread_id: str,
    label: Label,
    caption: str,
    data: bytes,
    extension: str,
    now: datetime | None = None,
) -> PublishedEvidence:
    """Add ``data`` to the repository's evidence branch and return how to embed it."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise EvidenceError("The screenshot is empty or larger than 5 MB.")
    moment = now or datetime.now(UTC)
    path = evidence_path(thread_id, label, data, moment, extension)
    blob = await _call(
        client,
        "POST",
        f"/repos/{full_name}/git/blobs",
        json={"content": base64.b64encode(data).decode(), "encoding": "base64"},
    )
    if blob.status_code != 201:
        raise await _fail(blob, "store the screenshot")
    entries: list[dict[str, str | None]] = [
        {"path": path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]}
    ]
    for attempt in range(_MAX_ATTEMPTS):
        tip = await _branch_tip(client, full_name)
        tree_body: dict[str, object] = {"tree": list(entries)}
        if tip is not None:
            base_tree, paths = await _existing_tree(client, full_name, tip)
            tree_body["base_tree"] = base_tree
            tree_body["tree"] = [
                *entries,
                *(
                    {"path": stale, "mode": "100644", "type": "blob", "sha": None}
                    for stale in stale_paths(paths, moment)
                ),
            ]
        tree = await _call(client, "POST", f"/repos/{full_name}/git/trees", json=tree_body)
        if tree.status_code != 201:
            raise await _fail(tree, "build the evidence tree")
        commit = await _call(
            client,
            "POST",
            f"/repos/{full_name}/git/commits",
            json={
                "message": "chore: update Open SWE browser evidence",
                "tree": tree.json()["sha"],
                "parents": [],
            },
        )
        if commit.status_code != 201:
            raise await _fail(commit, "commit the evidence")
        sha = commit.json()["sha"]
        if await _branch_tip(client, full_name) != tip:
            logger.info(
                "Evidence branch moved while publishing; retrying",
                extra={"repo_full_name": full_name, "attempt": attempt},
            )
            continue
        if tip is None:
            moved = await _call(
                client,
                "POST",
                f"/repos/{full_name}/git/refs",
                json={"ref": f"refs/heads/{EVIDENCE_BRANCH}", "sha": sha},
            )
            ok = moved.status_code == 201
        else:
            moved = await _call(
                client,
                "PATCH",
                f"/repos/{full_name}/git/refs/heads/{EVIDENCE_BRANCH}",
                json={"sha": sha, "force": True},
            )
            ok = moved.status_code == 200
        if not ok:
            if moved.status_code == 422 and attempt + 1 < _MAX_ATTEMPTS:
                continue
            raise await _fail(moved, "update the evidence branch")
        url = blob_url(full_name, path)
        alt = re.sub(r"[\[\]\n]", " ", caption).strip()[:120] or label
        return PublishedEvidence(path=path, url=url, markdown=f"![{alt}]({url})")
    raise EvidenceError("The evidence branch kept changing; try again.")
