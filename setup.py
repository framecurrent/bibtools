#! /usr/bin/env python
# Copyright 2014 Peter Williams <peter@newton.cx>
# Licensed under the GNU General Public License version 3 or higher

# I don't use the ez_setup module because it causes us to automatically build
# and install a new setuptools module, which I'm not interested in doing.

from setuptools import setup

setup (
    name = 'bibtools',
    version = '0.1',

    zip_safe = True,
    packages = ['bibtools', 'bibtools.hacked_bibtexparser'],

    # install_requires = ['docutils >= 0.3'],

    package_data = {
        'bibtools': ['*.sql', 'apj-issnmap.txt', 'defaults.cfg'],
    },

    entry_points = {
        'console_scripts': ['bib = bibtools.cli:driver'],
    },

    author = 'Peter Williams',
    author_email = 'peter@newton.cx',
    description = 'Command-line bibliography manager',
    license = 'GPLv3',
    keywords = 'bibliography',
    url = 'https://github.com/pkgw/bibtools/',
)
