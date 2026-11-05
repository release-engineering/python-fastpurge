import pytest
import requests
import requests_mock
import mock
import responses

try:
    from time import monotonic
except ImportError:
    from monotonic import monotonic

from fastpurge import FastPurgeClient, FastPurgeError
from fastpurge._client import compute_backoff, parse_retry_statuses

# pylint: disable=unused-argument

# NOTE: FastPurgeClient class attributes (MAX_RETRIES, RETRY_BACKOFF, etc.) are
# populated from environment variables once at import time, so monkeypatch.setenv
# has no effect on tests -- the class is already defined. Use
# monkeypatch.setattr(FastPurgeClient, "ATTR", value) instead, which is what the
# corresponding env var would have set had it been present before import.


@pytest.fixture
def client_auth():
    """Returns auth dict appropriate for passing to FastPurgeClient."""

    return dict(host='fastpurge.example.com',
                client_secret='some-secret',
                access_token='some-access-token',
                client_token='some-client-token')


@pytest.fixture
def client(client_auth):
    """Returns a FastPurgeClient and closes the client after the test."""

    with FastPurgeClient(auth=client_auth) as out:
        yield out


@pytest.fixture
def requests_mocker():
    with requests_mock.Mocker() as m:
        yield m


@pytest.fixture
def no_thread_retries():
    """Suppress retries for the duration of this fixture."""

    with mock.patch('more_executors.retry.ExceptionRetryPolicy') as policy_class:
        policy = policy_class.return_value
        policy.should_retry.return_value = False
        policy.sleep_time.return_value = 0.1
        yield


def test_purge_by_url(client, requests_mocker):
    """Deleting by URL succeeds with expected request and response."""

    seconds = 0.1
    response = {'some': ['return', 'value'], 'estimatedSeconds': seconds}
    requests_mocker.register_uri(
        method='POST',
        url='https://fastpurge.example.com/ccu/v3/delete/url/production',
        status_code=201,
        json=response)

    time_before_purge = monotonic()
    future = client.purge_by_url(["https://example.com/some-content"])

    # It should have succeeded
    result = future.result()

    # It should have returned just one response (for just one request)
    assert len(result) == 1

    # It should have waited at least for the estimatedSeconds to elapse
    time_after_purge = monotonic()
    assert time_after_purge - time_before_purge >= seconds

    # It should have returned whatever the API returned
    assert result[0] == response

    history = requests_mocker.request_history

    # There should have been a single purge request
    assert len(history) == 1

    request = history[0]

    # It should have used the edgegrid authentication scheme.
    # This is a basic sanity check only, since the authentication
    # is implemented by a separate library with its own tests.
    auth = request.headers['Authorization']
    assert auth.startswith('EG1-HMAC-SHA256 ')
    assert 'client_token=some-client-token' in auth

    # It should have requested purge of the given URL
    assert request.json() == {'objects': ["https://example.com/some-content"]}


def test_purge_by_tag(client, requests_mocker):
    """Invalidating by tag succeeds with expected request and response."""

    seconds = 0.1
    response = {'some': ['return', 'value'], 'estimatedSeconds': seconds}
    requests_mocker.register_uri(
        method='POST',
        url='https://fastpurge.example.com/ccu/v3/invalidate/tag/production',
        status_code=201,
        json=response)

    future = client.purge_by_tag(["red", "blue", "green"], purge_type='invalidate')

    # It should have succeeded
    assert future.result()

    history = requests_mocker.request_history

    # There should have been a single request, to purge the requested tags
    assert len(history) == 1
    assert history[0].json() == {'objects': ['red', 'blue', 'green']}


def test_scheme_port(client_auth, requests_mocker):
    """The client makes requests using the requested scheme and port."""

    client = FastPurgeClient(auth=client_auth, scheme='http', port=42)
    response = {'some': ['return', 'value'], 'estimatedSeconds': 0.1}

    requests_mocker.register_uri(
        method='POST',
        url='http://fastpurge.example.com:42/ccu/v3/delete/tag/staging',
        status_code=201,
        json=response)

    future = client.purge_by_tag(['red'], network='staging')
    assert future.result()


@responses.activate
def test_response_fails(client,  no_thread_retries, monkeypatch):
    """Requests fail with a FastPurgeError if API gives unsuccessful response."""
    url = 'https://fastpurge.example.com/ccu/v3/delete/cpcode/production'
    # Decrease backoff to avoid a multi-minute test run.
    monkeypatch.setattr(FastPurgeClient, "RETRY_BACKOFF", 0.001)
    monkeypatch.setattr(FastPurgeClient, "RETRY_BACKOFF_JITTER", 0)

    responses.add(responses.POST, url, status=503,
                  content_type="application/json", body="Error")
    future = client.purge_by_cpcode([1234, 5678])
    exception = future.exception()

    assert isinstance(exception, FastPurgeError)
    assert 'too many 503 error responses' in str(exception)


def test_split_requests(client, requests_mocker):
    """If a request is made to purge too many objects at once, the request is split into
    several smaller requests.
    """

    # Set extremely low max payload to enforce splitting up requests
    client.MAX_PAYLOAD = 80

    urls = ['https://example.com/%d' % i
            for i in range(0, 7)]

    requests_mocker.register_uri(
        method='POST',
        url='https://fastpurge.example.com/ccu/v3/delete/url/production',
        status_code=201,
        json={'estimatedSeconds': 0.1})

    future = client.purge_by_url(urls)

    # It should succeed
    result = future.result()

    # It should have split this into 4 requests (hence 4 responses)
    assert len(result) == 4

    history = requests_mocker.request_history

    # There should have been one API request per future
    assert len(history) == len(result)

    # Collect all the objects across every API request
    all_objects = []
    for request in history:
        # The request body should not exceed the max payload size
        assert len(request.text) <= client.MAX_PAYLOAD

        objects = request.json().get('objects')

        # There should have been at least one object in the request
        assert objects

        all_objects.extend(objects)

    # The total set of objects sent to the API should be equal to that
    # provided to the client
    assert sorted(all_objects) == sorted(urls)


def test_multiple_clients_with_the_same_auth_dict(client_auth):
    """Constructing multiple clients with the same auth dict instance should be allowed."""
    client1 = FastPurgeClient(auth=client_auth)
    client2 = FastPurgeClient(auth=client_auth)

    assert client1 is not client2


@responses.activate(registry=responses.registries.OrderedRegistry)
def test_retries_on_error(client_auth):
    """Sanity check for the retry functionality"""
    url = 'http://fastpurge.example.com:42/ccu/v3/delete/tag/staging'
    err_1 = responses.add(responses.POST, url, status=500,
                          content_type="application/json", body="Error")
    err_2 = responses.add(responses.POST, url, status=501,
                          content_type="application/json", body="Error")
    res = responses.add(responses.POST, url, status=201,
                        content_type="application/json",
                        json={'estimatedSeconds': 0.1})

    client = FastPurgeClient(auth=client_auth, scheme='http', port=42)

    future = client.purge_by_tag(['red'], network='staging')

    assert future.result()
    assert len(err_1.calls) == 1
    assert len(err_2.calls) == 1
    assert len(res.calls) == 1


@responses.activate(registry=responses.registries.OrderedRegistry)
def test_resigns_each_retry(client_auth, monkeypatch):
    """Each retry is re-signed, so the EdgeGrid Authorization header differs
    between attempts. (urllib3-level retries reused the original signature,
    which failed with "Invalid timestamp" past Akamai's ~30s window.)"""
    url = 'http://fastpurge.example.com:42/ccu/v3/delete/tag/staging'
    responses.add(responses.POST, url, status=429, body="Rate limited")
    responses.add(responses.POST, url, status=429, body="Rate limited")
    responses.add(responses.POST, url, status=201,
                  content_type="application/json",
                  json={'estimatedSeconds': 0.1})

    # No real delay between attempts.
    monkeypatch.setattr(FastPurgeClient, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(FastPurgeClient, "RETRY_BACKOFF_JITTER", 0)

    client = FastPurgeClient(auth=client_auth, scheme='http', port=42)

    assert client.purge_by_tag(['red'], network='staging').result()

    auth_headers = [call.request.headers['Authorization']
                    for call in responses.calls]
    assert len(auth_headers) == 3
    # A fresh signature (new timestamp + nonce) per attempt => all distinct.
    assert len(set(auth_headers)) == 3


@responses.activate
def test_does_not_retry_auth_errors(client_auth):
    """401/403 are not retried -- bad credentials/permissions won't self-correct."""
    url = 'http://fastpurge.example.com:42/ccu/v3/delete/tag/staging'
    responses.add(responses.POST, url, status=401, body="Unauthorized")

    client = FastPurgeClient(auth=client_auth, scheme='http', port=42)
    exception = client.purge_by_tag(['red'], network='staging').exception()

    assert isinstance(exception, FastPurgeError)
    assert 'non-retryable status 401' in str(exception)
    # Failed immediately, no retries.
    assert len(responses.calls) == 1


@responses.activate(registry=responses.registries.OrderedRegistry)
def test_retries_connection_errors(client_auth, monkeypatch):
    """Connection/network errors are retried, not just HTTP statuses."""
    url = 'http://fastpurge.example.com:42/ccu/v3/delete/tag/staging'
    responses.add(responses.POST, url,
                  body=requests.exceptions.ConnectionError("boom"))
    responses.add(responses.POST, url, status=201,
                  content_type="application/json",
                  json={'estimatedSeconds': 0.1})

    monkeypatch.setattr(FastPurgeClient, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(FastPurgeClient, "RETRY_BACKOFF_JITTER", 0)

    client = FastPurgeClient(auth=client_auth, scheme='http', port=42)

    assert client.purge_by_tag(['red'], network='staging').result()
    assert len(responses.calls) == 2


@responses.activate
def test_accepts_non_json_201(client_auth):
    """A 201 with a non-JSON body is still treated as an accepted purge
    (robust to an unexpected response-shape change), using the default estimate."""
    url = 'http://fastpurge.example.com:42/ccu/v3/delete/tag/staging'
    responses.add(responses.POST, url, status=201, body="OK")  # not JSON

    client = FastPurgeClient(auth=client_auth, scheme='http', port=42)
    result = client.purge_by_tag(['red'], network='staging').result()

    assert result == [{}]  # succeeded, with empty metadata


def test_default_retry_statuses():
    """Default allowlist: 429, 400 (EdgeGrid timestamp/WAF) and any 5xx are
    retryable; other client errors are not."""
    for status in (400, 429, 500, 501, 502, 503, 504, 599):
        assert status in FastPurgeClient.RETRY_STATUSES, status
    for status in (201, 401, 403, 404, 405, 413, 422):
        assert status not in FastPurgeClient.RETRY_STATUSES, status


def test_parse_retry_statuses():
    """FAST_PURGE_RETRY_STATUSES parsing: single codes, inclusive ranges, and
    tolerant of whitespace / empty items."""
    assert parse_retry_statuses("429") == frozenset((429,))
    assert parse_retry_statuses("400,429") == frozenset((400, 429))
    assert parse_retry_statuses("500-504") == frozenset(range(500, 505))
    assert parse_retry_statuses(" 429 , 500-599 ") == frozenset((429,)) | frozenset(range(500, 600))
    assert parse_retry_statuses("") == frozenset()


@responses.activate
def test_retry_statuses_is_configurable(client_auth, monkeypatch):
    """A status removed from RETRY_STATUSES is no longer retried (fails fast),
    confirming the allowlist drives behavior."""
    url = 'http://fastpurge.example.com:42/ccu/v3/delete/tag/staging'
    responses.add(responses.POST, url, status=503, body="Error")

    # Narrow the allowlist so 503 is NOT retryable.
    monkeypatch.setattr(FastPurgeClient, "RETRY_STATUSES", frozenset((429,)))

    client = FastPurgeClient(auth=client_auth, scheme='http', port=42)
    exception = client.purge_by_tag(['red'], network='staging').exception()

    assert isinstance(exception, FastPurgeError)
    assert 'non-retryable status 503' in str(exception)
    assert len(responses.calls) == 1  # failed immediately, no retries


def test_compute_backoff_matches_urllib3_schedule():
    """No jitter: first retry 0, then factor*2**(n-1), capped at backoff_max."""
    delays = [compute_backoff(n, 1.5, 9, 0) for n in range(1, 6)]
    assert delays == [0.0, 3.0, 6.0, 9.0, 9.0]  # 3, 6, 12->9, 24->9

    # Jitter adds up to `jitter` seconds (here forced to its max) before the cap.
    assert compute_backoff(2, 1.5, 100, 2.0, _rand=lambda: 1.0) == 3.0 + 2.0
    # Neither the initial attempt (0) nor the first retry (1) ever sleeps,
    # regardless of jitter.
    assert compute_backoff(0, 1.5, 100, 2.0, _rand=lambda: 1.0) == 0.0
    assert compute_backoff(1, 1.5, 100, 2.0, _rand=lambda: 1.0) == 0.0


@responses.activate
def test_honors_max_retries(client_auth, monkeypatch):
    """A persistently failing status is attempted exactly MAX_RETRIES + 1 times."""
    url = 'http://fastpurge.example.com:42/ccu/v3/delete/tag/staging'
    responses.add(responses.POST, url, status=503, body="Error")

    monkeypatch.setattr(FastPurgeClient, "MAX_RETRIES", 2)
    monkeypatch.setattr(FastPurgeClient, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(FastPurgeClient, "RETRY_BACKOFF_JITTER", 0)

    client = FastPurgeClient(auth=client_auth, scheme='http', port=42)
    exception = client.purge_by_tag(['red'], network='staging').exception()

    assert isinstance(exception, FastPurgeError)
    assert len(responses.calls) == 3  # 1 initial attempt + 2 retries
    assert 'too many 503 error responses' in str(exception)


@responses.activate
def test_connection_error_exhaustion(client_auth, monkeypatch):
    """When every attempt hits a connection error, a FastPurgeError is raised
    reporting the underlying error (not a 'too many N' status message)."""
    url = 'http://fastpurge.example.com:42/ccu/v3/delete/tag/staging'
    responses.add(responses.POST, url,
                  body=requests.exceptions.ConnectionError("boom"))

    monkeypatch.setattr(FastPurgeClient, "MAX_RETRIES", 2)
    monkeypatch.setattr(FastPurgeClient, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(FastPurgeClient, "RETRY_BACKOFF_JITTER", 0)

    client = FastPurgeClient(auth=client_auth, scheme='http', port=42)
    exception = client.purge_by_tag(['red'], network='staging').exception()

    assert isinstance(exception, FastPurgeError)
    assert len(responses.calls) == 3  # 1 initial attempt + 2 retries
    assert 'boom' in str(exception)
