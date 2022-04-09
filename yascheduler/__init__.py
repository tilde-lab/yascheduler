
from .variables import (
    __version__,
    CONFIG_FILE, LOG_FILE, PID_FILE,
    SLEEP_INTERVAL, N_IDLE_PASSES, DEFAULT_NODES_PER_PROVIDER
)


def connect_db(config):
    import pg8000

    connection = pg8000.connect(
        user=config.get('db', 'user'),
        password=config.get('db', 'password'),
        database=config.get('db', 'database'),
        host=config.get('db', 'host'),
        port=config.getint('db', 'port')
    )
    cursor = connection.cursor()
    return connection, cursor


def has_node(config, ip):
    connection, cursor = connect_db(config)
    cursor.execute('SELECT * FROM yascheduler_nodes WHERE ip=%s;', [ip])
    result = cursor.fetchall()
    connection.close()
    return result


def add_node(config, ip, ncpus=None, cloud=None, provisioned=False):
    connection, cursor = connect_db(config)
    if cloud and provisioned:
        cursor.execute("""DELETE FROM yascheduler_nodes WHERE ip IN (
            SELECT ip FROM yascheduler_nodes WHERE ip LIKE 'prov' || '%%' AND cloud='{}' LIMIT 1
        );""".format(cloud))
    cursor.execute('INSERT INTO yascheduler_nodes (ip, ncpus, cloud) VALUES (%s, %s, %s);', [ip, ncpus, cloud])
    connection.commit()
    connection.close()
    return True


def remove_node(config, ip):
    connection, cursor = connect_db(config)
    cursor.execute('DELETE FROM yascheduler_nodes WHERE ip=%s;', [ip])
    connection.commit()
    connection.close()
    return True
