import datetime
import json
import logging

from tornado import gen
from tornado.ioloop import IOLoop
from tornado.httpclient import HTTPRequest, HTTPError

from .client import AsyncHTTP2Client

logging.basicConfig(
    filename='/opt/dev/th2c/logs/th2c_run_%s.log' % (datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
    level=logging.DEBUG,
    format='[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s'
)

logging.getLogger('hpack').setLevel(logging.INFO)


@gen.coroutine
def main():
    # client = AsyncHTTP2Client(host="nghttp2.org", port=443, secure=True)
    # client = AsyncHTTP2Client(host="google.com", port=443, secure=True)
    # client = AsyncHTTP2Client(host="requestb.in", port=443, secure=True)
    client = AsyncHTTP2Client(
        host="localhost", port=8080, secure=True, verify_certificate=False, max_active_requests=10
    )

    # client = AsyncHTTP2Client(
    #     host="google.ro", port=443, secure=True, verify_certificate=False
    # )

    # req = HTTPRequest(
    #     # url="https://nghttp2.org/",
    #     # url="https://google.ro/",
    #     # url="https://requestb.in/u3mh8ku3",
    #     url="https://localhost:8080",
    #     method="POST",
    #     # method="GET",
    #     request_timeout=3,
    #     headers={
    #         'User-Agent': "th2c"
    #     },
    #     body=json.dumps({'test': 'a'})
    # )
    #
    # try:
    #     r = yield client.fetch(req)
    #     logging.info(["GOT RESPONSE!!!!!!!!", r.code, r.headers, r.body])
    # except:
    #     logging.error("Could not fetch", exc_info=True)

    requests = []
    for i in range(0, 100):
        req = HTTPRequest(
            url="https://localhost:8080",
            method="POST",
            request_timeout=15,  # seconds
            headers={
                'User-Agent': "th2c"
            },
            body=json.dumps({'test': 'a', 'number': i})
        )

        f = client.fetch(req)
        requests.append(f)

    j = 0
    for f in requests:
        try:
            r = yield f
            j += 1
            logging.info(["GOT RESPONSE!!!!!!!!", r.code, r.headers, r.body])
        except Exception as e:
            pass

    logging.info("FINISHED %d requests", j)
    print("FINISHED %d requests" % j)


if __name__ == "__main__":
    IOLoop.current().run_sync(main)
