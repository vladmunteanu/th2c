import logging
import sys

from tornado import gen
from tornado.ioloop import IOLoop
from tornado.httpclient import HTTPRequest, HTTPError

from .client import AsyncHTTP2Client

logging.basicConfig(
    stream=sys.stdout,
    level=logging.DEBUG,
    format='[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s'
)


@gen.coroutine
def main():
    # client = AsyncHTTP2Client(host="nghttp2.org", port=443)
    client = AsyncHTTP2Client(host="google.com", port=443)
    # client = AsyncHTTP2Client(host="requestb.in", port=443)

    req = HTTPRequest(
        # url="https://nghttp2.org/",
        url="https://google.com/",
        # url="https://requestb.in/u3mh8ku3",
        method="get",
        validate_cert=False,
        request_timeout=3
    )

    try:
        r = yield client.fetch(req)
        logging.info(["GOT RESPONSE!!!!!!!!", r, r.__dict__])
    except:
        logging.error("Could not fetch", exc_info=True)


if __name__ == "__main__":
    IOLoop.current().run_sync(main)
