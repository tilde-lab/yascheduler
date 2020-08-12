
import os
import random
import json
import time
import tempfile
import string
import logging
import socket
from configparser import ConfigParser
from datetime import datetime
from collections import Counter

from fabric import Connection as SSH_Connection
from yascheduler import connect_db, CONFIG_FILE, SLEEP_INTERVAL, N_IDLE_PASSES
from yascheduler.clouds import CloudAPIManager


RUN_CMD = "nohup /usr/bin/mpirun -np {ncpus} " \
          "--allow-run-as-root -wd {path} /usr/bin/Pcrystal > {path}/OUTPUT 2>&1 &"
# NB default ncpus: `grep -c ^processor /proc/cpuinfo`
logging.basicConfig(level=logging.INFO)


class Yascheduler(object):
    STATUS_TO_DO = 0
    STATUS_RUNNING = 1
    STATUS_DONE = 2

    RUNNING_MARKER = 'Pcrystal'
    CHECK_CMD = 'ps aux | grep crystal' # NB not "Pcrystal"

    def __init__(self, config):
        self.config = config
        self.connection, self.cursor = connect_db(config)
        self.ssh_conn_pool = {}
        self.ssh_custom_key = {}
        self.clouds = None

    def queue_get_resources(self):
        self.cursor.execute('SELECT ip, ncpus, enabled, cloud FROM yascheduler_nodes;')
        return self.cursor.fetchall()

    def queue_get_task(self, task_id):
        self.cursor.execute('SELECT label, metadata, ip, status FROM yascheduler_tasks WHERE task_id=%s;' % task_id)
        row = self.cursor.fetchone()
        if not row:
            return None
        return dict(task_id=task_id, label=row[0], metadata=row[1], ip=row[2], status=row[3])

    def queue_get_tasks_to_do(self, num_nodes):
        self.cursor.execute('SELECT task_id, label, metadata FROM yascheduler_tasks WHERE status=%s LIMIT %s;',
                            (self.STATUS_TO_DO, num_nodes))
        return [dict(task_id=row[0], label=row[1], metadata=row[2]) for row in self.cursor.fetchall()]

    def queue_get_tasks(self, jobs=None, status=None):
        if jobs is not None and status is not None:
            raise ValueError("jobs can be selected only by status or by task ids")
        if jobs is None and status is None:
            raise ValueError("jobs can only be selected by status or by task ids")
        if status is not None:
            query_string = 'status IN ({})'.format(', '.join(['%s'] * len(status)))
            params = status
        else:
            query_string = 'task_id IN ({})'.format(', '.join(['%s'] * len(jobs)))
            params = jobs

        sql_statement = 'SELECT task_id, label, ip, status FROM yascheduler_tasks WHERE {};'.format(query_string)
        self.cursor.execute(sql_statement, params)
        return [dict(task_id=row[0], label=row[1], ip=row[2], status=row[3]) for row in self.cursor.fetchall()]

    def queue_set_task_running(self, task_id, ip):
        self.cursor.execute("UPDATE yascheduler_tasks SET status=%s, ip=%s WHERE task_id=%s;",
                            (self.STATUS_RUNNING, ip, task_id))
        self.connection.commit()

    def queue_set_task_done(self, task_id, metadata):
        self.cursor.execute("UPDATE yascheduler_tasks SET status=%s, metadata=%s WHERE task_id=%s;",
                            (self.STATUS_DONE, json.dumps(metadata), task_id))
        self.connection.commit()
        #if self.clouds:
        # TODO: free-up CloudAPIManager().tasks

    def queue_submit_task(self, label, metadata):
        assert metadata['input']
        assert metadata['structure']
        metadata['remote_folder'] = os.path.join(self.config.get('remote', 'data_dir'),
                                                 '_'.join([datetime.now().strftime('%Y%m%d_%H%M%S'),
                                                           ''.join([random.choice(string.ascii_lowercase) for _ in range(4)])]))

        self.cursor.execute("""INSERT INTO yascheduler_tasks (label, metadata, ip, status)
            VALUES ('{label}', '{metadata}', NULL, {status})
            RETURNING task_id;""".format(
            label=label,
            metadata=json.dumps(metadata),
            status=self.STATUS_TO_DO
        ))
        self.connection.commit()
        logging.info(':::submitted: %s' % label)
        return self.cursor.fetchone()[0]

    def ssh_connect(self, new_nodes):
        old_nodes = self.ssh_conn_pool.keys()

        for ip in set(old_nodes) - set(new_nodes):
            self.ssh_conn_pool[ip].close()
            del self.ssh_conn_pool[ip]
        for ip in set(new_nodes) - set(old_nodes):
            self.ssh_conn_pool[ip] = SSH_Connection(host=ip, user=self.config.get('remote', 'user'),
                connect_kwargs=self.ssh_custom_key)

        logging.info('Nodes to watch: %s' % ', '.join(self.ssh_conn_pool.keys()))
        if not self.ssh_conn_pool:
            logging.warning('No nodes set!')

    def ssh_run_task(self, ip, ncpus, label, metadata):
        assert not self.ssh_check_task(ip), \
            "Cannot run the task %s at host %s, as this host is already occupied with another task!" % (
            label, ip) # TODO handle this situation

        assert metadata['remote_folder']
        assert metadata['input']
        assert metadata['structure']

        try:
            self.ssh_conn_pool[ip].run('mkdir -p %s' % metadata['remote_folder'], hide=True)
        except Exception as err:
            logging.error('SSH spawn cmd error: %s' % err)
            return False

        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(metadata['input'].encode('utf-8'))
            tmp.flush()
            self.ssh_conn_pool[ip].put(tmp.name, metadata['remote_folder'] + '/INPUT') # NB beware overflown remote
            tmp.seek(0)
            tmp.truncate()
            tmp.write(metadata['structure'].encode('utf-8'))
            tmp.flush()
            self.ssh_conn_pool[ip].put(tmp.name, metadata['remote_folder'] + '/fort.34') # NB beware overflown remote

        try:
            self.ssh_conn_pool[ip].run(RUN_CMD.format(
                path=metadata['remote_folder'],
                ncpus=ncpus or '`grep -c ^processor /proc/cpuinfo`'
            ), hide=True)
        except Exception as err:
            logging.error('SSH spawn cmd error: %s' % err)
            return False

        return True

    def ssh_check_node(self, ip):
        try:
            with SSH_Connection(host=ip, user=self.config.get('remote', 'user'),
                connect_kwargs=self.ssh_custom_key, connect_timeout=5
            ) as conn:
                result = conn.run(Yascheduler.CHECK_CMD, hide=True)
                if Yascheduler.RUNNING_MARKER in str(result):
                    logging.error('Cannot add a busy resourse %s@%s' % (self.config.get('remote', 'user'), ip))
                    return False
        except socket.timeout:
            logging.error('Host %s@%s is unreachable' % (self.config.get('remote', 'user'), ip))
            return False
        return True

    def ssh_check_task(self, ip):
        assert ip in self.ssh_conn_pool, "Node %s was referred by active task, however absent in node list" % ip
        try:
            result = self.ssh_conn_pool[ip].run(Yascheduler.CHECK_CMD, hide=True)
        except Exception as err:
            logging.error('SSH status cmd error: %s' % err)
            # TODO handle that situation properly, re-assign ip, etc.
            result = ""
        return Yascheduler.RUNNING_MARKER in str(result)

    def ssh_get_task(self, ip, work_folder, store_folder, remove=True):
        remote_local_files = {
            'INPUT': 'INPUT',
            'fort.34': 'fort.34',
            'OUTPUT': 'OUTPUT', # NB aiida still considers this as stderr
            'fort.9': 'fort.9', # wavefunction
            'fort.87': 'fort.87' # errors
        }
        for remote, local in remote_local_files.items():
            try:
                self.ssh_conn_pool[ip].get(work_folder + '/' + remote, store_folder + '/' + local)
            except IOError as err:
                # TODO handle that situation properly
                logging.error('Cannot scp %s: %s' % (work_folder + '/' + remote, err))
                if 'Connection timed out' in str(err): break

        # TODO get other files: "FREQINFO.DAT" "OPTINFO.DAT" "SCFOUT.LOG" "fort.13" "fort.98", "fort.78"
        # TODO but how to do it quickly or in the background?
        # NB recursive copying of folders is not supported :(
        if remove:
            self.ssh_conn_pool[ip].run('rm -rf %s' % work_folder, hide=True)

    def clouds_allocate(self, on_task):
        if self.clouds:
            self.clouds.allocate(on_task)

    def clouds_deallocate(self, ips):
        if self.clouds:
            self.clouds.deallocate(ips)

    def clouds_get_capacity(self, resources):
        if self.clouds:
            return self.clouds.get_capacity(resources)
        return 0


def daemonize(log_file=None):
    logger = get_logger(log_file)
    config = ConfigParser()
    config.read(CONFIG_FILE)

    yac, clouds = clouds.yascheduler, yac.clouds = Yascheduler(config), CloudAPIManager(config)
    clouds.initialize()

    chilling_nodes = Counter() # ips vs. their occurences

    # The main scheduler loop
    while True:
        resources = yac.queue_get_resources()
        all_nodes = [item[0] for item in resources if '.' in item[0]] # NB provision nodes have fake ips
        if sorted(yac.ssh_conn_pool.keys()) != sorted(all_nodes):
            yac.ssh_connect(all_nodes)

        enabled_nodes = {item[0]: item[1] for item in resources if item[2]}
        free_nodes = list(enabled_nodes.keys())

        # (I.) Tasks de-allocation clause
        tasks_running = yac.queue_get_tasks(status=(yac.STATUS_RUNNING,))
        logger.debug('running %s tasks: %s' % (len(tasks_running), tasks_running))
        for task in tasks_running:
            if yac.ssh_check_task(task['ip']):
                try: free_nodes.remove(task['ip'])
                except ValueError: pass
            else:
                ready_task = yac.queue_get_task(task['task_id'])
                store_folder = ready_task['metadata'].get('local_folder') or \
                    os.path.join(config.get('local', 'data_dir'),
                                 os.path.basename(ready_task['metadata']['remote_folder']))
                os.makedirs(store_folder, exist_ok=True) # TODO OSError if restart or invalid data_dir
                yac.ssh_get_task(ready_task['ip'], ready_task['metadata']['remote_folder'], store_folder)
                ready_task['metadata'] = dict(remote_folder=ready_task['metadata']['remote_folder'],
                                              local_folder=store_folder)
                yac.queue_set_task_done(ready_task['task_id'], ready_task['metadata'])
                logger.info(':::task_id={} {} done and saved in {}'.format(task['task_id'], ready_task['label'],
                                                                ready_task['metadata'].get('local_folder')))
                # TODO here we might want to notify our data consumers in an event-driven manner
                # TODO but how to do it quickly or in the background?

        # (II.) Resourses and tasks allocation clause
        clouds_capacity = yac.clouds_get_capacity(resources)
        if free_nodes or clouds_capacity:
            for task in yac.queue_get_tasks_to_do(clouds_capacity + len(free_nodes)):
                if not free_nodes:
                    yac.clouds_allocate(task['task_id'])
                    continue
                random.shuffle(free_nodes)
                ip = free_nodes.pop()
                logger.info(':::submitting task_id=%s %s to %s' % (task['task_id'], task['label'], ip))

                if yac.ssh_run_task(ip, enabled_nodes[ip], task['label'], task['metadata']):
                    yac.queue_set_task_running(task['task_id'], ip)

        # (III.) Resourses de-allocation clause
        if free_nodes: # candidates for removal
            chilling_nodes.update(free_nodes)
            deallocatable = Counter([
                x[0] for x in filter(lambda x: x[1] >= N_IDLE_PASSES, chilling_nodes.most_common())
            ])
            if deallocatable:
                yac.clouds_deallocate(list(deallocatable.elements()))
                chilling_nodes.subtract(deallocatable)

        time.sleep(SLEEP_INTERVAL)


def get_logger(log_file):
    logger = logging.getLogger('yascheduler')
    logger.setLevel(logging.INFO)

    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger


if __name__ == "__main__":
    daemonize()
