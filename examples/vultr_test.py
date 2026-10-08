#!/usr/bin/env python3
"""Vultr bare-metal test script for yascheduler integration.

Creates a Vultr bare-metal instance, waits for it to become active,
optionally checks SSH connectivity, then deletes it.

Usage:
    export VULTR_API_KEY='your_api_key_here'
    python examples/vultr_test.py test
    python examples/vultr_test.py create --server-type vbm-24c-256gb-amd
    python examples/vultr_test.py list
    python examples/vultr_test.py delete --id <instance_id>
    python examples/vultr_test.py list-ssh-keys
    python examples/vultr_test.py delete-ssh-key --id <ssh_key_id>
    python examples/vultr_test.py delete-ssh-key --non-unique [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.vultr.com/v2"

DEFAULT_LOCATION = "ams"
DEFAULT_SERVER_TYPE = "vbm-24c-256gb-amd"
DEFAULT_IMAGE_NAME = 2136
DEFAULT_SSH_KEY_PATH = os.path.expanduser("~/.ssh/id_rsa.pub")
POLL_INTERVAL = 20
POLL_TIMEOUT = 1200
LABEL_MAX = 40  # truncation width for ssh-key bodies in listings


def get_api_key() -> str:
    api_key = os.environ.get("VULTR_API_KEY")
    if not api_key:
        print("ERROR: environment variable VULTR_API_KEY is not set", file=sys.stderr)
        print("Run: export VULTR_API_KEY='your_key_here'", file=sys.stderr)
        sys.exit(1)
    return api_key


def vultr_request(method: str, path: str, body: dict | None = None) -> dict:
    api_key = get_api_key()
    url = API_BASE + path

    data = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        print(f"HTTP ERROR {e.code}: {raw}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"URL ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def print_table(rows: list[list[str]]) -> None:
    if not rows:
        return
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(str(value).ljust(widths[i]) for i, value in enumerate(row)))


def read_ssh_pubkey(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def get_or_create_ssh_key(pubkey: str, name: str) -> str:
    """Reuse an existing Vultr SSH key with the same public-key body.

    Match by whitespace-trimmed `ssh_key` body: Vultr's list response has no
    fingerprint field, and it may strip the trailing newline from the stored
    body.
    """
    data = vultr_request("GET", "/ssh-keys?per_page=500")
    for key in data.get("ssh_keys", []):
        if key.get("ssh_key", "").strip() == pubkey.strip():
            print(f"Reusing existing SSH key: id={key['id']}, name={key.get('name')}")
            return key["id"]

    body = {"name": name, "ssh_key": pubkey}
    data = vultr_request("POST", "/ssh-keys", body)
    key_id = data.get("ssh_key", {}).get("id")
    if not key_id:
        print(f"ERROR: could not create SSH key: {data}", file=sys.stderr)
        sys.exit(1)
    print(f"Created SSH key: id={key_id}, name={name}")
    return key_id


def build_cloud_init() -> str:
    """Build cloud-config user-data with bare-metal setup from README.

    Covers: RAID0 NVMe, /data mount, /dev/shm 200G, ulimit, apt packages,
    ScaLAPACK symlink. Matches the yascheduler Vultr integration exactly.
    """
    config = {
        "package_upgrade": True,
        "packages": [
            "mdadm",
            "openmpi-bin",
            "openmpi-common",
            "libopenmpi-dev",
            "libscalapack-openmpi-dev",
            "libxml2-dev",
            "libblas-dev",
            "liblapack-dev",
            "build-essential",
            "gfortran",
            "cmake",
            "git",
        ],
        "runcmd": [
            # Section 2: RAID0 NVMe (vbm-24c-256gb-amd)
            "mdadm --create /dev/md0 --level=0 --raid-devices=2 /dev/nvme0n1 /dev/nvme1n1 --force",
            "mkfs.ext4 -b 4096 -E stride=128,stripe-width=256 /dev/md0",
            'UUID=$(blkid -s UUID -o value /dev/md0) && mkdir -p /data && echo "UUID=$UUID /data ext4 defaults 0 2" >> /etc/fstab && mount /data',
            "mdadm --detail --scan >> /etc/mdadm/mdadm.conf",
            "update-initramfs -u",
            # Section 3: /dev/shm 200G
            "echo 'tmpfs /dev/shm tmpfs defaults,size=200G 0 0' >> /etc/fstab",
            "mount -o remount /dev/shm",
            # Section 4: ulimit
            "printf '* soft nofile 65536\\n* hard nofile 65536\\nroot soft nofile 65536\\nroot hard nofile 65536\\n' >> /etc/security/limits.conf",
            # Section 5: ScaLAPACK symlink
            "ln -sf /usr/lib/x86_64-linux-gnu/libscalapack-openmpi.so.2.1 /usr/lib/x86_64-linux-gnu/libscalapack-openmpi.so.2.2",
        ],
    }
    return "#cloud-config\n" + json.dumps(config)


def create_baremetal(
    location: str,
    server_type: str,
    image_name: int,
    label: str,
    hostname: str,
    sshkey_id: str | None = None,
    user_data: str | None = None,
) -> dict:
    body: dict = {
        "region": location,
        "plan": server_type,
        "os_id": image_name,
        "label": label,
        "hostname": hostname,
        "enable_ipv6": True,
    }
    if sshkey_id:
        body["sshkey_id"] = [sshkey_id]
    if user_data:
        body["user_data"] = base64.b64encode(user_data.encode()).decode()

    return vultr_request("POST", "/bare-metals", body)


def list_baremetals() -> None:
    data = vultr_request("GET", "/bare-metals?per_page=500")
    rows = [["ID", "LABEL", "STATUS", "IP", "REGION"]]
    rows.extend(
        [
            bm.get("id", ""),
            bm.get("label", ""),
            bm.get("status", ""),
            bm.get("main_ip", ""),
            bm.get("region", ""),
        ]
        for bm in data.get("bare_metals", [])
    )
    print_table(rows)


def delete_baremetal(instance_id: str) -> None:
    vultr_request("DELETE", f"/bare-metals/{instance_id}")
    print(f"Deleted bare-metal instance: {instance_id}")


def fetch_ssh_keys() -> list[dict]:
    """Fetch all account SSH keys, following cursor pagination."""
    keys: list[dict] = []
    cursor = ""
    while True:
        path = "/ssh-keys?per_page=500"
        if cursor:
            path += "&cursor=" + urllib.parse.quote(cursor)
        data = vultr_request("GET", path)
        keys.extend(data.get("ssh_keys", []))
        cursor = (data.get("meta") or {}).get("links", {}).get("next", "")
        if not cursor:
            return keys


def list_ssh_keys() -> None:
    """List account SSH keys; groups with identical key bodies are flagged.

    The duplicate report is the diagnostic tool for the Vultr duplicate-key
    bug: keys whose ``ssh_key`` bodies match are printed together so extra
    copies can be deleted by id.
    """
    keys = fetch_ssh_keys()
    print(f"SSH keys: {len(keys)}")
    if not keys:
        return

    rows = [["ID", "NAME", "DATE", "KEY"]]
    rows.extend(
        [
            key.get("id", ""),
            key.get("name", ""),
            key.get("date_created", ""),
            key.get("ssh_key", "")[:LABEL_MAX] + "...",
        ]
        for key in keys
    )
    print_table(rows)

    by_body: dict[str, list[dict]] = {}
    for key in keys:
        by_body.setdefault(key.get("ssh_key", ""), []).append(key)
    duplicates = {body: ks for body, ks in by_body.items() if len(ks) > 1}
    if duplicates:
        print(f"\nDuplicate key bodies: {len(duplicates)}")
        for body, group in sorted(duplicates.items()):
            entries = ", ".join(f"{k.get('id')} ({k.get('name')})" for k in group)
            label = body[:LABEL_MAX] + "..." if len(body) > LABEL_MAX else body
            print(f"  {label} -> {entries}")
        print(
            "Delete extras with: python examples/vultr_test.py delete-ssh-key --id <id>"
        )


def delete_ssh_key(key_id: str) -> None:
    vultr_request("DELETE", f"/ssh-keys/{key_id}")
    print(f"Deleted SSH key: {key_id}")


def delete_non_unique_ssh_keys(dry_run: bool) -> None:
    """Delete duplicate SSH keys, keeping the oldest key of each group.

    Groups keys by ``ssh_key`` body; every member beyond the oldest (by
    ``date_created``) of its group is a duplicate and gets deleted. Safe to
    re-run: unique keys and the kept oldest ones are never touched.
    """
    keys = fetch_ssh_keys()
    by_body: dict[str, list[dict]] = {}
    for key in keys:
        by_body.setdefault(key.get("ssh_key", ""), []).append(key)

    victims: list[dict] = []
    for group in by_body.values():
        if len(group) == 1:
            continue
        ordered = sorted(group, key=lambda k: k.get("date_created", ""))
        victims.extend(ordered[1:])

    if not victims:
        print("No duplicate SSH keys found")
        return

    action = "Would delete" if dry_run else "Deleting"
    print(
        f"{action} {len(victims)} of {len(keys)} keys (keeping oldest of each duplicate group)"
    )
    for key in victims:
        line = f"  {key.get('id')} ({key.get('name')}, {key.get('date_created')})"
        if dry_run:
            print(line)
        else:
            delete_ssh_key(key["id"])
    if dry_run:
        print("DRY RUN: no keys deleted")
    else:
        print(f"Deleted {len(victims)} duplicate keys")


def describe_body(body: str, head: int = 48, tail: int = 24) -> str:
    """One-line repr of a key body: head and tail, whitespace made visible."""
    if len(body) <= head + tail + 3:
        return repr(body)
    return repr(body[:head]) + "..." + repr(body[-tail:])


def export_scheduler_pubkey(path: str) -> str:
    """Export the public-key body exactly as the scheduler daemon does.

    The daemon loads the private key (yakey*), sets its comment to the file
    name, and exports via asyncssh ``export_public_key("openssh")`` — which
    appends a trailing newline. For a .pub file the raw file body is used.
    """
    with open(path, "rb") as f:
        head = f.read(64)
    if head.startswith(b"-----"):
        # lazy: keeps the script stdlib-only for non-diagnose commands
        import asyncssh  # noqa: PLC0415

        key = asyncssh.read_private_key(path)
        key.set_comment(os.path.basename(path))
        return key.export_public_key("openssh").decode("utf-8")
    with open(path, encoding="utf-8") as f:
        return f.read()


def poll_baremetal(instance_id: str, timeout: int = POLL_TIMEOUT) -> str | None:
    print(f"Waiting for instance {instance_id} to become active...")
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        data = vultr_request("GET", f"/bare-metals/{instance_id}")
        bm = data.get("bare_metal", data)
        status = bm.get("status", "")
        ip = bm.get("main_ip", "")
        if status != last_status:
            print(f"  status={status}, ip={ip}")
            last_status = status
        if status == "active" and ip and ip != "0.0.0.0":
            print(f"Instance is active: ip={ip}")
            return ip
        time.sleep(POLL_INTERVAL)
    print(f"ERROR: instance did not become active within {timeout}s", file=sys.stderr)
    return None


def check_ssh(ip: str, port: int = 22, timeout: int = 60) -> bool:
    print(f"Checking SSH connectivity to {ip}:{port}...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((ip, port), timeout=10):
                print("SSH port is open")
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            time.sleep(5)
    print(f"WARNING: could not reach SSH on {ip}:{port} within {timeout}s")
    return False


def cmd_create(args: argparse.Namespace) -> None:
    pubkey_path = args.ssh_key
    if not os.path.exists(pubkey_path):
        print(f"ERROR: SSH public key not found: {pubkey_path}", file=sys.stderr)
        print("Generate one with: ssh-keygen -t rsa -b 2048", file=sys.stderr)
        sys.exit(1)
    pubkey = read_ssh_pubkey(pubkey_path)
    key_name = os.path.basename(pubkey_path).replace(".pub", "")
    sshkey_id = get_or_create_ssh_key(pubkey, f"ya-{key_name}")

    label = args.label or f"ya-test-{os.urandom(4).hex()}"
    user_data = build_cloud_init()
    data = create_baremetal(
        location=args.location,
        server_type=args.server_type,
        image_name=args.image_name,
        label=label,
        hostname=args.hostname or label,
        sshkey_id=sshkey_id,
        user_data=user_data,
    )
    bm = data.get("bare_metal", data)
    instance_id = bm.get("id", "")
    ip = bm.get("main_ip", "")
    print(f"Created bare-metal: id={instance_id}, label={label}, ip={ip}")
    print(f"cloud-init user-data ({len(user_data)} bytes) applied")


def cmd_list(_: object) -> None:
    list_baremetals()


def cmd_delete(args: argparse.Namespace) -> None:
    delete_baremetal(args.id)


def cmd_list_ssh_keys(_: object) -> None:
    list_ssh_keys()


def cmd_delete_ssh_key(args: argparse.Namespace) -> None:
    if args.non_unique:
        delete_non_unique_ssh_keys(args.dry_run)
    else:
        delete_ssh_key(args.id)


def cmd_test(args: argparse.Namespace) -> None:
    pubkey_path = args.ssh_key
    if not os.path.exists(pubkey_path):
        print(f"ERROR: SSH public key not found: {pubkey_path}", file=sys.stderr)
        sys.exit(1)
    pubkey = read_ssh_pubkey(pubkey_path)
    key_name = os.path.basename(pubkey_path).replace(".pub", "")
    sshkey_id = get_or_create_ssh_key(pubkey, f"ya-{key_name}")

    label = f"ya-test-{os.urandom(4).hex()}"
    user_data = build_cloud_init()
    data = create_baremetal(
        location=args.location,
        server_type=args.server_type,
        image_name=args.image_name,
        label=label,
        hostname=label,
        sshkey_id=sshkey_id,
        user_data=user_data,
    )
    bm = data.get("bare_metal", data)
    instance_id = bm.get("id", "")
    print(f"Created bare-metal: id={instance_id}, label={label}")

    ip = poll_baremetal(instance_id, timeout=POLL_TIMEOUT)
    if not ip:
        print("Cleaning up failed instance...")
        delete_baremetal(instance_id)
        sys.exit(1)

    check_ssh(ip, timeout=args.ssh_timeout)

    if not args.keep:
        print(f"Deleting instance {instance_id}...")
        delete_baremetal(instance_id)
        print("Test complete.")
    else:
        print(
            f"Instance kept. Delete manually: python {sys.argv[0]} delete --id {instance_id}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vultr bare-metal test script for yascheduler integration"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="Create a bare-metal instance")
    p.add_argument("--location", default=DEFAULT_LOCATION)
    p.add_argument("--server-type", default=DEFAULT_SERVER_TYPE)
    p.add_argument("--image-name", type=int, default=DEFAULT_IMAGE_NAME)
    p.add_argument("--label", default=None)
    p.add_argument("--hostname", default=None)
    p.add_argument("--ssh-key", default=DEFAULT_SSH_KEY_PATH)
    p.set_defaults(func=cmd_create)

    sub.add_parser("list", help="List bare-metal instances").set_defaults(func=cmd_list)

    p = sub.add_parser("delete", help="Delete a bare-metal instance")
    p.add_argument("--id", required=True)
    p.set_defaults(func=cmd_delete)

    sub.add_parser(
        "list-ssh-keys", help="List account SSH keys (marks duplicates)"
    ).set_defaults(func=cmd_list_ssh_keys)

    p = sub.add_parser(
        "delete-ssh-key", help="Delete an SSH key by id or clean up duplicates"
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", help="Delete the SSH key with this id")
    g.add_argument(
        "--non-unique",
        action="store_true",
        help="Delete only duplicate keys (keep the oldest of each group)",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="Only print what would be deleted"
    )
    p.set_defaults(func=cmd_delete_ssh_key)

    p = sub.add_parser("test", help="Create, wait, check SSH, then delete")
    p.add_argument("--location", default=DEFAULT_LOCATION)
    p.add_argument("--server-type", default=DEFAULT_SERVER_TYPE)
    p.add_argument("--image-name", type=int, default=DEFAULT_IMAGE_NAME)
    p.add_argument("--ssh-key", default=DEFAULT_SSH_KEY_PATH)
    p.add_argument("--ssh-timeout", type=int, default=300)
    p.add_argument("--keep", action="store_true", help="Keep instance after test")
    p.set_defaults(func=cmd_test)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
