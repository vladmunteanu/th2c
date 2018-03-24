import collections
import functools
import logging

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h2.settings
from tornado import stack_context, httputil
from tornado.concurrent import Future
from tornado.httpclient import HTTPRequest, HTTPResponse, _RequestProxy
from tornado.ioloop import IOLoop
from tornado.tcpclient import TCPClient

from .config import DEFAULT_RECONNECT_INTERVAL, DEFAULT_CONNECTION_TIMEOUT
from .connection import HTTP2ClientConnection
from .exceptions import RequestTimeout, TH2CError
from .stream import HTTP2ClientStream

log = logging.getLogger(__name__)


class AsyncHTTP2Client(object):
    CLIENT_INSTANCES = dict()

    def __new__(cls, host, port, *args, **kwargs):
        """
        Create an asynchronous HTTP/2 client for this (host, port).
        Only one instance of AsyncHTTP2Client exists per (host, port).
        """
        if (host, port) in cls.CLIENT_INSTANCES:
            client = cls.CLIENT_INSTANCES[(host, port)]
        else:
            client = super(AsyncHTTP2Client, cls).__new__(cls)
            cls.CLIENT_INSTANCES[(host, port)] = client

        return client

    def __init__(self, host, port, secure=True, verify_certificate=True,
                 ssl_key=None, ssl_cert=None,
                 max_active_requests=10, io_loop=None,
                 auto_reconnect=False,
                 auto_reconnect_interval=DEFAULT_RECONNECT_INTERVAL,
                 _connection_cls=HTTP2ClientConnection,
                 _stream_cls=HTTP2ClientStream, **kwargs):

        if getattr(self, 'initialized', False):
            return
        else:
            self.initialized = True

        self.io_loop = io_loop or IOLoop.instance()

        self.host = host
        self.port = port

        self.secure = secure
        self.verify_certificate = verify_certificate
        self.ssl_key = ssl_key
        self.ssl_cert = ssl_cert

        self.closed = False

        self.connection_timeout = kwargs.get(
            'connection_timeout', DEFAULT_CONNECTION_TIMEOUT
        )

        self.auto_reconnect = auto_reconnect
        self.auto_reconnect_interval = auto_reconnect_interval
        self.max_active_requests = max_active_requests

        self.pending_requests = collections.deque()
        self.queue_timeouts = dict()
        self.active_requests = dict()

        self.connection_cls = _connection_cls
        self.stream_cls = _stream_cls

        self.tcp_client = TCPClient()

        self.connection = None

        self.connect()

    def connect(self):
        self.connection = self.connection_cls(
            self.host, self.port, self.tcp_client, self.secure, self.io_loop,
            self.on_connection_ready, self.on_connection_closed,
            ssl_options={
                'verify_certificate': self.verify_certificate,
                'key': self.ssl_key,
                'cert': self.ssl_cert
            },
            connect_timeout=self.connection_timeout,
            max_concurrent_streams=self.max_active_requests,
        )
        self.connection.add_event_handler(
            h2.events.RemoteSettingsChanged, self.on_settings_changed
        )
        self.connection.connect()

    def on_connection_ready(self):
        """ Callback executed when the connection is ready. """
        log.info(
            'HTTP/2 Connection established to %s:%d!',
            self.host, self.port
        )
        self.process_pending_requests()

    def on_connection_closed(self, reason):
        """ Callback executed when the connection is closed. """
        log.info(
            ['Connection closed', reason,
             'active requests:', len(self.active_requests),
             len(self.pending_requests)]
        )
        if not isinstance(reason, Exception):
            reason = TH2CError(reason)

        self.connection = None

        if self.auto_reconnect:
            log.info(
                'Attempting to reconnect after %d seconds',
                self.auto_reconnect_interval
            )
            self.io_loop.add_timeout(
                self.io_loop.time() + self.auto_reconnect_interval,
                self.connect
            )
        else:
            while self.pending_requests:
                key, _, callback = self.pending_requests.popleft()
                if key in self.queue_timeouts:
                    _, _, timeout_handle = self.queue_timeouts[key]
                    self.io_loop.remove_timeout(timeout_handle)
                    del self.queue_timeouts[key]

                callback(reason)

    def on_settings_changed(self, event):
        """ Called to handle a RemoteSettingsChanged event. """
        # read maximum concurrent streams (requests) (MAX_CONCURRENT_STREAMS)
        max_requests = event.changed_settings.get(
            h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS
        )
        if max_requests:
            log.debug(
                'Updating maximum concurrent streams according to '
                'remote settings (old: %s, new: %s).',
                max_requests.original_value,
                max_requests.new_value
            )
            self.max_active_requests = min(
                max_requests.new_value, self.max_active_requests
            )

    def fetch(self, request):
        """
        Asynchronously fetches a request.
        Returns a Future that will resolve when the request finishes or
        when an error occurs.

        :param request: client request
        :type request: tornado.httpclient.HTTPRequest
        :return: Future
        """
        # prepare the request object
        request.headers = httputil.HTTPHeaders(request.headers)
        request = _RequestProxy(request, dict(HTTPRequest._DEFAULTS))

        # wrap everything in a Future
        future = Future()

        def handle_response(response):
            """ Will be called by HTTP2Stream on request finished """
            if isinstance(response, HTTPResponse):
                if response.error:
                    future.set_exception(response.error)
                else:
                    future.set_result(response)
            else:
                future.set_exception(response)

        # unique request key
        key = object()

        # put the request in the pending queue
        self.pending_requests.append((key, request, handle_response))

        # if we are already processing maximum concurrent requests,
        # set a timeout for the time spent in queue
        if (
            not self.connection or not self.connection.is_ready
            or len(self.active_requests) >= self.max_active_requests
        ):
            timeout_handle = self.io_loop.add_timeout(
                self.io_loop.time() + request.request_timeout,
                functools.partial(self.on_queue_timeout, key)
            )
        else:
            timeout_handle = None

        # add the timeout for the queue
        self.queue_timeouts[key] = (request, handle_response, timeout_handle)
        self.process_pending_requests()
        if self.pending_requests:
            log.debug(
                'Queued request, {} active, {} in queue.'.format(
                    len(self.active_requests), len(self.pending_requests)
                )
            )
        return future

    def process_pending_requests(self):
        with stack_context.NullContext():
            while (
                self.connection
                and self.connection.is_ready
                and len(self.active_requests) < self.max_active_requests
                and self.pending_requests
            ):
                log.debug('Processing a new pending request!')
                key, request, callback = self.pending_requests.popleft()
                if key not in self.queue_timeouts:
                    continue

                request, callback, timeout_handle = self.queue_timeouts[key]
                if timeout_handle is not None:
                    self.io_loop.remove_timeout(timeout_handle)
                del self.queue_timeouts[key]

                self.active_requests[key] = (request, callback)
                remove_from_active_cb = functools.partial(
                    self.remove_active, key
                )

                self.handle_request(request, remove_from_active_cb, callback)

    def handle_request(self, request, callback_clear_active, callback):
        """
        Create an HTTP2Stream object for the current request.
        :param request: client request
        :type request: HTTPRequest
        :param callback_clear_active: function executed when the request
                                      finishes, removes the current stream
                                      from the list of active streams.
        :param callback: function executed when the request finishes
        """
        stream = self.stream_cls(
            self.connection, request, callback_clear_active, callback,
            self.io_loop
        )

        with stack_context.ExceptionStackContext(stream.handle_exception):
            stream.begin_request()

    def remove_active(self, key):
        """
        Called when a request is finished to remove it from
        current active requests.
        """
        log.debug('Stream removed from active requests')
        del self.active_requests[key]

        self.process_pending_requests()

    def on_queue_timeout(self, key):
        """
        Called when a request times out while in processing queue.
        Removes the request from pending list.
        """
        # remove the pending request
        request, callback, _ = self.queue_timeouts[key]
        self.pending_requests.remove((key, request, callback))
        del self.queue_timeouts[key]

        # call the request's associated callback with RequestTimeout
        callback(RequestTimeout('Timeout in processing queue'))

    def close(self):
        """
        Closes the AsyncHTTP2Client by terminating the connection and setting
        the `closed` flag.

        We assume the connection will call on_connection_closed.

        :return:
        """
        log.debug('Closing HTTP/2 client!')
        self.closed = True
        if self.connection:
            self.connection.close('Client closed!')
