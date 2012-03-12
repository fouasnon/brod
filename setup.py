#!/usr/bin/env python

import setuptools

setuptools.setup(
    name = 'brod',
    version = '0.3.1',
    license = 'MIT',
    description = open('README.md').read(),
    author = "Datadog, Inc.",
    author_email = "packages@datadoghq.com",
    url = 'https://github.com/datadog/brod',
    platforms = 'any',
    packages = ['brod'],
    zip_safe = True,
    verbose = False,
    install_requires = ["zc.zk==0.7.0",
                        "zc-zookeeper-static==3.3.4.0"],
    entry_points={
        'console_scripts': [
            'broderate = brod.util:broderate'
        ]
    }
)
