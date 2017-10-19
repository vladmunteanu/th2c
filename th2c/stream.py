import base64
import functools
import httplib
import io
import logging
from urlparse import urlsplit

import traceback

import h2.events
from tornado import httputil, log as tornado_log
from tornado.escape import to_unicode, utf8
from tornado.httpclient import HTTPError
from tornado.ioloop import IOLoop

from .response import HTTP2Response

logger = tornado_log.gen_log

log = logging.getLogger(__name__)


class HTTP2ClientStream(object):
    ALLOWED_METHODS = {
        "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"
    }

    def __init__(self, connection, request, callback_remove_active, callback_response):
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

    def on_timeout(self):
        IOLoop.current().remove_timeout(self._timeout)
        self._timeout = None
        self.handle_exception(HTTPError, HTTPError(599), None)

    def handle_exception(self, typ, val, tb):
        log.info(["Error while processing event", typ, val, traceback.format_tb(tb)])
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
        elif isinstance(event, h2.events.StreamEnded):
            self.finish()

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
        log.debug(["Sending headers", http2_headers])
        self.connection.h2_conn.send_headers(
            self.stream_id, http2_headers, end_stream=not self.request.body
        )

        # send body, if any
        if self.request.body:
            # TODO add flow control
            self.connection.h2_conn.send_data(
                self.stream_id, self.request.body, end_stream=True
            )

        # flush everything
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
        IOLoop.current().add_callback(
            functools.partial(self.callback_response, response)
        )
