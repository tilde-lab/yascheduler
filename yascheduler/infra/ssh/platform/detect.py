"""Platform detection — run adapter checks on a connected host, return first match and all matched platforms."""
# region MODULE_CONTRACT
# PURPOSE: Run adapter checks on a connected host, return first matching adapter and all matched platform tags.
# SCOPE: Platform detection by running each adapter's check sequence against the remote host.
# INVARIANTS: MAX_SESSIONS=10 is the default OpenSSH server MaxSessions — the semaphore cap matches the protocol's natural limit.
# DEPENDENCIES: USES API: asyncstdlib, asyncio.locks
# KEYWORDS: platform detection, adapter, detect, remote, checks
# endregion MODULE_CONTRACT

from __future__ import annotations

from asyncio.locks import Semaphore
from typing import TYPE_CHECKING

from asyncstdlib import all as aall
from asyncstdlib import map as amap

from .exceptions import PlatformGuessFailedError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncssh.connection import SSHClientConnection

    from .adapters import RemoteMachineAdapter
    from .types import SSHCheck

__all__ = ["MAX_SESSIONS", "_detect_platform"]

MAX_SESSIONS = 10  # default MaxSessions on OpenSSH server


# region FUNC__detect_platform
# PURPOSE: Run adapter checks on connected host, return first match and all matched platforms.
# ENSURES: Raises PlatformGuessFailedError if no adapter matches.
# INVARIANTS: Uses asyncstdlib.all + asyncstdlib.map to run each adapter's checks tuple concurrently under a Semaphore(MAX_SESSIONS) — MAX_SESSIONS=10 is the default OpenSSH server MaxSessions; the first adapter whose checks all pass is the returned adapter; the platforms list collects every matching adapter's platform tag (a Debian-12 host matches linux, debian-like, debian, debian-12 — all four tags are returned so the orchestrator's engine filter sees them).
# RATIONALE:
# - Q: why run every adapter's checks even after the first match?
#   A: the platforms list (not just the chosen adapter.platform) is what the orchestrator's engine-filter reads — early exit would lose the multi-platform tags that let a Debian-12 host match a linux-only engine AND a debian-12-only engine.
# - Q: why a Semaphore(MAX_SESSIONS)?
#   A: the adapter checks open SSH channels concurrently; OpenSSH's default MaxSessions=10 caps concurrent channels per connection — exceeding it surfaces as ChannelOpenError (a retryable SSHRetryExc); the semaphore caps the concurrency at the source.
async def _detect_platform(
    conn: SSHClientConnection,
    adapters: Sequence[RemoteMachineAdapter],
) -> tuple[RemoteMachineAdapter, Sequence[str]]:
    sess_lim = Semaphore(MAX_SESSIONS)

    async def with_limit(conn: SSHClientConnection, fn: SSHCheck) -> bool:
        async with sess_lim:
            return await fn(conn)

    adapter = None
    platforms: list[str] = []
    checks: Sequence[bool] = [
        await aall(amap(lambda y: with_limit(conn, y), x.checks)) for x in adapters
    ]
    for candidate, check in zip(adapters, checks, strict=True):
        if check:
            platforms.append(candidate.platform)
        if check and not adapter:
            adapter = candidate
    if not adapter:
        raise PlatformGuessFailedError
    return adapter, platforms


# endregion FUNC__detect_platform
