#!/usr/bin/env python3

from configparser import ConfigParser
from typing import Optional

from hcloud import Client, APIException
from hcloud.images.domain import Image
from hcloud.server_types.domain import ServerType
from hcloud.ssh_keys.client import BoundSSHKey
from hcloud.ssh_keys.domain import SSHKey

from yascheduler.clouds import AbstractCloudAPI


class HetznerCloudAPI(AbstractCloudAPI):

    name = "hetzner"

    client: Client
    _ssh_key_id: Optional[BoundSSHKey] = None

    def __init__(self, config: ConfigParser):
        super().__init__(
            config=config,
            max_nodes=config.getint("clouds", "hetzner_max_nodes", fallback=None),
        )
        self.client = Client(token=config.get("clouds", "hetzner_token"))

    @property
    def ssh_key_id(self) -> BoundSSHKey:
        if not self._ssh_key_id:
            try:
                self._ssh_key_id = self.client.ssh_keys.create(
                    name=self.key_name,
                    public_key=self.public_key,
                )
            except APIException as ex:
                if "already" in str(ex):
                    for key in self.client.ssh_keys.get_all():
                        if key.name.startswith("yakey") and len(key.name) == 14:
                            self._ssh_key_id = key.id
                else:
                    raise
        return self._ssh_key_id

    def create_node(self):
        response = self.client.servers.create(
            name=self.get_rnd_name("node"),
            server_type=ServerType("cx51"),
            image=Image(name="debian-10"),
            ssh_keys=[SSHKey(id=self.ssh_key_id, name=self.key_name)],
            user_data=self.cloud_config_data.render(),
        )
        server = response.server
        ip = server.public_net.ipv4.ip
        self._log.info("CREATED %s" % ip)

        # wait node up and ready
        self._run_ssh_cmd_with_backoff(
            ip, cmd="cloud-init status --wait", max_interval=5
        )

        return ip

    def delete_key(self):
        self.client.ssh_keys.delete(self.ssh_key_id.data_model)

    def delete_node(self, ip):
        server = None

        for s in self.client.servers.get_all():
            if s.public_net.ipv4.ip == ip:
                server = self.client.servers.get_by_id(s.id)
                break

        if server:
            server.delete()
            self._log.info("DELETED %s" % ip)

        else:
            self._log.info("NODE %s NOT DELETED AS UNKNOWN" % ip)
