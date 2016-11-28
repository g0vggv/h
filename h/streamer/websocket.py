# -*- coding: utf-8 -*-

from collections import namedtuple
import json
import logging
import weakref

from gevent.queue import Full
import jsonschema
from ws4py.websocket import WebSocket as _WebSocket

from memex import storage
from h.streamer import filter

log = logging.getLogger(__name__)

# Mapping incoming message type to handler function. Handlers are added inline
# below.
MESSAGE_HANDLERS = {}

# An incoming message from a WebSocket client.
Message = namedtuple('Message', ['socket', 'payload'])


class WebSocket(_WebSocket):
    # All instances of WebSocket, allowing us to iterate over open websockets
    instances = weakref.WeakSet()
    origins = []

    # Instance attributes
    client_id = None
    filter = None
    query = None

    def __init__(self, sock, protocols=None, extensions=None, environ=None):
        super(WebSocket, self).__init__(sock,
                                        protocols=protocols,
                                        extensions=extensions,
                                        environ=environ,
                                        heartbeat_freq=30.0)

        self.authenticated_userid = environ['h.ws.authenticated_userid']
        self.effective_principals = environ['h.ws.effective_principals']
        self.registry = environ['h.ws.registry']

        self._work_queue = environ['h.ws.streamer_work_queue']

    def __new__(cls, *args, **kwargs):
        instance = super(WebSocket, cls).__new__(cls, *args, **kwargs)
        cls.instances.add(instance)
        return instance

    def received_message(self, msg):
        try:
            self._work_queue.put(Message(socket=self, payload=msg.data),
                                 timeout=0.1)
        except Full:
            log.warn('Streamer work queue full! Unable to queue message from '
                     'WebSocket client having waited 0.1s: giving up.')

    def closed(self, code, reason=None):
        try:
            self.instances.remove(self)
        except KeyError:
            pass

    def send_json(self, payload):
        if not self.terminated:
            self.send(json.dumps(payload))


def handle_message(message, session=None):
    """
    Handle an incoming message from a client websocket.

    Receives a :py:class:`~h.streamer.websocket.Message` instance, which holds
    references to the :py:class:`~h.streamer.websocket.WebSocket` instance
    associated with the client connection, as well as the message payload.

    It updates state on the :py:class:`~h.streamer.websocket.WebSocket`
    instance in response to the message content.

    It may also passed a database session which *must* be used for any
    communication with the database.
    """
    socket = message.socket

    try:
        data = json.loads(message.payload)
    except ValueError:
        socket.close(reason='invalid message format')
        return

    type_ = data.get('type')

    # FIXME: This code is here to tolerate old and deprecated message formats.
    if type_ is None:
        if 'messageType' in data and data['messageType'] == 'client_id':
            type_ = 'client_id'
        if 'filter' in data:
            type_ = 'filter'

    # N.B. MESSAGE_HANDLERS[None] handles both incorrect and missing message
    # types.
    handler = MESSAGE_HANDLERS.get(type_, MESSAGE_HANDLERS[None])
    handler(socket, data, session=session)


def handle_client_id_message(socket, payload, session=None):
    """A client telling us its client ID."""
    if 'value' not in payload:
        # FIXME: send an error message to the client
        return
    socket.client_id = payload['value']
MESSAGE_HANDLERS['client_id'] = handle_client_id_message


def handle_filter_message(socket, payload, session=None):
    """A client updating its streamer filter."""
    if 'filter' not in payload:
        # FIXME: send an error message to the client
        return
    filter_ = payload['filter']
    try:
        jsonschema.validate(filter_, filter.SCHEMA)
    except jsonschema.ValidationError:
        # FIXME: send an error message to the client
        return
    if session is not None:
        # Add backend expands for clauses
        _expand_clauses(session, filter_)
    socket.filter = filter.FilterHandler(filter_)
MESSAGE_HANDLERS['filter'] = handle_filter_message


def handle_unknown_message(socket, payload, session=None):
    """Message type missing or not recognised."""
    # FIXME: send an error message to the client
MESSAGE_HANDLERS[None] = handle_unknown_message


def _expand_clauses(session, filter_):
    for clause in filter_['clauses']:
        if 'field' in clause and clause['field'] == '/uri':
            _expand_uris(session, clause)


def _expand_uris(session, clause):
    uris = clause['value']
    expanded = set()

    if not isinstance(uris, list):
        uris = [uris]

    for item in uris:
        expanded.update(storage.expand_uri(session, item))

    clause['value'] = list(expanded)
