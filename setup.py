"""
A setuptools script for yet another scheduler
"""

import atexit
import os
import sys
import shutil
import stat
import json
from setuptools import setup, find_packages
from setuptools.command.install import install

from yascheduler import __version__, CONFIG_FILE


package_name = "yascheduler"  # NB must be the same in setup.json


class CustomInstall(install):
    def run(self):
        def _post_install():
            def find_module_path():
                for p in sys.path:
                    if os.path.isdir(p) and package_name in os.listdir(p):
                        return os.path.join(p, package_name)

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
            except Exception:
                pass

        atexit.register(_post_install)
        install.run(self)


if __name__ == "__main__":

    with open("setup.json", "r") as info:
        kwargs = json.load(info)

    with open("requirements.txt") as f:
        requirements = f.read().splitlines()

    setup(
        version=__version__,
        packages=find_packages(),
        install_requires=requirements,
        cmdclass={"install": CustomInstall},
        **kwargs
    )
