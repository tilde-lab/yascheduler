
import time
import logging

from upcloud_api import CloudManager, Server, Storage, ZONE, login_user_block

from fabric import Connection as SSH_Connection

from yascheduler.clouds import AbstractCloudAPI


class UpCloudAPI(AbstractCloudAPI):

    name = 'upcloud'

    def __init__(self, config):
        super().__init__(max_nodes=config.getint('clouds', 'upcloud_max_nodes'))
        self.client = CloudManager(config.get('clouds', 'upcloud_login'), config.get('clouds', 'upcloud_pass'))
        self.client.authenticate()
        self.config = config

    def init_key(self):
        super().init_key()
        self.login_user = login_user_block(
            username=self.config.get('remote', 'user'),
            ssh_keys=[self.public_key],
            create_password=False
        )

    def create_node(self):
        assert self.ssh_custom_key

        server = self.client.create_server(Server(
            core_number=8,
            memory_amount=1024,
            hostname=self.get_rnd_name('node'),
            zone=ZONE.London,
            storage_devices=[
                Storage(os='Debian 10.0', size=20)
            ],
            login_user=self.login_user
        ))
        ip = server.get_public_ip()
        logging.info('CREATED %s' % ip)
        logging.info('WAITING FOR START...')
        time.sleep(30)

        # warm up
        for _ in range(10):
            ssh_conn = SSH_Connection(host=ip, user=self.config.get('remote', 'user'),
                connect_kwargs=self.ssh_custom_key)
            try: ssh_conn.run('whoami', hide=True)
            except: time.sleep(5)
            else: break

        return ip

    def delete_node(self, ip):
        for s in self.client.get_servers():
            if s.get_public_ip() == ip:
                s.stop()
                logging.info('WAITING FOR STOP...')
                time.sleep(20)
                while True:
                    try: s.destroy()
                    except: time.sleep(5)
                    else: break
                logging.info('DELETED %s' % ip)
                break
        # TODO remove the associated storage