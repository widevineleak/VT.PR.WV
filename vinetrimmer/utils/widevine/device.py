import base64
import json
import os
import random
import struct
import time
from abc import ABC, abstractmethod
from enum import Enum

import requests
import validators
from construct import BitStruct, Bytes, Const, Container
from construct import Enum as CEnum
from construct import Flag, If, Int8ub, Int16ub, Optional, Padded, Padding, Struct, this
from Cryptodome.Cipher import AES, PKCS1_OAEP
from Cryptodome.Hash import CMAC, HMAC, SHA1, SHA256
from Cryptodome.PublicKey import RSA
from Cryptodome.Random import get_random_bytes
from Cryptodome.Signature import pss
from Cryptodome.Util import Padding as CPadding
from google.protobuf.message import DecodeError

from vinetrimmer.utils.widevine.key import Key
from vinetrimmer.utils.widevine.protos import widevine_pb2 as widevine
from vinetrimmer.vendor.pymp4.parser import Box

try:
    import cdmapi
    cdmapi_supported = True
except ImportError:
    cdmapi_supported = False


class BaseDevice(ABC):
    class Types(Enum):
        CHROME = 1
        ANDROID = 2
        PLAYREADY = 3

    def __repr__(self):
        return "{name}({items})".format(
            name=self.__class__.__name__,
            items=", ".join([f"{k}={repr(v)}" for k, v in self.__dict__.items()])
        )

    def get_name(self):
        return self.__class__.__name__

    @abstractmethod
    def set_service_certificate(self, session, certificate):
        """
        Applies a service certificate to the device.
        This would be used for devices that wish to use Privacy Mode.
        It's akin to SSL/TLS in that it adds another layer of protection on the data itself from MiTM attacks.
        Chrome device_type keys beyond 906 require a Verified Media Path (VMP), which in turn requires a service
        certificate to be set (Privacy Mode).
        """

    @abstractmethod
    def get_license_challenge(self, session):
        """
        Get a license challenge (SignedLicenseRequest) to send to a service API.

        Returns:
            Base64-encoded SignedLicenseRequest (as bytes).
        """

    @abstractmethod
    def parse_license(self, session, license_res):
        """Parse license response data, derive keys."""


class LocalDevice(BaseDevice):
    WidevineDeviceStruct = Struct(
        "signature" / Const(b"WVD"),
        "version" / Int8ub,
        "type" / CEnum(
            Int8ub,
            **{t.name: t.value for t in BaseDevice.Types}
        ),
        "security_level" / Int8ub,
        "flags" / Padded(1, Optional(BitStruct(
            Padding(7),
            "send_key_control_nonce" / Flag
        ))),
        "private_key_len" / Int16ub,
        "private_key" / Bytes(this.private_key_len),
        "client_id_len" / Int16ub,
        "client_id" / Bytes(this.client_id_len),
        "vmp_len" / Optional(Int16ub),
        "vmp" / If(this.vmp_len, Optional(Bytes(this.vmp_len)))
    )
    WidevineDeviceStructVersion = 1  # latest version supported

    def __init__(self, *_, type, security_level, flags, private_key, client_id, vmp=None, **__):
        """
        This is the device key data that is needed for the CDM (Content Decryption Module).

        Parameters:
            type: Device Type
            security_level: Security level from 1 (highest ranking) to 3 (lowest ranking)
            flags: Extra flags
            private_key: Device Private Key
            client_id: Device Client Identification Blob
            vmp: Verified Media Path (VMP) File Hashes Blob

        Flags:
            send_key_control_nonce: Setting this to `true` will set a random int between 1 and 2^31 under
                `KeyControlNonce` on the License Request Challenge.
        """
        # *_,*__ is to ignore unwanted args, like signature and version from the struct.
        # `type` param is shadowing a built-in (not great) but required to match with the struct
        self.type = self.Types[type] if isinstance(type, str) else type
        self.security_level = security_level
        self.flags = flags
        self.api_vault=False
        self.private_key = RSA.importKey(private_key) if private_key else None
        self.client_id = widevine.ClientIdentification()
        try:
            self.client_id.ParseFromString(client_id)
        except DecodeError:
            raise ValueError("client_id could not be parsed as a ClientIdentification")
        self.vmp = widevine.FileHashes()
        if vmp:
            try:
                self.vmp.ParseFromString(vmp)
            except DecodeError:
                raise ValueError("Verified Media Path (VMP) could not be parsed as FileHashes")
            # noinspection PyProtectedMember
            self.client_id._FileHashes.CopyFrom(self.vmp)

        self.sessions = {}

        # shorthands
        self.system_id = None
        if self.client_id:
            # noinspection PyProtectedMember
            self.system_id = self.client_id.Token._DeviceCertificate.SystemId

    @classmethod
    def load(cls, uri, session=None):
        if isinstance(uri, bytes):
            # direct data
            return cls(**cls.WidevineDeviceStruct.parse(uri))
        elif validators.url(uri):
            # remote url
            return cls(**cls.WidevineDeviceStruct.parse((session or requests).get(uri).content))
        else:
            # local file
            with open(uri, "rb") as fd:
                return cls(**cls.WidevineDeviceStruct.parse_stream(fd))

    @classmethod
    def from_dir(cls, d):
        with open(os.path.join(d, "wv.json")) as fd:
            config = json.load(fd)

        try:
            with open(os.path.join(d, "device_private_key"), "rb") as fd:
                private_key = fd.read()
        except FileNotFoundError:
            private_key = None

        with open(os.path.join(d, "device_client_id_blob"), "rb") as fd:
            client_id = fd.read()

        try:
            with open(os.path.join(d, "device_vmp_blob"), "rb") as fd:
                vmp = fd.read()
        except FileNotFoundError:
            vmp = None

        return cls(
            type=getattr(cls.Types, config["session_id_type"].upper()),
            security_level=config["security_level"],
            flags={
                "send_key_control_nonce": config.get("send_key_control_nonce", config["session_id_type"] == "android"),
            },
            private_key=private_key,
            client_id=client_id,
            vmp=vmp,
        )

    def dumpb(self):
        private_key = self.private_key.export_key("DER") if self.private_key else None
        return self.WidevineDeviceStruct.build(dict(
            version=self.WidevineDeviceStructVersion,
            type=self.type.value,
            security_level=self.security_level,
            flags=self.flags,
            private_key_len=len(private_key) if private_key else 0,
            private_key=private_key,
            client_id_len=len(self.client_id.SerializeToString()) if self.client_id else 0,
            client_id=self.client_id.SerializeToString() if self.client_id else None,
            vmp_len=len(self.vmp.SerializeToString()) if self.vmp else 0,
            vmp=self.vmp.SerializeToString() if self.vmp else None
        ))

    def dump(self, path):
        with open(path, "wb") as fd:
            fd.write(self.dumpb())

    def set_service_certificate(self, session, certificate):
        if isinstance(certificate, str):
            certificate = base64.b64decode(certificate)  # assuming base64

        signed_message = widevine.SignedMessage()
        try:
            signed_message.ParseFromString(certificate)
        except DecodeError:
            raise ValueError("Certificate could not be parsed as a SignedMessage")

        signed_device_certificate = widevine.SignedDeviceCertificate()
        try:
            signed_device_certificate.ParseFromString(signed_message.Msg)
        except DecodeError:
            raise ValueError("Certificate's message could not be parsed as a SignedDeviceCertificate")

        session.signed_device_certificate = signed_device_certificate
        session.privacy_mode = True

        return True

    def get_license_challenge(self, session, service_name):
        if not self.client_id:
            raise ValueError("No client identification blob is available for this device.")
        if not self.private_key and not cdmapi_supported:
            raise ValueError("No device private key is available for this device and cdmapi is not installed.")

        license_request = None

        if session.raw:
            # raw pssh will be treated as bytes and not parsed
            license_request = widevine.SignedLicenseRequestRaw()
            license_request.Type = widevine.SignedLicenseRequestRaw.MessageType.Value("LICENSE_REQUEST")
            license_request.Msg.ContentId.CencId.Pssh = session.cenc_header  # bytes, init_data
        else:
            license_request = widevine.SignedLicenseRequest()
            license_request.Type = widevine.SignedLicenseRequest.MessageType.Value("LICENSE_REQUEST")
            license_request.Msg.ContentId.CencId.Pssh.CopyFrom(session.cenc_header)  # init_data

        license_type = "OFFLINE" if session.offline else "DEFAULT"
        license_request.Msg.ContentId.CencId.LicenseType = widevine.LicenseType.Value(license_type)
        license_request.Msg.ContentId.CencId.RequestId = session.session_id
        license_request.Msg.Type = widevine.LicenseRequest.RequestType.Value("NEW")
        license_request.Msg.RequestTime = int(time.time())
        license_request.Msg.ProtocolVersion = widevine.ProtocolVersion.Value("VERSION_2_1")

        if self.flags and self.flags.get("send_key_control_nonce"):
            license_request.Msg.KeyControlNonce = random.randrange(1, 2 ** 31)

        if session.privacy_mode:
            cid_aes_key = get_random_bytes(16)
            cid_iv = get_random_bytes(16)

            enc_client_id = widevine.EncryptedClientIdentification()
            if not session.signed_device_certificate:
                raise ValueError("Missing signed_device_certificate")
            enc_client_id.ServiceId = session.signed_device_certificate._DeviceCertificate.ServiceId.decode()
            enc_client_id.ServiceCertificateSerialNumber = (
                session.signed_device_certificate._DeviceCertificate.SerialNumber
            )
            enc_client_id.EncryptedClientId = AES.new(cid_aes_key, AES.MODE_CBC, cid_iv).encrypt(
                CPadding.pad(self.client_id.SerializeToString(), 16)
            )

            enc_client_id.EncryptedClientIdIv = cid_iv
            enc_client_id.EncryptedPrivacyKey = PKCS1_OAEP.new(
                RSA.importKey(session.signed_device_certificate._DeviceCertificate.PublicKey)
            ).encrypt(cid_aes_key)

            license_request.Msg.EncryptedClientId.CopyFrom(enc_client_id)
        else:
            license_request.Msg.ClientId.CopyFrom(self.client_id)

        if cdmapi_supported and not self.private_key:
            data = SHA1.new(license_request.Msg.SerializeToString())
            em = (pss._EMSA_PSS_ENCODE(data, 2047, get_random_bytes, lambda x, y: pss.MGF1(x, y, data), 20)).hex()
            sig = cdmapi.encrypt(em)
            license_request.Signature = bytes.fromhex(sig)
        else:
            license_request.Signature = pss.new(self.private_key).sign(
                SHA1.new(license_request.Msg.SerializeToString())
            )

        session.license_request = license_request

        return session.license_request.SerializeToString()

    def parse_license(self, session, license_res):
        if not session.license_request:
            raise ValueError("No license request for the session was created. Create one first.")

        if isinstance(license_res, str):
            license_res = base64.b64decode(license_res)

        signed_license = widevine.SignedLicense()
        try:
            signed_license.ParseFromString(license_res)
        except DecodeError:
            raise ValueError(f"Failed to parse license_res {license_res!r} as SignedLicense")
        session.signed_license = signed_license

        def get_auth_keys(*i, k, b):
            if len(i) > 1:
                return b"".join([get_auth_keys(x, k=k, b=b) for x in i])
            c = CMAC.new(k, ciphermod=AES)
            c.update(struct.pack("B", i[0]) + b)
            return c.digest()

        license_req_msg = session.license_request.Msg.SerializeToString()
        enc_key_base = b"ENCRYPTION\000%b\0\0\0\x80" % license_req_msg
        auth_key_base = b"AUTHENTICATION\0%b\0\0\2\0" % license_req_msg

        if cdmapi_supported and not self.private_key:
            session.session_key = bytes.fromhex(cdmapi.decrypt(session.signed_license.SessionKey.hex()))
        else:
            session.session_key = PKCS1_OAEP.new(self.private_key).decrypt(session.signed_license.SessionKey)
        session.derived_keys["enc"] = get_auth_keys(1, k=session.session_key, b=enc_key_base)
        session.derived_keys["auth_1"] = get_auth_keys(1, 2, k=session.session_key, b=auth_key_base)
        session.derived_keys["auth_2"] = get_auth_keys(3, 4, k=session.session_key, b=auth_key_base)

        lic_hmac = HMAC.new(session.derived_keys["auth_1"], digestmod=SHA256)
        lic_hmac.update(session.signed_license.Msg.SerializeToString())
        if lic_hmac.digest() != session.signed_license.Signature:
            raise ValueError("SignedLicense Signature doesn't match its Message")

        for key in session.signed_license.Msg.Key:
            key_type = widevine.License.KeyContainer.KeyType.Name(key.Type)
            permissions = []
            if key_type == "OPERATOR_SESSION":
                for (descriptor, value) in key._OperatorSessionKeyPermissions.ListFields():
                    if value == 1:
                        permissions.append(descriptor.name)
            session.keys.append(Key(
                kid=key.Id if key.Id else key_type.encode("utf-8"),
                key_type=key_type,
                key=CPadding.unpad(AES.new(session.derived_keys["enc"], AES.MODE_CBC, iv=key.Iv).decrypt(key.Key), 16),
                permissions=permissions
            ))

        return True


class RemoteDevice(BaseDevice):
    def __init__(self, *_, type, system_id, security_level, name, host, api_vault=False, key, device=None, **__):
        self.type = self.Types[type] if isinstance(type, str) else type
        self.system_id = system_id
        self.security_level = security_level
        self.name = name
        self.host = host
        self.api_vault=api_vault                                
        self.key = key
        self.device = device

        self.sessions = {}

        self.api_session_id = None

    def set_service_certificate(self, session, certificate):
        if isinstance(certificate, bytes):
            certificate = base64.b64encode(certificate).decode()

        # certificate needs to be base64 to be sent off to the API.
        # it needs to intentionally be kept as base64 encoded SignedMessage.

        session.signed_device_certificate = certificate
        session.privacy_mode = True

        return True

    def get_license_challenge(self, session, service_name):
        pssh = session.pssh
        if isinstance(pssh, Container):
            pssh = Box.build(pssh)
        if isinstance(pssh, bytes):
            pssh = base64.b64encode(pssh).decode()

        res = self.session("GetChallenge", {
            "init": pssh,
            "cert": session.signed_device_certificate,
            "raw": session.raw,
            "licensetype": "OFFLINE" if session.offline else "STREAMING",
            "device": self.device,
            "service": service_name
        })

        self.api_session_id = res["session_id"]

        return base64.b64decode(res["challenge"])

    def parse_license(self, session, license_res):
        if isinstance(license_res, bytes):
            license_res = base64.b64encode(license_res).decode()

        res = self.session("GetKeys", {
            "cdmkeyresponse": license_res,
            "session_id": self.api_session_id
        })

        session.keys.extend([Key(
            kid=bytes.fromhex(x["kid"]),
            key_type=x.get("type", "CONTENT"),
            key=bytes.fromhex(x["key"])
        ) for x in res["keys"]])

        return True

    def exchange(self, session, license_res, enc_key_id, hmac_key_id):
        if isinstance(license_res, bytes):
            license_res = base64.b64encode(license_res).decode()
        if isinstance(enc_key_id, bytes):
            enc_key_id = base64.b64encode(enc_key_id).decode()
        if isinstance(hmac_key_id, bytes):
            hmac_key_id = base64.b64encode(hmac_key_id).decode()
        res = self.session("GetKeysX", {
            "cdmkeyresponse": license_res,
            "encryptionkeyid": enc_key_id,
            "hmackeyid": hmac_key_id,
            "session_id": self.api_session_id
        })
        return base64.b64decode(res["encryption_key"]), base64.b64decode(res["sign_key"])

    def session(self, method, params=None):
        res = requests.post(
            self.host,
            json={
                "method": method,
                "params": params,
                "token": self.key
            }
        ).json()

        if res.get("status_code") != 200:
            raise ValueError(f"CDM API returned an error: {res['status_code']} - {res['message']}")

        return res["message"]



class TPDRemoteDevice(BaseDevice):
    def __init__(self, *_, type, system_id, security_level, name, host, key, device=None, **__):
        self.type = self.Types[type] if isinstance(type, str) else type
        self.system_id = system_id
        self.security_level = security_level
        self.name = name
        self.host = host
        self.key = key
        self.device = device

        self.sessions = {}

        self.api_session_id = None

    def set_service_certificate(self, session, certificate):
        if isinstance(certificate, bytes):
            certificate = base64.b64encode(certificate).decode()

        # certificate needs to be base64 to be sent off to the API.
        # it needs to intentionally be kept as base64 encoded SignedMessage.

        session.signed_device_certificate = certificate
        session.privacy_mode = True

        return True

    def get_license_challenge(self, session):
        pssh = session.pssh

        req = requests.post(self.host + "/get_challenge",
            headers={
                'Content-Type': 'application/json', 'X-Api-Key': self.key
            },
            json={
                "init_data": pssh
            },
        )
        if not req.ok:
            raise ValueError(req.text)

        res = req.json()

        return base64.b64decode(res["data"])

    def parse_license(self, session, license_res):
        if isinstance(license_res, bytes):
            license_res = base64.b64encode(license_res).decode()

        req = requests.post(self.host + "/get_keys",
            headers={
                'Content-Type': 'application/json', 'X-Api-Key': self.key
            },
            json={
                "lic_resp": license_res
            }
        )
        if not req.ok:
            raise ValueError(req.text)

        res = req.json()

        session.keys.extend([Key(
            kid=bytes.fromhex(x["key_id"]),
            key_type=x.get("type", "CONTENT"),
            key=bytes.fromhex(x["key"])
        ) for x in res])

        return True

    def exchange(self, session, license_res, enc_key_id, hmac_key_id):
        if isinstance(license_res, bytes):
            license_res = base64.b64encode(license_res).decode()
        if isinstance(enc_key_id, bytes):
            enc_key_id = base64.b64encode(enc_key_id).decode()
        if isinstance(hmac_key_id, bytes):
            hmac_key_id = base64.b64encode(hmac_key_id).decode()
        res = self.session("GetKeysX", {
            "cdmkeyresponse": license_res,
            "encryptionkeyid": enc_key_id,
            "hmackeyid": hmac_key_id,
            "session_id": self.api_session_id
        })
        return base64.b64decode(res["encryption_key"]), base64.b64decode(res["sign_key"])


class CRDRemoteDevice(BaseDevice):
    def __init__(self, *_, type, system_id, security_level, name, host, key, device=None, **__):
        self.type = self.Types[type] if isinstance(type, str) else type
        self.system_id = system_id
        self.security_level = security_level
        self.name = name
        self.host = host
        self.key = key
        self.device = device

        self.sessions = {}

        self.api_session_id = None

    def set_service_certificate(self, session, certificate):
        if isinstance(certificate, bytes):
            certificate = base64.b64encode(certificate).decode()

        # certificate needs to be base64 to be sent off to the API.
        # it needs to intentionally be kept as base64 encoded SignedMessage.

        session.signed_device_certificate = certificate
        session.privacy_mode = True

        return True

    def get_license_challenge(self, session):
        pssh = session.pssh

        req = requests.post(self.host + "/playready3000/get_challenge",
            headers={
                'Content-Type': 'application/json', 'X-Api-Key': self.key
            },
            json={
                "pssh": pssh
            },
        )
        if not req.ok:
            raise ValueError(req.text)

        res = req.json()
        return base64.b64decode(res["challenge"])

    def parse_license(self, session, license_res):
        if isinstance(license_res, bytes):
            license_res = base64.b64encode(license_res).decode()

        req = requests.post(self.host + "/playready3000/get_keys",
            headers={
                'Content-Type': 'application/json', 'X-Api-Key': self.key
            },
            json={
                "response": license_res
            }
        )
        if not req.ok:
            raise ValueError(req.text)

        res = req.json()
        session.keys.extend([Key(
            kid=bytes.fromhex(x["kid"]),
            key_type=x.get("type", "CONTENT"),
            key=bytes.fromhex(x["key"])
        ) for x in res])

        return True

    def exchange(self, session, license_res, enc_kid, hmac_kid):
        if isinstance(license_res, bytes):
            license_res = base64.b64encode(license_res).decode()
        if isinstance(enc_kid, bytes):
            enc_kid = base64.b64encode(enc_kid).decode()
        if isinstance(hmac_kid, bytes):
            hmac_kid = base64.b64encode(hmac_kid).decode()
        res = self.session("GetKeysX", {
            "cdmkeyresponse": license_res,
            "encryptionkeyid": enc_kid,
            "hmackeyid": hmac_key_id,
            "session_id": self.api_session_id
        })
        return base64.b64decode(res["encryption_key"]), base64.b64decode(res["sign_key"])