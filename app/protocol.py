
from twisted.internet.address import IPv4Address, IPv6Address
from twisted.internet.error import ConnectionDone
from twisted.internet.protocol import Protocol
from twisted.internet import threads, reactor
from twisted.python.failure import Failure

from typing import Union, Optional
from enum import Enum

from app.common.constants import ANCHOR_WEB_RESPONSE
from app.common.streams import StreamOut, StreamIn
from app.objects import OsuClient

import logging
import config
import utils
import gzip

IPAddress = Union[IPv4Address, IPv6Address]

class BanchoProtocol(Protocol):
    """This class will be a base for receiving and parsing packets and logins."""

    buffer  = b""
    busy    = False
    proxied = False
    connection_timeout = 20

    def __init__(self, address: IPAddress) -> None:
        self.logger = logging.getLogger(address.host)
        self.is_local = utils.is_local_ip(address.host)
        self.client: Optional[OsuClient] = None
        self.address = address

    def connectionMade(self):
        if not self.is_local or config.DEBUG:
            self.logger.info(
                f'-> <{self.address.host}:{self.address.port}>'
            )

    def connectionLost(self, reason: Failure = ...):
        if reason.type != ConnectionDone:
            self.logger.warning(
                f'<{self.address.host}> -> Lost connection: "{reason.getErrorMessage()}".'
            )
            return

        if not self.is_local or config.DEBUG:
            self.logger.info(
                f'<{self.address.host}> -> Connection done.'
            )

    def dataReceived(self, data: bytes):
        """For login data only. If client logged in, we will switch to packetDataReceived"""

        if self.busy:
            self.buffer += data
            return

        try:
            self.buffer += data.replace(b'\r', b'')
            self.busy = True

            if data.startswith(b'GET /'):
                # We received a web request
                self.send_web_response()
                self.close_connection()
                return

            if self.buffer.count(b'\n') < 3:
                return

            self.logger.debug(
                f'-> Received login: {self.buffer}'
            )

            # Login received
            username, password, client, self.buffer = self.buffer.split(b'\n', 3)

            self.client = OsuClient.from_string(
                client.decode(),
                self.address.host
            )

            # We now expect bancho packets from the client
            self.dataReceived = self.packetDataReceived

            # Handle login
            deferred = threads.deferToThread(
                self.login_received,
                username.decode(),
                password.decode(),
                self.client
            )

            deferred.addErrback(self.login_callback)
            deferred.addTimeout(15, reactor)
        except Exception as e:
            self.logger.error(
                f'Error on login: {e}',
                exc_info=e
            )
            self.close_connection(e)

        finally:
            self.busy = False

    def packetDataReceived(self, data: bytes):
        """For bancho packets only and will be used after login"""

        if self.busy:
            self.buffer += data
            return

        try:
            self.busy = True
            self.buffer += data

            while self.buffer:
                stream = StreamIn(self.buffer)

                try:
                    packet = stream.u16()
                    compression = stream.bool() if self.client.version.date > 323 else True
                    payload = stream.read(stream.u32())
                except OverflowError:
                    # Wait for next buffer
                    break

                if compression:
                    # gzip compression is only used in very old clients
                    payload = gzip.decompress(payload)

                self.packet_received(
                    packet_id=packet,
                    stream=StreamIn(payload)
                )

                # Reset buffer
                self.buffer = stream.readall()
        except Exception as e:
            self.logger.error(
                f'Error while receiving packet: {e}',
                exc_info=e
            )

            self.close_connection(e)

        finally:
            self.busy = False

    def enqueue(self, data: bytes):
        try:
            self.transport.write(data)
        except Exception as e:
            self.logger.error(
                f'Could not write to transport layer: {e}',
                exc_info=e
            )

    def send_web_response(self):
        self.enqueue('\r\n'.join([
            'HTTP/1.1 200 OK',
            'content-type: text/html',
            ANCHOR_WEB_RESPONSE
        ]).encode())

    def close_connection(self, error: Optional[Exception] = None):
        if not self.is_local or config.DEBUG:
            if error:
                self.send_error(message=str(error) if config.DEBUG else None)
                self.logger.warning(f'Closing connection -> <{self.address.host}>')
            else:
                self.logger.info(f'Closing connection -> <{self.address.host}>')

        self.transport.loseConnection()

    def send_packet(self, packet: Enum, encoders, *args):
        try:
            stream = StreamOut()
            data = encoders[packet](*args)

            self.logger.debug(
                f'<- "{packet.name}": {str(list(args)).removeprefix("[").removesuffix("]")}'
            )

            if self.client.version.date <= 323:
                # In version b323 and below, the compression is enabled by default
                data = gzip.compress(data)
                stream.legacy_header(packet, len(data))
            else:
                stream.header(packet, len(data))

            stream.write(data)

            reactor.callFromThread(self.enqueue, stream.get())
        except Exception as e:
            self.logger.error(
                f'Could not send packet "{packet.name}": {e}',
                exc_info=e
            )

    def login_callback(self, error: Failure):
        self.logger.error(
            f'Exception while logging in: "{error.getErrorMessage()}"',
            exc_info=error.value
        )

    def login_received(self, username: str, md5: str, client: OsuClient):
        ...

    def packet_received(self, packet_id: int, stream: StreamIn):
        ...

    def send_error(self, reason = -5, message = ""):
        ...
