===================
Async HTTP/2 Client
===================

A simple asynchronous HTTP/2 client for Tornado_ applications, based on the awesome h2_ library.

Intended for Python 2 (>= 2.7.9).

This package is in a very early development stage, so expect bugs or changes in the API.
If you spot anything wrong, or would like to suggest improvements, please open an issue or a pull request.


=======
Example
=======

Usage in a coroutine may be:

.. code-block:: python

    from th2c import AsyncHTTP2Client
    from tornado.httpclient import HTTPRequest

    client = AsyncHTTP2Client(
        host='nghttp2.org', port=443, secure=True,
    )

    request = HTTPRequest(
        url='https://nghttp2.org/httpbin/get',
        method='GET',
    )

    res = yield client.fetch(request)


====
TODO
====

- moar tests :)
- SERVER_PUSH
- follow_redirects
- priority control


=================
Development setup
=================

If you wish to create a development environment to work on th2c, you can use a Vagrant setup or a virtual environment.
The Vagrant setup is located under vm_, an Ubuntu 16.04 64bit box with Go and Python 2 installed, that maps the project directory under ``/opt/dev/th2c``.

For a minimal set of "integration tests", a Go web server is included in test_server_ that simply echoes back what it receives.

You can run it in debug mode, from the project directory, by executing:

    ``GODEBUG=http2debug=1 go run test_server/main.go``.

After the server has started, you should run the client by executing:

    ``python -m th2c``.

Log files should be produced under /opt/dev/th2c/logs.

You can also run unit tests with: ``nosetests tests``

.. _Tornado: http://www.tornadoweb.org/
.. _h2: https://python-hyper.org/h2/
.. _vm: https://github.com/vladmunteanu/th2c/tree/master/vm
.. _test_server: https://github.com/vladmunteanu/th2c/tree/master/test_server
