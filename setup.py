from setuptools import setup, find_packages
import os


def get_version(fname=os.path.join('gnn', '__init__.py')):
    with open(fname) as fin:
        for line in fin:
            line = line.strip()
            if '__version__' in line:
                v = line.split('=')[1]
                if "'" in v:
                    version = v.strip("' ")
                elif '"' in v:
                    version = v.strip('" ')
                break
    return version


setup(
    name='gnn',
    version=get_version(),
    packages=find_packages(),
    install_requires=['scipy', 'pytest'],
    author='Mingjian Wen',
    author_email='wenxx151@gmail.com',
    url='https://github.com/mjwen',
    description='short description',
    long_description='long description',
    classifiers=[
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'License :: OSI Approved :: Common Development and Distribution License 1.0 (CDDL-1.0)',
        'Operating System :: OS Independent',
    ],
    zip_safe=False,
)