fastpurge
=========

A client for the `Akamai Fast Purge API`_.

.. toctree::
   :maxdepth: 2
   :caption: Contents:

.. autoclass:: fastpurge.FastPurgeClient(auth=None, scheme='https', port=443)
   :members: purge_by_url, purge_by_tag, purge_by_cpcode

   .. automethod:: purge_objects(object_type, objects, network=None, purge_type=None)

.. autoclass:: fastpurge.FastPurgeError
   :members:

.. _Akamai Fast Purge API: https://developer.akamai.com/api/core_features/fast_purge/v3.html
