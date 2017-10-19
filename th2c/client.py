import collections
import functools
import logging
import time

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h2.settings

from tornado import log as tornado_log, stack_context, httputil
from tornado.concurrent import TracebackFuture
from tornado.httpclient import HTTPRequest, HTTPResponse, _RequestProxy, HTTPError
from tornado.ioloop import IOLoop
from tornado.netutil import BlockingResolver
from tornado.tcpclient import TCPClient

from .connection import HTTP2ClientConnection
from .stream import HTTP2ClientStream

logger = tornado_log.gen_log
log = logging.getLogger(__name__)

INITIAL_WINDOW_SIZE = 65535


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

    def __init__(self, host, port, dns_blocking=True, max_active_requests=10, *args, **kwargs):
        self.host = host
        self.port = port

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
            self.host, self.port, self.tcp_client,
            self.on_connection_ready, self.on_connection_closed,
            connect_timeout=10
        )
        self.connection.add_event_handler(
            h2.events.RemoteSettingsChanged, self.on_settings_changed
        )
        self.connection.connect()

    def on_connection_ready(self):
        """ Callback executed when the connection is ready. """
        log.info("Connection established to {}:{}!".format(self.host, self.port))
        self.process_pending_requests()

    def on_connection_closed(self, reason):
        """ Callback executed when the connection is closed. """
        log.info(["Connection closed", reason, "active requests:", self.active_requests, self.pending_requests])
        while self.pending_requests:
            key, req, callback = self.pending_requests.popleft()
            IOLoop.current().add_callback(
                callback,
                HTTPResponse(
                    req, 0, error=Exception(reason),
                    request_time=time.time() - req.start_time
                )
            )

    def on_settings_changed(self, event):
        log.info('settings updated: %r', event.changed_settings)
        max_requests = event.changed_settings.get(
            h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS
        )
        if max_requests:
            self.max_active_requests = min(
                max_requests.new_value, self.max_active_requests
            )
            if max_requests.new_value > max_requests.original_value:
                # we might be able to process more requests, so let's try
                self.process_pending_requests()

    def fetch(self, request, *args, **kwargs):
        """
        Asynchronously fetches a request.
        Returns a Future that will resolve when the request finishes or an error occurs.

        :param request: client request
        :type request: HTTPRequest
        :return: Future
        """
        log.info("Preparing to fetch request")
        # prepare the request object
        request.headers = httputil.HTTPHeaders(request.headers)
        request = _RequestProxy(request, dict(HTTPRequest._DEFAULTS))

        # wrap everything in a Future
        future = TracebackFuture()

        def handle_response(response):
            """ Will be called by HTTP2Stream on request finished """
            if response.error:
                future.set_exception(response.error)
            else:
                future.set_result(response)

        # unique request key
        key = object()

        # put the request in the pending queue
        self.pending_requests.append((key, request, handle_response))

        # if we are already processing maximum concurrent requests,
        # set a timeout for the time spent in queue
        if len(self.active_requests) >= self.max_active_requests:
            timeout_handle = IOLoop.current().add_timeout(
                IOLoop.current().time() + request.request_timeout,
                functools.partial(self.on_queue_timeout, key)
            )
        else:
            timeout_handle = None

        # add the timeout for the queue
        self.queue_timeouts[key] = (request, handle_response, timeout_handle)
        self.process_pending_requests()
        if self.pending_requests:
            log.info(
                "Queued request, {} active, {} in queue.".format(
                    len(self.active_requests), len(self.pending_requests)
                )
            )
        return future

    def process_pending_requests(self):
        if not self.connection.is_connected:
            return

        with stack_context.NullContext():
            while self.pending_requests and len(self.active_requests) < self.max_active_requests:
                log.info("Processing a new pending request!")
                key, request, callback = self.pending_requests.popleft()
                if key not in self.queue_timeouts:
                    continue

                request, callback, timeout_handle = self.queue_timeouts[key]
                if timeout_handle is not None:
                    IOLoop.current().remove_timeout(timeout_handle)
                del self.queue_timeouts[key]

                self.active_requests[key] = (request, callback)
                remove_from_active_cb = functools.partial(self.remove_active, key)

                self.handle_request(request, remove_from_active_cb, callback)

    def remove_active(self, key):
        """ Called when a request is finished """
        del self.active_requests[key]
        self.process_pending_requests()

    def on_queue_timeout(self, key):
        """ Called when a request timeout expires while in processing queue. """
        request, callback, timeout_handle = self.queue_timeouts[key]
        self.pending_requests.remove((key, request, callback))

        timeout_response = HTTPResponse(
            request, 599, error=HTTPError(599, "Timeout in processing queue"),
            request_time=IOLoop.current().time() - request.start_time
        )
        IOLoop.current().add_callback(callback, timeout_response)
        del self.queue_timeouts[key]

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
        stream = HTTP2ClientStream(
            self.connection, request, callback_clear_active, callback
        )
        try:
            stream.begin_request()
        except:
            log.error("Could not send request", exc_info=True)
