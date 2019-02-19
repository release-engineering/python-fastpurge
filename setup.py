from setuptools import setup


def get_description():
    return 'A client for the Akamai Fast Purge API'


def get_long_description():
    with open('README.md') as f:
        text = f.read()

    # Long description is everything after README's initial heading
    idx = text.find('\n\n')
    return text[idx:]


def get_requirements():
    with open('requirements.txt') as f:
        return f.read().splitlines()


setup(
    name='fastpurge',
    version='1.0.1',
    author='',
    author_email='',
    packages=['fastpurge'],
    url='https://github.com/release-engineering/python-fastpurge',
    license='GNU General Public License',
    description=get_description(),
    long_description=get_long_description(),
    long_description_content_type='text/markdown',
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)',
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.6',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],
    install_requires=get_requirements(),
)
