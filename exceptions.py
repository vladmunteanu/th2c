from tornado.httpclient import HTTPError


class HTTP2Error(HTTPError):
    pass


class HTTP2ConnectionTimeout(HTTP2Error):
    def __init__(self, time_cost=None, reason=None):
        super(HTTP2ConnectionTimeout, self).__init__(599)
        self.time_cost = time_cost
        self.reason = reason


class HTTP2ConnectionClosed(HTTP2Error):
    def __init__(self, reason=None):
        super(HTTP2ConnectionClosed, self).__init__(599)
        self.reason = reason


class _RequestTimeout(Exception):
    pass

