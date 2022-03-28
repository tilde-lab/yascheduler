
import json

from hcloud import Client, APIException
from hcloud.images.domain import Image
from hcloud.server_types.domain import ServerType
from hcloud.ssh_keys.domain import SSHKey

from yascheduler.clouds import AbstractCloudAPI


class HetznerCloudAPI(AbstractCloudAPI):

    name = 'hetzner'

    def __init__(self, config):
        super().__init__(max_nodes=config.getint('clouds', 'hetzner_max_nodes', fallback=None))
        self.client = Client(token=config.get('clouds', 'hetzner_token'))
        self.config = config

    def init_key(self):
        super().init_key()
        try:
            self.ssh_key_id = self.client.ssh_keys.create(name=self.key_name, public_key=self.public_key)
        except APIException as ex:
            if 'already' in str(ex):
                for key in self.client.ssh_keys.get_all():
                    if key.name.startswith('yakey') and len(key.name) == 14:
                        self.ssh_key_id = key.id
            else:
                raise

    def create_node(self):
        assert self.ssh_key_id
        assert self.ssh_custom_key

        response = self.client.servers.create(
            name=self.get_rnd_name('node'),
            server_type=ServerType('cx51'), image=Image(name='debian-10'),
            ssh_keys=[SSHKey(name=self.key_name)],
            user_data="#cloud-config\n" + json.dumps(self.cloud_config_data),
        )
        server = response.server
        ip = server.public_net.ipv4.ip
        self._log.info('CREATED %s' % ip)

        # wait node up and ready
        self._run_ssh_cmd_with_backoff(
            ip, cmd="cloud-init status --wait", max_interval=5
        )

        return ip

    def delete_key(self):
        self.client.ssh_keys.delete(self.ssh_key_id)

    def delete_node(self, ip):
        server = None

        for s in self.client.servers.get_all():
            if s.public_net.ipv4.ip == ip:
                server = self.client.servers.get_by_id(s.id)
                break

        if server:
            server.delete()
            self._log.info('DELETED %s' % ip)

        else:
            self._log.info('NODE %s NOT DELETED AS UNKNOWN' % ip)
