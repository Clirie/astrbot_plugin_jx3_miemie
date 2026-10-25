"""One reproducible daily card per platform account; no user records on disk."""
import hashlib
import hmac
import os
import random
import secrets
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
from .tarot import draw_cards, select_reading


def load_secret(directory):
    directory.mkdir(parents=True,exist_ok=True);path=directory/'daily-tarot.key'
    if not path.exists():
        fd,temporary=tempfile.mkstemp(prefix='.daily-tarot-',dir=directory)
        try:
            with os.fdopen(fd,'wb') as f:f.write(secrets.token_bytes(32))
            try:os.link(temporary,path)
            except FileExistsError:pass
        finally:os.unlink(temporary)
    secret=path.read_bytes()
    if len(secret)!=32:raise ValueError('daily tarot key must have 32 bytes')
    return secret


def daily_card(owner, secret, day=None):
    day=day or datetime.now(ZoneInfo('Asia/Shanghai')).date()
    digest=hmac.new(secret,(day.isoformat()+'\0'+owner).encode(),hashlib.sha256).digest()
    card=select_reading(draw_cards(1,random.Random(int.from_bytes(digest,'big')))[0],'综合')
    card['position']='今日牌'
    return {'card':card,'date':day.strftime('%Y.%m.%d'),'signature':digest.hex()}
