import uuid

from vinetrimmer.vendor.pymp4.parser import Box

from vinetrimmer.utils import base64, WidevineCencHeader
from vinetrimmer.utils.widevine.cdm import Cdm
from vinetrimmer.utils.xml import load_xml

def first_or_else(iterable, default):
    return next(iter(iterable or []), None) or default


def first_or_none(iterable):
    return first_or_else(iterable, None)

def first(iterable):
    return next(iter(iterable))

def build_pssh(*, kid=None, init_data=None):
    if not (bool(kid) ^ bool(init_data)):
        raise ValueError("Exactly one of kid or init_data must be provided")

    if kid:
        init_data = b"\x12\x10" + kid
    #print(Cdm.uuid)

    return Box.parse(Box.build({
        "type": b"pssh",
        "version": 0,
        "flags": 0,
        "system_ID": uuid.UUID("{9a04f079-9840-4286-ab92-e65be0885f95}"),
        "init_data": init_data,
    }))


def generate_from_kid(kid: str):
    if not kid:
        return None

    return build_pssh(kid=uuid.UUID(kid).bytes)


def generate_from_b64(pssh: str):
    if not pssh:
        return None

    return Box.parse(base64.decode(pssh))


def convert_playready_pssh(pssh):
    if isinstance(pssh, bytes):
        xml_str = pssh
    elif isinstance(pssh, str):
        xml_str = base64.decode(pssh)
    else:
        raise TypeError("PSSH must be bytes or str")

    xml_str = xml_str.decode("utf-16-le", "ignore")
    xml_str = xml_str[xml_str.index("<"):]

    xml = load_xml(xml_str).find("DATA")  # root: WRMHEADER

    kid = (
        xml.findtext("KID")  # v4.0.0.0
        or first_or_none(xml.xpath("PROTECTINFO/KID/@VALUE"))  # v4.1.0.0
        or first_or_none(xml.xpath("PROTECTINFO/KIDS/KID/@VALUE"))  # v4.3.0.0 - can be multiple?
    )
    #print(uuid.UUID(bytes_le=base64.decode(kid)).bytes)

    init_data = WidevineCencHeader()
    init_data.key_id.append(uuid.UUID(bytes_le=base64.decode(kid)).bytes)
    init_data.algorithm = 1  # 0=Clear, 1=AES-CTR
    kid_bytes = base64.decode(kid)
    kid_uuid = uuid.UUID(bytes_le=kid_bytes)

    return build_pssh(init_data=init_data.SerializeToString()),kid_uuid.hex
