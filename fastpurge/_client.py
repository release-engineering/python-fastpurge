import json
import logging
import os
import random
import time

from collections import namedtuple
from threading import local, Lock

import requests

try:
    from time import monotonic
except ImportError:  # pragma: no cover
    from monotonic import monotonic

from six import string_types
from six.moves.urllib.parse import urljoin

from akamai.edgegrid import EdgeGridAuth
from akamai.edgegrid.edgerc import EdgeRc
from more_executors import Executors
from more_executors.futures import f_sequence

LOG = logging.getLogger('fastpurge')


Purge = namedtuple('Purge', [
    # Response from Akamai when this purge was created
    'response_body',

    # estimated time for completion of a purge (in monotonic clock)
    'estimated_complete',
])


class FastPurgeError(RuntimeError):
    """Raised when the Fast Purge API reports an error.

    The exception message contains a summary of the error encountered."""


def compute_backoff(failures, backoff_factor, backoff_max, jitter,
                    _rand=random.random):
    """Delay (seconds) before retry number ``failures``.

    Mirrors urllib3's exponential backoff so the ``FAST_PURGE_RETRY_*``
    tunables keep their existing meaning: the first retry (``failures == 1``)
    has no delay, then ``backoff_factor * 2 ** (failures - 1)`` plus up to
    ``jitter`` seconds of random jitter, capped at ``backoff_max``.
    """
    if failures <= 1:
        return 0.0
    delay = backoff_factor * (2 ** (failures - 1))
    if jitter:
        delay += _rand() * jitter
    return min(backoff_max, delay)


def parse_retry_statuses(spec):
    """Parse a FAST_PURGE_RETRY_STATUSES spec into a frozenset of ints.

    ``spec`` is a comma-separated list where each item is either a single
    status code (``"429"``) or an inclusive range (``"500-599"``). Whitespace
    and empty items are ignored.
    """
    statuses = set()
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            low, high = item.split("-", 1)
            statuses.update(range(int(low), int(high) + 1))
        else:
            statuses.add(int(item))
    return frozenset(statuses)


class FastPurgeClient(object):
    """A client for the Akamai Fast Purge API.

    This class provides methods for purging content asynchronously.

    Purge operations are represented by :class:`~concurrent.futures.Future` objects
    which are resolved when the purge completes, or raise an exception when the
    purge fails.

    Though not mandatory, instances of this class may be used as a context manager, as in example:

    >>> with FastPurgeClient() as client:
    >>>     client.purge_urls(urls).result()

    Resources will be reclaimed when the context is exited, and any outstanding purge requests
    will be cancelled or abandoned.
    """

    # The below constants are not expected to require any changes for
    # different uses, but (undocumented) environment variables are
    # provided as an escape hatch in case something goes wrong, or for
    # testing.

    # Max request body size according to
    # https://developer.akamai.com/api/core_features/fast_purge/v3.html
    # (actually we set a little bit less than Akamai's limit, in case
    # there's some overhead beyond our control)
    MAX_PAYLOAD = int(os.environ.get("FAST_PURGE_MAX_PAYLOAD", "45000"))

    # Max number of pending purges allowed per client, the client will
    # throttle purges once there are this many in progress
    MAX_REQUESTS = int(os.environ.get("FAST_PURGE_MAX_REQUESTS", "10"))

    # Default network matches Akamai's documented default
    DEFAULT_NETWORK = os.environ.get("FAST_PURGE_DEFAULT_NETWORK", "production")

    # Max number of retries allowed for HTTP requests, and the backoff used
    # to extend the delay between requests.
    MAX_RETRIES = int(os.environ.get("FAST_PURGE_MAX_RETRIES", "10"))

    RETRY_BACKOFF = float(os.environ.get("FAST_PURGE_RETRY_BACKOFF", "0.15"))
    RETRY_BACKOFF_MAX = float(os.environ.get("FAST_PURGE_RETRY_BACKOFF_MAX", "150"))
    RETRY_BACKOFF_JITTER = float(os.environ.get("FAST_PURGE_RETRY_JITTER", "2"))

    # HTTP statuses worth retrying, as an allowlist -- anything not listed
    # fails fast (e.g. 401/403/404/413 are not retried pointlessly). The
    # default covers rate limiting (429), all server errors (500-599), and
    # 400: Akamai returns 400 "Invalid timestamp" for a stale signature (fixed
    # by re-signing on retry) and for WAF rate-control, both of which recover
    # on a fresh attempt. Override with FAST_PURGE_RETRY_STATUSES, a
    # comma-separated list of codes and/or inclusive ranges, e.g. "429,500-599".
    RETRY_STATUSES = parse_retry_statuses(
        os.environ.get("FAST_PURGE_RETRY_STATUSES", "400,429,500-599"))

    # Default purge type.
    # Akamai recommend "invalidate", so why is "delete" our default?
    # Here's what Akamai docs have to say:
    #
    # > You should typically Invalidate (...)
    # > You should use Delete for compliance- or copyright-related purge requests,
    # > as it completely removes all content from servers
    #
    # Delete seems a safer default since it achieves the needed result for *all* kinds of purge
    # requests, at the cost of being more expensive than needed for most use-cases.
    #
    # If the default was instead the fast-but-unsafe "invalidate", every usage of this
    # client would have to provide some ability for the caller to pass in "delete" instead
    # in case a compliance/copyright use-case would arise. But the implementer of the calling
    # code might not even think about that until it's too late.
    DEFAULT_PURGE_TYPE = os.environ.get("FAST_PURGE_DEFAULT_TYPE", "delete")

    # API is expected to give some estimatedSeconds for a purge, but the field is documented
    # as optional. If it's missing, we'll use this as the estimate.
    DEFAULT_DELAY = int(os.environ.get("FAST_PURGE_DEFAULT_DELAY", "5"))

    def __init__(self, **kwargs):
        """
        Args:
            auth (str, dict):

                The following values may be provided:

                :class:`dict`:
                    Must contain all authentication components from `.edgerc`_:
                    host, client_token, client_secret, access_token.

                :class:`str`:
                    Must be a path to an `.edgerc`_ file to load.

                `None`:
                    Credentials are loaded from `~/.edgerc`.

            scheme (str): Alternative scheme for requests (e.g. 'http').

            port (int): Alternative port for requests.

        .. _.edgerc: https://developer.akamai.com/legacy/introduction/Conf_Client.html
        """
        self.__auth = get_auth_dict(kwargs.pop('auth', None))
        self.__host = self.__auth.pop('host')
        self.__local = local()
        self.__lock = Lock()
        self.___executor = None

        self.__scheme = kwargs.pop('scheme', 'https')
        self.__port = kwargs.pop('port', 443)

        # Test creation of a session now.
        # This verifies that the auth parameter is correct (can be accepted
        # by edgegrid library) before running.
        assert self.__session

    @property
    def __executor(self):
        if self.___executor is None:
            with self.__lock:
                if self.___executor is None:
                    self.___executor = Executors.\
                        sync(name="fastpurge").\
                        with_poll(self.__poll_purges).\
                        with_throttle(count=self.MAX_REQUESTS).\
                        with_cancel_on_shutdown()
        return self.___executor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.__executor.shutdown(wait=True)
        self.___executor = None

    @classmethod
    def __poll_purges(cls, descriptors):
        now = monotonic()
        earliest = now + cls.DEFAULT_DELAY

        for descriptor in descriptors:
            purge_id = descriptor.result.response_body.get('purgeId', '<unknown purge>')
            when_complete = descriptor.result.estimated_complete
            earliest = min(earliest, when_complete)
            if when_complete > now:
                LOG.debug("purge_id %s not expected to complete yet", purge_id)
                continue

            # In CCUv3, unlike CCUv2, Akamai does not provide any method to query the status
            # of a purge :(
            #
            # Here's what they say in the FAQ about this https://bit.ly/2BvDR5x
            #
            # > Q: How do I know my purge is done?
            # > A: Since purges are completed almost instantly, we do not notify users about
            #   this today. You can expect your purges to be done in <5secs.
            #
            # So we've little choice right now but to simply trust the estimatedSeconds,
            # hence we consider the purge as "done" as soon as estimatedSeconds has passed.
            # If/when Akamai realize they really do need to provide a way to verify that a
            # purge has completed, the code for checking the purge status would be injected
            # here.
            descriptor.yield_result(descriptor.result.response_body)

        LOG.debug("now %s, earliest %s, sleep %s", now, earliest, earliest - now)
        return min(earliest - now, cls.DEFAULT_DELAY)

    @property
    def __baseurl(self):
        out = '{scheme}://{host}'.format(
            scheme=self.__scheme, host=self.__host)

        default = (self.__scheme == 'http' and self.__port == 80)
        default = default or (self.__scheme == 'https' and self.__port == 443)

        if default:
            return out

        return '{out}:{port}'.format(out=out, port=self.__port)

    @property
    def __session(self):
        session = getattr(self.__local, 'session', None)
        if not session:
            session = requests.Session()
            session.auth = EdgeGridAuth(**self.__auth)
            # Deliberately no transport-level (urllib3) retries: those resend
            # the request below the requests auth layer, reusing the original
            # EdgeGrid signature, so any retry past Akamai's ~30s timestamp
            # window fails with "Invalid timestamp". Retries are handled in
            # __start_purge, which re-issues post() so each attempt is re-signed.
            self.__local.session = session
        return session

    def __backoff_time(self, failures):
        return compute_backoff(failures, self.RETRY_BACKOFF,
                               self.RETRY_BACKOFF_MAX,
                               self.RETRY_BACKOFF_JITTER)

    def __get_request_bodies(self, objects):
        out_object = {"objects": objects}
        out_str = json.dumps(out_object)

        if len(out_str) < self.MAX_PAYLOAD or len(objects) == 1:
            # No problem with this request body (or it can't be split further)
            return [out_str]

        # Too big for a single request.
        # Split it in half and try again
        part = int(len(objects)/2)
        objects_a, objects_b = objects[:part], objects[part:]
        return self.__get_request_bodies(objects_a) + self.__get_request_bodies(objects_b)

    def __start_purge(self, endpoint, request_body):
        headers = {'Content-Type': 'application/json'}
        LOG.debug("POST JSON of size %s to %s", len(request_body), endpoint)

        attempts = self.MAX_RETRIES + 1
        last_reason = "no attempts made"
        # One initial attempt plus up to MAX_RETRIES retries.
        for failures in range(0, attempts):
            delay = self.__backoff_time(failures)
            if delay:
                time.sleep(delay)

            try:
                # A fresh post() re-prepares the request, so EdgeGridAuth signs
                # it again with a current timestamp. That is what lets a retry
                # succeed after Akamai's ~30s signature window -- urllib3-level
                # retries resend the original request with its now-stale
                # signature and can never recover.
                response = self.__session.post(
                    endpoint, data=request_body, headers=headers)
            except requests.exceptions.RequestException as error:
                # Connection reset / timeout / etc: transient, always retryable.
                last_reason = str(error)
                LOG.error("An error occurred when trying to POST to %s: %s",
                          endpoint, error)
                continue

            if response.status_code == 201:
                # 201 is how Fast Purge acknowledges a queued async purge.
                return self.__purge_from_response(response)

            # Same message as before, expanded with Retry-After and the response
            # body so the real Akamai error (e.g. "Invalid timestamp") is
            # visible. .get()/.text are safe even if the API omits them.
            LOG.error(
                "An invalid status code %s was received when trying to POST "
                "to %s: %s (Retry-After=%s): %s",
                response.status_code, endpoint, response.reason,
                response.headers.get("Retry-After"), (response.text or "")[:500])

            if response.status_code not in self.RETRY_STATUSES:
                # Permanent error (bad credentials/permissions/request) that
                # won't self-correct -- fail fast instead of burning the window.
                message = ("Request to %s failed with non-retryable status %s"
                           % (endpoint, response.status_code))
                LOG.debug("%s", message)
                raise FastPurgeError(message)

            last_reason = "too many %s error responses" % response.status_code

        message = ("Request to {endpoint} was unsuccessful after {retries} "
                   "retries: {reason}").format(
                       endpoint=endpoint, retries=self.MAX_RETRIES,
                       reason=last_reason)
        LOG.debug("%s", message)
        raise FastPurgeError(message)

    @staticmethod
    def __purge_from_response(response):
        # Tolerate an unexpected non-JSON 201 (e.g. a future API change): still
        # treat the purge as accepted, falling back to the default estimate.
        try:
            response_body = response.json()
        except ValueError:
            LOG.warning("Purge accepted (201) with non-JSON body: %s",
                        (response.text or "")[:200])
            response_body = {}
        estimated_seconds = response_body.get('estimatedSeconds', 5)
        return Purge(response_body, monotonic() + estimated_seconds)

    def purge_objects(self, object_type, objects, **kwargs):
        """Purge a collection of objects.

        The Fast Purge API limits the number of objects purged in a single request.
        If the number of objects given exceeds the limit, this method may split the
        collection of objects into smaller lists and issue multiple requests.

        Requests may be retried a few times on failure.

        Args:

            object_type (str):
                type of object used for purge: "url", "tag", "cpcode".

            objects (list):
                list of objects for purge (e.g. list of URLs, tags or cpcodes).

            network (str):
                "staging" or "production". The default is "production".

            purge_type (str):
                "delete" or "invalidate". See `fast purge concepts`_ for more information regarding
                these purge types. The default is "delete".

        Returns:
            :class:`~concurrent.futures.Future` of :class:`list`:
                a Future resolved when the purge completes or fails, with:

                result:
                    If a purge succeeds, the result is a list of responses from the
                    Fast Purge API (:class:`dict`). See the `fast purge resources`_
                    for the expected fields in the response.

                    Typically, the result would contain only a single response object.
                    Multiple responses are provided if the client split a large cache
                    invalidation request into multiple smaller requests.

                    **Warning:** future resolution is based only on the estimated time to
                    completion provided by the Fast Purge API. There is no guarantee that the
                    purge has completed.

                exception:
                    If purge(s) fail, an exception will be set; typically, though
                    not always, an instance of :class:`FastPurgeError`.

        .. _fast purge resources:
           https://developer.akamai.com/api/core_features/fast_purge/v3.html#resources
        .. _fast purge concepts:
           https://developer.akamai.com/api/core_features/fast_purge/v3.html#concepts
        """

        purge_type = kwargs.pop('purge_type', self.DEFAULT_PURGE_TYPE)
        network = kwargs.pop('network', self.DEFAULT_NETWORK)

        relative_endpoint = '/ccu/v3/{purge_type}/{object_type}/{network}'.format(
            purge_type=purge_type, object_type=object_type, network=network)
        endpoint = urljoin(self.__baseurl, relative_endpoint)

        request_bodies = self.__get_request_bodies(objects)

        futures = []
        for request_body in request_bodies:
            future = self.__executor.submit(self.__start_purge, endpoint, request_body)
            futures.append(future)

        return f_sequence(futures)

    def purge_by_url(self, urls, **kwargs):
        """Convenience method for :meth:`purge_objects` with `object_type='url'`"""
        return self.purge_objects('url', urls, **kwargs)

    def purge_by_tag(self, tags, **kwargs):
        """Convenience method for :meth:`purge_objects` with `object_type='tag'`"""
        return self.purge_objects('tag', tags, **kwargs)

    def purge_by_cpcode(self, cpcodes, **kwargs):
        """Convenience method for :meth:`purge_objects` with `object_type='cpcode'`"""
        return self.purge_objects('cpcode', cpcodes, **kwargs)


def get_auth_dict(value):
    """Get :class:`akamai.edgegrid.EdgeGridAuth` authentication data.

    Parameters:
        value (dict or str): If ``value`` is a :py:class:`dict`,
            then a shallow copy of that dict is returned. Otherwise,
            ``value`` must be a string representing a filesystem path
            to an .edgerc configuration file.

    Returns:
         dict: a dictionary representing the authentication data needed by
            the :class:`akamai.edgegrid.EdgeGridAuth` API.

    Raises:
        TypeError: if ``value`` is not a :py:class:`dict` or :py:class:`str`.
        FastPurgeError: if ``value`` represents a path to an .edgerc
            configuration file and that path does not exist.
    """
    if isinstance(value, dict):
        # shallow copy the auth dict if provided
        # to avoid conflicting in-place updates (e.g. pop())
        return dict(value)

    if value is None:
        value = os.path.expanduser('~/.edgerc')

    if not isinstance(value, string_types):
        raise TypeError("Invalid 'auth' argument")

    if not os.path.exists(value):
        raise FastPurgeError("Missing configuration file %s" % value)

    rc = EdgeRc(value)
    return dict(
        client_token=rc.get('default', 'client_token'),
        client_secret=rc.get('default', 'client_secret'),
        access_token=rc.get('default', 'access_token'),
        host=rc.get('default', 'host'),
    )
