import mock

from tornado import gen
from tornado.testing import AsyncTestCase, gen_test
from tornado.httpclient import HTTPRequest

from th2c.client import AsyncHTTP2Client
from th2c.connection import HTTP2ClientConnection
from th2c.stream import HTTP2ClientStream
from th2c.exceptions import RequestTimeout, TH2CError


class AsyncHTTP2ClientTestCase(AsyncTestCase):

    def tearDown(self):
        # make sure we reset the clients so that
        # we get new instances for each test
        AsyncHTTP2Client.CLIENT_INSTANCES = dict()

    def test_one_instance_per_host_and_port(self):
        connection_cls = mock.create_autospec(HTTP2ClientConnection)

        some_client_1 = AsyncHTTP2Client(
            'some_host', 0, _connection_cls=connection_cls, io_loop=self.io_loop
        )
        some_client_2 = AsyncHTTP2Client(
            'some_host', 0, _connection_cls=connection_cls, io_loop=self.io_loop
        )

        self.assertEqual(id(some_client_1), id(some_client_2))

    def test_different_instances_per_host_and_port(self):
        connection_cls = mock.create_autospec(HTTP2ClientConnection)

        some_client_1 = AsyncHTTP2Client(
            'some_host', 1, _connection_cls=connection_cls, io_loop=self.io_loop
        )
        some_client_2 = AsyncHTTP2Client(
            'some_host', 0, _connection_cls=connection_cls, io_loop=self.io_loop
        )

        self.assertNotEqual(id(some_client_1), id(some_client_2))

    def test_maximum_active_requests(self):
        connection_inst = mock.MagicMock(spec=HTTP2ClientConnection)
        connection_cls = mock.create_autospec(HTTP2ClientConnection)
        connection_cls.return_value = connection_inst

        stream_cls = mock.create_autospec(HTTP2ClientStream)

        maxmimum_active_requests = 1

        client = AsyncHTTP2Client(
            'host', 1234,
            max_active_requests=maxmimum_active_requests,
            _connection_cls=connection_cls,
            _stream_cls=stream_cls,
            io_loop=self.io_loop
        )

        req1 = HTTPRequest(url='host', method='GET')
        req2 = HTTPRequest(url='host', method='GET')

        client.fetch(req1)
        client.fetch(req2)

        # make connection ready and signal the client
        connection_inst.is_ready.return_value = True
        client.process_pending_requests()

        self.assertEqual(len(client.active_requests), maxmimum_active_requests)

        client.max_active_requests += 1
        client.process_pending_requests()
        self.assertEqual(
            len(client.active_requests), maxmimum_active_requests + 1
        )

    def test_max_active_requests_not_updated(self):
        connection_inst = mock.MagicMock(spec=HTTP2ClientConnection)
        connection_cls = mock.create_autospec(HTTP2ClientConnection)
        connection_cls.return_value = connection_inst

        max_active_requests = 1

        client = AsyncHTTP2Client(
            'host', 1234,
            max_active_requests=max_active_requests,
            _connection_cls=connection_cls,
            io_loop=self.io_loop
        )

        client.process_pending_requests = mock.MagicMock()

        import h2.settings

        event_setting = mock.MagicMock()
        event_setting.original_value = max_active_requests
        event_setting.new_value = max_active_requests + 1

        event = mock.MagicMock()

        event.changed_settings = {
            h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS: event_setting
        }

        client.on_settings_changed(event)

        # the processed settings contain a bigger max_active_requests value,
        client.process_pending_requests.assert_not_called()

        event_setting.original_value = max_active_requests + 1
        event_setting.original_value = max_active_requests + 1

    @gen_test
    def test_queue_timeout(self):
        connection_inst = mock.MagicMock(spec=HTTP2ClientConnection)
        connection_inst.is_ready = False
        connection_cls = mock.create_autospec(HTTP2ClientConnection)
        connection_cls.return_value = connection_inst

        stream_cls = mock.create_autospec(HTTP2ClientStream)

        client = AsyncHTTP2Client(
            'host', 1234,
            _connection_cls=connection_cls,
            _stream_cls=stream_cls,
            io_loop=self.io_loop
        )

        req1 = HTTPRequest(url='host', method='GET', request_timeout=1)

        f = client.fetch(req1)
        result = {}

        def future_done(fut):
            try:
                result['r'] = fut.result()
            except Exception as e:
                result['r'] = e

        f.add_done_callback(future_done)

        yield gen.sleep(1.1)

        self.assertIsInstance(result['r'], RequestTimeout)

    def test_close(self):
        connection_inst = mock.MagicMock(spec=HTTP2ClientConnection)
        connection_inst.is_ready = False
        connection_cls = mock.create_autospec(HTTP2ClientConnection)
        connection_cls.return_value = connection_inst

        stream_cls = mock.create_autospec(HTTP2ClientStream)

        client = AsyncHTTP2Client(
            'host', 1234,
            _connection_cls=connection_cls,
            _stream_cls=stream_cls,
            io_loop=self.io_loop
        )
        client.on_connection_closed = mock.MagicMock()

        client.close()

        self.assertEqual(client.closed, True)
        connection_inst.close.assert_called_once()

    def test_connection_closed_stops_pending(self):
        connection_inst = mock.MagicMock(spec=HTTP2ClientConnection)
        connection_inst.is_ready = False
        connection_cls = mock.create_autospec(HTTP2ClientConnection)
        connection_cls.return_value = connection_inst

        stream_cls = mock.create_autospec(HTTP2ClientStream)

        client = AsyncHTTP2Client(
            'host', 1234,
            _connection_cls=connection_cls,
            _stream_cls=stream_cls,
            io_loop=self.io_loop
        )

        # add 2 pending requests
        f1 = client.fetch(HTTPRequest(url='host', method='GET'))
        f2 = client.fetch(HTTPRequest(url='host', method='GET'))

        results = []

        def future_done(f):
            results.append(f.exc_info()[1])

        f1.add_done_callback(future_done)
        f2.add_done_callback(future_done)

        # assert len of pending requests is 2
        self.assertEqual(len(client.pending_requests), 2)

        # call on_connection_closed with client.auto_reconnect = False
        client.auto_reconnect = False
        client.on_connection_closed('it closed')

        # assert len of pending requests is 0
        self.assertEqual(len(client.pending_requests), 0)
        for res in results:
            self.assertIsInstance(res, TH2CError)

    @gen_test
    def test_connection_closed_auto_reconnect(self):
        connection_inst = mock.MagicMock(spec=HTTP2ClientConnection)
        connection_inst.is_ready = False
        connection_cls = mock.create_autospec(HTTP2ClientConnection)
        connection_cls.return_value = connection_inst

        stream_cls = mock.create_autospec(HTTP2ClientStream)

        client = AsyncHTTP2Client(
            'host', 1234,
            _connection_cls=connection_cls,
            _stream_cls=stream_cls,
            io_loop=self.io_loop
        )

        # add 2 pending requests
        f1 = client.fetch(HTTPRequest(url='host', method='GET'))
        f2 = client.fetch(HTTPRequest(url='host', method='GET'))

        results = []

        def future_done(f):
            results.append(f.exc_info() or f.result())

        f1.add_done_callback(future_done)
        f2.add_done_callback(future_done)

        # assert len of pending requests is 2
        self.assertEqual(len(client.pending_requests), 2)

        # call on_connection_closed with client.auto_reconnect = True
        client.auto_reconnect = True
        client.auto_reconnect_interval = 1

        connection_inst.reset_mock()
        client.on_connection_closed('it closed')

        connection_inst.connect.assert_not_called()
        self.assertEqual(client.connection, None)

        # assert len of pending requests is 2
        self.assertEqual(len(client.pending_requests), 2)

        yield gen.sleep(1)
        client.connection.connect.assert_called_once()
