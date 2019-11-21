"""
A setuptools script for yet another scheduler
"""

from setuptools import setup, find_packages


if __name__ == '__main__':
    setup(
        name="Yascheduler",
        version="0.0.1",
        author="Eugeny Blokhin",
        author_email="eb@tilde.pro",
        description="Yet another scheduler",
        long_description=open('README.rst').read(),
        license="MIT",
        packages=find_packages())
