import json
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

logging.getLogger('hpack').setLevel(logging.INFO)

@gen.coroutine
def main():
    # client = AsyncHTTP2Client(host="nghttp2.org", port=443, secure=True)
    # client = AsyncHTTP2Client(host="google.com", port=443, secure=True)
    # client = AsyncHTTP2Client(host="requestb.in", port=443, secure=True)
    client = AsyncHTTP2Client(host="localhost", port=8080, secure=True, verify_certificate=False)

    req = HTTPRequest(
        # url="https://nghttp2.org/",
        # url="https://google.com/",
        # url="https://requestb.in/u3mh8ku3",
        url="https://localhost:8080",
        method="POST",
        request_timeout=3,
        headers={
            'User-Agent': "th2c"
        },
        body=json.dumps({'test': 'a'})
    )

    try:
        r = yield client.fetch(req)
        logging.info(["GOT RESPONSE!!!!!!!!", r.code, r.headers, r.body])
    except:
        logging.error("Could not fetch", exc_info=True)

    # req2 = HTTPRequest(
    #     url="https://nghttp2.org/post",
    #     method="POST",
    #     validate_cert=False,
    #     request_timeout=5,
    #     body=json.dumps({'test1': 'test2'})
    # )
    #
    # try:
    #     r2 = yield client.fetch(req2)
    #     logging.info(["GOT RESPONSE!!!!!!!!", r2.code, r2.headers, r2.body])
    # except:
    #     logging.error("Could not fetch", exc_info=True)


if __name__ == "__main__":
    IOLoop.current().run_sync(main)
