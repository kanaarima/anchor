
from twisted.internet.error   import ConnectionDone
from twisted.internet.address import IPv4Address
from twisted.python.failure   import Failure

from datetime    import datetime, timedelta
from dataclasses import dataclass, field
from typing      import List, Optional

from ..protocol import BanchoProtocol, IPAddress
from ..streams import StreamIn
from ..logging import Console, File

from ..handlers.b20130606 import b20130606
from ..handlers import BaseHandler

from .client import OsuClient

from ..common.objects import (
    DBStats,
    DBScore,
    DBUser
)

from ..constants import (
    ResponsePacket,
    PresenceFilter,
    RequestPacket,
    ClientStatus,
    Permissions,
    LoginError,
    Mode,
    Mod
)

import hashlib
import timeago
import logging
import bancho
import bcrypt
import config

Handlers = {
    20130606: b20130606, # Latest supported version
                         # TODO: Implement more clients
}

@dataclass
class Status:
    action: ClientStatus = ClientStatus.Idle
    text: str = ""
    checksum: str = ""
    mods: List[Mod] = field(default_factory=list) # = []
    mode: Mode = Mode.Osu
    beatmap: int = -1

    def __repr__(self) -> str:
        return f"<[{self.action.name}] mode='{self.mode.name}' mods={self.mods} text='{self.text}' md5='{self.checksum}' beatmap={self.beatmap}>"

class Player(BanchoProtocol):
    def __init__(self, address: IPAddress) -> None:
        self.version = -1

        self.client: Optional[OsuClient]     = None
        self.object: Optional[DBUser]        = None
        self.stats:  Optional[List[DBStats]] = None
        self.status = Status()

        self.id   = -1
        self.name = ""
        self.pw   = ""

        self.away_message: Optional[str] = None
        self.handler: Optional[BaseHandler] = None
        self.channels = []
        self.address = address

        self.logger  = logging.getLogger(self.address.host)

        self.last_response = datetime.now()
        self.filter = PresenceFilter.All

        from .collections import Players
        from .multiplayer import Match

        self.spectating: Optional[Player] = None
        self.spectators: Players = Players()

        self.match: Optional[Match] = None
        self.in_lobby               = False

        self.spectator_channel = None

    def __repr__(self) -> str:
        return f'<Player ({self.id})>'
    
    @classmethod
    def bot_player(cls):
        player = Player(
            IPv4Address('TCP', '127.0.0.1', 1337)
        )

        player.object = bancho.services.database.user_by_id(1)
        player.handler = BaseHandler(player)
        player.client = OsuClient.empty()

        player.id     = player.object.id
        player.name   = player.object.name
        player.stats  = player.object.stats

        return player
    
    @property
    def silenced(self) -> bool:
        if self.object.silence_end:
            if self.remaining_silence > 0:
                return True
            else:
                # User is not silenced anymore
                self.unsilence()
                return False
        return False

    @property
    def remaining_silence(self):
        if self.object.silence_end:
            return int(self.object.silence_end.timestamp() - datetime.now().timestamp())
        return 0
    
    @property
    def supporter(self):
        if config.FREE_SUPPORTER:
            return True

        if self.object.supporter_end:
            # Check remaining supporter
            if self.remaining_supporter > 0:
                return True
            else:
                # Remove supporter
                self.object.supporter_end = None
                self.permissions.remove(Permissions.Subscriber)

                # Update database
                instance = bancho.services.database.session
                instance.query(DBUser).filter(DBUser.id == self.id).update(
                    {
                        'supporter_end': None,
                        'permissions': Permissions.pack(self.permissions)
                    }
                )
                instance.commit()

                # Update client
                # NOTE: Client will exit after it notices a permission change
                self.handler.enqueue_privileges()

    @property
    def remaining_supporter(self):
        if self.object.supporter_end:
            return self.object.supporter_end.timestamp() - datetime.now().timestamp()
        return 0

    @property
    def permissions(self) -> Optional[List[Permissions]]:
        if not self.object:
            return
        
        return Permissions.list(self.object.permissions)
    
    @property
    def restricted(self) -> bool:
        if not self.object:
            return False

        return self.object.restricted
    
    @property
    def friends(self) -> List[int]:
        return [rel.target_id for rel in self.object.relationships if rel.status == 0]
    
    @property
    def current_stats(self) -> DBStats:
        return self.stats[self.status.mode.value]
    
    @property
    def is_bot(self) -> bool:
        # Maybe there is a better way of doing this?
        return type(self.handler) == BaseHandler
    
    def silence(self, duration_sec: int, reason: str):
        duration = timedelta(seconds=duration_sec)

        if not self.object.silence_end:
            self.object.silence_end = datetime.now() + duration
        else:
            # Append duration, if user has been silenced already
            self.object.silence_end += duration
        
        # Update database
        instance = bancho.services.database.session
        instance.query(DBUser).filter(DBUser.id == self.id).update(
            {'silence_end': self.object.silence_end}
        )
        instance.commit()

        # Enqueue to client
        self.handler.enqueue_silence_info(duration_sec)

        self.logger.info(f'{self.name} was silenced for {timeago.format(datetime.now() + duration)}. Reason: "{reason}"')

    def unsilence(self):
        self.object.silence_end = None

        # Update database
        instance = bancho.services.database.session
        instance.query(DBUser).filter(DBUser.id == self.id).update(
            {'silence_end': None}
        )
        instance.commit()

        # Enqueue to client
        self.handler.enqueue_silence_info(0)

    def restrict(self, reason: str = None, autoban=False):
        self.object.restricted = True
        self.object.permissions = 0

        # Update user
        instance = bancho.services.database.session
        instance.query(DBUser).filter(DBUser.id == self.id).update(
            {
                'restricted': True,
                'permissions': 0
            }
        )
        # Remove stats
        instance.query(DBStats).filter(DBStats.user_id == self.id).delete()
        # Hide scores
        instance.query(DBScore).filter(DBScore.user_id == self.id).update(
            {'status': -1}
        )
        instance.commit()

        if reason:
            self.handler.enqueue_announce(f'You have been restricted for:\n{reason}')

        # Update client
        self.handler.enqueue_login_reply(LoginError.BANNED.value)

        self.logger.warning(f'{self.name} got {"auto-" if autoban else ""}restricted. Reason: {reason}')

        # Close connection
        self.closeConnection()

    def reload_object(self) -> DBUser:
        self.object = bancho.services.database.user_by_id(self.id)
        self.stats = self.object.stats
        return self.object

    def connectionLost(self, reason: Failure = Failure(ConnectionDone())):
        # Notify clients
        bancho.services.players.remove(self)
        bancho.services.players.exit(self)

        # Remove them from all channels
        for channel in self.channels:
            channel.remove(self)

        # Remove their spectator channel
        bancho.services.channels.remove(self.spectator_channel)

        # Remove player from any match
        if self.match:
            self.handler.leave_match()

        super().connectionLost(reason)

    def closeConnection(self, error: Optional[Exception] = None):
        self.connectionLost()
        super().closeConnection(error)
    
    def packetReceived(self, packet_id: int, stream: StreamIn):
        return self.handler.handle(packet_id, stream)

    def loginReceived(self, username: str, md5: str, client: OsuClient):
        self.logger.info(f'Login attempt as "{username}" with {client.version.string}.')

        self.client = client

        # Set client version
        self.version = self.client.version.date

        # If the client date was not found
        # take the handler, that is closest to it
        self.handler = Handlers[
            min(Handlers.keys(), key=lambda x:abs(x-self.version))
        ](self)

        # Send protocol version
        self.sendPacket(
            ResponsePacket.PROTOCOL_VERSION,
            int(21).to_bytes(4, 'little')
        )

        version = str(client.version)

        # Validate client
        if version not in config.CLIENT_HASHES.keys():
            self.logger.warning('Login failed: Invalid Client')
            self.loginFailed(
                LoginError.UPDATE_NEEDED,
                'This version of osu! is not compatible with bancho. Please contact an administrator if you want to use this version!'
            )
            return

        # Check client hash
        required_hash = config.CLIENT_HASHES[version]

        if required_hash and client.hash.md5 != required_hash:
            self.logger.warning('Login failed: Modified Client')
            self.loginFailed(
                LoginError.UPDATE_NEEDED,
                'You are using a modified version of osu!. If this was not intentional, please contact an administrator!'
            )
            return
        
        if hashlib.md5(client.hash.adapters.encode()).hexdigest() != client.hash.adapters_md5:
            self.transport.write('no.\r\n')
            self.closeConnection()
            return

        if not (user := bancho.services.database.user_by_name(username)):
            self.logger.warning('Login failed: Authentication Error')
            self.loginFailed(LoginError.AUTHENTICATION) # User does not exist
            return

        if not bcrypt.checkpw(md5.encode(), user.bcrypt.encode()):
            self.logger.warning('Login failed: Authentication Error')
            self.loginFailed(LoginError.AUTHENTICATION) # Password check failed
            return

        if user.restricted:
            self.logger.warning('Login failed: Restricted')
            self.loginFailed(LoginError.BANNED) # User is banned
            return

        if not user.activated:
            self.logger.warning('Login failed: Not Activated')
            self.loginFailed(LoginError.NOT_ACTIVATED) # User is not yet activated
            return
        
        if bancho.services.players.by_id(user.id):
            # User is already online
            self.logger.warning('Login failed: Already Online')
            self.closeConnection()
            return

        # TODO: Tourney clients

        self.object = user
        self.id     = user.id
        self.name   = user.name
        self.stats  = user.stats
        self.pw     = user.bcrypt

        if not self.stats:
            self.create_stats()
            self.reload_object()

            # This will allow the user to play again after they got banned
            self.handler.enqueue_silence_info(-1)

        self.loginSuccess()
        
    def loginFailed(self, reason = LoginError.SERVER_ERROR, message = ""):
        self.sendError(reason.value, message)
        self.closeConnection()

    def loginSuccess(self):
        from .channel import Channel

        self.spectator_channel = Channel(
            f'#spec_{self.id}',
            f"{self.name}'s spectator channel",
            1, 1,
            public=False
        )
        bancho.services.channels.append(self.spectator_channel)

        # User ID
        self.handler.enqueue_login_reply(self.id)

        # Menu Icon
        self.handler.enqueue_menu_icon(config.MENUICON_IMAGE, config.MENUICON_URL)

        # Privileges
        self.handler.enqueue_privileges()

        # Presence and stats        
        self.handler.enqueue_presence(self)
        self.handler.enqueue_stats(self)

        # Append to player collection
        bancho.services.players.append(self)

        # Friends
        self.handler.enqueue_friends()

        # All players that are online right now
        self.handler.enqueue_players(bancho.services.players)

        # Bot presence
        self.handler.enqueue_presence(bancho.services.bot_player)
        self.handler.enqueue_stats(bancho.services.bot_player)

        # Remaining silence
        if self.silenced:
            self.handler.enqueue_silence_info(self.remaining_silence)

        # All public channels
        for channel in bancho.services.channels:
            if (
                channel.can_read(Permissions.pack(self.permissions)) and
                channel.public
               ):
                self.handler.enqueue_channel(channel)

        self.handler.enqueue_channel_info_end()

    def update(self):
        # Reload database object
        self.reload_object()
        # Enqueue to players
        bancho.services.players.enqueue_stats(self)

    def create_stats(self):
        instance = bancho.services.database.session

        for mode in range(4):
            instance.add(
                DBStats(self.id, mode)
            )

        instance.commit()
