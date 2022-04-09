#!/usr/bin/env python

import json
import logging
import os
import queue
import random
import string
import tempfile
from configparser import ConfigParser
from datetime import datetime, timedelta
from collections import Counter
from typing import List, Optional

from fabric import Connection as SSH_Connection
from yascheduler import connect_db, CONFIG_FILE, SLEEP_INTERVAL, N_IDLE_PASSES
import yascheduler.clouds
from yascheduler.engines import init_engines, get_engines_check_cmd
from yascheduler.time import sleep_until
from yascheduler.webhook_worker import WebhookWorker, WebhookTask

logging.basicConfig(level=logging.INFO)


class Yascheduler(object):

    STATUS_TO_DO = 0
    STATUS_RUNNING = 1
    STATUS_DONE = 2

    _log: logging.Logger
    _webhook_queue: "queue.Queue[WebhookTask]"
    _webhook_threads: List[WebhookWorker]

    def __init__(self, config, logger: Optional[logging.Logger] = None):
        if logger:
            self._log = logger.getChild(self.__class__.__name__)
        else:
            self._log = logging.getLogger(self.__class__.__name__)
        self.config = config
        self.connection, self.cursor = connect_db(config)
        self.ssh_conn_pool = {}
        self.ssh_custom_key = {}
        self.clouds = None
        self.engines = init_engines(config)
        self._webhook_queue = queue.Queue()
        webhook_thread_num = int(
            config.get("local", "webhook_threads", fallback=2)
        )
        self._webhook_threads = []
        for i in range(webhook_thread_num):
            t = WebhookWorker(
                name=f"WebhookThread[{i}]",
                logger=self._log,
                task_queue=self._webhook_queue,
            )
            self._webhook_threads.append(t)

    def start(self) -> None:
        for t in self._webhook_threads:
            t.start()

    def queue_get_resources(self):
        self.cursor.execute('SELECT ip, ncpus, enabled, cloud FROM yascheduler_nodes;')
        return self.cursor.fetchall()

    def queue_get_resource(self, ip):
        self.cursor.execute(
            (
                "SELECT ip, ncpus, enabled, cloud "
                "FROM yascheduler_nodes WHERE ip=(%S);"
            ),
            (ip),
        )
        return self.cursor.fetchone()

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

    def enqueue_task_event(self, task_id: int) -> None:
        task = self.queue_get_task(task_id) or {}
        wt = WebhookTask.from_dict(task)
        self._webhook_queue.put(wt)

    def queue_set_task_running(self, task_id, ip):
        self.cursor.execute("UPDATE yascheduler_tasks SET status=%s, ip=%s WHERE task_id=%s;",
                            (self.STATUS_RUNNING, ip, task_id))
        self.connection.commit()
        self.enqueue_task_event(task_id)

    def queue_set_task_done(self, task_id, metadata):
        self.cursor.execute("UPDATE yascheduler_tasks SET status=%s, metadata=%s WHERE task_id=%s;",
                            (self.STATUS_DONE, json.dumps(metadata), task_id))
        self.connection.commit()
        self.enqueue_task_event(task_id)
        #if self.clouds:
        # TODO: free-up CloudAPIManager().tasks

    def queue_submit_task(self, label, metadata, engine):

        if engine not in self.engines:
            raise RuntimeError("Engine %s requested, but not supported" % engine)

        for input_file in self.engines[engine]['input_files']:
            if input_file not in metadata:
                raise RuntimeError("Input file %s was not provided" % input_file)

        metadata['engine'] = engine
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
        self._log.info(':::submitted: %s' % label)
        return self.cursor.fetchone()[0]

    def ssh_connect(self, new_nodes):
        old_nodes = self.ssh_conn_pool.keys()

        ip_cloud_map = {}
        resources = self.queue_get_resources()
        for row in resources:
            if row[0] in new_nodes:
                ip_cloud_map[row[0]] = row[3]

        for ip in set(old_nodes) - set(new_nodes):
            self.ssh_conn_pool[ip].close()
            del self.ssh_conn_pool[ip]
        for ip in set(new_nodes) - set(old_nodes):
            cloud = self.clouds and self.clouds.apis.get(ip_cloud_map.get(ip))
            ssh_user = (
                cloud and cloud.ssh_user or self.config.get("remote", "user")
            )
            self.ssh_conn_pool[ip] = SSH_Connection(
                host=ip, user=ssh_user, connect_kwargs=self.ssh_custom_key
            )

        self._log.info(
            "Nodes to watch: %s" % ", ".join(self.ssh_conn_pool.keys())
        )
        if not self.ssh_conn_pool:
            self._log.warning('No nodes set!')

    def ssh_run_task(self, ip, ncpus, label, metadata):
        assert not self.ssh_check_task(ip), \
            "Cannot run the task %s at host %s, as this host is already occupied with another task!" % (
            label, ip) # TODO handle this situation

        assert metadata['remote_folder']
        assert metadata['engine'] in self.engines

        try:
            self.ssh_conn_pool[ip].run('mkdir -p %s' % metadata['remote_folder'], hide=True)
        except Exception as err:
            self._log.error('SSH spawn cmd error: %s' % err)
            return False

        with tempfile.NamedTemporaryFile() as tmp: # NB beware overflown remote
            for input_file in self.engines[metadata['engine']]['input_files']:
                tmp.write(metadata[input_file].encode('utf-8'))
                tmp.flush()
                self.ssh_conn_pool[ip].put(tmp.name, metadata['remote_folder'] + '/' + input_file)
                tmp.seek(0)
                tmp.truncate()

        # placeholders {path} and {ncpus} are supported
        run_cmd = self.engines[metadata['engine']]['spawn'].format(
            path=metadata['remote_folder'],
            ncpus=ncpus or '`grep -c ^processor /proc/cpuinfo`'
        )
        self._log.debug(run_cmd)

        try:
            self.ssh_conn_pool[ip].run(run_cmd, hide=True, disown=True)
        except Exception as err:
            self._log.error('SSH spawn cmd error: %s' % err)
            return False

        return True

    def ssh_check_node(self, ip):
        try:
            node_info = self.queue_get_resource(ip)
            cloud = (
                self.clouds and self.clouds.apis.get(node_info and node_info[3])
            )
            ssh_user = (
                cloud and cloud.ssh_user or self.config.get("remote", "user")
            )
            with SSH_Connection(
                host=ip,
                user=ssh_user,
                connect_kwargs=self.ssh_custom_key,
                connect_timeout=5,
            ) as conn:
                result = str( conn.run(get_engines_check_cmd(self.engines), hide=True) )
                for engine_name in self.engines:
                    if self.engines[engine_name]['run_marker'] in result:
                        self._log.error(
                            "Cannot add a busy with %s resourse %s@%s"
                            % (engine_name, ssh_user, ip)
                        )
                        return False

        except Exception as err:
            self._log.error(
                "Host %s@%s is unreachable due to: %s"
                % (self.config.get("remote", "user"), ip, err)
            )
            return False

        return True

    def ssh_check_task(self, ip):
        assert ip in self.ssh_conn_pool, "Node %s was referred by active task, however absent in node list" % ip
        try:
            result = str( self.ssh_conn_pool[ip].run(get_engines_check_cmd(self.engines), hide=True) )
        except Exception as err:
            self._log.error('SSH status cmd error: %s' % err)
            # TODO handle that situation properly, re-assign ip, etc.
            result = ""

        for engine in self.engines.values():
            if engine['run_marker'] in result:
                return True

        return False

    def ssh_get_task(self, ip, engine, work_folder, store_folder, remove=True):
        for output_file in self.engines[engine]['output_files']:
            try:
                self.ssh_conn_pool[ip].get(work_folder + '/' + output_file, store_folder + '/' + output_file)
            except IOError as err:
                # TODO handle that situation properly
                self._log.error(
                    "Cannot scp %s/%s: %s" % (work_folder, output_file, err)
                )
                if 'Connection timed out' in str(err): break

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

    def stop(self):
        self._log.info("Stopping threads...")
        for t in self._webhook_threads:
            t.stop()
            t.join()


def daemonize(log_file=None):
    logger = get_logger(log_file)
    config = ConfigParser()
    config.read(CONFIG_FILE)

    yac, clouds = clouds.yascheduler, yac.clouds = Yascheduler(
        config
    ), yascheduler.clouds.CloudAPIManager(config, logger=logger)
    logging.getLogger('Yascheduler').setLevel(logging.DEBUG)
    clouds.initialize()
    yac.start()

    chilling_nodes = Counter() # ips vs. their occurences

    logger.debug('Available computing engines: %s' % ', '.join([engine_name for engine_name in yac.engines]))

    def step():
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
                webhook_url = ready_task['metadata'].get('webhook_url')
                store_folder = ready_task['metadata'].get('local_folder') or \
                    os.path.join(config.get('local', 'data_dir'),
                                 os.path.basename(ready_task['metadata']['remote_folder']))
                os.makedirs(store_folder, exist_ok=True) # TODO OSError if restart or invalid data_dir
                yac.ssh_get_task(ready_task['ip'], ready_task['metadata']['engine'], ready_task['metadata']['remote_folder'], store_folder)
                ready_task['metadata'] = dict(
                    remote_folder=ready_task['metadata']['remote_folder'],
                    local_folder=store_folder,
                )
                if webhook_url:
                    ready_task['metadata']['webhook_url'] = webhook_url
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

        # process results of allocators
        clouds.do_async_work()

        # print stats
        nodes = yac.queue_get_resources()
        enabled_nodes = list(filter(lambda x: x[2], nodes))
        logger.info(
            "NODES:\tenabled: %s\ttotal: %s",
            str(len(enabled_nodes)),
            str(len(nodes))
        )
        logger.info(
            "TASKS:\trunning: %s\tto do: %s\tdone: %s",
            len(yac.queue_get_tasks(status=(yac.STATUS_RUNNING,))),
            len(yac.queue_get_tasks(status=(yac.STATUS_TO_DO,))),
            len(yac.queue_get_tasks(status=(yac.STATUS_DONE,))),
        )

    # The main scheduler loop
    try:
        while True:
            end_time = datetime.now() + timedelta(seconds=SLEEP_INTERVAL)
            step()
            sleep_until(end_time)
    except KeyboardInterrupt:
        clouds.stop()
        yac.stop()


def get_logger(log_file):
    logger = logging.getLogger('yascheduler')
    logger.setLevel(logging.DEBUG)

    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger


if __name__ == "__main__":
    daemonize()
