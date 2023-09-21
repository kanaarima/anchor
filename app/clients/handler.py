
from . import DefaultResponsePacket as ResponsePacket
from . import DefaultRequestPacket as RequestPacket

from ..common.database.objects import DBBeatmap
from ..objects.multiplayer import Match
from ..objects.channel import Channel
from ..objects.player import Player
from .. import session, commands

from ..common.database.repositories import (
    relationships,
    beatmaps,
    messages,
    scores
)

from ..common.objects import (
    bBeatmapInfoRequest,
    bReplayFrameBundle,
    bBeatmapInfoReply,
    bStatusUpdate,
    bBeatmapInfo,
    bScoreFrame,
    bMatchJoin,
    bMessage,
    bMatch
)

from ..common.constants import (
    MatchTeamTypes,
    PresenceFilter,
    SlotStatus,
    SlotTeam,
    GameMode,
    Grade,
    Mods
)

from typing import Callable, Tuple, List
from concurrent.futures import Future
from threading import Thread
from copy import copy

import time

def register(packet: RequestPacket) -> Callable:
    def wrapper(func) -> Callable:
        session.handlers[packet] = func
        return func

    return wrapper

def thread_callback(future: Future):
    if (exc := future.exception()):
        session.logger.error(
            f'Exception in {future}: {exc}'
        )
        raise exc

    session.logger.debug(
        f'Future Result: {future}'
    )

def run_in_thread(func):
    def wrapper(*args, **kwargs) -> Future:
        try:
            f = session.packet_executor.submit(
                func,
                *args,
                **kwargs
            )
            f.add_done_callback(
                thread_callback
            )
            return f
        except RuntimeError:
            exit()

    return wrapper

@register(RequestPacket.PONG)
def pong(player: Player):
    pass

@register(RequestPacket.EXIT)
@run_in_thread
def exit(player: Player, updating: bool):
    player.update_activity()

@register(RequestPacket.RECEIVE_UPDATES)
def receive_updates(player: Player, filter: PresenceFilter):
    player.filter = filter

    if filter.value <= 0:
        return

    players = session.players if filter == PresenceFilter.All else \
              player.online_friends

    player.enqueue_players(players, stats_only=True)

@register(RequestPacket.PRESENCE_REQUEST)
@run_in_thread
def presence_request(player: Player, players: List[int]):
    for id in players:
        if not (target := session.players.by_id(id)):
            continue

        player.enqueue_presence(target)

@register(RequestPacket.PRESENCE_REQUEST_ALL)
@run_in_thread
def presence_request_all(player: Player):
    player.enqueue_players(session.players)

@register(RequestPacket.STATS_REQUEST)
@run_in_thread
def stats_request(player: Player, players: List[int]):
    for id in players:
        if not (target := session.players.by_id(id)):
            continue

        player.enqueue_stats(target)

@register(RequestPacket.CHANGE_STATUS)
@run_in_thread
def change_status(player: Player, status: bStatusUpdate):
    player.status.checksum = status.beatmap_checksum
    player.status.beatmap = status.beatmap_id
    player.status.action = status.action
    player.status.mods = status.mods
    player.status.mode = status.mode
    player.status.text = status.text

    player.update_status_cache()
    player.update_activity()
    player.reload_rank()

    # (This needs to be done for older clients)
    session.players.send_stats(player)

@register(RequestPacket.REQUEST_STATUS)
@run_in_thread
def request_status(player: Player):
    player.reload_rank()
    player.enqueue_stats(player)

@register(RequestPacket.JOIN_CHANNEL)
@run_in_thread
def handle_channel_join(player: Player, channel_name: str):
    try:
        channel = None

        if channel_name == '#spectator':
            if player.spectating:
                channel = player.spectating.spectator_chat
            else:
                channel = player.spectator_chat

        elif channel_name == '#multiplayer':
            channel = player.match.chat
    except AttributeError:
        player.revoke_channel(channel_name)
        return

    if not channel:
        channel = session.channels.by_name(channel_name)

        if not channel:
            player.revoke_channel(channel_name)
            return

    channel.add(player)

@register(RequestPacket.LEAVE_CHANNEL)
@run_in_thread
def channel_leave(player: Player, channel_name: str, kick: bool = False):
    try:
        channel = None

        if channel_name == '#spectator':
            if player.spectating:
                channel = player.spectating.spectator_chat
            else:
                channel = player.spectator_chat

        elif channel_name == '#multiplayer':
            channel = player.match.chat
    except AttributeError:
        player.revoke_channel(channel_name)
        return

    if not channel:
        channel = session.channels.by_name(channel_name)

        if not channel:
            player.revoke_channel(channel_name)
            return

    if kick:
        player.revoke_channel(channel_name)

    channel.remove(player)

@register(RequestPacket.SEND_MESSAGE)
def send_message(player: Player, message: bMessage):
    try:
        channel = None

        if message.target == '#spectator':
            if player.spectating:
                channel = player.spectating.spectator_chat
            else:
                channel = player.spectator_chat

        elif message.target == '#multiplayer':
            channel = player.match.chat
    except AttributeError:
        player.revoke_channel(message.target)
        return

    if not channel:
        channel = session.channels.by_name(message.target)

        if not channel:
            player.revoke_channel(message.target)
            return

    if message.content.startswith('/me'):
        message.content = f'\x01ACTION{message.content.removeprefix("/me")}\x01'

    if (parsed_message := message.content.strip()).startswith('!'):
        # A command was executed
        Thread(
            target=commands.execute,
            args=[
                player,
                channel,
                parsed_message
            ],
            daemon=True
        ).start()
        return

    channel.send_message(player, parsed_message)

    session.executor.submit(
        messages.create,
        player.name,
        channel.name,
        message.content
    )

    session.executor.submit(
        player.update_activity
    )

@register(RequestPacket.SEND_PRIVATE_MESSAGE)
def send_private_message(sender: Player, message: bMessage):
    if not (target := session.players.by_name(message.target)):
        sender.revoke_channel(message.target)
        return

    if sender.silenced:
        sender.logger.warning(
            'Failed to send private message: Sender was silenced'
        )
        return

    if target.silenced:
        sender.enqueue_silenced_target(target.name)
        return

    if target.client.friendonly_dms:
        if sender.id not in target.friends:
            sender.enqueue_blocked_dms(sender.name)
            return

    if (parsed_message := message.content.strip()).startswith('!') \
        or target == session.bot_player:
        # A command was executed
        Thread(
            target=commands.execute,
            args=[
                sender,
                target,
                parsed_message
            ],
            daemon=True
        ).start()
        return

    # Limit message size
    if len(message.content) > 512:
        message.content = message.content[:512] + '... (truncated)'

    if target.away_message:
        sender.enqueue_message(
            bMessage(
                target.name,
                 f'\x01ACTION is away: {target.away_message}\x01',
                target.name,
                target.id,
                is_private=True
            )
        )

    target.enqueue_message(
        bMessage(
            sender.name,
            message.content,
            sender.name,
            sender.id,
            is_private=True
        )
    )

    # Send to their tourney clients
    for client in session.players.get_all_tourney_clients(target.id):
        if client.address.port == target.address.port:
            continue

        client.enqueue_message(
            bMessage(
                sender.name,
                message.content,
                sender.name,
                sender.id,
                is_private=True
            )
        )

    sender.logger.info(f'[PM -> {target.name}]: {message.content}')

    session.executor.submit(
        messages.create,
        sender.name,
        target.name,
        message.content
    )

    session.executor.submit(
        sender.update_activity
    )

@register(RequestPacket.SET_AWAY_MESSAGE)
@run_in_thread
def away_message(player: Player, message: bMessage):
    if player.away_message is None and message.content == "":
        return

    if message.content != "":
        player.away_message = message.content
        player.enqueue_message(
            bMessage(
                session.bot_player.name,
                f'You have been marked as away: {message.content}',
                session.bot_player.name,
                session.bot_player.id,
                is_private=True
            )
        )
    else:
        player.away_message = None
        player.enqueue_message(
            bMessage(
                session.bot_player.name,
                'You are no longer marked as being away',
                session.bot_player.name,
                session.bot_player.id,
                is_private=True
            )
        )

@register(RequestPacket.ADD_FRIEND)
@run_in_thread
def add_friend(player: Player, target_id: int):
    if not (target := session.players.by_id(target_id)):
        return

    if target.id in player.friends:
        return

    relationships.create(
        player.id,
        target_id
    )

    session.logger.info(f'{player.name} is now friends with {target.name}.')

    player.reload_object()
    player.enqueue_friends()

@register(RequestPacket.REMOVE_FRIEND)
@run_in_thread
def remove_friend(player: Player, target_id: int):
    if not (target := session.players.by_id(target_id)):
        return

    if target.id not in player.friends:
        return

    relationships.delete(
        player.id,
        target_id
    )

    session.logger.info(f'{player.name} no longer friends with {target.name}.')

    player.reload_object()
    player.enqueue_friends()

@register(RequestPacket.BEATMAP_INFO)
@run_in_thread
def beatmap_info(player: Player, info: bBeatmapInfoRequest, ignore_limit: bool = False):
    maps: List[Tuple[int, DBBeatmap]] = []

    # Limit request filenames/ids

    if not ignore_limit:
        info.beatmap_ids = info.beatmap_ids[:100]
        info.filenames = info.filenames[:100]

    # Fetch all matching beatmaps from database

    for index, filename in enumerate(info.filenames):
        if not (beatmap := beatmaps.fetch_by_file(filename)):
            continue

        maps.append((
            index,
            beatmap
        ))

    for id in info.beatmap_ids:
        if not (beatmap := beatmaps.fetch_by_id(id)):
            continue

        maps.append((
            -1,
            beatmap
        ))

    player.logger.info(f'Got {len(maps)} beatmap requests')

    # Create beatmap response

    map_infos: List[bBeatmapInfo] = []

    for index, beatmap in maps:
        ranked = {
            -2: 0, # Graveyard: Pending
            -1: 0, # WIP: Pending
             0: 0, # Pending: Pending
             1: 1, # Ranked: Ranked
             2: 2, # Approved: Approved
             3: 2, # Qualified: Approved
             4: 2, # Loved: Approved
        }[beatmap.status]

        # Get personal best in every mode for this beatmap
        grades = {
            0: Grade.N,
            1: Grade.N,
            2: Grade.N,
            3: Grade.N
        }

        for mode in range(4):
            personal_best = scores.fetch_personal_best(
                beatmap.id,
                player.id,
                mode
            )

            if personal_best:
                grades[mode] = Grade[personal_best.grade]

        map_infos.append(
            bBeatmapInfo(
                index,
                beatmap.id,
                beatmap.set_id,
                beatmap.set_id, # thread_id
                ranked,
                grades[0], # standard
                grades[2], # fruits
                grades[1], # taiko
                grades[3], # mania
                beatmap.md5
            )
        )

    player.logger.info(f'Sending reply with {len(map_infos)} beatmaps')

    player.send_packet(
        player.packets.BEATMAP_INFO_REPLY,
        bBeatmapInfoReply(map_infos)
    )

@register(RequestPacket.START_SPECTATING)
def start_spectating(player: Player, player_id: int):
    if player_id == player.id:
        player.logger.warning('Player tried to spectate himself?')
        return

    if not (target := session.players.by_id(player_id)):
        return

    if target.id == session.bot_player.id:
        return

    # TODO: Check osu! mania support

    if (player.spectating) or (player in target.spectators) and not player.is_tourney_client:
        stop_spectating(player)
        return

    player.spectating = target

    # Join their channel
    player.enqueue_channel(target.spectator_chat.bancho_channel, autojoin=True)
    target.spectator_chat.add(player)

    # Enqueue to others
    for p in target.spectators:
        p.enqueue_fellow_spectator(player.id)

    # Enqueue to target
    target.spectators.append(player)
    target.enqueue_spectator(player.id)
    target.enqueue_channel(target.spectator_chat.bancho_channel)

    # Check if target joined #spectator
    if target not in target.spectator_chat.users and not player.is_tourney_client:
        target.spectator_chat.add(target)

@register(RequestPacket.STOP_SPECTATING)
def stop_spectating(player: Player):
    if not player.spectating:
        return

    # Leave spectator channel
    player.spectating.spectator_chat.remove(player)

    # Remove from target
    player.spectating.spectators.remove(player)

    # Enqueue to others
    for p in player.spectating.spectators:
        p.enqueue_fellow_spectator_left(player.id)

    # Enqueue to target
    player.spectating.enqueue_spectator_left(player.id)

    # If target has no spectators anymore
    # kick them from the spectator channel
    if not player.spectating.spectators:
        player.spectating.spectator_chat.remove(
            player.spectating
        )

    player.spectating = None

@register(RequestPacket.CANT_SPECTATE)
def cant_spectate(player: Player):
    if not player.spectating:
        return

    player.spectating.enqueue_cant_spectate(player.id)

    for p in player.spectating.spectators:
        p.enqueue_cant_spectate(player.id)

@register(RequestPacket.SEND_FRAMES)
@run_in_thread
def send_frames(player: Player, bundle: bReplayFrameBundle):
    if not player.spectators:
        return

    # TODO: Check osu! mania support

    for p in player.spectators:
        p.enqueue_frames(bundle)

@register(RequestPacket.JOIN_LOBBY)
def join_lobby(player: Player):
    for p in session.players:
        p.enqueue_lobby_join(player.id)

    player.in_lobby = True

    for match in session.matches.active:
        player.enqueue_match(match.bancho_match)

@register(RequestPacket.PART_LOBBY)
def part_lobby(player: Player):
    player.in_lobby = False

    for p in session.players:
        p.enqueue_lobby_part(player.id)

@register(RequestPacket.MATCH_INVITE)
@run_in_thread
def invite(player: Player, target_id: int):
    if player.silenced:
        return

    if not player.match:
        return

    if not (target := session.players.by_id(target_id)):
        return

    # TODO: Check invite spams

    target.enqueue_invite(
        bMessage(
            player.name,
            f'Come join my multiplayer match: {player.match.embed}',
            player.name,
            player.id,
            is_private=True
        )
    )

@register(RequestPacket.CREATE_MATCH)
@run_in_thread
def create_match(player: Player, bancho_match: bMatch):
    if not player.in_lobby:
        player.logger.warning('Tried to create match, but not in lobby')
        player.enqueue_matchjoin_fail()
        return

    if player.is_tourney_client:
        player.logger.warning('Tried to create match, but was inside tourney client')
        player.enqueue_matchjoin_fail()
        return

    if player.silenced:
        player.logger.warning('Tried to create match, but was silenced')
        player.enqueue_matchjoin_fail()
        return

    if player.match:
        player.logger.warning('Tried to create match, but was already inside one')
        player.enqueue_matchjoin_fail()
        return

    match = Match.from_bancho_match(bancho_match, player)

    if not session.matches.append(match):
        player.logger.warning('Tried to create match, but max match limit was reached')
        player.enqueue_matchjoin_fail()
        return

    session.channels.append(
        c := Channel(
            name=f'#multi_{match.id}',
            topic=match.name,
            owner=match.host.name,
            read_perms=1,
            write_perms=1,
            public=False
        )
    )
    match.chat = c

    session.logger.info(f'Created match: "{match.name}"')

    join_match(
        player,
        bMatchJoin(
            match.id,
            match.password
        )
    )

@register(RequestPacket.JOIN_MATCH)
@run_in_thread
def join_match(player: Player, match_join: bMatchJoin):
    if not (match := session.matches[match_join.match_id]):
        # Match was not found
        player.logger.warning(f'{player.name} tried to join a match that does not exist')
        player.enqueue_matchjoin_fail()
        player.enqueue_match_disband(match_join.match_id)
        return

    match.last_activity = time.time()

    if player.is_tourney_client:
        player.logger.warning('Tried to join match, but was inside tourney client')
        player.enqueue_matchjoin_fail()
        return

    if player.match:
        # Player already joined the match
        player.logger.warning(f'{player.name} tried to join a match, but is already inside one')
        player.enqueue_matchjoin_fail()
        return

    if player is not match.host:
        if match_join.password != match.password:
            # Invalid password
            player.logger.warning('Failed to join match: Invalid password')
            player.enqueue_matchjoin_fail()
            return

        if (slot_id := match.get_free()) is None:
            # Match is full
            player.logger.warning('Failed to join match: Match full')
            player.enqueue_matchjoin_fail()
            return
    else:
        # Player is creating the match
        slot_id = 0

    # Join the chat
    player.enqueue_channel(match.chat.bancho_channel, autojoin=True)
    match.chat.add(player)

    slot = match.slots[slot_id]

    if match.team_type in (MatchTeamTypes.TeamVs, MatchTeamTypes.TagTeamVs):
        slot.team = SlotTeam.Red

    slot.status = SlotStatus.NotReady
    slot.player = player

    player.match = match
    player.enqueue_matchjoin_success(match.bancho_match)

    match.logger.info(f'{player.name} joined')
    match.update()

@register(RequestPacket.LEAVE_MATCH)
def leave_match(player: Player):
    if not player.match:
        return

    player.match.last_activity = time.time()

    slot = player.match.get_slot(player)
    assert slot is not None

    if slot.status == SlotStatus.Locked:
        status = SlotStatus.Locked
    else:
        status = SlotStatus.Open

    slot.reset(status)

    channel_leave(
        player,
        player.match.chat.name,
        kick=True
    )

    if all(slot.empty for slot in player.match.slots):
        player.enqueue_match_disband(player.match.id)

        for p in session.players.in_lobby:
            p.enqueue_match_disband(player.match.id)

        # Match is empty
        session.matches.remove(player.match)
        player.match.starting = None
    else:
        if player is player.match.host:
            # Player was host, transfer to next player
            for slot in player.match.slots:
                if slot.status.value & SlotStatus.HasPlayer.value:
                    player.match.host = slot.player
                    player.match.host.enqueue_match_transferhost()

        player.match.update()

    player.match = None

@register(RequestPacket.MATCH_CHANGE_SLOT)
def change_slot(player: Player, slot_id: int):
    if not player.match:
        return

    if not 0 <= slot_id < 8:
        return

    if player.match.slots[slot_id].status != SlotStatus.Open:
        return

    player.match.last_activity = time.time()

    slot = player.match.get_slot(player)
    assert slot is not None

    player.match.slots[slot_id].copy_from(slot)
    slot.reset()

    player.match.update()

@register(RequestPacket.MATCH_CHANGE_SETTINGS)
def change_settings(player: Player, match: bMatch):
    if not player.match:
        return

    if player is not player.match.host:
        return

    player.match.last_activity = time.time()

    player.match.change_settings(match)

@register(RequestPacket.MATCH_CHANGE_BEATMAP)
def change_beatmap(player: Player, new_match: bMatch):
    if not (match := player.match):
        return

    if player is not player.match.host:
        return

    player.match.last_activity = time.time()

    # New map has been chosen
    match.logger.info(f'Selected: {new_match.beatmap_text}')
    match.unready_players()

    # Unready players with no beatmap
    match.unready_players(SlotStatus.NoMap)

    # Lookup beatmap in database
    beatmap = beatmaps.fetch_by_checksum(new_match.beatmap_checksum)

    if beatmap:
        match.beatmap_id   = beatmap.id
        match.beatmap_hash = beatmap.md5
        match.beatmap_name = beatmap.full_name
        match.mode         = GameMode(beatmap.mode)
        beatmap_text       = beatmap.link
    else:
        match.beatmap_id   = new_match.beatmap_id
        match.beatmap_hash = new_match.beatmap_checksum
        match.beatmap_name = new_match.beatmap_text
        match.mode         = new_match.mode
        beatmap_text       = new_match.beatmap_text

    match.chat.send_message(
        session.bot_player,
        f'Selected: {beatmap_text}'
    )

    match.update()

@register(RequestPacket.MATCH_CHANGE_MODS)
def change_mods(player: Player, mods: Mods):
    if not player.match:
        return

    player.match.last_activity = time.time()

    mods_before = copy(player.match.mods)

    if player.match.freemod:
        if player is player.match.host:
            # Onky keep SpeedMods
            player.match.mods = mods & Mods.SpeedMods

            # There is a bug, where DT and NC are enabled at the same time
            if Mods.DoubleTime|Mods.Nightcore in player.match.mods:
                player.match.mods &= ~Mods.DoubleTime

        slot = player.match.get_slot(player)
        assert slot is not None

        # Only keep mods that are "FreeModAllowed"
        slot.mods = mods & Mods.FreeModAllowed

        player.match.logger.info(
            f'{player.name} changed their mods to {slot.mods.short}'
        )
    else:
        if player is not player.match.host:
            player.logger.warning(f'{player.name} tried to change mods, but was not host')
            return

        player.match.mods = mods

        # There is a bug, where DT and NC are enabled at the same time
        if Mods.DoubleTime|Mods.Nightcore in player.match.mods:
            player.match.mods &= ~Mods.DoubleTime

        player.match.logger.info(f'Changed mods to: {player.match.mods.short}')

    mods_changed = player.match.mods != mods_before

    if mods_changed:
        player.match.unready_players()

    player.match.update()

@register(RequestPacket.MATCH_READY)
def ready(player: Player):
    if not player.match:
        return

    player.match.last_activity = time.time()

    slot = player.match.get_slot(player)
    assert slot is not None

    slot.status = SlotStatus.Ready
    player.match.update()

@register(RequestPacket.MATCH_HAS_BEATMAP)
@register(RequestPacket.MATCH_NOT_READY)
def not_ready(player: Player):
    if not player.match:
        return

    player.match.last_activity = time.time()

    slot = player.match.get_slot(player)
    assert slot is not None

    slot.status = SlotStatus.NotReady
    player.match.update()

@register(RequestPacket.MATCH_NO_BEATMAP)
def no_beatmap(player: Player):
    if not player.match:
        return

    player.match.last_activity = time.time()

    if player.match.beatmap_id <= 0:
        # Beatmap is being selected by the host
        return

    slot = player.match.get_slot(player)
    assert slot is not None

    slot.status = SlotStatus.NoMap
    player.match.update()

@register(RequestPacket.MATCH_LOCK)
def lock(player: Player, slot_id: int):
    if not player.match:
        return

    if player is not player.match.host:
        return

    player.match.last_activity = time.time()

    if not 0 <= slot_id < 8:
        return

    slot = player.match.slots[slot_id]

    if slot.player is player:
        # Player can't kick themselves
        return

    if slot.has_player:
        player.match.kick_player(slot.player)

    if slot.status == SlotStatus.Locked:
        slot.status = SlotStatus.Open
    else:
        slot.status = SlotStatus.Locked

    player.match.update()

@register(RequestPacket.MATCH_CHANGE_TEAM)
def change_team(player: Player):
    if not player.match:
        return

    if not player.match.ffa:
        return

    player.match.last_activity = time.time()

    slot = player.match.get_slot(player)
    assert slot is not None

    slot.team = {
        SlotTeam.Blue: SlotTeam.Red,
        SlotTeam.Red: SlotTeam.Blue
    }[slot.team]

    player.match.update()

@register(RequestPacket.MATCH_TRANSFER_HOST)
def transfer_host(player: Player, slot_id: int):
    if not player.match:
        return

    if player is not player.match.host:
        return

    player.match.last_activity = time.time()

    if not 0 <= slot_id < 8:
        return

    if not (target := player.match.slots[slot_id].player):
        player.match.logger.warning('Host tried to transfer host into an empty slot?')
        return

    player.match.host = target
    player.match.host.enqueue_match_transferhost()

    player.match.logger.info(f'Changed host to: {target.name}')
    player.match.update()

@register(RequestPacket.MATCH_CHANGE_PASSWORD)
def change_password(player: Player, new_password: str):
    if not player.match:
        return

    if player is not player.match.host:
        return

    player.match.password = new_password
    player.match.update()

    player.match.logger.info(
        f'Changed password to: {new_password}'
    )

@register(RequestPacket.MATCH_START)
def match_start(player: Player):
    if not player.match:
        return

    player.match.last_activity = time.time()

    if player is not player.match.host:
        return

    player.match.start()

@register(RequestPacket.MATCH_LOAD_COMPLETE)
def load_complete(player: Player):
    if not player.match:
        return

    if not player.match.in_progress:
        return

    slot = player.match.get_slot(player)
    assert slot is not None

    slot.loaded = True

    if all(player.match.loaded_players):
        for slot in player.match.slots:
            if not slot.has_map:
                continue

            slot.player.enqueue_match_all_players_loaded()

        player.match.update()

@register(RequestPacket.MATCH_SKIP)
def skip(player: Player):
    if not player.match:
        return

    if not player.match.in_progress:
        return

    slot, id = player.match.get_slot_with_id(player)
    assert slot is not None

    slot.skipped = True

    for p in player.match.players:
        p.enqueue_player_skipped(id)

    for slot in player.match.slots:
        if slot.status == SlotStatus.Playing and not slot.skipped:
            return

    for p in player.match.players:
        p.enqueue_match_skip()

@register(RequestPacket.MATCH_FAILED)
def player_failed(player: Player):
    if not player.match:
        return

    if not player.match.in_progress:
        return

    slot_id = player.match.get_slot_id(player)
    assert slot_id is not None

    for p in player.match.players:
        p.enqueue_player_failed(slot_id)

# TODO: There have been some issues when running this inside a thread...
@register(RequestPacket.MATCH_SCORE_UPDATE)
def score_update(player: Player, scoreframe: bScoreFrame):
    if not player.match:
        return

    slot, id = player.match.get_slot_with_id(player)
    assert slot is not None

    if not slot.is_playing:
        return

    scoreframe.id = id

    for p in player.match.players:
        p.enqueue_score_update(scoreframe)

    for p in session.players.in_lobby:
        p.enqueue_score_update(scoreframe)

@register(RequestPacket.MATCH_COMPLETE)
def match_complete(player: Player):
    if not player.match:
        return

    if not player.match.in_progress:
        return

    player.match.last_activity = time.time()

    slot = player.match.get_slot(player)
    assert slot is not None

    slot.status = SlotStatus.Complete

    if any([slot.is_playing for slot in player.match.slots]):
        return

    # Players that have been playing this round
    players = [
        slot.player for slot in player.match.slots
        if slot.status.value & SlotStatus.Complete.value
        and slot.has_player
    ]

    player.match.unready_players(SlotStatus.Complete)
    player.match.in_progress = False

    for p in players:
        p.enqueue_match_complete()

    player.match.logger.info('Match finished')
    player.match.update()

@register(RequestPacket.TOURNAMENT_MATCH_INFO)
@run_in_thread
def tourney_match_info(player: Player, match_id: int):
    if not player.supporter:
        return

    if not player.is_tourney_client:
        return

    if not (match := session.matches[match_id]):
        return

    player.enqueue_match(match.bancho_match)

@register(RequestPacket.ERROR_REPORT)
@run_in_thread
def bancho_error(player: Player, error: str):
    session.logger.error(f'Bancho Error Report:\n{error}')

@register(RequestPacket.CHANGE_FRIENDONLY_DMS)
@run_in_thread
def change_friendonly_dms(player: Player, enabled: bool):
    player.client.friendonly_dms = enabled