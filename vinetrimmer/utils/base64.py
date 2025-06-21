import base64


def encode(s):
    if isinstance(s, str):
        s = s.encode()
    return base64.b64encode(s).decode()


def decode(s):
    if isinstance(s, str):
        s = s.encode()
    return base64.b64decode(s + b"==")


def urlsafe_encode(s):
    if isinstance(s, str):
        s = s.encode()
    return base64.urlsafe_b64encode(s).decode().rstrip("=")


def urlsafe_decode(s):
    if isinstance(s, str):
        s = s.encode()
    return base64.urlsafe_b64decode(s + b"==")
