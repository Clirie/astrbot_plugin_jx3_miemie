"""Credential-safe diagnostics; never include raw request bodies in logs."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE = {"token", "toekn", "ticket", "authorization", "password", "passwd", "secret", "apikey", "accesskey", "credential", "cookie"}

def sensitive_key(key):
    normalized = str(key).lower().replace("_", "").replace("-", "")
    return any(part in normalized for part in _SENSITIVE)

def redact(value):
    if isinstance(value, dict):
        return {k: "***" if sensitive_key(k) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str) and value.startswith(("http://", "https://", "ws://", "wss://")):
        return safe_url(value)
    return value

def safe_url(value):
    try:
        parts = urlsplit(str(value))
        host = parts.netloc.rsplit("@", 1)[-1]
        query = urlencode([(k, "***" if sensitive_key(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, host, parts.path, query, ""))
    except ValueError:
        return "<invalid URL>"
