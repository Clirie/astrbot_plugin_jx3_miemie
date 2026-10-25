"""Market names and short-lived choices; no chat history or persistent profiles."""
import re
import time
import asyncio
from collections import OrderedDict
from dataclasses import dataclass


def normalize_query(text):
    text = str(text or '').strip().replace('关灯', '观灯')
    text = re.sub(r'([蓝白黄红紫黑])光灯', r'\1观灯', text)
    return text.replace('标准版', '标准').replace('豪华版', '豪华').replace('典藏版', '典藏')


def canonical_name(label):
    return str(label).split('｜', 1)[0].strip()


def compact(text):
    return re.sub(r'[\s·•.。]', '', normalize_query(text))


def menu_lines(options):
    """Show a colour once when its standard and deluxe editions form a pair."""
    rows = [(key, canonical_name(label).split('·'), label) for key, label in options.items()]
    if (not rows or any(len(parts) < 3 or parts[-1] not in {'标准','豪华','典藏'} for _,parts,_ in rows)
            or len({parts[0] for _,parts,_ in rows}) != 1):
        return [f'{key}. {label}' for key,label in options.items()]
    groups = OrderedDict()
    for key, parts, label in rows:
        family = '·'.join(parts[1:-1])
        group = groups.setdefault(family, {'colour':'', 'choices':[]})
        colour = re.search(r'(?:标准|豪华|典藏)([蓝白黄红紫黑])', label.split('｜')[-1])
        if colour:
            group['colour'] = colour.group(1) + '色'
        group['choices'].append(f'{key} {parts[-1]}')
    return [(f"{group['colour']}（{family}）" if group['colour'] else family)
            + '：' + ' ｜ '.join(group['choices']) for family,group in groups.items()]


def matches_filter(item, word):
    word = normalize_query(word)
    name, alias = str(item.get('name') or ''), str(item.get('alias') or '')
    if word in {'标准', '豪华', '典藏'}:
        # The provider sometimes labels a deluxe item with a standard alias.
        return word in name
    if re.fullmatch('[蓝白黄红紫黑]色', word):
        word = word[0]
    return compact(word) in compact(name + alias)


def build_menu(items, query, filters=()):
    query = normalize_query(query)
    rows, seen = [], set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if not all(matches_filter(item, word) for word in filters):
            continue
        requested_editions = [word for word in ('标准', '豪华', '典藏') if word in query]
        if requested_editions and not all(word in name for word in requested_editions):
            continue
        rows.append(item)
    exact = [row for row in rows if compact(row['name']) == compact(query)]
    if len(exact) == 1:
        rows = exact
    elif not any(word in query for word in ('白发', '黑发', '头发', '衣', '披风')):
        editions = [row for row in rows if row['name'].rsplit('·', 1)[-1] in {'标准', '豪华', '典藏'}]
        if editions:
            rows = editions
    # Put a standard edition first, then keep colour pairs together. Preserve
    # upstream relevance between colours instead of depending on random IDs.
    standard = next((row for row in rows if row['name'].endswith('·标准')), None)
    if standard:
        rows = [standard, *[row for row in rows if row is not standard]]
    families = list(dict.fromkeys(row['name'].rsplit('·', 1)[0] for row in rows))
    edition_order = {'标准': 0, '豪华': 1, '典藏': 2}
    rows.sort(key=lambda row: (families.index(row['name'].rsplit('·', 1)[0]),
                              edition_order.get(row['name'].rsplit('·', 1)[-1], 3)))
    menu = {}
    for item in rows[:12]:
        name, alias = item['name'], str(item.get('alias') or '').strip()
        # A conflicting edition alias is omitted; the canonical edition wins.
        if any(word in alias and word not in name for word in ('标准', '豪华', '典藏')):
            alias = ''
        menu[str(len(menu) + 1)] = name + ('｜' + alias if alias and alias != name else '')
    return menu


@dataclass
class Choice:
    options: dict
    server: str
    expires: float
    revision: int = 0
    selected: str = '1'
    expiry_handle: object = None


class MarketChoices:
    def __init__(self, ttl=120, limit=256, clock=time.monotonic):
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self.entries = OrderedDict()

    @staticmethod
    def key(event):
        sender = str(event.get_sender_id() or '')
        return (str(event.unified_msg_origin), sender) if sender else None

    def clear(self, event):
        self._discard(self.key(event))

    def _discard(self, key):
        state = self.entries.pop(key, None)
        if state and state.expiry_handle:
            state.expiry_handle.cancel()

    def close(self):
        for key in list(self.entries):
            self._discard(key)

    def start(self, event, options, server):
        key = self.key(event)
        if key is None:
            return None
        now = self.clock()
        for old, state in list(self.entries.items()):
            if state.expires <= now:
                self._discard(old)
        self._discard(key)
        state = Choice(dict(options), server, now + self.ttl, selected=next(iter(options), ''))
        self.entries[key] = state
        try:
            state.expiry_handle = asyncio.get_running_loop().call_later(self.ttl, self._discard, key)
        except RuntimeError:
            pass
        self.entries.move_to_end(key)
        while len(self.entries) > self.limit:
            self._discard(next(iter(self.entries)))
        return state

    def active(self, event):
        state = self.entries.get(self.key(event))
        if state and state.expires <= self.clock():
            self._discard(self.key(event))
            return None
        return state

    def current(self, event, state, revision):
        return self.entries.get(self.key(event)) is state and state.revision == revision

    def numeric_reply(self, event, text):
        """Only a current requester's direct reply to the bot is a selection."""
        if not re.fullmatch(r'[0-9]{1,2}', text):
            return False
        private = getattr(event, 'is_private_chat', lambda: False)()
        addressed = getattr(event, 'is_at_or_wake_command', False) is True
        if not (private or addressed):
            return False
        state = self.active(event)
        return bool(state and state.options)


class ChoiceEvent:
    """Keep original event semantics while suppressing a superseded response."""
    def __init__(self, event, valid):
        self.event, self.valid = event, valid

    def __getattr__(self, key):
        return getattr(self.event, key)

    async def send(self, result):
        if self.valid():
            return await self.event.send(result)
