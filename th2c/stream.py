import copy
import httplib
import io
import logging
import traceback
from urlparse import urlsplit, urljoin

import h2.events
import h2.exceptions
from tornado import httputil, gen
from tornado.escape import to_unicode

from .exceptions import RequestTimeout, TH2CError
from .flowcontrol import FlowControlWindow
from .response import HTTP2Response

log = logging.getLogger(__name__)

REDIRECT_HTTP_CODES = {301, 302, 303, 307, 308}


class HTTP2ClientStream(object):
    ALLOWED_METHODS = {
        'GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'
    }

    def __init__(self, connection, request,
                 callback_cleanup, callback_response,
                 io_loop, client_cls):
        """
        :param connection: connection object
        :type connection: th2c.connection.HTTP2ClientConnection
        :param request: request object
        :type request: tornado.httpclient.HTTPRequest
        :param callback_cleanup: should be called to do cleanup in parents
        :param callback_response: should be called with the final result
        :param io_loop: instance of a tornado IOLoop object
        :param client_cls: client class that will be instantiated for redirects
        """
        self.io_loop = io_loop
        self.client_cls = client_cls

        self.connection = connection
        self.request = request

        self.callback_response = callback_response
        self.callback_cleanup = callback_cleanup

        self.stream_id = None

        self.headers = None
        self.code = None
        self.reason = None

        self.scheme = None

        self._chunks = []

        self.stream_id = self.connection.begin_stream(self)

        self.closed = False
        self.timed_out = False

        self._timeout = None
        if request.request_timeout:
            self._timeout = self.io_loop.add_timeout(
                self.request.start_time + request.request_timeout,
                self.on_timeout
            )

        self.flow_control_window = FlowControlWindow(
            initial_value=self.connection.initial_window_size
        )
        self.max_frame_size = self.connection.max_frame_size

    def on_timeout(self):
        self.io_loop.remove_timeout(self._timeout)
        self._timeout = None
        self.timed_out = True

        self.handle_exception(
            RequestTimeout,
            RequestTimeout('Timeout while processing request.'),
            None
        )

    def handle_exception(self, typ, val, tb):
        log.debug(
            ['STREAM %i Error' % self.stream_id,
             typ, val, traceback.format_tb(tb)]
        )
        self.finish(val)

    def handle_event(self, event):
        # read headers
        if isinstance(event, h2.events.ResponseReceived):
            self.process_headers(event)
        elif isinstance(event, h2.events.DataReceived):
            # TODO: decompress if necessary
            self._chunks.append(event.data)
        elif isinstance(event, h2.events.WindowUpdated):
            self.flow_control_window.produce(event.delta)
        elif isinstance(event, h2.events.StreamEnded):
            self.perform_redirect()
            self.finish()
        elif isinstance(event, h2.events.StreamReset):
            self.finish()

    @gen.coroutine
    def begin_request(self):
        if not self.connection.is_ready:
            raise TH2CError('Connection not ready!')

        parsed = urlsplit(to_unicode(self.request.url))
        if (self.request.method not in self.ALLOWED_METHODS and
                not self.request.allow_nonstandard_methods):
            raise TH2CError('Unknown method %s' % self.request.method)

        if 'Host' not in self.request.headers:
            if not parsed.netloc:
                self.request.headers['Host'] = self.connection.host
            elif '@' in parsed.netloc:
                self.request.headers['Host'] = parsed.netloc.rpartition('@')[-1]
            else:
                self.request.headers['Host'] = parsed.netloc

        if self.request.user_agent:
            self.request.headers['User-Agent'] = self.request.user_agent

        if self.request.body is not None:
            self.request.headers['Content-Length'] = str(len(self.request.body))

        if (
            self.request.method == 'POST'
            and 'Content-Type' not in self.request.headers
        ):
            self.request.headers['Content-Type'] = (
                'application/x-www-form-urlencoded'
            )

        self.request.url = (
            (parsed.path or '/') +
            (('?' + parsed.query) if parsed.query else '')
        )

        self.scheme = parsed.scheme

        http2_headers = [
            (':authority', self.request.headers.pop('Host')),
            (':path', self.request.url),
            (':scheme', self.scheme),
            (':method', self.request.method),
        ] + self.request.headers.items()

        # send headers
        log.debug('STREAM %d Sending headers', self.stream_id)
        if self.connection.is_ready:
            self.connection.h2conn.send_headers(
                self.stream_id, http2_headers, end_stream=not self.request.body
            )
            self.connection.flush()

        # send body, if any
        if self.request.body:
            yield self.send_body()

    @gen.coroutine
    def send_body(self):
        log.debug(
            'STREAM %d Attempting to send body of %d length',
            self.stream_id, len(self.request.body)
        )
        total = len(self.request.body)
        sent = 0

        while (
            sent < total
            and not self.timed_out
            and self.connection.is_ready
        ):
            log.debug(
                'STREAM %d Waiting for windows to be available!',
                self.stream_id
            )

            try:
                yield self.flow_control_window.available()
                yield self.connection.flow_control_window.available()
            except AttributeError:
                return

            # we might have timed out after waiting
            # for control windows to become available
            if self.timed_out or not self.connection.is_ready:
                return

            remaining = total - sent
            sw = self.flow_control_window.value
            log.debug(
                'STREAM %d STREAM window has %d available',
                self.stream_id, sw
            )
            cw = self.connection.flow_control_window.value
            log.debug(
                'STREAM %d CONNECTION window has %d available',
                self.stream_id, cw
            )
            to_send = min(self.max_frame_size, sw, cw, remaining)
            log.debug(
                'STREAM %d Will consume %d',
                self.stream_id, to_send
            )
            if not to_send:
                # if the minimum is 0, we probably got it from
                # connection window, so let's try again later
                continue

            # consume what we need from flow control windows, and send
            self.flow_control_window.consume(to_send)
            self.connection.flow_control_window.consume(to_send)

            # we consumed another chunk, let's try to send it
            try:
                end_stream = False
                if sent + to_send >= total:
                    end_stream = True
                data_chunk = self.request.body[sent:sent + to_send]
                sent += to_send

                self.connection.h2conn.send_data(
                    self.stream_id, data_chunk, end_stream=end_stream
                )
                self.connection.flush()
            except Exception:
                log.error(
                    'STREAM %d could not send body chunk',
                    self.stream_id, exc_info=True
                )
                self.flow_control_window.produce(to_send)
                self.connection.flow_control_window.produce(to_send)

    def process_headers(self, event):
        """
        Called when headers are received to parse and redirect if necessary.
        :param event: h2 event containing received headers
        :type event: h2.events.ResponseReceived
        """
        # TODO: look at content-encoding and set the decompressor
        log.debug('STREAM %d processing headers', self.stream_id)
        headers = httputil.HTTPHeaders()
        for name, value in event.headers:
            headers.add(name, value)

        self.headers = headers
        self.code = int(headers.pop(':status'))
        self.reason = httplib.responses.get(self.code, 'Unknown')

        start_line = httputil.ResponseStartLine(
            'HTTP/2.0', self.code, self.reason
        )

        if self.request.header_callback is not None:
            # Reassemble the start line.
            self.request.header_callback('%s %s %s\r\n' % start_line)

            for k, v in self.headers.get_all():
                self.request.header_callback('%s: %s\r\n' % (k, v))

            self.request.header_callback('\r\n')

    def perform_redirect(self):
        """
        Checks and performs a redirect if needed.
        Conditions to follow a redirect are:
            - request.follow_redirects == True
            - request.max_redirects > 0
            - status code one of 301, 302, 303, 307, 308
        """
        if (
            self.request.follow_redirects
            and self.request.max_redirects > 0
            and self.code in REDIRECT_HTTP_CODES
        ):
            redirect_request = copy.copy(self.request)
            redirect_request.url = urljoin(
                self.request.url, self.headers['Location']
            )

            redirect_request.max_redirects = self.request.max_redirects - 1

            redirect_request.original_request = self.request

            # cleanup
            self.callback_cleanup()
            self.connection.end_stream(self)

            # remove timeout and update the timeout for the redirect request
            self.io_loop.remove_timeout(self._timeout)
            self._timeout = None
            if self.request.request_timeout:
                time_spent = self.io_loop.time() - self.request.start_time
                redirect_request.request_timeout = (
                    self.request.request_timeout - time_spent
                )

            # determine connection info for the new request
            # secure, host and port
            parsed = urlsplit(to_unicode(self.headers['Location']))
            secure = True
            if parsed.scheme == 'http':
                secure = False

            netloc = parsed.netloc.split(':')
            if len(netloc) == 2:
                host = netloc[0]
                port = int(netloc[1])
            else:
                host = netloc[0]
                port = 443
                if not secure:
                    port = 80

            log.debug(
                'STREAM %d redirecting to %s:%d', self.stream_id, host, port
            )

            cb_response = self.callback_response
            self.callback_response = None
            client = self.client_cls(
                host, port, secure=secure, auto_reconnect=False,
                verify_certificate=self.request.validate_cert
            )

            client.fetch(redirect_request, callback=cb_response)

    def finish(self, exc=None):
        log.debug('STREAM %d finished', self.stream_id)

        if self._timeout:
            self.io_loop.remove_timeout(self._timeout)
            self._timeout = None

        if exc:
            response = exc
        else:
            # compose the body
            data = io.BytesIO(b''.join(self._chunks))

            # extract the original request if a redirect was made
            request = getattr(self.request, 'original_request', self.request)

            response = HTTP2Response(
                request,
                self.code,
                reason=self.reason,
                headers=self.headers,
                buffer=data,
                request_time=self.io_loop.time() - request.start_time,
                effective_url=self.request.url
            )

        # do cleanup
        if self.callback_response:
            self.connection.end_stream(self)
            self.callback_cleanup()
            self.callback_response(response)
