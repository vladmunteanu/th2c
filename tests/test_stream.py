from __future__ import absolute_import
import mock

import h2.events
from tornado import gen
from tornado.testing import AsyncTestCase, gen_test

from th2c.exceptions import RequestTimeout
from th2c.response import HTTP2Response
from th2c.stream import HTTP2ClientStream


class HTTP2ClientStreamTestCase(AsyncTestCase):

    @gen_test
    def test_timeout(self):
        timeout = 1

        connection = mock.MagicMock()
        connection.begin_stream.return_value = 1

        request = mock.MagicMock()
        request.request_timeout = timeout
        request.start_time = self.io_loop.time()

        callback_cleanup = mock.MagicMock()
        callback_response = mock.MagicMock()

        stream = HTTP2ClientStream(
            connection, request,
            callback_cleanup, callback_response, self.io_loop
        )

        self.assertEqual(stream.timed_out, False)

        yield gen.sleep(timeout + 0.1)

        callback_cleanup.assert_called_once()

        callback_response.assert_called_once()
        response_args, response_kwargs = callback_response.call_args
        self.assertIsInstance(response_args[0], RequestTimeout)

        self.assertEqual(stream.timed_out, True)
        self.assertEqual(stream._timeout, None)

    def test_flow_control_window_incremented(self):
        initial_window_size = 10

        connection = mock.MagicMock()
        connection.initial_window_size = initial_window_size
        connection.begin_stream.return_value = 1

        request = mock.MagicMock()
        request.start_time = self.io_loop.time()
        request.request_timeout = 3

        callback_cleanup = mock.MagicMock()
        callback_response = mock.MagicMock()

        stream = HTTP2ClientStream(
            connection, request,
            callback_cleanup, callback_response, self.io_loop
        )

        self.assertEqual(stream.flow_control_window.value, initial_window_size)

        event = mock.Mock(spec=h2.events.WindowUpdated)
        event.delta = initial_window_size

        stream.handle_event(event)

        self.assertEqual(
            stream.flow_control_window.value, initial_window_size * 2
        )

    def test_finish_called_on_stream_ended_or_reset(self):
        connection = mock.MagicMock()
        connection.initial_window_size = 10
        connection.begin_stream.return_value = 1

        request = mock.MagicMock()
        request.start_time = self.io_loop.time()
        request.request_timeout = 3

        callback_cleanup = mock.MagicMock()
        callback_response = mock.MagicMock()

        stream = HTTP2ClientStream(
            connection, request,
            callback_cleanup, callback_response, self.io_loop
        )

        stream.finish = mock.MagicMock()

        event = mock.Mock(spec=h2.events.StreamEnded)

        stream.handle_event(event)
        stream.finish.assert_called_once()

        stream.finish.reset_mock()

        event = mock.Mock(spec=h2.events.StreamReset)
        stream.handle_event(event)
        stream.finish.assert_called_once()

    def test_finish_clean(self):
        connection = mock.MagicMock()
        connection.initial_window_size = 10
        connection.begin_stream.return_value = 1

        request = mock.MagicMock()
        request.start_time = self.io_loop.time()
        request.request_timeout = 3

        callback_cleanup = mock.MagicMock()
        callback_response = mock.MagicMock()

        stream = HTTP2ClientStream(
            connection, request,
            callback_cleanup, callback_response, self.io_loop
        )

        stream.finish()

        callback_response.assert_called_once()
        response_args, response_kwargs = callback_response.call_args
        self.assertIsInstance(response_args[0], HTTP2Response)

    def test_finish_exception(self):
        connection = mock.MagicMock()
        connection.initial_window_size = 10
        connection.begin_stream.return_value = 1

        request = mock.MagicMock()
        request.start_time = self.io_loop.time()
        request.request_timeout = 3

        callback_cleanup = mock.MagicMock()
        callback_response = mock.MagicMock()

        stream = HTTP2ClientStream(
            connection, request,
            callback_cleanup, callback_response, self.io_loop
        )

        exc = Exception('Some error')

        stream.finish(exc)

        callback_response.assert_called_once_with(exc)

    def test_finish_cleanup(self):
        connection = mock.MagicMock()
        connection.initial_window_size = 10
        connection.begin_stream.return_value = 1

        request = mock.MagicMock()
        request.start_time = self.io_loop.time()
        request.request_timeout = 3

        callback_cleanup = mock.MagicMock()
        callback_response = mock.MagicMock()

        stream = HTTP2ClientStream(
            connection, request,
            callback_cleanup, callback_response, self.io_loop
        )

        stream.finish()

        connection.end_stream.assert_called_once_with(stream)

        callback_cleanup.assert_called_once()

        callback_response.assert_called_once()

        self.assertEqual(stream._timeout, None)
