"""Optional QQ native command buttons, enabled only after platform approval."""
import asyncio
import random

from astrbot.api import logger

from .market_choices import canonical_name


def keyboard(options, sender, prefix='', server=''):
    buttons = []
    for key, label in list(options.items())[:12]:
        # Full names keep an old button tied to its own product, even after a
        # newer search has changed the session's numeric selection mapping.
        command = f'{prefix}物价 {canonical_name(label)}'
        if server:
            command += f' {server}'
        buttons.append({
            'id': f'market-{key}',
            'render_data': {'label': key, 'visited_label': key, 'style': 1},
            'action': {
                'type': 2, 'enter': True,
                'permission': {'type': 0, 'specify_user_ids': [sender]},
                'data': command, 'unsupport_tips': f'请发送 {prefix}物价选{key}',
            },
        })
    return {'content': {'rows': [{'buttons': buttons[i:i + 4]} for i in range(0, len(buttons), 4)]}}


class MarketButtons:
    def __init__(self):
        self.denied = set()

    async def send(self, event, text, options, server='', prefix='', enabled=False):
        if not enabled or not options:
            return False
        if hasattr(event, 'valid') and not event.valid():
            return True
        try:
            platform = event.get_platform_name()
            platform_id = event.get_platform_id()
            sender = str(event.get_sender_id() or '')
        except AttributeError:
            return False
        if platform not in {'qq_official', 'qqofficial_webhook'} or platform_id in self.denied or not sender:
            return False
        from botpy.message import GroupMessage, C2CMessage
        from botpy.errors import ForbiddenError, ServerError
        source = event.message_obj.raw_message
        if isinstance(source, GroupMessage) and source.group_openid:
            send = event.bot.api.post_group_message
            recipient = {'group_openid': source.group_openid}
        elif isinstance(source, C2CMessage):
            send = event.bot.api.post_c2c_message
            recipient = {'openid': sender}
        else:
            return False
        try:
            async with asyncio.timeout(4):
                await send(**recipient, msg_id=event.message_obj.message_id,
                           msg_seq=random.randint(10001, 20000), msg_type=2,
                           markdown={'content': text},
                           keyboard=keyboard(options, sender, prefix, server))
            getattr(event, 'event', event)._has_send_oper = True
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Permission/validation rejection is a definite non-send: use the
            # normal text path and stop repeating failed button requests.
            status = getattr(exc, 'status', None)
            denied = isinstance(exc, ForbiddenError) or (
                isinstance(exc, ServerError) and any(word in str(exc).lower() for word in
                    ('不允许', '权限', 'permission', 'keyboard', '按钮', 'markdown'))
            )
            if denied or (isinstance(status, int) and 400 <= status < 500 and status != 429):
                self.denied.add(platform_id)
                logger.warning('物价按钮被平台拒绝，改用文字选款 status=%s', status)
                return False
            # A transport timeout may already have delivered the card. Avoid
            # sending it twice; the independently rendered image still follows.
            logger.warning('物价按钮发送回执未确认 type=%s', type(exc).__name__)
            return True
