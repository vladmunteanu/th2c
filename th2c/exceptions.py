class TH2CError(Exception):
    """
    Base class for all exceptions raised by th2c.
    """
    pass


class RequestTimeout(TH2CError):
    """
    Raised when a request times out before receiving a response.
    """
    pass


class ConnectionError(TH2CError):
    """
    Base class for any connection errors
    """
    pass


class ConnectionTimeout(ConnectionError):
    """
    Raised when the HTTP/2 connection could not be established fast enough.
    """
    pass
