"""The per-thread scheduler cron that sweeps a browser session while one is active."""

from langgraph_sdk import get_client

from agent.utils.thread_ops import langgraph_url

CRON_KIND = "browser_session"
CRON_SCHEDULE = "* * * * *"
# The server drops a ``thread_id`` key from cron metadata, so crons are tagged with this instead.
CRON_THREAD_KEY = "agent_thread_id"


def _client():
    return get_client(url=langgraph_url())


async def _cron_ids(thread_id: str) -> list[str]:
    crons = await _client().crons.search(
        metadata={"kind": CRON_KIND, CRON_THREAD_KEY: thread_id}, limit=10
    )
    return [
        cron_id
        for cron in crons or []
        if isinstance(cron, dict) and isinstance((cron_id := cron.get("cron_id")), str)
    ]


async def ensure_sweep_cron(thread_id: str) -> str:
    client = _client()
    ids = await _cron_ids(thread_id)
    if ids:
        for duplicate in ids[1:]:
            await client.crons.delete(duplicate)
        return ids[0]
    cron = await client.crons.create(
        "scheduler",
        schedule=CRON_SCHEDULE,
        input={"task": CRON_KIND, "thread_id": thread_id},
        config={"configurable": {"task": CRON_KIND, "thread_id": thread_id}},
        metadata={"kind": CRON_KIND, CRON_THREAD_KEY: thread_id},
        timezone="UTC",
    )
    cron_id = cron.get("cron_id") if isinstance(cron, dict) else getattr(cron, "cron_id", None)
    if not isinstance(cron_id, str) or not cron_id:
        raise RuntimeError("browser sweep cron creation did not return a cron_id")
    return cron_id


async def delete_sweep_cron(thread_id: str) -> None:
    client = _client()
    for cron_id in await _cron_ids(thread_id):
        await client.crons.delete(cron_id)
