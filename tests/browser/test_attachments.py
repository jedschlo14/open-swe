"""A recording uploads as a GitHub attachment only with the account's own token, and a refusal is reported."""

import httpx2
import pytest

from agent.browser import attachments

ASSET = "https://github.com/user-attachments/assets/be9b3920-1111-2222-3333-444455556666"


def _client(upload_status: int, uploads: list[httpx2.Request]) -> httpx2.AsyncClient:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.github.com":
            return httpx2.Response(200, json={"id": 1234})
        uploads.append(request)
        if upload_status != 201:
            return httpx2.Response(upload_status, json={"message": "Not Found"})
        return httpx2.Response(201, json={"url": ASSET})

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


async def test_upload_sends_the_video_for_the_repository_and_returns_its_asset_url() -> None:
    uploads: list[httpx2.Request] = []
    async with _client(201, uploads) as client:
        url = await attachments.upload_video(
            client, "ghu_secret", "acme/app", name="recording.mp4", data=b"mp4-bytes"
        )

    assert url == ASSET
    (request,) = uploads
    assert request.headers["authorization"] == "token ghu_secret"
    assert request.url.params["repository_id"] == "1234"
    assert request.url.params["content_type"] == "video/mp4"
    assert request.content == b"mp4-bytes"


async def test_a_refused_upload_raises_so_the_caller_can_fall_back() -> None:
    async with _client(404, []) as client:
        with pytest.raises(attachments.AttachmentError, match="404"):
            await attachments.upload_video(
                client, "ghu_secret", "acme/app", name="recording.mp4", data=b"mp4-bytes"
            )
