import os
import textwrap

import pytest

from fastpurge import FastPurgeError
from fastpurge._client import get_auth_dict

# pylint: disable=unused-argument


@pytest.fixture
def tmp_home(tmpdir):
    """Temporarily overrides HOME to point to pytest's tmpdir."""
    old_home = os.environ.get('HOME')
    os.environ['HOME'] = str(tmpdir)
    yield tmpdir

    if old_home is not None:
        os.environ['HOME'] = old_home


def test_auth_from_dict():
    """get_auth_dict, if given a dict, returns the input."""

    some_dict = {'foo': 'bar'}
    result = get_auth_dict(some_dict)

    # It should return exactly the input value.
    assert result is some_dict


def test_auth_bad_type():
    """get_auth_dict, if given an unsupported type, raises TypeError."""

    try:
        get_auth_dict(42)
        raise AssertionError("Should have raised!")
    except TypeError:
        pass


def test_auth_missing_settings(tmp_home):
    """get_auth_dict raises FastPurgeError if config file is needed and missing."""

    try:
        get_auth_dict(None)
        raise AssertionError("Was expected to raise!")
    except FastPurgeError as error:
        assert 'Missing configuration file' in str(error)


def test_auth_from_home_edgerc(tmp_home):
    """get_auth_dict reads config from ~/.edgerc by default."""

    tmp_home.join('.edgerc').write(textwrap.dedent("""
        [default]
        client_secret = some-secret
        host = some-host
        access_token = some-access-token
        client_token = some-client-token
        other_value = irrelevant
    """))

    result = get_auth_dict(None)

    assert result == {
        'client_secret': 'some-secret',
        'host': 'some-host',
        'access_token': 'some-access-token',
        'client_token': 'some-client-token',
    }


def test_auth_from_custom_edgerc(tmpdir):
    """get_auth_dict reads config from edgerc at the given path, if provided a string."""

    tmpdir.join('some-file').write(textwrap.dedent("""
        [default]
        client_secret = some-secret
        host = some-host
        access_token = some-access-token
        client_token = some-client-token
        other_value = irrelevant
    """))

    result = get_auth_dict(str(tmpdir.join('some-file')))

    assert result == {
        'client_secret': 'some-secret',
        'host': 'some-host',
        'access_token': 'some-access-token',
        'client_token': 'some-client-token',
    }
