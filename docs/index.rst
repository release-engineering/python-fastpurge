fastpurge
=========

A client for the `Akamai Fast Purge API`_.

.. contents::
  :local:

Quick Start
-----------

Install the fastpurge client from PyPI:

::

    pip install fastpurge

Authorize a client in the `Akamai Control Center`_.

Place the client credentials in an `edgerc`_ file located at ``$HOME/.edgerc``;
this is an INI-style configuration file with the following structure:

::

    [default]
    host = <Base hostname without the scheme>
    client_token = <Client token>
    client_secret = <Secret>
    access_token = <Access Token>

In your Python code, construct a ``FastPurgeClient`` and call the
purge methods.  Make sure to block on the returned ``Future``
if you want to wait for the purge to complete.

.. code-block:: python

    from fastpurge import FastPurgeClient
    client = FastPurgeClient()

    # start a purge of two URLs
    future = client.purge_by_url(["https://example.com/resource1",
                                  "https://example.com/resource2"])

    # wait for the purge to complete (or raise if purge fails)
    future.result()

Usage of ``$HOME/.edgerc`` for credentials is a convention supported
by other clients in the Akamai ecosystem.  If you'd like to configure
the credentials separately, you can pass them to the client constructor
explicitly, as in example:

.. code-block:: python

    from fastpurge import FastPurgeClient
    client = FastPurgeClient(auth={
        # The entries from ~/.edgerc can be supplied directly here
        "host": "akaa-xxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxx.purge.akamaiapis.net",
        "client_token": "akab-xxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxx",
        "client_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "access_token": "akab-xxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxx",
    })


API Reference
-------------

.. autoclass:: fastpurge.FastPurgeClient(auth=None, scheme='https', port=443)
   :members: purge_by_url, purge_by_tag, purge_by_cpcode

   .. automethod:: purge_objects(object_type, objects, network=None, purge_type=None)

.. autoclass:: fastpurge.FastPurgeError
   :members:

.. _Akamai Control Center: https://developer.akamai.com/legacy/introduction/Luna_Setup.html
.. _Akamai Fast Purge API: https://developer.akamai.com/api/core_features/fast_purge/v3.html
.. _edgerc: https://developer.akamai.com/legacy/introduction/Conf_Client.html
