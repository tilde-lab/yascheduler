"""
A setuptools script for yet another scheduler
"""

import atexit
import json
import os
import shutil
import stat
import sys

from setuptools import find_packages, setup
from setuptools.command.install import install

PACKAGE_NAME = "yascheduler"  # NB must be the same in setup.json


class CustomInstall(install):
    """Custom install"""

    def run(self):
        # workaround https://github.com/python/cpython/issues/86813
        from concurrent.futures import ThreadPoolExecutor  # noqa: F401

        def _post_install():
            from yascheduler.variables import CONFIG_FILE

            def find_module_path():
                for path in sys.path:
                    if os.path.isdir(path) and PACKAGE_NAME in os.listdir(path):
                        return os.path.join(path, PACKAGE_NAME)
                return PACKAGE_NAME

            install_path = find_module_path()
            src_config = os.path.join(install_path, "data/yascheduler.conf")

            # create config file in /etc if absent
            if not os.path.isfile(CONFIG_FILE):
                config_dir = os.path.dirname(CONFIG_FILE)
                os.makedirs(config_dir)
                shutil.copy(src_config, CONFIG_FILE)

            # chmod and link executable
            try:
                target = os.path.join(install_path, "scheduler.py")
                os.chmod(target, os.stat(target).st_mode | stat.S_IEXEC)
                os.symlink(target, "/usr/bin/yascheduler")
            except OSError:
                pass

        atexit.register(_post_install)
        install.run(self)


if __name__ == "__main__":

    with open("setup.json", encoding="utf-8") as info:
        kwargs = json.load(info)

    with open("requirements.txt", encoding="utf-8") as f:
        requirements = f.read().splitlines()

    setup(
        packages=find_packages(),
        install_requires=requirements,
        cmdclass={"install": CustomInstall},
        **kwargs,
    )
