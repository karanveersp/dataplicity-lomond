"""
The session manages the mechanics of receiving and sending data over
the websocket.

"""

from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import logging
import math
import select
import socket
import ssl
import threading
import time

from .frame import Frame
from . import errors
from . import events


log = logging.getLogger('lomond')


class SelectorBase(object):
    """Abstraction for a kernel object that waits for socket data."""

    def __init__(self, socket):
        """Construct with an open socket."""
        self._socket = socket

    def wait_readable(self, timeout=0.0):
        """Block until socket is readable or a timeout occurs, return
        `True` if the socket is readable, or `False` if the timeout
        occurred.

        """

    def close(self):
        """Close the selector (not the socket)."""


class SelectSelector(SelectorBase):
    """Select Selector for use on Windows."""

    def __repr__(self):
        return '<SelectSelector>'

    def wait_readable(self, timeout=0.0):
        rlist, _wlist, _xlist = (
            select.select([self._socket.fileno()], [], [], timeout)
        )
        return bool(rlist)


class KQueueSelector(SelectorBase):
    """KQueue selector for MacOS & BSD"""
    def __init__(self, socket):
        super(KQueueSelector, self).__init__(socket)
        self._queue = select.kqueue()
        self._events = [
            select.kevent(
                self._socket.fileno(),
                filter=select.KQ_FILTER_READ
            )
        ]

    def __repr__(self):
        return '<KQueueSelector>'

    def wait_readable(self, timeout=0.0):
        events = self._queue.control(
            self._events, 1, timeout
        )
        return bool(events)

    def close(self):
        self._queue.close()


class PollSelector(SelectorBase):
    """Poll selector for *nix"""
    def __init__(self, socket):
        super(PollSelector, self).__init__(socket)
        self._poll = select.poll()
        events = (
            select.POLLIN |
            select.POLLPRI |
            select.POLLERR |
            select.POLLHUP
        )
        self._poll.register(socket.fileno(), events)

    def __repr__(self):
        return '<PollSelector>'

    def wait_readable(self, timeout):
        events = self._poll.poll(timeout * 1000.0)
        return bool(events)


class WebsocketSession(object):
    """Manages the mechanics of running the websocket."""

    # Pick the appropriate selector for the given platform
    if hasattr(select, 'kqueue'):
        _selector_cls = KQueueSelector
    elif hasattr(select, 'poll'):
        _selector_cls = PollSelector
    else:
        _selector_cls = SelectSelector

    def __init__(self, websocket):
        self.websocket = websocket

        self._address = (websocket.host, websocket.port)
        self._lock = threading.Lock()

        self._sock = None
        self._poll_start = None
        self._next_ping = None
        self._last_pong = None
        self._start_time = None
        self._ready = False

    def __repr__(self):
        return "<ws-session '{}'>".format(self.websocket.url)

    @property
    def _time(self):
        """Get the time since the socket started."""
        return time.time() - self._start_time

    def close(self):
        """Close the websocket, if it is open."""
        self._close_socket()
        self._sock = None

    def write(self, data):
        """Send raw data."""
        with self._lock:
            if self._sock is None:
                log.debug('WebSocket unavailable; data not sent')
                raise errors.WebSocketUnavailable('not connected')
            if self.websocket.is_closed:
                log.debug('WebSocket closed; data not sent')
                raise errors.WebSocketClosed('data not sent')
            if self.websocket.is_closing:
                log.debug('WebSocket closing; data not sent')
                raise errors.WebSocketClosing('data not sent')
            try:
                self._sock.sendall(data)
            except socket.error as error:
                log.debug('WebSocket send error; %s', error)
                raise errors.TransportFail(
                    'socket fail; {}',
                    error
                )
            except Exception as error:
                log.warning('WebSocket send error; %s', error)
                raise errors.TransportFail(
                    'socket error; {}',
                    error
                )

    def send(self, opcode, data):
        """Send a WS Frame."""
        frame = Frame(opcode, payload=data)
        self.write(frame.to_bytes())
        log.debug('CLI -> SRV : %r', frame)

    class _SocketFail(Exception):
        """Used internally to respond to socket fails."""

    class _ForceDisconnect(Exception):
        """Used internally when the close timeout is tripped."""

    @classmethod
    def _socket_fail(cls, msg, *args, **kwargs):
        """Raises a socket fail error to exit select loop."""
        _msg = msg.format(*args, **kwargs)
        log.debug(_msg)
        raise cls._SocketFail(_msg)

    def _connect(self):
        """Create socket and connect."""
        sock = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM
        )
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(30)  # TODO: make a parameter for this?
        if self.websocket.is_secure:
            sock = self._wrap_socket(sock)
        sock.connect(self._address)
        # The timeout makes the socket non-blocking
        # We want to the socket to block after the connection
        sock.settimeout(None)
        return sock

    def _wrap_socket(self, sock):
        """Wrap the socket with an SSL proxy."""
        # sniff SNI support (added Python 2.7.9)
        has_sni = (
            hasattr(ssl, 'SSLContext') and
            getattr(ssl, 'HAS_SNI', False)
        )
        if has_sni:
            _protocol = getattr(
                ssl,
                'PROTOCOL_TLS',  # Supported since 2.7.13
                ssl.PROTOCOL_SSLv23   # Supported since 2.7.9
            )
            ssl_context = ssl.SSLContext(_protocol)
            ssl_sock = ssl_context.wrap_socket(
                sock,
                server_hostname=self.websocket.host
            )
        else:
            # Fallback for no SNI
            log.warning('no SNI support')
            ssl_sock = ssl.wrap_socket(sock)
        return ssl_sock

    def _close_socket(self):
        """Close the socket safely."""
        # Is a no-op if the socket is already closed.
        if self._sock is None:
            return
        try:
            # Get the write lock, so we can be certain data sending
            # in another thread is sent.
            with self._lock:
                self._sock.shutdown(socket.SHUT_RDWR)
                self._sock.close()
        except socket.error:
            # Socket is already closed.
            # That's fine, just a no-op.
            pass
        except Exception as error:
            # Paranoia
            log.warning('error closing socket; %s', error)
        finally:
            self._sock = None

    def _send_request(self):
        """Send the request over the wire."""
        request_bytes = self.websocket.build_request()
        self.write(request_bytes)

    def _check_poll(self, poll):
        """Check if it is time for a poll."""
        current_time = self._time
        if (self._poll_start is None or
            current_time - self._poll_start >= poll):
            self._poll_start = current_time
            return True
        else:
            return False

    def _check_auto_ping(self, ping_rate):
        """Check if a ping is required."""
        if ping_rate:
            current_time = self._time
            if current_time > self._next_ping:
                # Calculate next ping time that is in the future.
                self._next_ping = (
                    math.ceil(current_time / ping_rate) * ping_rate
                )
                # TODO: Calculate the round trip time
                try:
                    self.websocket.send_ping()
                except errors.WebSocketError:
                    # Just in case the websocket has gone away
                    pass

    def _check_ping_timeout(self, ping_timeout):
        """Check if the server is not responding to pings."""
        if ping_timeout:
            time_since_last_pong = self._time - self._last_pong
            if time_since_last_pong > ping_timeout:
                log.debug('ping_timeout time exceeded')
                return True
        return False

    def _check_close_timeout(self, close_timeout):
        """Check if the close timeout was tripped."""
        if not close_timeout:
            return False
        sent_close_time = self.websocket.sent_close_time
        return (
            sent_close_time is not None and
            self._time >= sent_close_time + close_timeout
        )

    def _recv(self, count):
        """Receive and return pending data from the socket."""
        try:
            if self.websocket.is_secure:
                # exhaust ssl buffer
                recv_bytes = []
                while count:
                    data = self._sock.recv(count)
                    recv_bytes.append(data)
                    count = self._sock.pending()
                return b''.join(recv_bytes)
            else:
                # Plain socket recv
                return self._sock.recv(count)
        except socket.error as error:
            self._socket_fail('recv fail; {}', error)

    def _regular(self, poll, ping_rate, ping_timeout, close_timeout):
        """Run regularly to do polling / pings."""
        # Check for regularly running actions.
        if self._check_poll(poll):
            yield events.Poll()
        self._check_auto_ping(ping_rate)
        if self._check_ping_timeout(ping_timeout):
            yield events.Unresponsive()
            raise self._ForceDisconnect(
                'exceeded {:.0f}s ping timeout'.format(ping_timeout)
            )
        if self._check_close_timeout(close_timeout):
            raise self._ForceDisconnect(
                "server didn't respond to close packet "
                "within {}s".format(close_timeout)
            )

    def _send_pong(self, event):
        """Send a pong message in response to ping event."""
        try:
            self.websocket.send_pong(event.data)
        except errors.WebSocketError:
            # In case the websocket has gone away
            pass

    def _on_pong(self, event):
        """Record last pong time."""
        self._last_pong = self._time

    def _on_ready(self):
        """Called when a ready event is received."""
        self._last_pong = 0.0
        self._next_ping = 0.0
        self._start_time = time.time()

    def run(self,
            poll=5,
            ping_rate=30,
            ping_timeout=None,
            auto_pong=True,
            close_timeout=None):
        """Run the websocket."""
        websocket = self.websocket
        url = websocket.url
        # Connecting event
        yield events.Connecting(url)

        # Create socket and connect to remote server
        try:
            sock = self._sock = self._connect()
        except socket.error as error:
            yield events.ConnectFail('{}'.format(error))
            return
        except Exception as error:
            log.error('error connecting to %s; %s', url, error)
            yield events.ConnectFail('error; {}'.format(error))
            return

        # We now have a socket.
        # Send the request.
        try:
            self._send_request()
        except errors.TransportFail as error:
            self._close_socket()
            yield events.ConnectFail('request failed; {}'.format(error))
            return
        except Exception as error:
            self._close_socket()
            log.error('error sending request; %s', error)
            yield events.ConnectFail('request error; {}'.format(error))
            return

        # Connected to the server, but not yet upgraded to websockets
        yield events.Connected(url)

        selector = self._selector_cls(sock)
        log.debug('%r created', selector)

        def _regular():
            """Run regular events if websocket is ready."""
            if self._ready:
                _iter_events = self._regular(
                    poll,
                    ping_rate,
                    ping_timeout,
                    close_timeout
                )
                for event in _iter_events:
                    yield event

        def _on_event(event):
            """Handle logic in response to an event."""
            if event.name == 'ready':
                self._on_ready()
                self._ready = True
            elif event.name == 'ping':
                if auto_pong:
                    self._send_pong(event)
            elif event.name == 'pong':
                self._on_pong(event)
            yield event
            for event in _regular():
                yield event

        try:
            while not websocket.is_closed:
                readable = selector.wait_readable(poll)

                for event in _regular():
                    yield event

                if readable:
                    data = self._recv(64 * 1024)
                    if data:
                        for event in self.websocket.feed(data):
                            for event in _on_event(event):
                                yield event
                    else:
                        if websocket.is_active:
                            self._socket_fail('connection lost')
                        else:
                            break

        except self._ForceDisconnect as error:
            self._close_socket()
            yield events.Disconnected('disconnected; {}'.format(error))

        except self._SocketFail as error:
            # Session methods will translate socket errors to this
            # exception. The result is we are disconnected.
            self._close_socket()
            yield events.Disconnected('socket fail; {}'.format(error))
        except Exception as error:
            # It pays to be paranoid.
            log.exception('error in websocket loop')
            self._close_socket()
            yield events.Disconnected('error; {}'.format(error))
        else:
            # The websocket instance terminated the loop, which means
            # it was a graceful exit.
            self._close_socket()
            yield events.Disconnected(graceful=True)
        finally:
            selector.close()


if __name__ == "__main__":  # pragma: no cover

    # Test with wstest -m echoserver -w ws://127.0.0.1:9001 -d
    # Get wstest app from http://autobahn.ws/testsuite/

    from .websocket import WebSocket

    #ws = WebSocket('wss://echo.websocket.org')
    ws = WebSocket('ws://127.0.0.1:9001/')
    for event in ws.connect(poll=5):
        print(event)
        if isinstance(event, events.Poll):
            ws.send_text('Hello, World')
            ws.send_binary(b'hello world in binary')

