from setuptools import setup, find_packages

setup(
    name='causalnlica',
    version='0.1.0',
    packages=find_packages()+find_packages(where='./src')+find_packages(where='./pytorch_flows')
)