
import warnings


def init_engines(config):

    engines = {}

    for section in config.sections():
        if not section.startswith('engine.'):
            continue

        name = section[7:]

        engines[name] = dict(config[section])

        assert 'spawn' in engines[name]
        assert 'check' in engines[name]
        assert 'run_marker' in engines[name]
        assert engines[name]['spawn'].startswith('nohup')
        assert 'input_files' in engines[name] and engines[name]['input_files']
        engines[name]['input_files'] = [x.strip() for x in filter(None, engines[name]['input_files'].split())]
        assert 'output_files' in engines[name] and engines[name]['output_files']
        engines[name]['output_files'] = [x.strip() for x in filter(None, engines[name]['output_files'].split())]

        # TODO own sleep_interval

        if 'deployable' not in engines[name] or not len(engines[name]['deployable'].strip()):
            warnings.warn('Engine %s has no *deployable* config set, cloud usage is impossible' % name)

    if not engines:
        raise RuntimeError('No engines were set up')

    return engines


def get_engines_check_cmd(engines):

    cmd = []

    for name in engines:
        cmd.append(engines[name]['check'].strip())

    return ' && '.join(cmd)