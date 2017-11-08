import os
import datetime
import json
import time
import logging

from tornado import gen
from tornado.ioloop import IOLoop
from tornado.httpclient import HTTPRequest

from .client import AsyncHTTP2Client

if not os.path.exists('/opt/dev/th2c/logs'):
    os.makedirs('/opt/dev/th2c/logs')


logging.basicConfig(
    filename='/opt/dev/th2c/logs/th2c_run_%s.log' %
             (datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
    level=logging.DEBUG,
    format='[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s'
)

logging.getLogger('hpack').setLevel(logging.INFO)


@gen.coroutine
def test_apple():
    assert("SHOULD NOT RUN THIS" is False)

    host = 'api.development.push.apple.com'
    port = 443
    scheme = 'https'

    payload = {
        'aps': {
            'alert': 'TH2C APNS test message'
        }
    }

    key_file = 'key.pem'
    cert_file = 'cert.pem'
    device_token_file = 'device_token.txt'
    apple_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', '.apple'
    )

    with open(os.path.join(apple_path, device_token_file), 'r') as f:
        device_token = f.read()

    path = '/3/device/{}'.format(device_token)

    async_http_client_ssl = AsyncHTTP2Client(
        host=host,
        port=port,
        secure=True,
        ssl_key=os.path.join(apple_path, key_file),
        ssl_cert=os.path.join(apple_path, cert_file)
    )

    req = HTTPRequest(
        url="{scheme}://{host}:{port}{path}".format(
            scheme=scheme, host=host, port=port, path=path
        ),
        method="POST",
        request_timeout=5,
        headers={
            'User-Agent': 'th2c',
        },
        body=json.dumps(payload)
    )

    try:
        st = time.time()
        r = yield async_http_client_ssl.fetch(req)
        logging.info(["Got response in", time.time() - st, r, r.body])
    except Exception:
        logging.error("Could not fetch request", exc_info=True)


@gen.coroutine
def test_local():
    client = AsyncHTTP2Client(
        host="localhost", port=8080, secure=True,
        verify_certificate=False
    )

    req = HTTPRequest(
        url="https://localhost:8080",
        method="POST",
        request_timeout=5,
        headers={
            'User-Agent': "th2c"
        },
        body=json.dumps({'test': 'a', 'value': 0})
    )

    try:
        st = time.time()
        r = yield client.fetch(req)
        logging.info(
            ["GOT RESPONSE in", time.time() - st, r.code, r.headers, r.body]
        )
    except Exception as e:
        logging.error("Could not fetch", exc_info=True)
        logging.info(["ERROR", e.__dict__])
    finally:
        client.close()


@gen.coroutine
def test_local_many(n):

    client = AsyncHTTP2Client(
        host="localhost", port=8080, secure=True,
        verify_certificate=False, max_active_requests=10
    )

    st = time.time()
    futures = []
    for i in range(n):
        req = HTTPRequest(
            url="https://localhost:8080",
            method="POST",
            request_timeout=5,
            headers={
                'User-Agent': "th2c"
            },
            body=json.dumps({'test': 'a', 'value': i})
        )
        futures.append(client.fetch(req))

    try:
        yield gen.multi_future(futures, quiet_exceptions=(Exception,))
    except Exception:
        logging.error("Something bad happened")

    logging.info(["FINISHED", n, "requests in", time.time() - st])


@gen.coroutine
def main():
    try:
        # yield test_local_many(100)
        yield test_local()
    except Exception:
        logging.error("Test failed", exc_info=True)


if __name__ == "__main__":
    IOLoop.current().run_sync(main)
