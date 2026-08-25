"""
Running independent upstream reads at the same time.

Most pages here need several unrelated things from Zadara — a network page wants
VPCs, subnets, groups, gateways and addresses — and waiting for them one after
another is pure latency. Measured on this deployment: the seven calls behind
`/user/networks` take 4453 ms in sequence and 1167 ms together.

Threads rather than async: every call is I/O-bound `requests` work, the views are
ordinary sync Django, and rewriting the client for asyncio would buy nothing that
a thread pool does not.

Two things this must get right, and both are easy to get wrong:

1. **A failure stays local.** These pages already degrade per source — rights
   differ per resource kind, so a refused group list must not take the VPCs with
   it. `gather` therefore never raises; it hands back the exception alongside the
   results that did arrive.

2. **Worker threads must not leak database connections.** Django connections are
   thread-local and are only cleaned up automatically on the request thread. A
   task that touches the ORM would leave a connection open in the pool thread,
   and those accumulate until the database refuses new ones. Every task is
   wrapped so its own thread's connections are closed when it finishes.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from django.db import connections

logger = logging.getLogger('zadara')

# Enough for the widest page (networking asks for seven things) without letting a
# single request open an unbounded number of sockets to the cloud.
MAX_WORKERS = 8


@dataclass
class Fetched:
    """One source's outcome. `value` is None when `error` is set."""

    value: Any = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _run(name: str, fetch: Callable[[], Any], *, in_worker: bool = True) -> Fetched:
    try:
        return Fetched(value=fetch())
    except BaseException as error:  # noqa: BLE001 — the point is to not lose the others
        logger.info('parallel source %s failed: %s', name, error)

        return Fetched(error=error)
    finally:
        # Only in a pool thread. `connections.close_all()` is thread-local, and
        # the single-source path runs inline on the REQUEST thread — closing
        # there would throw away the connection the request is using, and inside
        # a transaction it poisons the atomic block outright.
        if in_worker:
            connections.close_all()


def gather(sources: dict[str, Callable[[], Any]]) -> dict[str, Fetched]:
    """Fetch every source concurrently. Returns one `Fetched` per name.

    Never raises: inspect `.ok` / `.error` per source. With a single source it
    runs inline, so the common case pays nothing for a pool it does not need.
    """
    if not sources:
        return {}

    if len(sources) == 1:
        name, fetch = next(iter(sources.items()))

        return {name: _run(name, fetch, in_worker=False)}

    with ThreadPoolExecutor(max_workers=min(len(sources), MAX_WORKERS)) as pool:
        futures = {name: pool.submit(_run, name, fetch) for name, fetch in sources.items()}

        return {name: future.result() for name, future in futures.items()}
