# The MIT License (MIT)
#
# Copyright (c) 2019 ladyada for Adafruit Industries
# Copyright (c) 2020 Scott Shawcroft for Adafruit Industries
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""
`adafruit_requests`
================================================================================

A requests-like library for web interfacing


* Author(s): ladyada, Paul Sokolovsky, Scott Shawcroft

Implementation Notes
--------------------

Adapted from https://github.com/micropython/micropython-lib/tree/master/urequests

micropython-lib consists of multiple modules from different sources and
authors. Each module comes under its own licensing terms. Short name of
a license can be found in a file within a module directory (usually
metadata.txt or setup.py). Complete text of each license used is provided
at https://github.com/micropython/micropython-lib/blob/master/LICENSE

author='Paul Sokolovsky'
license='MIT'

**Software and Dependencies:**

* Adafruit CircuitPython firmware for the supported boards:
  https://github.com/adafruit/circuitpython/releases

"""

import gc

__version__ = "0.0.0-auto.0"
__repo__ = "https://github.com/adafruit/Adafruit_CircuitPython_Requests.git"


class _RawResponse:
    def __init__(self, response):
        self._response = response

    def read(self, size=-1):
        if size == -1:
            return self._response.content
        return self._response.socket.recv(size)

    def readinto(self, buf):
        return self._response._readinto(buf)


class Response:
    """The response from a request, contains all the headers/content"""

    encoding = None

    def __init__(self, sock, session=None):
        self.socket = sock
        self.encoding = "utf-8"
        self._cached = None
        self._headers = {}

        # _start_index and _receive_buffer are used when parsing headers.
        # _receive_buffer will grow by 32 bytes everytime it is too small.
        self._received_length = 0
        self._receive_buffer = bytearray(32)
        self._remaining = None
        self._chunked = False

        self._backwards_compatible = not hasattr(sock, "recv_into")
        if self._backwards_compatible:
            print("Socket missing recv_into. Using more memory to be compatible")

        http = self._readto(b" ")
        if not http:
            raise RuntimeError("Unable to read HTTP response.")
        self.status_code = int(self._readto(b" "))
        self.reason = self._readto(b"\r\n")
        self._parse_headers()
        self._raw = None
        self._content_read = 0
        self._session = session

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _recv_into(self, buf, size=0):
        if self._backwards_compatible:
            size = len(buf) if size == 0 else size
            b = self.socket.recv(size)
            read_size = len(b)
            buf[:read_size] = b
            return read_size
        else:
            return self.socket.recv_into(buf, size)

    def _readto(self, first, second=b""):
        buf = self._receive_buffer
        end = self._received_length
        while True:
            firsti = buf.find(first, 0, end)
            secondi = -1
            if second:
                secondi = buf.find(second, 0, end)

            i = -1
            needle_len = 0
            if firsti >= 0:
                i = firsti
                needle_len = len(first)
            if secondi >= 0 and (firsti < 0 or secondi < firsti):
                i = secondi
                needle_len = len(second)
            if i >= 0:
                result = buf[:i]
                new_start = i + needle_len

                if i + needle_len <= end:
                    new_end = end - new_start
                    buf[:new_end] = buf[new_start:end]
                    self._received_length = new_end
                return result

            # Not found so load more.

            # If our buffer is full, then make it bigger to load more.
            if end == len(buf):
                new_size = len(buf) + 32
                new_buf = bytearray(new_size)
                new_buf[: len(buf)] = buf
                buf = new_buf
                self._receive_buffer = buf

            read = self._recv_into(memoryview(buf)[end:])
            if read == 0:
                self._received_length = 0
                return buf[:end]
            end += read

        return b""

    def _read_from_buffer(self, buf=None, nbytes=None):
        if self._received_length == 0:
            return 0
        read = self._received_length
        if nbytes < read:
            read = nbytes
        membuf = memoryview(self._receive_buffer)
        if buf:
            buf[:read] = membuf[:read]
        if read < self._received_length:
            new_end = self._received_length - read
            self._receive_buffer[:new_end] = membuf[read : self._received_length]
            self._received_length = new_end
        else:
            self._received_length = 0
        return read

    def _readinto(self, buf):
        if not self.socket:
            raise RuntimeError(
                "Newer Response closed this one. Use Responses immediately."
            )

        if not self._remaining:
            # Consume the chunk header if need be.
            if self._chunked:
                # Consume trailing \r\n for chunks 2+
                if self._remaining == 0:
                    self._throw_away(2)
                chunk_header = self._readto(b";", b"\r\n")
                http_chunk_size = int(chunk_header, 16)
                if http_chunk_size == 0:
                    self._chunked = False
                    self._parse_headers()
                    return 0
                self._remaining = http_chunk_size
            else:
                return 0

        nbytes = len(buf)
        if nbytes > self._remaining:
            nbytes = self._remaining

        read = self._read_from_buffer(buf, nbytes)
        if read == 0:
            read = self._recv_into(buf, nbytes)
        self._remaining -= read

        return read

    def _throw_away(self, nbytes):
        nbytes -= self._read_from_buffer(nbytes=nbytes)

        buf = self._receive_buffer
        for i in range(nbytes // len(buf)):
            self._recv_into(buf)
        remaining = nbytes % len(buf)
        if remaining:
            self._recv_into(buf, remaining)

    def close(self):
        """Drain the remaining ESP socket buffers. We assume we already got what we wanted."""
        if not self.socket:
            return
        # Make sure we've read all of our response.
        if self._cached is None:
            if self._remaining > 0:
                self._throw_away(self._remaining)
            elif self._chunked:
                while True:
                    chunk_header = self._readto(b";", b"\r\n")
                    chunk_size = int(chunk_header, 16)
                    if chunk_size == 0:
                        break
                    self._throw_away(chunk_size + 2)
                self._parse_headers()
        if self._session:
            self._session.free_socket(self.socket)
        else:
            self.socket.close()
        self.socket = None

    def _parse_headers(self):
        """
        Parses the header portion of an HTTP request/response from the socket.
        Expects first line of HTTP request/response to have been read already.
        """
        while True:
            title = self._readto(b": ", b"\r\n")
            if not title:
                break

            content = self._readto(b"\r\n")
            if title and content:
                title = str(title, "utf-8")
                content = str(content, "utf-8")
                # Check len first so we can skip the .lower allocation most of the time.
                if (
                    len(title) == len("content-length")
                    and title.lower() == "content-length"
                ):
                    self._remaining = int(content)
                if (
                    len(title) == len("transfer-encoding")
                    and title.lower() == "transfer-encoding"
                ):
                    self._chunked = content.lower() == "chunked"
                self._headers[title] = content

    @property
    def headers(self):
        """
        The response headers. Does not include headers from the trailer until
        the content has been read.
        """
        return self._headers

    @property
    def content(self):
        """The HTTP content direct from the socket, as bytes"""
        if self._cached is not None:
            if isinstance(self._cached, bytes):
                return self._cached
            raise RuntimeError("Cannot access content after getting text or json")

        self._cached = b"".join(self.iter_content(chunk_size=32))
        return self._cached

    @property
    def text(self):
        """The HTTP content, encoded into a string according to the HTTP
        header encoding"""
        if self._cached is not None:
            if isinstance(self._cached, str):
                return self._cached
            raise RuntimeError("Cannot access text after getting content or json")
        self._cached = str(self.content, self.encoding)
        return self._cached

    def json(self):
        """The HTTP content, parsed into a json dictionary"""
        # pylint: disable=import-outside-toplevel
        import json

        # The cached JSON will be a list or dictionary.
        if self._cached:
            if isinstance(self._cached, (list, dict)):
                return self._cached
            raise RuntimeError("Cannot access json after getting text or content")
        if not self._raw:
            self._raw = _RawResponse(self)

        obj = json.load(self._raw)
        if not self._cached:
            self._cached = obj
        self.close()
        return obj

    def iter_content(self, chunk_size=1, decode_unicode=False):
        """An iterator that will stream data by only reading 'chunk_size'
        bytes and yielding them, when we can't buffer the whole datastream"""
        if decode_unicode:
            raise NotImplementedError("Unicode not supported")

        b = bytearray(chunk_size)
        while True:
            size = self._readinto(b)
            if size == 0:
                break
            if size < chunk_size:
                chunk = bytes(memoryview(b)[:size])
            else:
                chunk = bytes(b)
            yield chunk
        self.close()


class Session:
    def __init__(self, socket_pool, ssl_context=None):
        self._socket_pool = socket_pool
        self._ssl_context = ssl_context
        # Hang onto open sockets so that we can reuse them.
        self._open_sockets = {}
        self._socket_free = {}
        self._last_response = None

    def free_socket(self, socket):
        if socket not in self._open_sockets.values():
            raise RuntimeError("Socket not from session")
        self._socket_free[socket] = True

    def _get_socket(self, host, port, proto, *, timeout=1):
        key = (host, port, proto)
        if key in self._open_sockets:
            sock = self._open_sockets[key]
            if self._socket_free[sock]:
                self._socket_free[sock] = False
                return sock
        if proto == "https:" and not self._ssl_context:
            raise RuntimeError(
                "ssl_context must be set before using adafruit_requests for https"
            )
        addr_info = self._socket_pool.getaddrinfo(
            host, port, 0, self._socket_pool.SOCK_STREAM
        )[0]
        sock = self._socket_pool.socket(addr_info[0], addr_info[1], addr_info[2])
        if proto == "https:":
            sock = self._ssl_context.wrap_socket(sock, server_hostname=host)
        sock.settimeout(timeout)  # socket read timeout
        ok = True
        try:
            sock.connect((host, port))
        except MemoryError:
            if not any(self._socket_free.items()):
                raise
            ok = False

        # We couldn't connect due to memory so clean up the open sockets.
        if not ok:
            free_sockets = []
            for s in self._socket_free:
                if self._socket_free[s]:
                    s.close()
                    free_sockets.append(s)
            for s in free_sockets:
                del self._socket_free[s]
                key = None
                for k in self._open_sockets:
                    if self._open_sockets[k] == s:
                        key = k
                        break
                if key:
                    del self._open_sockets[key]
            # Recreate the socket because the ESP-IDF won't retry the connection if it failed once.
            sock = None  # Clear first so the first socket can be cleaned up.
            sock = self._socket_pool.socket(addr_info[0], addr_info[1], addr_info[2])
            if proto == "https:":
                sock = self._ssl_context.wrap_socket(sock, server_hostname=host)
            sock.settimeout(timeout)  # socket read timeout
            sock.connect((host, port))
        self._open_sockets[key] = sock
        self._socket_free[sock] = False
        return sock

    # pylint: disable=too-many-branches, too-many-statements, unused-argument, too-many-arguments, too-many-locals
    def request(
        self, method, url, data=None, json=None, headers=None, stream=False, timeout=60
    ):
        """Perform an HTTP request to the given url which we will parse to determine
        whether to use SSL ('https://') or not. We can also send some provided 'data'
        or a json dictionary which we will stringify. 'headers' is optional HTTP headers
        sent along. 'stream' will determine if we buffer everything, or whether to only
        read only when requested
        """
        if not headers:
            headers = {}

        try:
            proto, dummy, host, path = url.split("/", 3)
            # replace spaces in path
            path = path.replace(" ", "%20")
        except ValueError:
            proto, dummy, host = url.split("/", 2)
            path = ""
        if proto == "http:":
            port = 80
        elif proto == "https:":
            port = 443
        else:
            raise ValueError("Unsupported protocol: " + proto)

        if ":" in host:
            host, port = host.split(":", 1)
            port = int(port)

        if self._last_response:
            self._last_response.close()
            self._last_response = None

        socket = self._get_socket(host, port, proto, timeout=timeout)
        socket.send(
            b"%s /%s HTTP/1.1\r\n" % (bytes(method, "utf-8"), bytes(path, "utf-8"))
        )
        if "Host" not in headers:
            socket.send(b"Host: %s\r\n" % bytes(host, "utf-8"))
        if "User-Agent" not in headers:
            socket.send(b"User-Agent: Adafruit CircuitPython\r\n")
        # Iterate over keys to avoid tuple alloc
        for k in headers:
            socket.send(k.encode())
            socket.send(b": ")
            socket.send(headers[k].encode())
            socket.send(b"\r\n")
        if json is not None:
            assert data is None
            # pylint: disable=import-outside-toplevel
            try:
                import json as json_module
            except ImportError:
                import ujson as json_module
            data = json_module.dumps(json)
            socket.send(b"Content-Type: application/json\r\n")
        if data:
            if isinstance(data, dict):
                sock.send(b"Content-Type: application/x-www-form-urlencoded\r\n")
                _post_data = ""
                for k in data:
                    _post_data = "{}&{}={}".format(_post_data, k, data[k])
                data = _post_data[1:]
            socket.send(b"Content-Length: %d\r\n" % len(data))
        socket.send(b"\r\n")
        if data:
            if isinstance(data, bytearray):
                socket.send(bytes(data))
            else:
                socket.send(bytes(data, "utf-8"))

        resp = Response(socket, self)  # our response
        if "location" in resp.headers and not 200 <= resp.status_code <= 299:
            raise NotImplementedError("Redirects not yet supported")

        self._last_response = resp
        return resp

    def head(self, url, **kw):
        """Send HTTP HEAD request"""
        return self.request("HEAD", url, **kw)

    def get(self, url, **kw):
        """Send HTTP GET request"""
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        """Send HTTP POST request"""
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        """Send HTTP PUT request"""
        return self.request("PUT", url, **kw)

    def patch(self, url, **kw):
        """Send HTTP PATCH request"""
        return self.request("PATCH", url, **kw)

    def delete(self, url, **kw):
        """Send HTTP DELETE request"""
        return self.request("DELETE", url, **kw)


# Backwards compatible API:

_default_session = None


class FakeSSLContext:
    def wrap_socket(self, socket, server_hostname=None):
        return socket


def set_socket(sock, iface=None):
    global _default_session
    _default_session = Session(sock, FakeSSLContext())
    if iface:
        sock.set_interface(iface)


def request(method, url, data=None, json=None, headers=None, stream=False, timeout=1):
    _default_session.request(
        method,
        url,
        data=data,
        json=json,
        headers=headers,
        stream=stream,
        timeout=timeout,
    )


def head(url, **kw):
    """Send HTTP HEAD request"""
    return _default_session.request("HEAD", url, **kw)


def get(url, **kw):
    """Send HTTP GET request"""
    return _default_session.request("GET", url, **kw)


def post(url, **kw):
    """Send HTTP POST request"""
    return _default_session.request("POST", url, **kw)


def put(url, **kw):
    """Send HTTP PUT request"""
    return _default_session.request("PUT", url, **kw)


def patch(url, **kw):
    """Send HTTP PATCH request"""
    return _default_session.request("PATCH", url, **kw)


def delete(url, **kw):
    """Send HTTP DELETE request"""
    return _default_session.request("DELETE", url, **kw)
