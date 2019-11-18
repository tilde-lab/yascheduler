#!/usr/bin/env python3
"""
Postgres schema:
CREATE TABLE yascheduler_nodes (
ip VARCHAR(15) UNIQUE
);
INSERT INTO yascheduler_nodes (ip) VALUES ('X.X.X.X');
CREATE TABLE yascheduler_tasks (
task_id INT PRIMARY KEY,
label VARCHAR(256),
metadata jsonb,
ip VARCHAR(15),
status SMALLINT
);
CREATE SEQUENCE task_id_seq START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;
ALTER SEQUENCE task_id_seq OWNED BY yascheduler_tasks.task_id;
ALTER TABLE ONLY yascheduler_tasks ALTER COLUMN task_id SET DEFAULT nextval('task_id_seq'::regclass);

Known bugs: exits on Enter press
"""
import os
import time
import random
import json
import tempfile
import logging
from datetime import datetime
from configparser import ConfigParser

import pg8000
from fabric import Connection as SSH_Connection


RUN_CMD = "nohup /usr/bin/mpirun -np 4 --allow-run-as-root -wd {path} /root/bin/Pcrystal > {path}/OUTPUT 2>&1 &" # TODO grep ^cpu\\scores /proc/cpuinfo | uniq | awk '{print $4}'
sleep_interval = 6
logging.basicConfig(level=logging.INFO)


class Yascheduler(object):
    STATUS_TO_DO = 0
    STATUS_RUNNING = 1
    STATUS_DONE = 2

    RUNNING_MARKER = 'Pcrystal'
    CHECK_CMD = 'top -b -n 1 > /tmp/top.tmp && head -n27 /tmp/top.tmp | tail -n20'

    def __init__(self, config):
        self.config = config
        self.queue_connect()
        self.ssh_conn_pool = {}

    def queue_connect(self):
        self.pgconn = pg8000.connect(
            user=self.config.get('db', 'user'),
            password=self.config.get('db', 'password'),
            database=self.config.get('db', 'database'),
            host=self.config.get('db', 'host'),
            port=self.config.getint('db', 'port')
        )
        self.pgcursor = self.pgconn.cursor()

    def queue_get_all_nodes(self):
        self.pgcursor.execute('SELECT ip FROM yascheduler_nodes;')
        return [row[0] for row in self.pgcursor.fetchall()]

    def queue_get_free_nodes(self, all_nodes, tasks_running):
        running_nodes = set([task['ip'] for task in tasks_running])
        return [ip for ip in all_nodes if ip not in running_nodes]

    def queue_get_task(self, task_id):
        self.pgcursor.execute('SELECT label, metadata, ip, status FROM yascheduler_tasks WHERE task_id=%s;' % task_id)
        row = self.pgcursor.fetchone()
        if not row:
            return None
        return dict(task_id=task_id, label=row[0], metadata=row[1], ip=row[2], status=row[3])

    def queue_get_tasks_to_do(self, num_nodes):
        self.pgcursor.execute('SELECT task_id, label, metadata FROM yascheduler_tasks WHERE status=%s LIMIT %s;' % (Yascheduler.STATUS_TO_DO, num_nodes))
        return [dict(task_id=row[0], label=row[1], metadata=row[2]) for row in self.pgcursor.fetchall()]

    def queue_get_tasks_running(self):
        self.pgcursor.execute('SELECT task_id, ip FROM yascheduler_tasks WHERE status=%s;' % Yascheduler.STATUS_RUNNING)
        return [dict(task_id=row[0], ip=row[1]) for row in self.pgcursor.fetchall()]

    def queue_set_task_running(self, task_id, ip):
        self.pgcursor.execute("UPDATE yascheduler_tasks SET status=%s, ip='%s' WHERE task_id=%s;" % (Yascheduler.STATUS_RUNNING, ip, task_id))
        self.pgconn.commit()

    def queue_set_task_done(self, task_id, metadata):
        self.pgcursor.execute("UPDATE yascheduler_tasks SET status=%s, metadata='%s' WHERE task_id=%s;" % (
            Yascheduler.STATUS_DONE, json.dumps(metadata), task_id
        ))
        self.pgconn.commit()

    def queue_submit_task(self, label, metadata):
        assert metadata['input']
        assert metadata['structure']

        metadata['work_folder'] = self.config.get('remote', 'data_dir') + '/' + datetime.now().strftime('%Y%m%d_%H%M%S') + \
                                '_' + ''.join([random.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(4)])
        self.pgcursor.execute("INSERT INTO yascheduler_tasks (label, metadata, ip, status) VALUES ('{label}', '{metadata}', NULL, {status});".format(
            label=label,
            metadata=json.dumps(metadata),
            status=Yascheduler.STATUS_TO_DO
        ))
        self.pgconn.commit()
        logging.info(':::submitted: %s' % label)

    def ssh_connect(self):
        new_nodes = self.queue_get_all_nodes()
        old_nodes = self.ssh_conn_pool.keys()

        for ip in set(old_nodes) - set(new_nodes):
            self.ssh_conn_pool[ip].close()
            del self.ssh_conn_pool[ip]
        for ip in set(new_nodes) - set(old_nodes):
            self.ssh_conn_pool[ip] = SSH_Connection(host=ip, user='root')

        assert self.ssh_conn_pool
        logging.info('New nodes: %s' % ', '.join(self.ssh_conn_pool.keys()))

    def ssh_run_task(self, ip, label, metadata):
        assert not self.ssh_check_task(ip) # TODO handle this situation
        assert metadata['work_folder']
        assert metadata['input']
        assert metadata['structure']

        self.ssh_conn_pool[ip].run('mkdir -p %s' % metadata['work_folder'], hide=True)

        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(metadata['input'].encode('utf-8'))
            tmp.flush()
            self.ssh_conn_pool[ip].put(tmp.name, metadata['work_folder'] + '/INPUT')
            tmp.seek(0)
            tmp.write(metadata['structure'].encode('utf-8'))
            tmp.flush()
            self.ssh_conn_pool[ip].put(tmp.name, metadata['work_folder'] + '/fort.34')

        self.ssh_conn_pool[ip].run(RUN_CMD.format(path=metadata['work_folder']), hide=True)

    def ssh_check_task(self, ip):
        assert ip in self.ssh_conn_pool
        try:
            result = self.ssh_conn_pool[ip].run(Yascheduler.CHECK_CMD, hide=True)
        except Exception as err:
            logging.error('SSH status cmd error: %s' % err)
            # TODO handle that situation properly, re-assign ip, etc.
            result = ""
        return Yascheduler.RUNNING_MARKER in str(result)

    def ssh_get_task(self, ip, work_folder, store_folder, remove=True):
        self.ssh_conn_pool[ip].get(work_folder + '/INPUT', store_folder + '/INPUT')
        self.ssh_conn_pool[ip].get(work_folder + '/fort.34', store_folder + '/fort.34')
        self.ssh_conn_pool[ip].get(work_folder + '/OUTPUT', store_folder + '/OUTPUT')
        # TODO get other files: "FREQINFO.DAT" "OPTINFO.DAT" "SCFOUT.LOG" "fort.13" "fort.34" "fort.98" "fort.20"
        # TODO but how to do it quickly or in the background?
        # NB recursive copying of folders is not supported :(
        if remove:
            self.ssh_conn_pool[ip].run('rm -rf %s' % work_folder, hide=True)


if __name__ == "__main__":

    config = ConfigParser()
    config.read('env.ini')

    yac = Yascheduler(config)
    yac.ssh_connect()

    while True:
        nodes = yac.queue_get_all_nodes()
        if sorted(yac.ssh_conn_pool.keys()) != sorted(nodes):
            yac.ssh_connect()

        tasks_running = yac.queue_get_tasks_running()
        logging.info('tasks_running: %s' % tasks_running)
        for task in tasks_running:
            if not yac.ssh_check_task(task['ip']):
                ready_task = yac.queue_get_task(task['task_id'])
                ready_task['metadata']['store_folder'] = yac.config.get('local', 'data_dir') + '/' + ready_task['metadata']['work_folder'].split('/')[-1]
                os.mkdir(ready_task['metadata']['store_folder']) # TODO OSError if restart
                try:
                    yac.ssh_get_task(ready_task['ip'], ready_task['metadata']['work_folder'], ready_task['metadata']['store_folder'])
                except IOError as err:
                    logging.error('SSH download error: %s' % err)
                    # TODO handle that situation properly, re-spawn, etc.
                    ready_task['metadata'] = dict(error='No remote data!')
                yac.queue_set_task_done(ready_task['task_id'], ready_task['metadata'])
                logging.info(':::%s done and saved in %s' % (ready_task['label'], ready_task['metadata'].get('store_folder')))
                # TODO here we might want to notify our data consumers in an event-driven manner
                # TODO but how to do it quickly or in the background?

        if len(tasks_running) < len(nodes):
            for task in yac.queue_get_tasks_to_do(len(nodes) - len(tasks_running)):
                logging.info(':::to do: %s' % task['label'])
                ip = random.choice(yac.queue_get_free_nodes(nodes, tasks_running))
                try:
                    yac.ssh_run_task(ip, task['label'], task['metadata'])
                except Exception as err:
                    logging.error('SSH spawn cmd error: %s' % err)
                    # TODO handle that situation properly, re-assign ip, etc.
                    continue
                yac.queue_set_task_running(task['task_id'], ip)
                tasks_running.append(dict(task_id=task['task_id'], ip=ip))

        time.sleep(sleep_interval)