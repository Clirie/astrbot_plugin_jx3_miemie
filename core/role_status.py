"""Only explicit game-login evidence can establish an online or offline state."""


def parse_game_online(payload):
    if not isinstance(payload, dict):
        return None
    if payload.get('code', 0) not in (0, 200, '0', '200'):
        return None
    rows = payload.get('data')
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return None
    value = rows[0].get('gameLogin')
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return value == 1
    if isinstance(value, str):
        return {'true': True, 'false': False, '1': True, '0': False}.get(value.strip().lower())
    return None
