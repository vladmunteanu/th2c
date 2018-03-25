#!/usr/bin/env python
# -*- coding: utf-8 -*-
import codecs
import re

from setuptools import setup

# Get the version (stole this from h2)
version_regex = r'__version__ = ["\']([^"\']*)["\']'
with open('th2c/__init__.py', 'r') as f:
    text = f.read()
    match = re.search(version_regex, text)

    version = match.group(1)

readme = codecs.open('README.rst', encoding='utf-8').read()

setup(
    name='th2c',
    version=version,
    description='HTTP/2 Async Client written with h2, for tornado',
    long_description=readme,
    author='Vlad Stefan Munteanu',
    author_email='vladstefanmunteanu@gmail.com',
    url='https://github.com/vladmunteanu/th2c',
    packages=['th2c'],
    package_data={'': ['LICENSE', 'README.rst']},
    package_dir={'th2c': 'th2c'},
    include_package_data=True,
    license='MIT License',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: Implementation :: CPython',
    ],
    install_requires=[
        'tornado>=4.2',
        'h2==3.0.1',
    ]
)
