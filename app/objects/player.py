
from app.common.constants import (
    PresenceFilter,
    Permissions,
    LoginError,
    GameMode
)

from app.common.objects import (
    UserPresence,
    StatusUpdate,
    UserStats,
    Message,
    Channel
)

from app.common.database.repositories import users

from app.protocol import BanchoProtocol, IPAddress
from app.common.streams import StreamIn

from app.common.database import DBUser, DBStats
from app.objects import OsuClient, Status

from typing import Optional, Callable, Tuple, List, Dict
from datetime import datetime
from enum import Enum
from copy import copy

from twisted.internet.error import ConnectionDone
from twisted.internet.address import IPv4Address
from twisted.python.failure import Failure

from app.clients.packets import PACKETS
from app.clients import (
    DefaultResponsePacket,
    DefaultRequestPacket
)

import hashlib
import logging
import config
import bcrypt
import utils
import app

class Player(BanchoProtocol):
    def __init__(self, address: IPAddress) -> None:
        self.is_local = utils.is_localip(address.host)
        self.logger = logging.getLogger(address.host)
        self.address = address

        self.away_message: Optional[str] = None
        self.client: Optional[OsuClient] = None
        self.object: Optional[DBUser] = None
        self.stats:  Optional[List[DBStats]] = None
        self.status = Status()

        self.id = -1
        self.name = ""

        self.request_packets = DefaultRequestPacket
        self.packets = DefaultResponsePacket
        self.decoders: Dict[Enum, Callable] = {}
        self.encoders: Dict[Enum, Callable] = {}

        self.channels = set() # TODO: Add type
        self.filter = PresenceFilter.All

        # TODO: Add spectator channel
        # TODO: Add spectator collection

        from .collections import Players
        from .channel import Channel

        self.spectators = Players()
        self.spectating: Optional[Player] = None
        self.spectator_chat: Optional[Channel] = None

        # TODO: Add current match
        self.in_lobby = False

    def __repr__(self) -> str:
        return f'<Player ({self.id})>'

    @classmethod
    def bot_player(cls):
        player = Player(
            IPv4Address(
                'TCP',
                '127.0.0.1',
                1337
            )
        )

        player.object = users.fetch_by_id(1)
        player.client = OsuClient.empty()

        player.id = -player.object.id # Negative user id -> IRC Player
        player.name = player.object.name
        player.stats  = player.object.stats

        player.client.ip.country_code = "OC"
        player.client.ip.city = "w00t p00t!"

        return player

    @property
    def is_bot(self) -> bool:
        return True if self.id == -1 else False

    @property
    def silenced(self) -> bool:
        return False # TODO

    @property
    def supporter(self) -> bool:
        return True # TODO

    @property
    def restricted(self) -> bool:
        if not self.object:
            return False
        return self.object.restricted

    @property
    def current_stats(self) -> Optional[DBStats]:
        for stats in self.stats:
            if stats.mode == self.status.mode.value:
                return stats
        return None

    @property
    def permissions(self) -> Optional[Permissions]:
        if not self.object:
            return
        return Permissions(self.object.permissions)

    @property
    def friends(self) -> List[int]:
        return [
            rel.target_id
            for rel in self.object.relationships
            if rel.status == 0
        ]

    @property
    def user_presence(self) -> Optional[UserPresence]:
        try:
            return UserPresence(
                self.id,
                False,
                self.name,
                self.client.utc_offset,
                self.client.ip.country_index,
                self.permissions,
                self.status.mode,
                self.client.ip.longitude,
                self.client.ip.latitude,
                self.current_stats.rank
            )
        except AttributeError:
            return None

    @property
    def user_stats(self) -> Optional[UserStats]:
        try:
            return UserStats(
                self.id,
                StatusUpdate(
                    self.status.action,
                    self.status.text,
                    self.status.mods,
                    self.status.mode,
                    self.status.checksum,
                    self.status.beatmap
                ),
                self.current_stats.rscore,
                self.current_stats.tscore,
                self.current_stats.acc,
                self.current_stats.playcount,
                self.current_stats.rank,
                self.current_stats.pp,
            )
        except AttributeError:
            return None

    def connectionLost(self, reason: Failure = Failure(ConnectionDone())):
        app.session.players.remove(self)

        for channel in copy(self.channels):
            channel.remove(self)

        # TODO: Notify other clients
        # TODO: Remove spectator channel from collection
        # TODO: Remove from match

        super().connectionLost(reason)

    def reload_object(self) -> DBUser:
        """Reload player stats from database"""
        self.object = users.fetch_by_id(self.id)
        self.stats = self.object.stats

        # TODO: Update leaderboard cache

        return self.object

    def close_connection(self, error: Optional[Exception] = None):
        self.connectionLost()
        super().close_connection(error)

    def send_error(self, reason=-5, message=""):
        if self.encoders and message:
            self.send_packet(
                self.packets.ANNOUNCE,
                message
            )

        self.send_packet(
            self.packets.LOGIN_REPLY,
            reason
        )

    def send_packet(self, packet_type: Enum, *args):
        if self.is_bot:
            return

        return super().send_packet(
            packet_type,
            self.encoders,
            *args
        )

    def login_failed(self, reason = LoginError.ServerError, message = ""):
        self.send_error(reason.value, message)
        self.close_connection()

    def get_client(self, version: int) -> Tuple[Dict[Enum, Callable], Dict[Enum, Callable]]:
        """Figure out packet sender/decoder, closest to version of client"""

        decoders, encoders = PACKETS[
            min(
                PACKETS.keys(),
                key=lambda x:abs(x-version)
            )
        ]

        return decoders, encoders

    def login_received(self, username: str, md5: str, client: OsuClient):
        self.logger.info(f'Login attempt as "{username}" with {client.version.string}.')
        self.logger.name = f'Player "{username}"'

        # TODO: Set packet enums

        # Get decoders and encoders
        self.decoders, self.encoders = self.get_client(client.version.date)

        # Send protocol version
        self.send_packet(self.packets.PROTOCOL_VERSION, 18) # TODO: Define constant

        # Check adapters md5
        adapters_hash = hashlib.md5(client.hash.adapters.encode()).hexdigest()

        if adapters_hash != client.hash.adapters_md5:
            self.transport.write('no.\r\n')
            self.close_connection()
            return

        if not (user := users.fetch_by_name(username)):
            self.logger.warning('Login Failed: User not found')
            self.login_failed(LoginError.Authentication)
            return

        if not bcrypt.checkpw(md5.encode(), user.bcrypt.encode()):
            self.logger.warning('Login Failed: Authentication error')
            self.login_failed(LoginError.Authentication)
            return

        if user.restricted:
            # TODO: Check ban time
            self.logger.warning('Login Failed: Restricted')
            self.login_failed(LoginError.Banned)
            return

        if not user.activated:
            self.logger.warning('Login Failed: Not activated')
            self.login_failed(LoginError.NotActivated)
            return

        if app.session.players.by_id(user.id):
            # TODO: Check connection of other user
            self.logger.warning('Login failed: Already Online')
            self.close_connection()
            return

        # TODO: Tournament clients

        self.id = user.id
        self.name = user.name
        self.stats = user.stats
        self.object = user

        self.status.mode = GameMode(self.object.preferred_mode)

        if not self.stats:
            # TODO: Create stats
                # Reload object
                # Reset ban info
            pass

        # TODO: Update leaderboards

        self.login_success()

    def login_success(self):
        from .channel import Channel

        self.spectator_chat = Channel(
            name=f'#spec_{self.id}',
            topic=f"{self.name}'s spectator channel",
            owner=self.name,
            read_perms=1,
            write_perms=1,
            public=False
        )

        # Update latest activity
        self.update_activity()

        # Protocol Version
        self.send_packet(self.packets.PROTOCOL_VERSION, 18)

        # User ID
        self.send_packet(self.packets.LOGIN_REPLY, self.id)

        # Menu Icon
        self.send_packet(
            self.packets.MENU_ICON,
            config.MENUICON_IMAGE,
            config.MENUICON_URL
        )

        # Permissions
        self.send_packet(
            self.packets.LOGIN_PERMISSIONS,
            self.permissions
        )

        # Presence
        self.enqueue_presence(self)
        self.enqueue_stats(self)

        # Bot presence
        self.enqueue_presence(app.session.bot_player)

        # Friends
        self.send_packet(
            self.packets.FRIENDS_LIST,
            self.friends
        )

        # Append to player collection
        app.session.players.append(self)

        # Enqueue other players
        self.enqueue_players(app.session.players)

        for channel in app.session.channels.public:
            if channel.can_read(self.permissions):
                self.enqueue_channel(channel)

        self.send_packet(self.packets.CHANNEL_INFO_COMPLETE)

        # TODO: Remaining silence

    def packet_received(self, packet_id: int, stream: StreamIn):
        if self.is_bot:
            return

        try:
            packet = self.request_packets(packet_id)
            self.logger.debug(f'-> "{packet.name}": {stream.get()}')

            decoder = self.decoders[packet]
            args = decoder(stream)

            handler_function = app.session.handlers[packet]
            handler_function(
               *[self, args] if args != None else
                [self]
            )
        except KeyError as e:
            self.logger.error(
                f'Could not find decoder/handler for "{packet.name}": {e}'
            )
        except ValueError as e:
            self.logger.error(
                f'Could not find packet with id "{packet_id}": {e}'
            )

    def update_activity(self):
        users.update(
            user_id=self.id,
            updates={
                'latest_activity': datetime.now()
            }
        )

    def enqueue_player(self, player):
        self.send_packet(
            self.packets.USER_PRESENCE_SINGLE,
            player.id
        )

    def enqueue_players(self, players):
        n = max(1, 150)

        # Split players into chunks to avoid any buffer overflows
        for chunk in (players[i:i+n] for i in range(0, len(players), n)):
            self.send_packet(
                self.packets.USER_PRESENCE_BUNDLE,
                [player.id for player in chunk]
            )

    def enqueue_presence(self, player):
        self.send_packet(
            self.packets.USER_PRESENCE,
            player.user_presence
        )

    def enqueue_stats(self, player):
        self.send_packet(
            self.packets.USER_STATS,
            player.user_stats
        )

    def enqueue_message(self, message: Message):
        self.send_packet(
            self.packets.SEND_MESSAGE,
            message
        )

    def enqueue_channel(self, channel: Channel, autojoin: bool = False):
        self.send_packet(
            self.packets.CHANNEL_AVAILABLE if not autojoin else \
            self.packets.CHANNEL_AVAILABLE_AUTOJOIN,
            channel
        )

    def join_success(self, name: str):
        self.send_packet(
            self.packets.CHANNEL_JOIN_SUCCESS,
            name
        )

    def revoke_channel(self, name: str):
        self.send_packet(
            self.packets.CHANNEL_REVOKED,
            name
        )
