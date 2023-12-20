
from typing import List, Union, Optional, NamedTuple, Callable
from pytimeparse.timeparse import timeparse
from datetime import timedelta, datetime
from dataclasses import dataclass
from threading import Thread

from .common.cache import leaderboards
from .common.database.repositories import (
    infringements,
    beatmapsets,
    beatmaps,
    matches,
    clients,
    reports,
    events,
    scores,
    stats,
    users,
)

from .common.constants import (
    MatchScoringTypes,
    MatchTeamTypes,
    Permissions,
    SlotStatus,
    EventType,
    SlotTeam,
    GameMode,
    Mods
)

from .objects.multiplayer import StartingTimers
from .objects.channel import Channel
from .common.objects import bMessage
from .objects.player import Player

import timeago
import config
import random
import time
import app

@dataclass
class Context:
    player: Player
    trigger: str
    target: Union[Channel, Player]
    args: List[str]

@dataclass
class CommandResponse:
    response: List[str]
    hidden: bool

class Command(NamedTuple):
    triggers: List[str]
    callback: Callable
    permissions: Permissions
    hidden: bool
    doc: Optional[str]

class CommandSet:
    def __init__(self, trigger: str, doc: str) -> None:
        self.trigger = trigger
        self.doc = doc

        self.conditions: List[Callable] = []
        self.commands: List[Command] = []

    def register(
        self,
        aliases:
        List[str],
        p: Permissions = Permissions.Normal,
        hidden: bool = False
    ) -> Callable:
        def wrapper(f: Callable):
            self.commands.append(
                Command(
                    aliases,
                    f,
                    p,
                    hidden,
                    doc=f.__doc__
                )
            )
            return f
        return wrapper

    def condition(self, f: Callable) -> Callable:
        self.conditions.append(f)
        return f

commands: List[Command] = []
sets = [
    mp_commands := CommandSet('mp', 'Multiplayer Commands'),
    system_commands := CommandSet('system', 'System Commands')
]

# TODO: !system deploy
# TODO: !system restart
# TODO: !system shutdown
# TODO: !system stats
# TODO: !system exec

@system_commands.condition
def is_admin(ctx: Context) -> bool:
    return ctx.player.is_admin

@system_commands.register(['maintenance', 'panic'], Permissions.Admin)
def maintenance_mode(ctx: Context) -> List[str]:
    """<on/off>"""
    if ctx.args:
        # Change maintenance value based on input
        config.MAINTENANCE = ctx.args[0].lower() == 'on'
    else:
        # Toggle maintenance value
        config.MAINTENANCE = not config.MAINTENANCE

    if config.MAINTENANCE:
        for player in app.session.players:
            if player.is_admin:
                continue

            player.close_connection()

    return [
        f'Maintenance mode is now {"enabled" if config.MAINTENANCE else "disabled"}.'
    ]

@mp_commands.condition
def inside_match(ctx: Context) -> bool:
    return ctx.player.match is not None

@mp_commands.condition
def inside_chat(ctx: Context) -> bool:
    return ctx.target is ctx.player.match.chat

@mp_commands.condition
def is_host(ctx: Context) -> bool:
    return (ctx.player is ctx.player.match.host) or \
           (ctx.player.is_tourney_manager) or \
           (ctx.player.is_admin)

@mp_commands.register(['help', 'h'], hidden=True)
def mp_help(ctx: Context):
    """- Shows this message"""
    response = []

    for command in mp_commands.commands:
        if command.permissions not in ctx.player.permissions:
            continue

        if not command.doc:
            continue

        response.append(f'!{mp_commands.trigger.upper()} {command.triggers[0].upper()} {command.doc}')

    return response

@mp_commands.register(['start', 'st'])
def mp_start(ctx: Context):
    """<force/seconds/cancel> - Start the match, with any players that are ready"""
    if len(ctx.args) > 1:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <force/seconds/cancel>']

    match = ctx.player.match

    if match.in_progress:
        return ['This match is already running.']

    if not ctx.args:
        # Check if match is starting
        if match.starting:
            time_remaining = round(match.starting.time - time.time())
            return [f'Match starting in {time_remaining} seconds.']

        # Check if players are ready
        if any(s.status == SlotStatus.NotReady for s in match.slots):
            return [f'Not all players are ready ("!{mp_commands.trigger}" {ctx.trigger} force" to start anyways)']

        match.start()
        return ['Match was started. Good luck!']

    if ctx.args[0].isdecimal():
        # Host wants to start a timer

        if match.starting:
            # Timer is already running
            time_remaining = round(match.starting.time - time.time())
            return [f'Match starting in {time_remaining} seconds.']

        duration = int(ctx.args[0])

        if duration < 0:
            return ['no.']

        if duration > 300:
            return ['Please lower your duration!']

        match.starting = StartingTimers(
            time.time() + duration,
            timer := Thread(
                target=match.execute_timer,
                daemon=True
            )
        )

        timer.start()

        return [f'Match starting in {duration} {"seconds" if duration != 1 else "second"}.']

    elif ctx.args[0] in ('cancel', 'c'):
        # Host wants to cancel the timer
        if not match.starting:
            return ['Match timer is not active!']

        # The timer thread will check if 'starting' is None
        match.starting = None
        return ['Match timer was cancelled.']

    elif ctx.args[0] in ('force', 'f'):
        match.start()
        return ['Match was started. Good luck!']

    return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <force/seconds/cancel>']

@mp_commands.register(['close', 'terminate', 'disband'])
def mp_close(ctx: Context):
    """- Close a match and kick all players"""
    ctx.player.match.logger.info('Match was closed.')
    ctx.player.match.close()

    return ['Match was closed.']

@mp_commands.register(['abort'])
def mp_abort(ctx: Context):
    """- Abort the current match"""
    if not ctx.player.match.in_progress:
        return ["Nothing to abort."]

    ctx.player.match.abort()
    ctx.player.match.logger.info('Match was aborted.')

    return ['Match aborted.']

@mp_commands.register(['map', 'setmap', 'beatmap'])
def mp_map(ctx: Context):
    """<beatmap_id> - Select a new beatmap by it's id"""
    if len(ctx.args) != 1 or not ctx.args[0].isdecimal():
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <beatmap_id>']

    match = ctx.player.match
    beatmap_id = int(ctx.args[0])

    if beatmap_id == match.beatmap_id:
        return ['That map was already selected.']

    if not (map := beatmaps.fetch_by_id(beatmap_id)):
        return ['Could not find that beatmap.']

    match.beatmap_id = map.id
    match.beatmap_hash = map.md5
    match.beatmap_name = map.full_name
    match.mode = GameMode(map.mode)
    match.update()

    match.logger.info(f'Selected: {map.full_name}')

    return [f'Selected: {map.link}']

@mp_commands.register(['mods', 'setmods'])
def mp_mods(ctx: Context):
    """<mods> - Set the current match's mods (e.g. HDHR)"""
    if len(ctx.args) != 1 or len(ctx.args[0]) % 2 != 0:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <mods>']

    match = ctx.player.match
    mods = Mods.from_string(ctx.args[0])
    # TODO: Filter out invalid mods

    if mods == match.mods:
        return [f'Mods are already set to {match.mods.short}.']

    if match.freemod:
        # Set match mods
        match.mods = mods & ~Mods.FreeModAllowed

        # Set host mods
        match.host_slot.mods = mods & ~Mods.SpeedMods
    else:
        match.mods = mods

    match.logger.info(f'Updated match mods to {match.mods.short}.')

    match.update()
    return [f'Updated match mods to {match.mods.short}.']

@mp_commands.register(['freemod', 'fm', 'fmod'])
def mp_freemod(ctx: Context):
    """<on/off> - Enable or disable freemod status."""
    if len(ctx.args) != 1 or ctx.args[0] not in ("on", "off"):
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <on/off>']

    freemod = ctx.args[0] == 'on'
    match = ctx.player.match

    if match.freemod == freemod:
        return [f'Freemod is already {ctx.args[0]}.']

    match.unready_players()
    match.freemod = freemod
    match.logger.info(f'Freemod: {freemod}')

    if freemod:
        for slot in match.slots:
            if slot.status.value & SlotStatus.HasPlayer.value:
                # Set current mods to every player inside the match, if they are not speed mods
                slot.mods = match.mods & ~Mods.SpeedMods

                # TODO: Fix for older clients without freemod support
                # slot.mods = []

            # The speedmods are kept in the match mods
            match.mods = match.mods & ~Mods.FreeModAllowed
    else:
        # Keep mods from host
        match.mods |= match.host_slot.mods

        # Reset any mod from players
        for slot in match.slots:
            slot.mods = Mods.NoMod

    match.update()
    return [f'Freemod is now {"enabled" if freemod else "disabled"}.']

@mp_commands.register(['host', 'sethost'])
def mp_host(ctx: Context):
    """<name> - Set the host for this match"""
    if len(ctx.args) <= 0:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <name>']

    name = ' '.join(ctx.args[:])
    match = ctx.player.match

    if not (target := match.get_player(name)):
        return ['Could not find this player.']

    events.create(
        match.db_match.id,
        type=EventType.Host,
        data={'old_host': target.id, 'new_host': match.host.id}
    )

    match.host = target
    match.host.enqueue_match_transferhost()

    match.logger.info(f'Changed host to: {target.name}')
    match.update()

    return [f'{target.name} is now host of this match.']

bot_invites = [
    "Uhh... sorry, no time to play. (°_o)",
    "I'm too busy!",
    "nope.",
    "idk how to play this game... ¯\(°_o)/¯"
]

@mp_commands.register(['invite', 'inv'])
def mp_invite(ctx: Context):
    """<name> - Invite a player to this match"""
    if len(ctx.args) <= 0:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <name>']

    name = ' '.join(ctx.args[:])
    match = ctx.player.match

    if name == app.session.bot_player.name:
        return [bot_invites[random.randrange(0, len(bot_invites))]]

    if not (target := app.session.players.by_name(name)):
        return [f'Could not find the player "{name}".']

    if target is ctx.player:
        return ['You are already here.']

    if target.match is match:
        return ['This player is already here.']

    target.enqueue_invite(
        bMessage(
            ctx.player.name,
            f'Come join my multiplayer match: {match.embed}',
            ctx.player.name,
            ctx.player.id
        )
    )

    return [f'Invited {target.name} to this match.']

@mp_commands.register(['force', 'forceinvite'], Permissions.Admin)
def mp_force_invite(ctx: Context):
    """<name> - Force a player to join this match"""
    if len(ctx.args) <= 0:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <name>']

    name = ' '.join(ctx.args[:])
    match = ctx.player.match

    if not (target := app.session.players.by_name(name)):
        return [f'Could not find the player "{name}".']

    if target.match is match:
        return [f'{target.name} is already in this match.']

    if target.match is not None:
        target.match.kick_player(target)

    if (slot_id := match.get_free()) is None:
        return ['This match is full.']

    # Join the chat
    target.enqueue_channel(match.chat.bancho_channel, autojoin=True)
    match.chat.add(target)

    slot = match.slots[slot_id]

    if match.team_type in (MatchTeamTypes.TeamVs, MatchTeamTypes.TagTeamVs):
        slot.team = SlotTeam.Red

    slot.status = SlotStatus.NotReady
    slot.player = target

    target.match = match
    target.enqueue_matchjoin_success(match.bancho_match)

    match.logger.info(f'{target.name} joined')
    match.update()

    return ['Welcome.']

@mp_commands.register(['lock'])
def mp_lock(ctx: Context):
    """- Lock all unsued slots in the match."""
    for slot in ctx.player.match.slots:
        if slot.has_player:
            ctx.player.match.kick_player(slot.player)

        if slot.status == SlotStatus.Open:
            slot.status = SlotStatus.Locked

    ctx.player.match.update()
    return ['Locked all unused slots.']

@mp_commands.register(['unlock'])
def mp_unlock(ctx: Context):
    """- Unlock all locked slots in the match."""
    for slot in ctx.player.match.slots:
        if slot.status == SlotStatus.Locked:
            slot.status = SlotStatus.Open

    ctx.player.match.update()
    return ['Locked all unused slots.']

@mp_commands.register(['kick', 'remove'])
def mp_kick(ctx: Context):
    """<name> - Kick a player from the match"""
    if len(ctx.args) <= 0:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <name>']

    name = ' '.join(ctx.args[:]).strip()
    match = ctx.player.match

    if name == app.session.bot_player.name:
        return ["no."]

    if name == ctx.player.name:
        return ["no."]

    for player in match.players:
        if player.name != name:
            continue

        match.kick_player(player)

        if all(slot.empty for slot in match.slots):
            match.close()
            match.logger.info('Match was disbanded.')

        return ["Player was kicked from the match."]

    return [f'Could not find the player "{name}".']

@mp_commands.register(['ban', 'restrict'])
def mp_ban(ctx: Context):
    """<name> - Ban a player from the match"""
    if len(ctx.args) <= 0:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <name>']

    name = ' '.join(ctx.args[:]).strip()
    match = ctx.player.match

    if name == app.session.bot_player.name:
        return ["no."]

    if name == ctx.player.name:
        return ["no."]

    if not (player := app.session.players.by_name(name)):
        return [f'Could not find the player "{name}".']

    match.ban_player(player)

    if all(slot.empty for slot in match.slots):
        match.close()
        match.logger.info('Match was disbanded.')

    return ["Player was banned from the match."]

@mp_commands.register(['unban', 'unrestrict'])
def mp_unban(ctx: Context):
    """<name> - Unban a player from the match"""
    if len(ctx.args) <= 0:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <name>']

    name = ' '.join(ctx.args[:]).strip()
    match = ctx.player.match

    if not (player := app.session.players.by_name(name)):
        return [f'Could not find the player "{name}".']

    if player.id not in match.banned_players:
        return ['Player was not banned from the match.']

    match.unban_player(player)

    return ["Player was unbanned from the match."]

@mp_commands.register(['name', 'setname'])
def mp_name(ctx: Context):
    """<name> - Change the match name"""
    if len(ctx.args) <= 0:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <name>']

    name = ' '.join(ctx.args[:]).strip()
    match = ctx.player.match

    match.name = name
    match.update()

    matches.update(
        match.db_match.id,
        {
            "name": name
        }
    )

@mp_commands.register(['set'])
def mp_set(ctx: Context):
    """<teammode> (<scoremode>) (<size>)"""
    if len(ctx.args) <= 0:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <teammode> (<scoremode>) (<size>)']

    try:
        match = ctx.player.match
        match.team_type = MatchTeamTypes(int(ctx.args[0]))

        if len(ctx.args) > 1:
            match.scoring_type = MatchScoringTypes(int(ctx.args[1]))

        if len(ctx.args) > 2:
            size = max(1, min(int(ctx.args[2]), 8))

            for slot in match.slots[size:]:
                if slot.has_player:
                    match.kick_player(slot.player)

                slot.reset(SlotStatus.Locked)

            for slot in match.slots[:size]:
                if slot.has_player:
                    continue

                slot.reset()

            if all(slot.empty for slot in match.slots):
                match.close()
                return ["Match was disbanded."]

        match.update()
    except ValueError:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <teammode> (<scoremode>) (<size>)']

    slot_size = len([slot for slot in match.slots if not slot.locked])

    return [f"Settings changed to {match.team_type.name}, {match.scoring_type.name}, {slot_size} slots."]

@mp_commands.register(['size'])
def mp_size(ctx: Context):
    """<size> - Set the amount of available slots (1-8)"""
    if len(ctx.args) <= 0:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <size>']

    match = ctx.player.match
    size = max(1, min(int(ctx.args[0]), 8))

    for slot in match.slots[size:]:
        if slot.has_player:
            match.kick_player(slot.player)

        slot.reset(SlotStatus.Locked)

    for slot in match.slots[:size]:
        if slot.has_player:
            continue

        slot.reset()

    if all(slot.empty for slot in match.slots):
        match.close()
        return ["Match was disbanded."]

    match.update()

    return [f"Changed slot size to {size}."]

@mp_commands.register(['move'])
def mp_move(ctx: Context):
    """<name> <slot> - Move a player to a slot"""
    if len(ctx.args) <= 1:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <name> <slot>']

    match = ctx.player.match
    name = ctx.args[0]
    slot_id = max(1, min(int(ctx.args[1]), 8))

    if not (player := match.get_player(name)):
        return [f'Could not find player {name}.']

    old_slot = match.get_slot(player)

    # Check if slot is already used
    if (slot := match.slots[slot_id-1]).has_player:
        return [f'This slot is already in use by {slot.player.name}.']

    slot.copy_from(old_slot)
    old_slot.reset()

    match.update()

    return [f'Moved {player.name} into slot {slot_id}.']

@mp_commands.register(['settings'])
def mp_settings(ctx: Context):
    """- View the current match settings"""
    match = ctx.player.match
    beatmap_link = f'[http://osu.{config.DOMAIN_NAME}/b/{match.beatmap_id} {match.beatmap_name}]' \
                    if match.beatmap_id > 0 else match.beatmap_name
    return [
        f"Room Name: {match.name} ([http://osu.{config.DOMAIN_NAME}/mp/{match.db_match.id} View History])",
        f"Beatmap: {beatmap_link}",
        f"Active Mods: +{match.mods.short}",
        f"Team Mode: {match.team_type.name}",
        f"Win Condition: {match.scoring_type.name}",
        f"Players: {len(match.players)}",
        *[
            f"{match.slots.index(slot) + 1} ({slot.status.name}) - [http://osu.{config.DOMAIN_NAME}/u/{slot.player.id} {slot.player.name}]{f' +{slot.mods.short}' if slot.mods > 0 else ' '} [{'Host' if match.host == slot.player else ''}]"
            for slot in match.slots
            if slot.has_player
        ],
    ]

@mp_commands.register(['team', 'setteam'])
def mp_team(ctx: Context):
    """<name> <color> - Set a players team color"""
    if len(ctx.args) <= 1:
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <name> <color>']

    match = ctx.player.match
    name = ctx.args[0]
    team = ctx.args[1].capitalize()

    if team not in ("Red", "Blue", "Neutral"):
        return [f'Invalid syntax: !{mp_commands.trigger} {ctx.trigger} <name> <red/blue>']

    if team == "Neutral" and match.ffa:
        match.team_type = MatchTeamTypes.HeadToHead

    elif team != "Neutral" and not match.ffa:
        match.team_type = MatchTeamTypes.TeamVs

    if not (player := match.get_player(name)):
        return [f'Could not find player "{name}"']

    slot = match.get_slot(player)
    slot.team = SlotTeam[team]

    match.update()

    return [f"Moved {player.name} to team {team}."]

@mp_commands.register(['password', 'setpassword', 'pass'])
def mp_password(ctx: Context):
    """(<password>) - (Re)set the match password"""
    match = ctx.player.match

    if not ctx.args:
        match.password = ""
        match.update()
        return ["Match password was reset."]

    match.password = ctx.args[:]
    match.update()

    return ["Match password was set."]

# TODO: Tourney rooms
# TODO: Match refs

def command(
    aliases: List[str],
    p: Permissions = Permissions.Normal,
    hidden: bool = True,
) -> Callable:
    def wrapper(f: Callable) -> Callable:
        commands.append(
            Command(
                aliases,
                f,
                p,
                hidden,
                f.__doc__
            ),
        )
        return f
    return wrapper

@command(['help', 'h', ''])
def help(ctx: Context) -> Optional[List]:
    """- Shows this message"""
    response = ['Standard Commands:']

    response.extend(
        f'!{command.triggers[0].upper()} {command.doc}'
        for command in commands
        if command.permissions in ctx.player.permissions
    )
    # Command sets
    for set in sets:
        if not set.commands:
            # Set has no commands
            continue

        for condition in set.conditions:
            if not condition(ctx):
                break
        else:
            response.append(f'{set.doc} (!{set.trigger}):')

            for command in set.commands:
                if command.permissions not in ctx.player.permissions:
                    continue

                if not command.doc:
                    continue

                response.append(
                    f'!{set.trigger.upper()} {command.triggers[0].upper()} {command.doc}'
                )

    return response

@command(['roll'], hidden=False)
def roll(ctx: Context) -> Optional[List]:
    """<number> - Roll a dice and get random result from 1 to <number> (default 100)"""
    max_roll = 100

    if ctx.args and ctx.args[0].isdecimal():
        max_roll = int(ctx.args[0])

        if max_roll <= 0:
            return ['no.']

        # User set a custom roll number
        max_roll = min(max_roll, 0x7FFF)

    return [f'{ctx.player.name} rolls {random.randrange(0, max_roll+1)}!']

@command(['report'])
def report(ctx: Context) -> Optional[List]:
    """<username> <reason>"""
    if len(ctx.args) < 1:
        return [f'Invalid syntax: !{ctx.trigger} <username> (<reason>)']

    username = ctx.args[0]
    reason = ' '.join(ctx.args[1:])[:255]

    if not (target := users.fetch_by_name(username)):
        return [f'Could not find player "{username}".']

    if target.id == ctx.player.id:
        return ['You cannot report yourself.']

    if target.name == app.session.bot_player.name:
        return ['no.']

    if r := reports.fetch_by_sender_to_target(ctx.player.id, target.id):
        seconds_since_last_report = (
            datetime.now().timestamp() - r.time.timestamp()
        )

        if seconds_since_last_report <= 86400:
            return [
                'You have already reported that user. '
                'Please wait until you report them again!'
            ]

    if channel := app.session.channels.by_name('#admin'):
        # Send message to admin chat
        channel.send_message(
            app.session.bot_player,
            f'[{ctx.target.name}] {ctx.player.link} reported {target.link} for: "{reason}".'
        )

    # Create record in database
    reports.create(
        target.id,
        ctx.player.id,
        reason
    )

    return ['Chat moderators have been alerted. Thanks for your help.']

@command(['search'], Permissions.Supporter, hidden=False)
def search(ctx: Context):
    """<query> - Search a beatmap"""
    query = ' '.join(ctx.args[:])

    if len(query) <= 2:
        return ['Query too short']

    if not (result := beatmapsets.search_one(query)):
        return ['No matches found']

    status = {
        -2: 'Graveyarded',
        -1: 'WIP',
         0: 'Pending',
         1: 'Ranked',
         2: 'Approved',
         3: 'Qualified',
         4: 'Loved'
    }[result.status]

    return [f'{result.link} [{status}]']

@command(['where', 'location'], hidden=False)
def where(ctx: Context):
    """<name> - Get a player's current location"""
    if len(ctx.args) < 1:
        return [f'Invalid syntax: !{ctx.trigger} <username>']

    name = ' '.join(ctx.args[:])

    if not (target := app.session.players.by_name(name)):
        return ['Player is not online']

    if not target.client.ip:
        return ['The players location data could not be resolved']

    city_string = f"({target.client.ip.city})" if target.client.display_city else ""
    location_string = target.client.ip.country_name

    return [f'{target.name} is in {location_string} {city_string}']

@command(['stats'], hidden=False)
def get_stats(ctx: Context):
    """<username> - Get the stats of a player"""
    if len(ctx.args) < 1:
        return [f'Invalid syntax: !{ctx.trigger} <username>']

    name = ' '.join(ctx.args[:])

    if not (target := app.session.players.by_name(name)):
        return ['Player is not online']

    global_rank = leaderboards.global_rank(target.id, target.status.mode.value)
    score_rank = leaderboards.score_rank(target.id, target.status.mode.value)

    return [
        f'Stats for [http://osu.{config.DOMAIN_NAME}/u/{target.id} {target.name}] is {target.status.action.name}:',
        f'  Score:    {format(target.current_stats.rscore, ",d")} (#{score_rank})',
        f'  Plays:    {target.current_stats.playcount} (lv{target.level})',
        f'  Accuracy: {round(target.current_stats.acc * 100, 2)}%',
        f'  PP:       {round(target.current_stats.pp, 2)}pp (#{global_rank})'
    ]

@command(['client', 'version'], hidden=False)
def get_client_version(ctx: Context):
    """<username> - Get the version of the client that a player is currently using"""
    if len(ctx.args) < 1:
            return [f'Invalid syntax: !{ctx.trigger} <username>']

    name = ' '.join(ctx.args[:])

    if not (target := app.session.players.by_name(name)):
        return ['Player is not online']

    return [f"{target.name} is playing on {target.client.version.string}"]

@command(['monitor'], Permissions.Admin)
def monitor(ctx: Context) -> Optional[List]:
    """<name> - Monitor a player"""

    if len(ctx.args) < 1:
        return [f'Invalid syntax: !{ctx.trigger} <name>']

    name = ' '.join(ctx.args[:])

    if not (player := app.session.players.by_name(name)):
        return ['Player is not online']

    player.enqueue_monitor()

    return ['Player has been monitored']

@command(['alert', 'announce', 'broadcast'], Permissions.Admin)
def alert(ctx: Context) -> Optional[List]:
    """<message> - Send a message to all players"""

    if not ctx.args:
        return [f'Invalid syntax: !{ctx.trigger} <message>']

    app.session.players.announce(' '.join(ctx.args))

    return [f'Alert was sent to {len(app.session.players)} players.']

@command(['alertuser'], Permissions.Admin)
def alertuser(ctx: Context) -> Optional[List]:
    """<username> <message> - Send a notification to a player"""

    if len(ctx.args) < 2:
        return [f'Invalid syntax: !{ctx.trigger} <username> <message>']

    username = ctx.args[0]

    if not (player := app.session.players.by_name(username)):
        return [f'Could not find player "{username}".']

    player.enqueue_announcement(' '.join(ctx.args[1:]))

    return [f'Alert was sent to {player.name}.']

@command(['silence', 'mute'], Permissions.Admin, hidden=False)
def silence(ctx: Context) -> Optional[List]:
    """<username> <duration> (<reason>)"""

    if len(ctx.args) < 2:
        return [f'Invalid syntax: !{ctx.trigger} <username> <duration> (<reason>)']

    name = ctx.args[0]
    duration = timeparse(ctx.args[1])
    reason = ' '.join(ctx.args[2:])

    if (player := app.session.players.by_name(name)):
        player.silence(duration, reason)
        silence_end = player.object.silence_end
    else:
        if not (player := users.fetch_by_name(name)):
            return [f'Player "{name}" was not found.']

        if player.silence_end:
            player.silence_end += timedelta(seconds=duration)
        else:
            player.silence_end = datetime.now() + timedelta(seconds=duration)

        users.update(
            player.id,
            {
                'silence_end': player.silence_end
            }
        )

        silence_end = player.silence_end

        # Add entry inside infringements table
        infringements.create(
            player.id,
            action=1,
            length=(datetime.now() + timedelta(seconds=duration)),
            description=reason
        )

    time_string = timeago.format(silence_end)
    time_string = time_string.replace('in ', '')

    return [f'{player.name} was silenced for {time_string}']

@command(['unsilence', 'unmute'], Permissions.Admin, hidden=False)
def unsilence(ctx: Context):
    """- <username>"""

    if len(ctx.args) < 1:
        return [f'Invalid syntax: !{ctx.trigger} <name>']

    name = ctx.args[0]

    if (player := app.session.players.by_name(name)):
        player.unsilence()
        return [f'{player.name} was unsilenced.']

    if not (player := users.fetch_by_name(name)):
        return [f'Player "{name}" was not found.']

    users.update(player.id, {'silence_end': None})

    return [f'{player.name} was unsilenced.']

@command(['restrict', 'ban'], Permissions.Admin, hidden=False)
def restrict(ctx: Context) -> Optional[List]:
    """ <name> <length/permanent> (<reason>)"""

    if len(ctx.args) < 2:
        return [f'Invalid syntax: !{ctx.trigger} <name> <length/permanent> (<reason>)']

    username = ctx.args[0]
    length   = ctx.args[1]
    reason   = ' '.join(ctx.args[2:])

    if not length.startswith('perma'):
        until = datetime.now() + timedelta(seconds=timeparse(length))
    else:
        until = None

    if not (player := app.session.players.by_name(username)):
        # Player is not online, or was not found
        player = users.fetch_by_name(username)

        if not player:
            return [f'Player "{username}" was not found']

        player.restricted = True
        player.permissions = 0

        # Update user
        users.update(player.id,
            {
                'restricted': True,
                'permissions': 0
            }
        )
        leaderboards.remove(
            player.id,
            player.country
        )
        stats.delete_all(player.id)
        scores.hide_all(player.id)

        # Update hardware
        clients.update_all(player.id, {'banned': True})

        # Add entry inside infringements table
        infringements.create(
            player.id,
            action=0,
            length=until,
            description=reason,
            is_permanent=not until,
        )
    else:
        # Player is online
        player.restrict(
            reason,
            until
        )

    return [f'{player.name} was restricted.']

@command(['unrestrict', 'unban'], Permissions.Admin, hidden=False)
def unrestrict(ctx: Context) -> Optional[List]:
    """<name> <restore scores (true/false)>"""

    if len(ctx.args) < 1:
        return [f'Invalid syntax: !{ctx.trigger} <name> <restore scores (true/false)>']

    username = ctx.args[0]
    restore_scores = eval(ctx.args[1].capitalize()) if len(ctx.args) > 1 else False
    if not (player := users.fetch_by_name(username)):
        return [f'Player "{username}" was not found.']

    if not player.restricted:
        return [f'Player "{username}" is not restricted.']

    users.update(player.id,
        {
            'restricted': False,
            'permissions': 5 if config.FREE_SUPPORTER else 1
        }
    )

    # Update hardware
    clients.update_all(player.id, {'banned': False})

    if restore_scores:
        try:
            scores.restore_hidden_scores(player.id)
            stats.restore(player.id)
        except Exception as e:
            app.session.logger.error(
                f'Failed to restore scores of player "{player.name}": {e}',
                exc_info=e
            )
            return ['Failed to restore scores, but player was unrestricted.']

    return [f'Player "{username}" was unrestricted.']

@command(['moderated'], Permissions.Admin, hidden=False)
def moderated(ctx: Context) -> Optional[List]:
    """<on/off>"""
    if len(ctx.args) != 1 and ctx.args[0] not in ('on', 'off'):
        return [f'Invalid syntax: !{ctx.trigger} <on/off>']

    if type(ctx.target) != Channel:
        return ['Target is not a channel.']

    ctx.target.moderated = ctx.args[0] == "on"

    return [f'Moderated mode is now {"enabled" if ctx.target.moderated else "disabled"}.']

@command(['kick', 'disconnect'], Permissions.Admin, hidden=False)
def kick(ctx: Context) -> Optional[List]:
    """<username>"""
    if len(ctx.args) <= 0:
        return [f'Invalid syntax: !{ctx.trigger} <username>']

    username = ' '.join(ctx.args[:])

    if not (player := app.session.players.by_name(username)):
        return [f'User "{username}" was not found.']

    player.close_connection()

    return [f'{player.name} was disconnected from bancho.']

@command(['kill', 'close'], Permissions.Admin, hidden=False)
def kill(ctx: Context) -> Optional[List]:
    """<username>"""
    if len(ctx.args) <= 0:
        return [f'Invalid syntax: !{ctx.trigger} <username>']

    username = ' '.join(ctx.args[:])

    if not (player := app.session.players.by_name(username)):
        return [f'User "{username}" was not found.']

    player.object.permissions = 255
    player.enqueue_permissions()
    player.enqueue_ping()
    player.close_connection()

    return [f'{player.name} was disconnected from bancho.']

# TODO: !recent
# TODO: !rank
# TODO: !faq
# TODO: !top

def get_command(
    player: Player,
    target: Union[Channel, Player],
    message: str
) -> Optional[CommandResponse]:
    # Parse command
    trigger, *args = message.strip()[1:].split(' ')
    trigger = trigger.lower()

    # Regular commands
    for command in commands:
        if trigger in command.triggers:
            # Check permissions
            if command.permissions not in player.permissions:
                return None

            # Try running the command
            try:
                response = command.callback(
                    Context(
                        player,
                        trigger,
                        target,
                        args
                    )
                )
            except Exception as e:
                player.logger.error(
                    f'Command error: {e}',
                    exc_info=e
                )

                response = ['An error occurred while running this command.']

            return CommandResponse(
                response,
                command.hidden
            )

    try:
        set_trigger, trigger, *args = trigger, *args
    except ValueError:
        return

    # Command sets
    for set in sets:
        if set.trigger != set_trigger:
            continue

        for command in set.commands:
            if trigger in command.triggers:
                # Check permissions
                if command.permissions not in player.permissions:
                    return None

                ctx = Context(
                    player,
                    trigger,
                    target,
                    args
                )

                # Check set conditions
                for condition in set.conditions:
                    if not condition(ctx):
                        break
                else:
                    # Try running the command
                    try:
                        response = command.callback(ctx)
                    except Exception as e:
                        player.logger.error(
                            f'Command error: {e}',
                            exc_info=e
                        )

                        response = ['An error occurred while running this command.']

                    return CommandResponse(
                        response,
                        command.hidden
                    )

    return None

def execute(
    player: Player,
    target: Union[Channel, Player],
    command_message: str
):
    if not command_message.startswith('!'):
        command_message = f'!{command_message}'

    command = get_command(
        player,
        target,
        command_message
    )

    if not command:
        return

    # Send to others
    if not command.hidden and type(target) == Channel:
        target.send_message(
            player,
            command_message,
            submit_to_database=True
        )

        for message in command.response:
            target.send_message(
                app.session.bot_player,
                message,
                submit_to_database=True
            )
        return

    player.logger.info(f'[{player.name}]: {command_message}')
    player.logger.info(f'[{app.session.bot_player.name}]: {", ".join(command.response)}')

    target_name = target.name \
        if type(target) == Player \
        else target.display_name

    # Send to sender
    for message in command.response:
        player.enqueue_message(
            bMessage(
                app.session.bot_player.name,
                message,
                target_name,
                app.session.bot_player.id
            )
        )
