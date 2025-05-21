"OS checks"

from functools import partial
from typing import Optional

from asyncssh.connection import SSHClientConnection
from asyncstdlib import lru_cache


@lru_cache
async def check_is_linux(conn: SSHClientConnection) -> bool:
    "Check for generic Linux"
    proc = await conn.run("uname")
    return (
        proc.returncode == 0
        and proc.stdout is not None
        and proc.stdout.strip() == "Linux"
    )


@lru_cache
async def check_is_darwin(conn: SSHClientConnection) -> bool:
    "Check for Mac"
    proc = await conn.run("uname")
    return (
        proc.returncode == 0
        and proc.stdout is not None
        and proc.stdout.strip() == "Darwin"
    )


@lru_cache
async def _get_os_release(conn: SSHClientConnection) -> Optional[tuple[str, ...]]:
    "Get os release string on linuxes"
    proc = await conn.run(
        "sh -c 'source /etc/os-release; echo $ID@@@$ID_LIKE@@@$VERSION_ID'"
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return tuple(map(lambda x: x.strip(), str(proc.stdout).split("@@@", maxsplit=3)))


async def check_is_debian_like(conn: SSHClientConnection) -> bool:
    "Check for any Debian-like"
    os_release = await _get_os_release(conn)
    return "debian" in os_release[0:2] if os_release else False


async def check_is_debian(conn: SSHClientConnection) -> bool:
    "Check for any Debian"
    os_release = await _get_os_release(conn)
    return os_release[0] == "debian" if os_release else False


async def _check_debian_version(version: str, conn: SSHClientConnection) -> bool:
    "Check for Debian version"
    os_release = await _get_os_release(conn)
    return len(os_release) >= 2 and os_release[2] == version if os_release else False


check_is_debian_10 = partial(_check_debian_version, "10")
check_is_debian_11 = partial(_check_debian_version, "11")
check_is_debian_12 = partial(_check_debian_version, "12")
check_is_debian_13 = partial(_check_debian_version, "13")
check_is_debian_14 = partial(_check_debian_version, "14")
check_is_debian_15 = partial(_check_debian_version, "15")


@lru_cache
async def check_is_windows(conn: SSHClientConnection) -> bool:
    "Check for any Windows with Powershell"
    proc = await conn.run("[environment]::OSVersion")
    return proc.returncode == 0


@lru_cache
async def get_wmi_w32_os_caption(conn: SSHClientConnection) -> Optional[str]:
    "Get OS caption from WMI object"
    proc = await conn.run("(Get-WmiObject -class Win32_OperatingSystem).Caption")
    if proc.stdout:
        return str(proc.stdout)


async def _check_is_windows_caption_version(
    version: str, conn: SSHClientConnection
) -> bool:
    "Check for Windows version in caption"
    caption = await get_wmi_w32_os_caption(conn)
    return version in caption if caption else False


check_is_windows7 = partial(_check_is_windows_caption_version, "7")
check_is_windows8 = partial(_check_is_windows_caption_version, "8")
check_is_windows10 = partial(_check_is_windows_caption_version, "10")
check_is_windows11 = partial(_check_is_windows_caption_version, "11")
check_is_windows12 = partial(_check_is_windows_caption_version, "12")
