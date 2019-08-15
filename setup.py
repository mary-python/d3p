from __future__ import absolute_import, division, print_function

import os
import sys

from setuptools import find_packages, setup

PROJECT_PATH = os.path.dirname(os.path.abspath(__file__))

setup(
    name='dppp',
    version='0.1.0',
    description='Differentially-Private Probabilistic Programming',
    packages=find_packages(include=['dppp', 'dppp.*']),
    author='PADS @ Helsinki University and Aalto University',
    install_requires=[
        'numpyro @ git+https://github.com/pyro-ppl/numpyro.git@6d9af73380fae3b6217c68601e7a979eb708494a#egg=numpyro'
    ],
    extras_require={
        'examples': ['matplotlib'],
    },
    long_description="",
    long_description_content_type='text/markdown',
    tests_require=[],
    keywords='probabilistic machine learning bayesian statistics differential-privacy',
    classifiers=[
        'Intended Audience :: Developers',
        'Intended Audience :: Education',
        'Intended Audience :: Science/Research',
        'Operating System :: POSIX :: Linux',
        'Operating System :: MacOS :: MacOS X',
        'Programming Language :: Python :: 3.7',
    ],
)
