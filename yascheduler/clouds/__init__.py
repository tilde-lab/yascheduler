
import os
import sys
import string
import random
import logging
import subprocess
from importlib import import_module
import inspect
from configparser import NoSectionError, ConfigParser

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend as crypto_default_backend

from fabric import Connection as SSH_Connection
from paramiko.rsakey import RSAKey

from yascheduler import DEFAULT_NODES_PER_PROVIDER


logging.basicConfig(level=logging.INFO)

class AbstractCloudAPI(object):

    name = "abstract"
    config: ConfigParser

    def __init__(self, max_nodes=None):
        self.max_nodes = int(max_nodes if max_nodes is not None else DEFAULT_NODES_PER_PROVIDER)
        self.yascheduler = None

        self.public_key = None
        self.ssh_custom_key = None

    @property
    def ssh_user(self):
        return self.config.get(
            "clouds",
            f"{self.name}_user",
            fallback=self.config.get("remote", "user", fallback="root"),
        )

    def init_key(self):
        if self.ssh_custom_key:
            return

        for filename in os.listdir(self.config.get('local', 'data_dir')):
            if not filename.startswith('yakey') or not os.path.isfile(
                os.path.join(self.config.get('local', 'data_dir'), filename)):
                continue
            key_path = os.path.join(self.config.get('local', 'data_dir'), filename)
            self.key_name = key_path.split(os.sep)[-1]
            pmk_key = RSAKey.from_private_key_file(key_path)
            logging.info('LOADED KEY %s' % key_path)
            break

        else:
            self.key_name = self.get_rnd_name('yakey')
            key = rsa.generate_private_key(
                backend=crypto_default_backend(),
                public_exponent=65537,
                key_size=2048
            )
            pmk_key = RSAKey(key=key)
            key_path = os.path.join(self.config.get('local', 'data_dir'), self.key_name)
            pmk_key.write_private_key_file(key_path)
            logging.info('WRITTEN KEY %s' % key_path)

        self.public_key = "%s %s" % (pmk_key.get_name(), pmk_key.get_base64())

        self.ssh_custom_key = {'pkey': pmk_key}
        if self.yascheduler:
            self.yascheduler.ssh_custom_key = self.ssh_custom_key

    def get_rnd_name(self, prefix):
        return prefix + '-' + \
            ''.join([random.choice(string.ascii_lowercase) for _ in range(8)])

    def setup_node(self, ip):
        ssh_conn = SSH_Connection(
            host=ip, user=self.ssh_user, connect_kwargs=self.ssh_custom_key
        )
        sudo_prefix = "" if self.ssh_user == "root" else "sudo "
        apt_cmd = f"{sudo_prefix}apt-get -o DPkg::Lock::Timeout=600"
        ssh_conn.run(f'{apt_cmd} -y update && {apt_cmd} -y upgrade', hide=True)
        ssh_conn.run(f'{apt_cmd} -y install openmpi-bin', hide=True)

        ssh_conn.run('mkdir -p ~/bin', hide=True)
        if self.config.get('local', 'deployable').startswith('http'):
            # downloading binary from a trusted non-public address
            ssh_conn.run('cd ~/bin && wget %s' % self.config.get('local', 'deployable'), hide=True)
        else:
            # uploading binary from local; requires broadband connection
            ssh_conn.put(self.config.get('local', 'deployable'), '~/bin/Pcrystal') # TODO

        if self.config.get('local', 'deployable').endswith('.gz'):
            # binary may be gzipped, without subfolders, with an arbitrary archive name,
            # but the name of the binary must remain Pcrystal
            ssh_conn.run('cd ~/bin && tar xvf %s' % self.config.get('local', 'deployable').split('/')[-1], hide=True)
        #ssh_conn.run('ln -sf ~/bin/Pcrystal /usr/bin/Pcrystal', hide=True)

        # print and ensure versions
        result = ssh_conn.run('/usr/bin/mpirun --allow-run-as-root -V', hide=True)
        logging.info(result.stdout)
        result = ssh_conn.run('cat /etc/issue', hide=True)
        logging.info(result.stdout)
        result = ssh_conn.run('grep -c ^processor /proc/cpuinfo', hide=True)
        logging.info(result.stdout)


class CloudAPIManager(object):

    def __init__(self, config):
        self.apis = {}
        self.yascheduler = None
        self.tasks = set()
        active_providers = set()

        try:
            for name, value in dict(config.items('clouds')).items():
                if not value:
                    continue
                active_providers.add(name.split('_')[0])
        except NoSectionError: pass

        for name in active_providers:
            if config.getint('clouds', name + '_max_nodes', fallback=None) == 0:
                continue
            self.apis[name] = load_cloudapi(name)(config)

        logging.info( 'Active cloud APIs: ' + (', '.join(self.apis.keys()) or '-') )

    def __bool__(self):
        return bool(len(self.apis))

    def initialize(self):
        assert self.yascheduler
        for cloudapi in self.apis:
            self.apis[cloudapi].yascheduler = self.yascheduler
            self.apis[cloudapi].init_key()

    def allocate(self, on_task):
        if on_task in self.tasks:
            return
        self.tasks.add(on_task)

        subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), 'allocate_node.py')])
        logging.info('STARTED BACKGROUND ALLOCATION')

    def deallocate(self, ips):
        self.yascheduler.cursor.execute("SELECT ip, cloud FROM yascheduler_nodes WHERE cloud IS NOT NULL AND ip IN ('%s');" % "', '".join(ips))
        for row in self.yascheduler.cursor.fetchall():
            subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), 'deallocate_node.py'), row[1], row[0]])
            logging.info('STARTED BACKGROUND DEALLOCATION')

    def get_capacity(self, resources):
        n_busy_cloud_nodes = len([item for item in resources if item[3]]) # Yascheduler.queue_get_resources()
        max_nodes = sum([self.apis[cloudapi].max_nodes for cloudapi in self.apis])
        diff = max_nodes - n_busy_cloud_nodes
        return diff if diff > 0 else 0


def load_cloudapi(name):
    cloudapi_mod = import_module('.' + name, package='yascheduler.clouds')
    for _, cls in inspect.getmembers(cloudapi_mod):
        if inspect.isclass(cls) and hasattr(cls, 'create_node') and hasattr(cls, 'delete_node'):
            return cls
    raise ImportError