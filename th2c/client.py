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
from tornado.concurrent import TracebackFuture
from tornado.httpclient import (HTTPRequest, HTTPResponse,
                                _RequestProxy)
from tornado.ioloop import IOLoop
from tornado.netutil import BlockingResolver
from tornado.tcpclient import TCPClient

from .connection import HTTP2ClientConnection
from .stream import HTTP2ClientStream
from .exceptions import RequestTimeout

log = logging.getLogger(__name__)

INITIAL_WINDOW_SIZE = 65535
DEFAULT_CONNECTION_TIMEOUT = 10  # seconds


class AsyncHTTP2Client(object):
    CLIENT_INSTANCES = dict()

    def __new__(cls, host, *args, **kwargs):
        """
        Create an asynchronous HTTP/2 client for this host.
        Only one instance of AsyncHTTP2Client exists per host.
        """
        if host in cls.CLIENT_INSTANCES:
            client = cls.CLIENT_INSTANCES[host]
        else:
            client = super(AsyncHTTP2Client, cls).__new__(cls)
            cls.CLIENT_INSTANCES[host] = client

        return client

    def __init__(self, host, port, secure=True, dns_blocking=True,
                 max_active_requests=10, verify_certificate=True,
                 io_loop=None, *args, **kwargs):

        self.io_loop = io_loop or IOLoop.instance()

        self.host = host
        self.port = port
        self.secure = secure

        self.closed = False

        # use BlockingResolver if dns_blocking is True,
        # otherwise don't pass any, uses whichever is already configured.
        if dns_blocking:
            self.tcp_client = TCPClient(resolver=BlockingResolver())
        else:
            self.tcp_client = TCPClient()

        self.max_active_requests = max_active_requests

        self.pending_requests = collections.deque()
        self.queue_timeouts = dict()
        self.active_requests = dict()

        self.connection = HTTP2ClientConnection(
            self.host, self.port, self.tcp_client, self.secure,
            self.on_connection_ready, self.on_connection_closed,
            ssl_options={
                'verify_certificate': verify_certificate
            },
            connect_timeout=DEFAULT_CONNECTION_TIMEOUT,
            max_concurrent_streams=self.max_active_requests,
            io_loop=self.io_loop
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
            reason = Exception(reason)

        while self.pending_requests:
            key, req, callback = self.pending_requests.popleft()

            self.io_loop.add_callback(
                callback, reason
            )

        # TODO: Reconnect after a customizable refresh_interval value

    def on_settings_changed(self, event):
        """ Called to handle a RemoteSettingsChanged event. """
        # read maximum concurrent streams (requests) (MAX_CONCURRENT_STREAMS)
        max_requests = event.changed_settings.get(
            h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS
        )
        if max_requests:
            log.debug(
                "Updating maximum concurrent streams according to "
                "remote settings (old: %s, new: %s).",
                max_requests.original_value,
                max_requests.new_value
            )
            self.max_active_requests = min(
                max_requests.new_value, self.max_active_requests
            )
            if max_requests.new_value > max_requests.original_value:
                # we might be able to process more requests, so let's try
                self.process_pending_requests()

        # TODO: read maximum frame size (MAX_FRAME_SIZE)

        # TODO: MAX_HEADER_LIST_SIZE, HEADER_TABLE_SIZE

    def fetch(self, request, *args, **kwargs):
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
        future = TracebackFuture()

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
        if len(self.active_requests) >= self.max_active_requests:
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
                self.connection.is_ready
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
                remove_from_active_cb = functools.partial(self.remove_active, key)

                self.handle_request(request, remove_from_active_cb, callback)

    def handle_request(self, request, callback_clear_active, callback):
        """
        Create an HTTP2Stream object for the current request.
        :param request: client request
        :type request: HTTPRequest
        :param callback_clear_active: function, executed when the request
                                      finishes. Removes the current stream
                                      from the list of active streams.
        :param callback: function executed when the request finishes
        """
        if not self.connection.is_ready:
            log.error('Trying to send a request while connection is not ready!')

        stream = HTTP2ClientStream(
            self.connection, request, callback_clear_active, callback,
            self.io_loop
        )

        # fut = stream.begin_request()
        #
        # def handle_request_finished(f):
        #     exc_info = f.exc_info()
        #     if exc_info:
        #         stream.handle_exception(*exc_info)
        #
        # fut.add_done_callback(handle_request_finished)

        with stack_context.ExceptionStackContext(stream.handle_exception):
            stream.begin_request()

    def remove_active(self, key):
        """ Called when a request is finished """
        log.debug('Stream removed from active requests')
        del self.active_requests[key]

        self.process_pending_requests()

    def on_queue_timeout(self, key):
        """ Called when a request timeout expires while in processing queue. """
        request, callback, timeout_handle = self.queue_timeouts[key]
        self.pending_requests.remove((key, request, callback))
        callback(RequestTimeout('Timeout in processing queue'))
        del self.queue_timeouts[key]

    def close(self):
        log.debug('Closing HTTP/2 client!')
        self.closed = True
        self.connection.close('Client closed!')
