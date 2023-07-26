"OS checks"

from typing import Optional, Tuple

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
async def _get_os_release(conn: SSHClientConnection) -> Optional[Tuple[str, str, str]]:
    "Get os release string on linuxes"
    proc = await conn.run("source /etc/os-release; echo $ID@@@$ID_LIKE@@@$VERSION_ID")
    if proc.returncode != 0 or not proc.stdout:
        return None
    return tuple(map(lambda x: x.strip(), str(proc.stdout).split("@@@", maxsplit=3)))


async def check_is_debian_like(conn: SSHClientConnection) -> bool:
    "Check for any Debian-like"
    os_release = await _get_os_release(conn)
    return "debian" in [os_release[0], os_release[1]] if os_release else False


async def check_is_debian(conn: SSHClientConnection) -> bool:
    "Check for any Debian"
    os_release = await _get_os_release(conn)
    return os_release[0] == "debian" if os_release else False


async def check_is_debian_buster(conn: SSHClientConnection) -> bool:
    "Check for Debian 10"
    os_release = await _get_os_release(conn)
    return os_release[2] == "10" if os_release else False


async def check_is_debian_bullseye(conn: SSHClientConnection) -> bool:
    "Check for Debian 11"
    os_release = await _get_os_release(conn)
    return os_release[2] == "11" if os_release else False


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


async def check_is_windows7(conn: SSHClientConnection) -> bool:
    "Check for Windows 7"
    caption = await get_wmi_w32_os_caption(conn)
    return "7" in caption if caption else False


async def check_is_windows8(conn: SSHClientConnection) -> bool:
    "Check for Windows 8"
    caption = await get_wmi_w32_os_caption(conn)
    return "8" in caption if caption else False


async def check_is_windows10(conn: SSHClientConnection) -> bool:
    "Check for Windows 10"
    caption = await get_wmi_w32_os_caption(conn)
    return "10" in caption if caption else False


async def check_is_windows11(conn: SSHClientConnection) -> bool:
    "Check for Windows 11"
    caption = await get_wmi_w32_os_caption(conn)
    return "11" in caption if caption else False
