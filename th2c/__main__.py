import os
import datetime
import json
import logging

from tornado import gen
from tornado.ioloop import IOLoop
from tornado.httpclient import HTTPRequest, HTTPError
from tornado.simple_httpclient import SimpleAsyncHTTPClient

from .client import AsyncHTTP2Client

if not os.path.exists('/opt/dev/th2c/logs'):
    os.makedirs('/opt/dev/th2c/logs')


logging.basicConfig(
    filename='/opt/dev/th2c/logs/th2c_run_%s.log' % (datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
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
        request_timeout=2,
        headers={
            'User-Agent': 'th2c',
        },
        body=json.dumps(payload)
    )

    try:
        r = yield async_http_client_ssl.fetch(req)
        logging.info(["Got response", r, r.body])
    except:
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


@gen.coroutine
def main():
    yield test_local()


if __name__ == "__main__":
    IOLoop.current().run_sync(main)
