import httplib
import io
import logging
from urlparse import urlsplit

import traceback

import h2.events
from tornado import httputil, gen
from tornado.escape import to_unicode
from tornado.httpclient import HTTPError
from tornado.ioloop import IOLoop

from .flowcontrol import FlowControlWindow
from .response import HTTP2Response
from .config import DEFAULT_WINDOW_SIZE, MAX_FRAME_SIZE

log = logging.getLogger(__name__)


class HTTP2ClientStream(object):
    ALLOWED_METHODS = {
        "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"
    }

    def __init__(self, connection, request, callback_remove_active,
                 callback_response,
                 window_size=DEFAULT_WINDOW_SIZE, frame_size=MAX_FRAME_SIZE):
        """

        :param connection:
        :type connection: th2c.connection.HTTP2ClientConnection
        :param request:
        :param callback_remove_active:
        :param callback_response:
        :param window_size:
        """
        self.connection = connection
        self.request = request

        self.callback_response = callback_response
        self.callback_remove_active = callback_remove_active

        self.stream_id = None

        self.headers = None
        self.code = None
        self.reason = None

        self.scheme = None

        self._chunks = []

        self.stream_id = self.connection.begin_stream(self)

        self._timeout = None
        if request.request_timeout:
            self._timeout = IOLoop.current().add_timeout(
                self.request.start_time + request.request_timeout,
                self.on_timeout
            )

        self.flow_control_window = FlowControlWindow(initial_value=window_size)
        self.max_frame_size = frame_size

    def on_timeout(self):
        IOLoop.current().remove_timeout(self._timeout)
        self._timeout = None
        self.handle_exception(HTTPError, HTTPError(599, "Timeout while processing request."), None)

    def handle_exception(self, typ, val, tb):
        log.info(["STREAM %i Error while processing event" % self.stream_id, typ, val, traceback.format_tb(tb)])
        self.finish()

    def handle_event(self, event):
        # read headers
        if isinstance(event, h2.events.ResponseReceived):
            # TODO: look at content-encoding and set the decompressor
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
                    self.request.header_callback("%s: %s\r\n" % (k, v))

                self.request.header_callback('\r\n')

        elif isinstance(event, h2.events.DataReceived):
            # TODO: decompress if necessary
            self._chunks.append(event.data)
        elif isinstance(event, h2.events.WindowUpdated):
            self.flow_control_window.produce(event.delta)
        elif isinstance(event, h2.events.StreamEnded):
            self.finish()
        elif isinstance(event, h2.events.StreamReset):
            # TODO: close stream
            self.finish()

    @gen.coroutine
    def begin_request(self):
        parsed = urlsplit(to_unicode(self.request.url))
        if (self.request.method not in self.ALLOWED_METHODS and
                not self.request.allow_nonstandard_methods):
            raise KeyError("unknown method %s" % self.request.method)

        if "Host" not in self.request.headers:
            if not parsed.netloc:
                self.request.headers['Host'] = self.connection.host
            elif '@' in parsed.netloc:
                self.request.headers["Host"] = parsed.netloc.rpartition('@')[-1]
            else:
                self.request.headers["Host"] = parsed.netloc

        if self.request.user_agent:
            self.request.headers["User-Agent"] = self.request.user_agent

        if self.request.body is not None:
            self.request.headers["Content-Length"] = str(len(self.request.body))

        if self.request.method == "POST" and "Content-Type" not in self.request.headers:
            self.request.headers["Content-Type"] = "application/x-www-form-urlencoded"

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
        log.info(["Sending headers", http2_headers])
        self.connection.h2conn.send_headers(
            self.stream_id, http2_headers, end_stream=not self.request.body
        )
        self.connection.flush()

        # send body, if any
        if self.request.body:
            # TODO add flow control
            to_send = len(self.request.body)
            sent = 0
            log.info("Attempting to send body of %d length", len(self.request.body))
            while sent < to_send:
                log.info("STREAM %i Waiting for windows to be available!", self.stream_id)
                yield self.flow_control_window.available()
                yield self.connection.flow_control_window.available()

                remaining = to_send - sent
                sw = self.flow_control_window.value
                log.info("STREAM %i STREAM window has %d available", self.stream_id, sw)
                cw = self.connection.flow_control_window.value
                log.info("STREAM %i CONNECTION window has %d available", self.stream_id, cw)
                to_consume = min(self.max_frame_size, sw, cw, remaining)
                log.info("STREAM %i Will consume %d", self.stream_id, to_consume)
                if to_consume == 0:
                    # if the minimum is 0, we probably got it from connection
                    # window, so let's try again later
                    continue

                consumed = self.flow_control_window.consume(to_consume)
                if consumed < to_consume:
                    raise Exception("STREAM %i Stream window less than minimum available." % self.stream_id)

                consumed = self.connection.flow_control_window.consume(to_consume)
                if consumed < to_consume:
                    raise Exception("STREAM %i Connection window less than minimum available." % self.stream_id)

                # we consumed another chunk, let's send it
                end_stream = False
                if sent + to_consume >= to_send:
                    end_stream = True
                data_chunk = self.request.body[sent:sent + to_consume]
                sent += to_consume

                self.connection.h2conn.send_data(
                    self.stream_id, data_chunk, end_stream=end_stream
                )
                self.connection.flush()

    def finish(self):
        # mark stream as finished
        self.connection.end_stream(self)

        if self._timeout:
            IOLoop.current().remove_timeout(self._timeout)
            self._timeout = None

        # compose the body
        data = io.BytesIO(b''.join(self._chunks))

        response = HTTP2Response(
            self.request,
            self.code,
            reason=self.reason,
            headers=self.headers,
            buffer=data,
            request_time=IOLoop.current().time() - self.request.start_time,
            effective_url=self.request.url
        )

        # run callbacks
        self.callback_remove_active()
        self.callback_response(response)
