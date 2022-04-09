"""
A setuptools script for yet another scheduler
"""

import atexit
import os
import sys
import shutil
from setuptools import setup, find_packages
from setuptools.command.install import install
from yascheduler import __version__, CONFIG_FILE

package_name = "yascheduler"
package_version = __version__


class CustomInstall(install):
    def run(self):
        def _post_install():
            def find_module_path():
                for p in sys.path:
                    if os.path.isdir(p) and package_name in os.listdir(p):
                        return os.path.join(p, package_name)
            install_path = find_module_path()
            src_config = os.path.join(install_path, 'data/yascheduler.conf')
            # create config file in /etc if absent
            if not os.path.isfile(CONFIG_FILE):
                config_dir = os.path.dirname(CONFIG_FILE)
                os.makedirs(config_dir)
                shutil.copy(src_config, CONFIG_FILE)

            # link executable
            try: os.symlink(os.path.join(install_path, 'scheduler.py'), '/usr/bin/yascheduler')
            except Exception: pass

        atexit.register(_post_install)
        install.run(self)


if __name__ == '__main__':

    with open('requirements.txt') as f:
        requirements = f.read().splitlines()

    setup(
        name=package_name,
        version=package_version,
        author="Evgeny Blokhin",
        author_email="eb@tilde.pro",
        description="Yet another computing scheduler and cloud orchestration engine",
        long_description="*Yascheduler* is a simple job scheduler designed for submitting scientific simulations and copying back their results from the computing clouds.",
        license="MIT",
        packages=find_packages(),
        package_data={'yascheduler': ['data/*']},
        install_requires=requirements,
        entry_points={
            "console_scripts": [
                "yasubmit = yascheduler.utils:submit",
                "yastatus = yascheduler.utils:check_status",
                "yanodes = yascheduler.utils:show_nodes",
                "yasetnode = yascheduler.utils:manage_node",
                "yainit = yascheduler.utils:init"
            ],
            "aiida.schedulers": [
                "yascheduler = yascheduler.aiida_plugin:YaScheduler",
            ],
        },
        cmdclass={'install': CustomInstall},
        python_requires='>=3.5',
        classifiers=[
            'Development Status :: 4 - Beta',
            'Intended Audience :: Science/Research',
            'Topic :: Scientific/Engineering :: Chemistry',
            'Topic :: Scientific/Engineering :: Physics',
            'Topic :: Scientific/Engineering :: Information Analysis',
            'Topic :: Software Development :: Libraries :: Python Modules',
            'License :: OSI Approved :: MIT License',
            'Programming Language :: Python :: 3',
            'Programming Language :: Python :: 3.5',
            'Programming Language :: Python :: 3.6',
            'Programming Language :: Python :: 3.7',
            'Programming Language :: Python :: 3.8',
            'Programming Language :: Python :: 3.9'
        ]
    )
