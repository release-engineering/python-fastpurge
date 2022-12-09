python-fastpurge
================

A Python client for the [Akamai Fast Purge API](https://developer.akamai.com/api/core_features/fast_purge/v3.html).

[![Build Status](https://github.com/release-engineering/python-fastpurge/actions/workflows/tox-test.yml/badge.svg)](https://github.com/release-engineering/python-fastpurge/actions/workflows/tox-test.yml)
[![codecov](https://codecov.io/gh/release-engineering/python-fastpurge/branch/master/graph/badge.svg?token=cRnzaGyvkk)](https://codecov.io/gh/release-engineering/python-fastpurge)
[![Maintainability](https://api.codeclimate.com/v1/badges/2a5d60f6ddb557d88055/maintainability)](https://codeclimate.com/github/release-engineering/python-fastpurge/maintainability)

- [Source](https://github.com/release-engineering/python-fastpurge)
- [Documentation](https://release-engineering.github.io/python-fastpurge/)
- [PyPI](https://pypi.org/project/fastpurge)

This library provides a simple asynchronous Python wrapper for the Fast Purge
API. Features include:

- convenient handling of authentication
- recovery from errors
- splitting large requests into smaller pieces


Installation
------------

Install the `fastpurge` package from PyPI.

```
pip install fastpurge
```

Usage Example
-------------

Assuming a valid `~/.edgerc` file prepared with credentials according to
Akamai's documentation:

```python
from fastpurge import FastPurgeClient

# Omit credentials to read from ~/.edgerc
client = FastPurgeClient()

# Start purge of some URLs
purged = client.purge_by_url(['https://example.com/resource1', 'https://example.com/resource2'])

# purged is a Future, if we want to ensure purge completed
# we can block on the result:
result = purged.result()
print("Purge completed:", result)
```

License
-------

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
