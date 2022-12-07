import pytest
import requests_mock
import mock

try:
    from time import monotonic
except ImportError:
    from monotonic import monotonic

from fastpurge import FastPurgeClient, FastPurgeError

# pylint: disable=unused-argument


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
def no_retries():
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


def test_response_fails(client, requests_mocker, no_retries):
    """Requests fail with a FastPurgeError if API gives unsuccessful response."""

    requests_mocker.register_uri(
        method='POST',
        url='https://fastpurge.example.com/ccu/v3/delete/cpcode/production',
        status_code=503,
        reason='simulated internal error')

    future = client.purge_by_cpcode([1234, 5678])
    exception = future.exception()

    assert isinstance(exception, FastPurgeError)
    assert '503 simulated internal error' in str(exception)


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
