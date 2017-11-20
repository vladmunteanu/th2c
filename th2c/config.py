# Default size of the flow control window.
# Used to initialize the connection flow control window,
# as well as the window sizes for initiated streams.
DEFAULT_WINDOW_SIZE = 65535  # bytes

# Maximum size of an HTTP/2 frame.
MAX_FRAME_SIZE = 16384  # bytes

# Maximum number of concurrent streams (concurrent requests).
MAX_CONCURRENT_STREAMS = 100

# Default number of seconds to wait for the connection to be established.
DEFAULT_CONNECTION_TIMEOUT = 5  # seconds

# Default number of seconds to wait before trying to reconnect.
# This setting is used only if AsyncHTTP2Client has been instantiated
# with auto_reconnect=True.
DEFAULT_RECONNECT_INTERVAL = 1  # seconds
