"""Heroshi worker implementation.

Gets URLs to crawl from queue server, crawls them via io-worker,
sends crawl info back to queue server."""

from datetime import datetime
import eventlet
from eventlet import GreenPool, greenthread, sleep, spawn, with_timeout
from eventlet.queue import Empty, Queue
import httplib2
import json
import random, time, urllib, urlparse
import robotparser
import sys

from heroshi import TIME_FORMAT
from heroshi import api, error, get_logger
log = get_logger("worker.Crawler")
from heroshi.conf import settings
from heroshi.data import PoolMap
from heroshi.error import ApiError, CrawlError, FetchError, RobotsError
from heroshi.misc import reraise_errors
from heroshi.worker import io

eventlet.monkey_patch(all=False, os=True, socket=True, select=True)


class Stop(error.Error): pass


class Crawler(object):
    def __init__(self, max_connections, input_is_plain):
        self.max_connections = max_connections
        self.input_is_plain = input_is_plain

        self.queue = Queue(1)
        self.closed = False
        self._handler_pool = GreenPool(self.max_connections)
        self._robots_cache = PoolMap(self.get_robots_checker,
                                     pool_max_size=1,
                                     timeout=600)

        # Start IO worker and die if he does.
        self.io_worker = io.Worker(lambda: self.closed)
        t = spawn(self.io_worker.run_loop)
        t.link(reraise_errors, greenthread.getcurrent())

        log.debug(u"Crawler started. Max connections: %d.",
                  self.max_connections)

    def crawl(self, forever=True):
        # TODO: do something special about signals?

        if forever:
            self.start_queue_updater()

        while not self.closed:
            # `get_nowait` will only work together with sleep(0) here
            # because we need greenlet switch to reraise exception from `do_process`.
            sleep()
            try:
                item = self.queue.get_nowait()
            except Empty:
                if not forever:
                    self.graceful_stop()
                sleep(0.01)
                continue
            t = self._handler_pool.spawn(self.do_process, item)
            t.link(reraise_errors, greenthread.getcurrent())

    def stop(self):
        self.closed = True

    def graceful_stop(self, timeout=None):
        """Stops crawler and waits for all already started crawling requests to finish.

        If `timeout` is supplied, it waits for at most `timeout` time to finish
            and returns True if allocated time was enough.
            Returns False if `timeout` was not enough.
        """
        self.closed = True
        if timeout is not None:
            with eventlet.Timeout(timeout, False):
                if hasattr(self, '_queue_updater_thread'):
                    self._queue_updater_thread.kill()
                self._handler_pool.waitall()
                return True
            return False
        else:
            if hasattr(self, '_queue_updater_thread'):
                self._queue_updater_thread.kill()
            self._handler_pool.waitall()

    def start_queue_updater(self):
        self._queue_updater_thread = spawn(self.queue_updater)
        self._queue_updater_thread.link(reraise_errors, greenthread.getcurrent())

    def queue_updater(self):
        log.debug("Waiting for crawl jobs on stdin.")
        for line in sys.stdin:
            if self.closed: break

            line = line.strip()

            if self.input_is_plain:
                job = {'url': line}
            else:
                try:
                    job = json.loads(line)
                except ValueError:
                    log.error(u"Decoding input line: %s", line)
                    continue

            # extend worker queue
            # 1. skip duplicate URLs
            for queue_item in self.queue.queue:
                if queue_item['url'] == job['url']: # compare URLs
                    break
            else:
                # 2. extend queue with new items
                # May block here, when queue is full. This is a feature.
                self.queue.put(job)

        # Stdin exhausted -> stop.
        while not self.queue.empty():
            sleep(0.01)

        sleep(2) # FIXME: Crutch to prevent stopping too early.

        self.graceful_stop()

    def get_robots_checker(self, scheme, authority):
        """PoolMap func :: scheme, authority -> (agent, uri -> bool)."""
        robots_uri = "%s://%s/robots.txt" % (scheme, authority)

        fetch_result = self.io_worker.fetch(robots_uri)
        # Graceful stop thing.
        if fetch_result is None:
            return None

        if fetch_result['success']:
            # TODO: set expiration time from headers
            # but this must be done after `self._robots_cache.put` or somehow else...
            if 200 <= fetch_result['status_code'] < 300:
                parser = robotparser.RobotFileParser()
                content_lines = fetch_result['content'].splitlines()
                try:
                    parser.parse(content_lines)
                except KeyError:
                    raise RobotsError(u"Known robotparser bug: KeyError at urllib.quote(path).")
                return parser.can_fetch
            # Authorization required and Forbidden are considered Disallow all.
            elif fetch_result['status_code'] in (401, 403):
                return lambda _agent, _uri: False
            # /robots.txt Not Found is considered Allow all.
            elif fetch_result['status_code'] == 404:
                return lambda _agent, _uri: True
            # FIXME: this is an optimistic rule and probably should be detailed with more specific checks
            elif fetch_result['status_code'] >= 400:
                return lambda _agent, _uri: True
            # What other cases left? 100 and redirects. Consider it Disallow all.
            else:
                return lambda _agent, _uri: False
        else:
            raise FetchError(u"/robots.txt fetch problem: %s" % (fetch_result['result']))

    def ask_robots(self, uri, scheme, authority):
        key = scheme+":"+authority
        with self._robots_cache.getc(key, scheme, authority) as checker:
            try:
                # Graceful stop thing.
                if checker is None:
                    return None
                return checker(settings.identity['name'], uri)
            except Exception, e:
                log.exception(u"Get rid of this. ask_robots @ %s", uri)
                raise RobotsError(u"Error checking robots.txt permissions for URI '%s': %s" % (uri, unicode(e)))

    def do_process(self, item):
        try:
            report = self._process(item)
            if not report.get('visited'):
                timestamp = datetime.utcnow().strftime(TIME_FORMAT)
                report['visited'] = timestamp
            report_item(report)
            self.queue.task_done()
        except Stop:
            pass

    def _process(self, item):
        url = item['url']
        log.debug(u"Crawling: %s", url)
        uri = httplib2.iri2uri(url)
        report = {
            'url': url,
            'result': None,
            'status_code': None,
            'visited': None,
        }

        total_start_time = time.time()

        (scheme, authority, _path, _query, _fragment) = httplib2.parse_uri(uri)
        if scheme is None or authority is None:
            report['result'] = u"Invalid URI"
            return report

        try:
            # this line is copied from robotsparser.py:can_fetch
            urllib.quote(urlparse.urlparse(urllib.unquote(url))[2])
        except KeyError:
            report['result'] = u"Malformed URL quoting."
            return report

        try:
            robot_check_result = self.ask_robots(uri, scheme, authority)
            # Graceful stop thing.
            if robot_check_result is None:
                raise Stop()
        except CrawlError, e:
            report['result'] = unicode(e)
            return report
        if robot_check_result == True:
            pass
        elif robot_check_result == False:
            report['result'] = u"Deny by robots.txt"
            return report
        else:
            assert False, u"This branch should not be executed."
            report['result'] = u"FIXME: unhandled branch in _process."
            return report

        fetch_result = with_timeout(settings.socket_timeout,
                                    self.io_worker.fetch,
                                    uri, timeout_value='timeout')
        if fetch_result is None:
            raise Stop()

        if fetch_result == 'timeout':
            fetch_result = {}
            report['result'] = u"Fetch timeout"

        fetch_result.pop('cached', None)
        fetch_result.pop('success', None)

        total_end_time = time.time()
        report['total_time'] = int((total_end_time - total_start_time) * 1000)
        report.update(fetch_result)

        return report


def report_item(item):
    log.debug(u"Reporting %s results back to URL server.", unicode(item['url']))
    try:
        api.report_result(item)
    except ApiError:
        log.exception(u"report_item")
