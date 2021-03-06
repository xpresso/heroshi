"""Heroshi client (worker) API implementation.

Crawler uses these helpers to communicate with URL server."""

from eventlet.pools import Pool
import httplib2

try:
    import yajl as json
except ImportError:
    import json

import socket
from urllib import urlencode

from heroshi import get_logger
log = get_logger("api")
from heroshi.conf import settings
from heroshi.error import ApiError


manager_connections = Pool(max_size=2)
manager_connections.create = lambda: httplib2.Http(timeout=20)


def request_manager(resource, method, data=None, headers=None):
    use_headers = {
        'User-Agent': settings.identity['user_agent'],
        'X-Heroshi-Auth': settings.api_key,
        'Expect': '', # a try to fix result: 100 not-ok problem
    }
    if headers is not None:
        use_headers.update(headers)

    # concat URIs carefully
    base_url = settings.manager_url
    url = base_url.strip('/') + resource

    # make request
    http = manager_connections.get()
    try:
        response, content = http.request(url, method, body=data, headers=use_headers)
    except socket.timeout:
        raise ApiError("timeout")
    finally:
        manager_connections.put(http)

    if not (200 <= response.status < 300):
        raise ApiError("non-ok-result. Code: %s" % response.status)

    return content

def get_crawl_queue(limit):
    response = request_manager('/crawl-queue', 'POST',
                               urlencode({'limit': limit}),
                               {'Content-Type': "application/x-www-form-urlencoded"})
    queue = json.loads(response)
    return queue

def report_result(item):
    try:
        item_json = json.dumps(item)
    except UnicodeDecodeError:
        item['result'] = "FIXME: unicode decode error"
        item.pop('content')
        try:
            item_json = json.dumps(item)
        except UnicodeDecodeError:
            log.error("Fatal unicode decode error @ %s", item['url'])
            return
    request_manager('/report', 'PUT', item_json)

