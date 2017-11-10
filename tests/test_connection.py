import mock

from tornado import gen
from tornado.testing import AsyncTestCase, gen_test
from tornado.httpclient import HTTPRequest

from th2c.client import AsyncHTTP2Client
from th2c.connection import HTTP2ClientConnection
from th2c.stream import HTTP2ClientStream
from th2c.exceptions import RequestTimeout


class HTTP2ClientConnectionTestCase(AsyncTestCase):

    @gen_test
    def test_connect_timeout(self):
        tcp_client = mock.MagicMock()
        connection_closed = mock.MagicMock()

        connection = HTTP2ClientConnection(
            'host', 1234, tcp_client, False, self.io_loop,
            connect_timeout=1,
            on_connection_closed=connection_closed
        )

        connection.connect()

        yield gen.sleep(1.1)

        connection_closed.assert_called_once()
        self.assertEqual(connection.timed_out, True)

    def test_connection_cleanup(self):
        tcp_client = mock.MagicMock()
        connection_closed = mock.MagicMock()

        connection = HTTP2ClientConnection(
            'host', 1234, tcp_client, False, self.io_loop,
            on_connection_closed=connection_closed
        )

        connection.connect()

        self.assertEqual(connection.closed, False)

        exc = Exception('some error')
        connection.close(exc)

        connection_closed.assert_called_once_with(exc)
        self.assertEqual(connection.is_ready, False)
        self.assertEqual(connection.is_connected, False)
        self.assertEqual(connection.closed, True)
