
from app.common.objects import UserPresence, UserQuit, UserStats
from app.clients.sender import BaseSender
from app.common.streams import StreamOut

from .constants import ResponsePacket
from .writer import Writer

from typing import List, Optional

class PacketSender(BaseSender):

    protocol_version = 18
    packets = ResponsePacket
    writer = Writer

    def send_login_reply(self, reply: int):
        self.player.send_packet(
            self.packets.LOGIN_REPLY,
            int(reply).to_bytes(
                length=4,
                byteorder='little',
                signed=True
            )
        )

    def send_protocol_version(self, version: int):
        self.player.send_packet(
            self.packets.PROTOCOL_VERSION,
            int(version).to_bytes(
                length=4,
                byteorder='little'
            )
        )

    def send_ping(self):
        self.player.send_packet(self.packets.PING)

    def send_announcement(self, message: str):
        stream = StreamOut()
        stream.string(message)

        self.player.send_packet(
            self.packets.ANNOUNCE,
            stream.get()
        )

    def send_menu_icon(self, image: Optional[str], url: Optional[str]):
        stream = StreamOut()
        stream.string(
            '|'.join([
                f'{image if image else ""}',
                f'{url if url else ""}'
            ])
        )

        self.player.send_packet(
            self.packets.ANNOUNCE,
            stream.get()
        )

    def send_presence(self, presence: UserPresence):
        writer = self.writer()
        writer.write_presence(presence)

        self.player.send_packet(
            self.packets.USER_PRESENCE,
            writer.stream.get()
        )

    def send_stats(self, stats: UserStats):
        writer = self.writer()
        writer.write_stats(stats)

        self.player.send_packet(
            self.packets.USER_PRESENCE,
            writer.stream.get()
        )

    def send_player(self, player_id: int):
        self.player.send_packet(
            self.packets.USER_PRESENCE_SINGLE,
            int(player_id).to_bytes(
                length=4,
                byteorder='little',
                signed=True
            )
        )

    def send_players(self, player_ids: List[int]):
        writer = self.writer()
        writer.write_intlist(player_ids)

        self.player.send_packet(
            self.packets.USER_PRESENCE_BUNDLE,
            writer.stream.get()
        )

    def send_exit(self, user_quit: UserQuit):
        stream = StreamOut()
        stream.s32(user_quit.user_id)
        stream.u8(user_quit.quit_state.value)
