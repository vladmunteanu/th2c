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
from tornado.iostream import StreamClosedError

from .config import (DEFAULT_WINDOW_SIZE,
                     MAX_FRAME_SIZE,
                     DEFAULT_CONNECTION_TIMEOUT,
                     MAX_CONCURRENT_STREAMS)
from .exceptions import ConnectionError, ConnectionTimeout
from .flowcontrol import FlowControlWindow

log = logging.getLogger(__name__)

AlPN_PROTOCOLS = ['h2']


class HTTP2ClientConnection(object):

    def __init__(self, host, port, tcp_client, secure, io_loop,
                 on_connection_ready=None, on_connection_closed=None,
                 connect_timeout=DEFAULT_CONNECTION_TIMEOUT, ssl_options=None,
                 max_concurrent_streams=MAX_CONCURRENT_STREAMS):
        """
        :param host: address host
        :type host: str
        :param port: address port
        :type port: int
        :param tcp_client: tcp client factory
        :type tcp_client: tornado.tcpclient.TCPClient
        :param secure: Boolean flag indicating whether this connection
        should be secured over TLS or not
        :type secure: bool
        :param on_connection_ready: called when connection is ready
        :param on_connection_closed: called when connection is closed
        :param connect_timeout: seconds to wait for a successful connection
        :type connect_timeout: int
        :param ssl_options: ssl options that will be parsed into a SSLContext
        and passed to tcp_client
        :type ssl_options: dict
        :param max_concurrent_streams: maximum number of concurrent streams
        :type max_concurrent_streams: int
        """
        self.io_loop = io_loop

        self.host = host
        self.port = port
        self.secure = secure

        self.tcp_client = tcp_client
        self.on_connection_ready = on_connection_ready
        self.on_connection_closed = on_connection_closed

        self.closed = False

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

        self.max_frame_size = MAX_FRAME_SIZE

    def parse_ssl_opts(self):
        """
        Parses ssl options and creates a SSLContext if self.secure is True.
        """
        if not self.secure:
            return

        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.options |= ssl.OP_NO_TLSv1
        ssl_context.options |= ssl.OP_NO_TLSv1_1

        if not self.ssl_options.get('verify_certificate', True):
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        if self.ssl_options.get('key') and self.ssl_options.get('cert'):
            ssl_context.load_cert_chain(
                self.ssl_options.get('cert'),
                keyfile=self.ssl_options.get('key')
            )

        ssl_context.set_ciphers('ECDHE+AESGCM')
        ssl_context.set_alpn_protocols(AlPN_PROTOCOLS)

        self.ssl_context = ssl_context

    def connect(self):
        if self._connect_timeout_t:
            # don't try to connect twice,
            # it seems we are already waiting for a connection
            log.warning('Tried to connect while waiting for a connection!')
            return

        self.timed_out = False
        self._is_connected = False
        self._is_ready = False
        self.closed = False

        # set the connection timeout
        start_time = self.io_loop.time()
        self._connect_timeout_t = self.io_loop.add_timeout(
            start_time + self.connect_timeout, self.on_timeout
        )

        def _on_tcp_client_connected(f):
            exc = f.exc_info()
            if exc is not None:
                self.on_error('during connection', *exc)
            else:
                self.on_connect(f.result())

        ft = self.tcp_client.connect(
            self.host, self.port, af=socket.AF_UNSPEC,
            ssl_options=self.ssl_context,
        )
        ft.add_done_callback(_on_tcp_client_connected)

    def close(self, reason):
        """ Closes the connection, sending a GOAWAY frame. """
        log.debug('Closing HTTP2Connection with reason %s', reason)

        if self._connect_timeout_t:
            self.io_loop.remove_timeout(self._connect_timeout_t)
            self._connect_timeout_t = None

        if self.h2conn:
            try:
                self.h2conn.close_connection()
                self.flush()
            except Exception:
                log.error(
                    'Could not send GOAWAY frame, connection terminated!',
                    exc_info=True
                )
            finally:
                self.h2conn = None
                self.flow_control_window = None

        if self.io_stream:
            try:
                self.io_stream.close()
            except Exception:
                log.error('Could not close IOStream!', exc_info=True)
            finally:
                self.io_stream = None

        if not reason and self.io_stream and self.io_stream.error:
            reason = self.io_stream.error

        self.closed = True
        self._is_ready = False
        self._is_connected = False
        self.on_connection_closed(reason)

    @property
    def is_connected(self):
        return self._is_connected

    @property
    def is_ready(self):
        return self._is_connected and not self.closed and self._is_ready

    def on_connect(self, io_stream):
        log.debug('IOStream opened %s', type(io_stream))
        if self.timed_out:
            log.debug('Connection timeout before socket connected!')
            io_stream.close()
            return

        if self.secure:
            if io_stream.socket.selected_alpn_protocol() not in AlPN_PROTOCOLS:
                log.error(
                    'Negotiated protocols mismatch, got %s, expected one of %s',
                    io_stream.socket.selected_alpn_protocol(),
                    AlPN_PROTOCOLS
                )
                raise ConnectionError('Negotiated protocols mismatch')

        self._is_connected = True

        # remove the connection timeout
        self.io_loop.remove_timeout(self._connect_timeout_t)
        self._connect_timeout_t = None

        self.io_stream = io_stream
        self.io_stream.set_nodelay(True)

        # set the close callback
        self.io_stream.set_close_callback(self.on_close)

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
        with stack_context.ExceptionStackContext(
                functools.partial(self.on_error, 'during read')
        ):
            self.io_stream.read_bytes(
                num_bytes=65535,
                streaming_callback=self.data_received,
                callback=self.data_received
            )

        self.flush()

    def on_close(self):
        """ Called when the underlying socket is closed. """
        if self.closed:
            return
        log.debug(['IOStream closed with reason', self.io_stream.error])

        err = self.io_stream.error
        if not err:
            err = ConnectionError('Connection closed by remote end!')

        self.end_all_streams(
            type(err), err, None
        )
        self.close(err)

    def on_timeout(self):
        """ Connection timed out. """
        log.debug(
            'HTTP2ClientConnection timed out after %d', self.connect_timeout
        )
        self.timed_out = True

        exc = ConnectionTimeout('Connection could not be established!')

        self.end_all_streams(ConnectionTimeout, exc, None)
        self.close(exc)

    def on_error(self, phase, typ, val, tb):
        """
        Connection error.
        :param phase: phase we encountered the error in
        :param typ: type of error
        :param val: error
        :param tb: traceback information
        """
        if self.closed:
            return
        log.error(
            ['HTTP2ClientConnection error ',
             phase, typ, val, traceback.format_tb(tb)]
        )

        self.end_all_streams(typ, val, tb)
        self.close(val)

    def data_received(self, data):
        log.debug('Received %d bytes on IOStream', len(data))
        try:
            events = self.h2conn.receive_data(data)
            if events:
                self.process_events(events)
        except Exception:
            log.error(
                'Could not process events received on the HTTP/2 connection',
                exc_info=True
            )

    def process_events(self, events):
        """
        Processes events received on the HTTP/2 connection and
        dispatches them to their corresponding HTTPStreams.
        """
        recv_streams = dict()

        for event in events:
            log.debug(['PROCESSING EVENT', event])
            stream_id = getattr(event, 'stream_id', None)

            if type(event) in self.event_handlers:
                for ev_handler in self.event_handlers[type(event)]:
                    ev_handler(event)

            if isinstance(event, h2.events.DataReceived):
                recv_streams[stream_id] = (recv_streams.get(stream_id, 0) +
                                           event.flow_controlled_length)
            elif isinstance(event, h2.events.WindowUpdated):
                if stream_id == 0:
                    self.flow_control_window.produce(event.delta)
                    log.debug(
                        'INCREMENTED CONNECTION WINDOW BY %d, NOW AT %d',
                        event.delta, self.flow_control_window.value
                    )
            elif isinstance(event, h2.events.RemoteSettingsChanged):
                self.process_settings(event)

            if stream_id and stream_id in self._ongoing_streams:
                stream = self._ongoing_streams[stream_id]
                with stack_context.ExceptionStackContext(
                        stream.handle_exception
                ):
                    stream.handle_event(event)

        recv_connection = 0
        for stream_id, num_bytes in recv_streams.iteritems():
            if not num_bytes or stream_id not in self._ongoing_streams:
                continue

            log.debug(
                'Incrementing flow control window for stream %d with %d',
                stream_id, num_bytes
            )
            self.h2conn.increment_flow_control_window(num_bytes, stream_id)

            recv_connection += num_bytes

        if recv_connection:
            log.debug(
                'Incrementing window flow control with %d', recv_connection
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
        for name, value in event.changed_settings.iteritems():
            log.debug('Received setting %s : %s', name, value)

        initial_window_size = event.changed_settings.get(
            h2.settings.SettingCodes.INITIAL_WINDOW_SIZE
        )
        if initial_window_size:
            self.initial_window_size = initial_window_size.new_value

        max_frame_size = event.changed_settings.get(
            h2.settings.SettingCodes.MAX_FRAME_SIZE
        )
        if max_frame_size:
            self.max_frame_size = max_frame_size.new_value

        if not self._negotiated_settings:
            self._negotiated_settings = True
            self._is_ready = True

            if not self.flow_control_window:
                self.flow_control_window = FlowControlWindow(
                    initial_value=self.initial_window_size
                )

            # the initial settings have been negotiated,
            # we can begin sending streams
            self.on_connection_ready()

    def begin_stream(self, stream):
        stream_id = self.h2conn.get_next_available_stream_id()
        self._ongoing_streams[stream_id] = stream
        return stream_id

    def end_stream(self, stream):
        del self._ongoing_streams[stream.stream_id]

    def end_all_streams(self, typ, val, tb):
        for stream_id, stream in self._ongoing_streams.items():
            stream.handle_exception(typ, val, tb)

    def add_event_handler(self, event, handler):
        if event not in self.event_handlers:
            self.event_handlers[event] = set()

        self.event_handlers[event].add(handler)

    def remove_event_handler(self, event, handler):
        if event in self.event_handlers:
            self.event_handlers[event].remove(handler)

    def flush(self):
        data_to_send = self.h2conn.data_to_send()
        if data_to_send:
            log.debug('Flushing %d bytes to IOStream', len(data_to_send))
            try:
                f = self.io_stream.write(data_to_send)
            except StreamClosedError:
                # TODO: not clear whether we should call on_error here.
                log.error('Immediate write exception', exc_info=True)
                return

            f.add_done_callback(self.on_write_done)

    def on_write_done(self, f):
        exc_info = f.exc_info()
        if exc_info:
            if self.closed:
                log.debug('Write exception after connection closed')
            self.on_error('on write', *exc_info)
        else:
            log.debug('No write error')
