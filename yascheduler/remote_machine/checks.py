#!/usr/bin/env python3

from typing import Optional, Tuple

from asyncssh.connection import SSHClientConnection
from asyncstdlib import lru_cache


@lru_cache
async def check_is_linux(conn: SSHClientConnection) -> bool:
    r = await conn.run("uname")
    return r.returncode == 0 and r.stdout is not None and r.stdout.strip() == "Linux"


@lru_cache
async def _get_os_release(conn: SSHClientConnection) -> Optional[Tuple[str, str, str]]:
    r = await conn.run("source /etc/os-release; echo $ID@@@$ID_LIKE@@@$VERSION_ID")
    if r.returncode != 0 or not r.stdout:
        return None
    return tuple(map(lambda x: x.strip(), str(r.stdout).split("@@@", maxsplit=3)))


async def check_is_debian_like(conn: SSHClientConnection) -> bool:
    os_release = await _get_os_release(conn)
    return "debian" in [os_release[0], os_release[1]] if os_release else False


async def check_is_debian(conn: SSHClientConnection) -> bool:
    os_release = await _get_os_release(conn)
    return os_release[0] == "debian" if os_release else False


async def check_is_debian_buster(conn: SSHClientConnection) -> bool:
    os_release = await _get_os_release(conn)
    return os_release[2] == "10" if os_release else False


async def check_is_debian_bullseye(conn: SSHClientConnection) -> bool:
    os_release = await _get_os_release(conn)
    return os_release[2] == "11" if os_release else False


@lru_cache
async def check_is_windows(conn: SSHClientConnection) -> bool:
    r = await conn.run("[environment]::OSVersion")
    return r.returncode == 0


@lru_cache
async def get_wmi_w32_os_caption(conn: SSHClientConnection) -> Optional[str]:
    r = await conn.run("(Get-WmiObject -class Win32_OperatingSystem).Caption")
    if r.stdout:
        return str(r.stdout)


async def check_is_windows7(conn: SSHClientConnection) -> bool:
    c = await get_wmi_w32_os_caption(conn)
    return "7" in c if c else False


async def check_is_windows8(conn: SSHClientConnection) -> bool:
    c = await get_wmi_w32_os_caption(conn)
    return "8" in c if c else False


async def check_is_windows10(conn: SSHClientConnection) -> bool:
    c = await get_wmi_w32_os_caption(conn)
    return "10" in c if c else False


async def check_is_windows11(conn: SSHClientConnection) -> bool:
    c = await get_wmi_w32_os_caption(conn)
    return "11" in c if c else False
