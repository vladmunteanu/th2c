import functools
import logging
import socket
import ssl
import traceback

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h2.settings
from tornado import stack_context
from tornado.httpclient import HTTPError
from tornado.ioloop import IOLoop

from .flowcontrol import FlowControlWindow
from .config import DEFAULT_WINDOW_SIZE

log = logging.getLogger(__name__)


class HTTP2ClientConnection(object):

    def __init__(self, host, port, tcp_client, secure,
                 on_connection_ready=None, on_connection_closed=None,
                 connect_timeout=None, ssl_options=None,
                 max_concurrent_streams=None,
                 *args, **kwargs):
        """
        :param host:
        :param port:
        :param tcp_client:
        :type tcp_client: tornado.tcpclient.TCPClient
        :param secure: Boolean flag indicating whether this connection should be secured over TLS or not
        :type secure: bool
        :param on_connection_ready:
        :param on_connection_closed:
        :param connect_timeout:
        :param ssl_options: dictionary with ssl options that will be passed
        """
        self.host = host
        self.port = port
        self.secure = secure

        self.tcp_client = tcp_client
        self.on_connection_ready = on_connection_ready
        self.on_connection_closed = on_connection_closed

        # private value, set when the socket opens
        self._is_connected = False

        # private value, set when the connection is ready to send streams on
        self._is_ready = False

        # private value, set when settings have been negotiated after
        # the connection is established
        self._negotiated_settings = False

        # Checked when the socket connection is made,
        # to make sure we didn't connect too late.
        self.timed_out = False

        # Underlying Tornado IOStream (or SSLIOStream) object
        self.io_stream = None

        self.connect_timeout = connect_timeout
        self._connect_timeout_t = None

        self.h2conn = None

        self._ongoing_streams = dict()
        self.event_handlers = dict()

        self.ssl_context = None
        self.ssl_options = ssl_options or dict()

        # parse ssl options and create the appropriate SSLContext, if necessary
        self.parse_ssl_opts()

        self.max_concurrent_streams = max_concurrent_streams

        self.initial_window_size = DEFAULT_WINDOW_SIZE
        self.flow_control_window = None

    def parse_ssl_opts(self):
        """
        Parses self.ssl_options and creates a SSLContext if self.secure is True.
        """
        if not self.secure:
            return

        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.options |= (
             ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_COMPRESSION
        )

        if not self.ssl_options.get('verify_certificate', True):
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        ssl_context.set_ciphers("ECDHE+AESGCM")
        ssl_context.set_alpn_protocols(["h2"])

        self.ssl_context = ssl_context

    def connect(self):
        # don't try to connect twice,
        # it seems we are already waiting for a connection
        if self._connect_timeout_t:
            log.warning("Tried to connect while waiting for a connection!")
            return

        self.timed_out = False
        self._is_connected = False
        self._is_ready = False

        # set the connection timeout
        start_time = IOLoop.current().time()
        self._connect_timeout_t = IOLoop.current().add_timeout(
            start_time + self.connect_timeout, self.on_timeout
        )

        # connect the tcp client, passing self.on_connect as callback
        with stack_context.ExceptionStackContext(functools.partial(self.on_error, "during connection")):
            self.tcp_client.connect(
                self.host, self.port, af=socket.AF_UNSPEC,
                ssl_options=self.ssl_context,  # self.ssl_options,
                callback=self.on_connect
            )

    def close(self):
        """ TODO: close the connection, sending the GOAWAY frame. """
        self._is_ready = False
        self._is_connected = False

        if self._connect_timeout_t:
            IOLoop.instance().remove_timeout(self._connect_timeout_t)
            self._connect_timeout_t = None

        self.h2conn.close_connection()

        try:
            self.flush()
        except:
            log.error(
                "Could not send GOAWAY frame, connection terminated!",
                exc_info=True
            )
        finally:
            self.h2conn = None
            self.flow_control_window = None

        try:
            self.io_stream.close()
        except:
            log.error("Could not close IOStream!", exc_info=True)

        # TODO: close all ongoing streams?

    @property
    def is_connected(self):
        return self._is_connected

    @property
    def is_ready(self):
        return self._is_ready

    def on_connect(self, io_stream):
        log.info(["IOStream opened", io_stream])
        if self.timed_out:
            log.info("Connection timeout!")
            io_stream.close()
            return

        self._is_connected = True

        # remove the connection timeout
        IOLoop.current().remove_timeout(self._connect_timeout_t)
        self._connect_timeout_t = None

        self.io_stream = io_stream
        self.io_stream.set_nodelay(True)

        # set the close callback
        self.io_stream.set_close_callback(
            functools.partial(self.on_close, io_stream.error)
        )

        # initialize the connection
        self.h2conn = h2.connection.H2Connection(
            h2.config.H2Configuration(client_side=True)
        )

        # initiate the h2 connection
        self.h2conn.initiate_connection()

        # disable server push
        self.h2conn.update_settings({
            h2.settings.SettingCodes.ENABLE_PUSH: 0,
            h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS:
                self.max_concurrent_streams,
            h2.settings.SettingCodes.INITIAL_WINDOW_SIZE:
                self.initial_window_size
        })

        # set the stream reading callback
        with stack_context.ExceptionStackContext(functools.partial(self.on_error, "during read")):
            self.io_stream.read_bytes(
                num_bytes=65535,
                streaming_callback=self.data_received,
                callback=self.data_received
            )

        self.flush()

        # self.on_connection_ready()

    def on_close(self, reason):
        """
        TODO: clean up on_close logic,
        this seems to be called from many places,
        maybe we need to call close() instead.
        """
        log.info(["IOStream closed with reason", reason])
        # cleanup
        self._is_connected = False
        self.h2conn = None

        if self.io_stream:
            try:
                self.io_stream.close()
            except:
                log.error("Error trying to close stream", exc_info=True)
            finally:
                self.io_stream = None

        # callback connection closed
        self.on_connection_closed(reason)

    def on_timeout(self):
        """
        Connection timed out.
        """
        log.info(
            "HTTP2ClientConnection timed out after {}".format(
                self.connect_timeout
            )
        )
        self.timed_out = True
        self._connect_timeout_t = False
        self.on_close(HTTPError(599))

    def on_error(self, phase, typ, val, tb):
        """
        Connection error.
        :param phase: phase we encountered the error in
        :param typ: type of error
        :param val: error
        :param tb: traceback information
        """
        log.error(
            ["HTTP2ClientConnection error ", phase, typ, val, traceback.format_tb(tb)]
        )
        self.on_close(val)

    def data_received(self, data):
        log.info(["Received data on IOStream", len(data)])
        try:
            events = self.h2conn.receive_data(data)
            log.info(["Events to process", events])
            if events:
                self.process_events(events)
        except:
            log.info(
                "Could not process events received on the HTTP/2 connection",
                exc_info=True
            )

    def process_events(self, events):
        """
        Processes events received on the HTTP/2 connection and
        dispatches them to their corresponding HTTPStreams.
        """
        recv_streams = dict()

        for event in events:
            log.info(["PROCESSING EVENT", event])
            stream_id = getattr(event, 'stream_id', None)

            if isinstance(event, h2.events.DataReceived):
                recv_streams[stream_id] = recv_streams.get(stream_id, 0) + event.flow_controlled_length
            elif isinstance(event, h2.events.WindowUpdated):
                if stream_id == 0:
                    self.flow_control_window.produce(event.delta)
                    log.info("INCREMENTED CONNECTION WINDOW BY %d, NOW AT %d", event.delta, self.flow_control_window.value)
            elif isinstance(event, h2.events.RemoteSettingsChanged):
                self.process_settings(event)

            if stream_id and stream_id in self._ongoing_streams:
                stream = self._ongoing_streams[stream_id]
                with stack_context.ExceptionStackContext(stream.handle_exception):
                    stream.handle_event(event)

            if type(event) in self.event_handlers:
                for ev_handler in self.event_handlers[type(event)]:
                    ev_handler(event)

        recv_connection = 0
        for stream_id, num_bytes in recv_streams.iteritems():
            if not num_bytes or stream_id not in self._ongoing_streams:
                continue
            recv_connection += num_bytes

            try:
                log.info(
                    "Incrementing flow control window for stream %d with %d",
                    stream_id, num_bytes
                )
                self.h2conn.increment_flow_control_window(num_bytes, stream_id)
            except h2.exceptions.StreamClosedError:
                # TODO: maybe cleanup stream?
                log.warning("Failed to increment flow control window for closed stream")
            except KeyError:
                log.error("WEIRD %i is stream id in ongoing_streams? %s", stream_id, stream_id in self._ongoing_streams, exc_info=True)

        if recv_connection:
            log.info(
                "Incrementing window flow control with %d", recv_connection
            )
            self.h2conn.increment_flow_control_window(recv_connection)

        # flush data that has been generated after processing these events
        self.flush()

    def process_settings(self, event):
        """
        Called to process a RemoteSettingsChanged event.
        :param event: a RemoteSettingsChanged event
        :type event: h2.events.RemoteSettingsChanged
        """

        initial_window_size = event.changed_settings.get(
            h2.settings.SettingCodes.INITIAL_WINDOW_SIZE
        )
        if initial_window_size:
            self.initial_window_size = initial_window_size

        for name, value in event.changed_settings.iteritems():
            log.info("Received setting %s : %s", name, value)

        if not self._negotiated_settings:
            self._negotiated_settings = True
            self._is_ready = True

            if not self.flow_control_window:
                self.flow_control_window = FlowControlWindow(
                    initial_value=self.initial_window_size
                )

            self.on_connection_ready()

    def begin_stream(self, stream):
        stream_id = self.h2conn.get_next_available_stream_id()
        self._ongoing_streams[stream_id] = stream
        return stream_id

    def end_stream(self, stream):
        del self._ongoing_streams[stream.stream_id]

    def flush(self):
        data_to_send = self.h2conn.data_to_send()
        if data_to_send:
            log.info("Flushing %d bytes to IOStream", len(data_to_send))
            self.io_stream.write(data_to_send)

    def add_event_handler(self, event, handler):
        if event not in self.event_handlers:
            self.event_handlers[event] = set()

        self.event_handlers[event].add(handler)

    def remove_event_handler(self, event, handler):
        if event in self.event_handlers:
            self.event_handlers[event].remove(handler)
