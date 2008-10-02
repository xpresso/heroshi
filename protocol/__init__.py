# -*- coding: utf-8 -*-

"""Heroshi queue protocol.
Gets URLs to crawl from queue server, crawls them, store pages
and send crawl info back to queue server.

GET : Data is number of items, client wishes to get
PUT : Data is pickled items, client wishes to send to server
QUIT : When worker receives this message, it stops any jobs and quits
"""

import sys, os, time
from optparse import OptionParser
import cPickle as pickle
import cjson as json
import random
from BeautifulSoup import BeautifulSoup
from twisted.protocols.basic import Int32StringReceiver
from twisted.internet.protocol import ServerFactory
from twisted.internet.protocol import ClientFactory
from twisted.internet import reactor
from twisted.internet.error import ConnectionDone
import urllib2
import urllib

import misc
from misc import HEROSHI_VERSION, debug
from link import Link
from page import Page
from storage import save_page

BIND_PORT = 15822
KNOWN_ACTIONS = ( 'GET', 'PUT', 'QUIT', )


class Event(object):
    """Basic event object"""
    # TODO: search for python stdlib implementations

    handlers = []

    def __init__(self, handler=None):
        if handler:
            self.handlers.append(tuple(handler, [], {}))

    def add(self, handler, *args, **kwargs):
        if not handler:
            raise Exception, "Cannot add None to event handlers. Please specify a callable."
        self.handlers.append(tuple([handler, args, kwargs]))

    def __add__(self, handler):
        self.add(handler)

    def fire(self):
        for handler, args, kwargs in self.handlers:
            handler(*args, **kwargs)

    def __call__(self):
        self.fire()


class ProtocolMessage(object):
    """Message within connection
    Could be action from client or response from server"""

    action = None
    status = None
    data = None

    def __init__(self, action=None, data=None, raw=None):
        """Creates new message.
        *data* is Python object for transfer. It will be serialized in .pack() method.
        *raw* is raw string from network. It will be unserialized and result will be available in .data"""

        self.action = action
        if data:
            self.data = data
        if raw:
            self.unpack(raw)

    def pack(self):
        packed = json.encode({'action': self.action, 'data': self.data})
        return packed

    def unpack(self, raw):
        unpacked = json.decode(raw)
        self.action = unpacked.get('action')
        self.status = unpacked.get('status')
        self.data = unpacked['data']

    def get_http_request_url(self, address, port):
        return "http://%s:%d/?r=%s" % (address, port, urllib.pathname2url(self.pack()))

    def __unicode__(self):
        return self.action or u'Empty protocol message'

    def __str__(self):
        return unicode(self)

    def __repr__(self):
        return unicode(self)


def read_message(s):
    """Parses string from network and returns appropriate MessageProtocol"""

    action, raw_data = s.split('.', 1)
    message = ProtocolAction(action, raw=raw_data)
    if not action in KNOWN_ACTIONS:
        # TODO: custom exception
        raise Exception, "Incorrect protocol used. Action %s is not recognized" % id
    return message
