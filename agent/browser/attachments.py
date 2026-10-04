"""Uploads a recording as a GitHub attachment, which renders as a video player in a PR.

GitHub shows a player only for a ``user-attachments`` URL on a line of its own.
The upload endpoint is the one ``gh pr edit --attach`` uses; it takes a person's
token, and whether it accepts a GitHub App user token is decided by GitHub, so a
refusal here is expected and the caller falls back to an animated image.
Attachments cannot be deleted, so the retention window of the evidence branch
does not apply to them.
"""

import re
from urllib.parse import quote

import httpx2

from agent.github.http import GITHUB_API_BASE, GITHUB_HEADERS_VERSION

UPLOAD_URL = "https://uploads.github.com/user-attachments/assets"
MAX_VIDEO_BYTES = 10 * 1024 * 1024
_ASSET_URL = re.compile(r"^https://github\.com/user-attachments/assets/[A-Za-z0-9-]+$")


class AttachmentError(RuntimeError):
    """GitHub would not take the recording as an attachment."""


async def upload_video(
    client: httpx2.AsyncClient, token: str, full_name: str, *, name: str, data: bytes
) -> str:
    """Upload ``data`` as an mp4 attachment of ``full_name`` and return its asset URL."""
    if not data or len(data) > MAX_VIDEO_BYTES:
        raise AttachmentError("The recording is empty or larger than 10 MB.")
    repository = await client.get(
        f"{GITHUB_API_BASE}/repos/{full_name}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_HEADERS_VERSION,
        },
    )
    repository_id = repository.json().get("id") if repository.status_code == 200 else None
    if not isinstance(repository_id, int):
        raise AttachmentError(
            f"The signed-in account cannot read {full_name} ({repository.status_code})."
        )
    response = await client.post(
        f"{UPLOAD_URL}?name={quote(name)}&content_type=video%2Fmp4&repository_id={repository_id}",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/octet-stream",
        },
        content=data,
    )
    if response.status_code not in (200, 201):
        raise AttachmentError(
            f"GitHub refused the attachment upload ({response.status_code}): {response.text[:200]}"
        )
    url = response.json().get("url")
    if not isinstance(url, str) or _ASSET_URL.match(url) is None:
        raise AttachmentError("GitHub returned no attachment URL.")
    return url
