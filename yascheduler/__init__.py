
import pg8000

__version__ = "0.1.0"


CONFIG_FILE = '/etc/yascheduler/yascheduler.conf'
LOG_FILE = '/var/log/yascheduler.log'
PID_FILE = '/var/run/yascheduler.pid'
SLEEP_INTERVAL = 6
N_IDLE_PASSES = 6
DEFAULT_NODES_PER_PROVIDER = 10


def connect_db(config):
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
    cursor.execute('SELECT * from yascheduler_nodes WHERE ip=%s;', [ip])
    result = cursor.fetchall()
    connection.close()
    return result


def provision_node(config, cloud):
    connection, cursor = connect_db(config)
    cursor.execute("""INSERT INTO yascheduler_nodes (ip, enabled, cloud) VALUES (
        'prov' || SUBSTR(MD5(RANDOM()::TEXT), 0, 11),
        FALSE,
        %s
    );""", [cloud])
    connection.commit()
    connection.close()
    return True


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
    cursor.execute('DELETE from yascheduler_nodes WHERE ip=%s;', [ip])
    connection.commit()
    connection.close()
    return True
