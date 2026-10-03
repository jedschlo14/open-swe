"""Publishing evidence rewrites one parentless branch and drops files past retention."""

import json
from datetime import UTC, datetime

import httpx2

from agent.browser import evidence

REPO = "acme/app"
NOW = datetime(2026, 10, 3, tzinfo=UTC)


class FakeGitHub:
    def __init__(self, tracked: list[str] | None) -> None:
        self.tracked = tracked
        self.tree_body: dict[str, object] = {}
        self.commit_body: dict[str, object] = {}
        self.ref_writes: list[tuple[str, str]] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path.endswith("/git/blobs"):
            return httpx2.Response(201, json={"sha": "blobsha"})
        if path.endswith(f"/git/ref/heads/{evidence.EVIDENCE_BRANCH}"):
            if self.tracked is None:
                return httpx2.Response(404, json={})
            return httpx2.Response(200, json={"object": {"sha": "tip"}})
        if path.endswith("/git/commits/tip"):
            return httpx2.Response(200, json={"tree": {"sha": "oldtree"}})
        if path.endswith("/git/trees/oldtree"):
            tree = [{"type": "blob", "path": item} for item in self.tracked or []]
            return httpx2.Response(200, json={"tree": tree, "truncated": False})
        if path.endswith("/git/trees") and method == "POST":
            self.tree_body = body
            return httpx2.Response(201, json={"sha": "newtree"})
        if path.endswith("/git/commits") and method == "POST":
            self.commit_body = body
            return httpx2.Response(201, json={"sha": "newcommit"})
        if "/git/refs" in path:
            self.ref_writes.append((method, body["sha"]))
            return httpx2.Response(201 if method == "POST" else 200, json={})
        return httpx2.Response(500, json={"path": path})


async def _publish(fake: FakeGitHub) -> evidence.PublishedEvidence:
    transport = httpx2.MockTransport(fake.handle)
    async with httpx2.AsyncClient(transport=transport) as client:
        return await evidence.publish_image(
            client,
            REPO,
            thread_id="abcd-1234-ef",
            label="before",
            caption="Settings page [broken]",
            data=b"\x89PNG-bytes",
            extension="png",
            now=NOW,
        )


async def test_publishing_prunes_expired_files_and_rewrites_a_parentless_branch() -> None:
    fake = FakeGitHub(["evidence/2026-09-01/old.png", "evidence/2026-09-30/keep.png", "README"])

    published = await _publish(fake)

    entries = fake.tree_body["tree"]
    assert isinstance(entries, list)
    deleted = {entry["path"] for entry in entries if entry["sha"] is None}
    assert deleted == {"evidence/2026-09-01/old.png", "README"}
    assert fake.tree_body["base_tree"] == "oldtree"
    assert fake.commit_body["parents"] == []
    assert fake.ref_writes == [("PATCH", "newcommit")]
    assert published.url == (
        f"https://github.com/{REPO}/blob/{evidence.EVIDENCE_BRANCH}/{published.path}?raw=true"
    )
    assert published.path.startswith("evidence/2026-10-03/abcd1234-before-")
    assert published.markdown == f"![Settings page  broken]({published.url})"


async def test_the_first_publish_creates_the_evidence_branch() -> None:
    fake = FakeGitHub(None)

    await _publish(fake)

    assert "base_tree" not in fake.tree_body
    assert fake.ref_writes == [("POST", "newcommit")]
