# Fixed getting movie data

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any, Optional, Union
from urllib.parse import urlparse
from vinetrimmer.utils.widevine.device import LocalDevice
import click
import requests
from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService
from langcodes import Language

class NowTVUK(BaseService):
    """
    \b
    Service code for Now TV's streaming service (https://nowtv.com)
    Only UK is currently supported

    \b
    Authorization: Cookies
    Robustness:
      Widevine:
        L1: 2160p, 1080p, DDP5.1
        L3: 720p, AAC2.0

    \b
    Tips:
        - Input should be the slug of the title, e.g.:
            /house-of-the-dragon/iYEQZ2rcf32XRKvQ5gm2Aq
            /five-nights-at-freddys-2023/A5EK6sKrAaye7uXVJ57V7
    """

    ALIASES = ["NOW", "nowtvuk"]
    #GEOFENCE = ["gb"]

    TITLE_RE = r"https?://(?:www\.)?nowtv\.com/watch/asset(?:/movies|/tv)?(?P<id>/[a-z0-9-]+/[a-zA-Z0-9]+)"

    @staticmethod
    @click.command(name="NowTVUK", short_help="https://nowtv.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return NowTVUK(ctx, **kwargs)

    def __init__(self, ctx, title, movie):
        # self.title = title
        self.parse_title(ctx, title)
        self.movie = movie
        super().__init__(ctx)
        self.cdm = ctx.obj.cdm
        self.license_api = None
        self.skyCEsidismesso01 = None
        self.userToken = None
        self.vcodec = ctx.parent.params["vcodec"]
        self.vrange= ctx.parent.params["range_"]

        self.configure()

    def configure(self):       
        self.skyCEsidismesso01 = self.session.cookies.get('skyCEsidismesso01')
        self.userToken = self.get_token()

    def get_titles(self):
        #if not self.title.startswith("/"):
        #   self.title = "/" + self.title


        res = self.session.get(
            url="https://eu.api.atom.nowtv.com/adapter-calypso/v3/query/node?slug="+self.title,
            params="represent=(items(items)%2Crecs%5Btake%3D8%5D%2Ccollections(items(items%5Btake%3D8%5D))%2Ctrailers)&features=upcoming&contentSegments=ENTERTAINMENT%2CHAYU%2CKIDS%2CMOVIES%2CNEWS%2CSHORTFORM%2CSPORTS%2CSPORTS_CORE%2CSPORTS_ESSENTIALS%2CSPORTS_EVENTS%2CSPORTS_EVENTS_EXCLUSIVE%2CSPORTS_EXTRA%2CSPORTS_EXTRA_EXCLUSIVE%2CSSN",
            headers={
                "Accept": "*",
                "X-SkyOTT-Device": "COMPUTER",
                "X-SkyOTT-Platform": "PC",
                "X-SkyOTT-Proposition": "NOWOTT",
                "X-SkyOTT-Provider": "NOWTV",
                "X-SkyOTT-Territory": self.config["client"]["territory"],
            },
        ).json()

        if self.movie:           
            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=res["attributes"]["title"],
                year=res["attributes"]["year"],
                original_lang="eng",  # TODO: Don't assume
                source=self.ALIASES[0],
                service_data=res
            )
            
        else:
            titles = [
                episode
                for season in res["relationships"]["items"]["data"]
                for episode in season["relationships"]["items"]["data"]
            ]           
            return [Title(
                id_=self.title,
                type_=Title.Types.TV,
                name=res["attributes"]["title"],
                season=episode["attributes"].get("seasonNumber", 0),
                episode=episode["attributes"].get("episodeNumber", 0),
                episode_name=episode["attributes"].get("title"),
                original_lang="eng",  # TODO: Don't assume
                source=self.ALIASES[0],
                service_data=episode
            ) for episode in titles]


    def get_tracks(self, title):
        supported_colour_spaces=["SDR"]

        if self.vrange == "HDR10":
            self.log.info("Switched dynamic range to  HDR10")
            supported_colour_spaces=["HDR10"]
        if self.vrange == "DV":
            self.log.info("Switched dynamic range to  DV")
            supported_colour_spaces=["DV"]
    
        variant_id = title.service_data["attributes"]["providerVariantId"]
        url = self.config["endpoints"]["vod"]

        headers = {
            "accept": "application/vnd.playvod.v1+json",
            "content-type": "application/vnd.playvod.v1+json",
            "x-skyott-activeterritory": self.config["client"]["territory"],
            "x-skyott-device": self.config["client"]["device"],
            "x-skyott-platform": self.config["client"]["platform"],
            "x-skyott-proposition": self.config["client"]["proposition"],
            "x-skyott-provider": self.config["client"]["provider"],
            "x-skyott-territory": self.config["client"]["territory"],
            "x-skyott-usertoken": self.get_token(),
        }

        data = {
            "device": {
                "capabilities": [
                    # H265 EAC3
                    {
                        "transport": "DASH",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "vcodec": "H265",
                        "acodec": "EAC3",
                        "container": "TS",
                    },
                    {
                        "transport": "DASH",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "vcodec": "H265",
                        "acodec": "EAC3",
                        "container": "ISOBMFF",
                    },
                    {
                        "container": "MP4",
                        "vcodec": "H265",
                        "acodec": "EAC3",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "transport": "DASH",
                    },
                    # H264 EAC3
                    {
                        "transport": "DASH",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "vcodec": "H264",
                        "acodec": "EAC3",
                        "container": "TS",
                    },
                    {
                        "transport": "DASH",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "vcodec": "H264",
                        "acodec": "EAC3",
                        "container": "ISOBMFF",
                    },
                    {
                        "container": "MP4",
                        "vcodec": "H264",
                        "acodec": "EAC3",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "transport": "DASH",
                    },
                    # H265 AAC
                    {
                        "transport": "DASH",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "vcodec": "H265",
                        "acodec": "AAC",
                        "container": "TS",
                    },
                    {
                        "transport": "DASH",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "vcodec": "H265",
                        "acodec": "AAC",
                        "container": "ISOBMFF",
                    },
                    {
                        "container": "MP4",
                        "vcodec": "H265",
                        "acodec": "AAC",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "transport": "DASH",
                    },
                    # H264 AAC
                    {
                        "transport": "DASH",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "vcodec": "H264",
                        "acodec": "AAC",
                        "container": "TS",
                    },
                    {
                        "transport": "DASH",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "vcodec": "H264",
                        "acodec": "AAC",
                        "container": "ISOBMFF",
                    },
                    {
                        "container": "MP4",
                        "vcodec": "H264",
                        "acodec": "AAC",
                        "protection": "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE",
                        "transport": "DASH",
                    },
                ],
                "model": self.config["client"]["model"],
                "maxVideoFormat": "UHD" if self.vcodec == "H265" else "HD", # "HD", "UHD"
                "hdcpEnabled": "True",
                "supportedColourSpaces": supported_colour_spaces,
            },
            "providerVariantId": variant_id,
            "parentalControlPin": "null",
        }

        data = json.dumps(data)
        headers["x-sky-signature"] = self.calculate_signature("POST", url, headers, data)

        response = self.session.post(url, headers=headers, data=data).json()
        if response.get("errorCode"):
            self.log.error(response.get("description"))
            sys.exit(1)

        manifest = response["asset"]["endpoints"][0]["url"]
        self.license_api = response['protection']['licenceAcquisitionUrl']
        locale = response["asset"].get("audioTracks", [])[0].get("locale", "en-GB")
        
        tracks = Tracks.from_mpd(
            url=manifest,
            session=self.session,
            source=self.ALIASES[0]
        )
        
        if supported_colour_spaces == ["HDR10"]:
            for track in tracks.videos:
                track.hdr10 = True if supported_colour_spaces == ["HDR10"] else False
        if supported_colour_spaces == ["DV"]:
            for track in tracks.videos:
                track.dolbyvison = True if supported_colour_spaces == ["DV"] else False

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, challenge, **_):
        return None if self.cdm.device.type == LocalDevice.Types.PLAYREADY else self.license(challenge)

    def license(self, challenge, **_):
        # TODO
        # returns b'{"errorCode":"OVP_00118","description":"OTT proposition mismatch"}'
        # Maybe needs UK proxy
        #path = "/" + self.license_api.split("://", 1)[1].split("/", 1)[1]
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            res = self.session.post(
                url=self.license_api,
                #headers={
                    #"Accept": "*/*",
                    #'Content-Type':'application/octet-stream',
                    #"X-Sky-Signature": self.calculate_signature('POST', path, {}, "")
                #},
                data=challenge,  # expects bytes
            ).content
        else:
            path = "/" + self.license_api.split("://", 1)[1].split("/", 1)[1]

            res = self.session.post(
                url=self.license_api,
                headers={
                    "Accept": "*/*",
                    'Content-Type':'application/octet-stream',
                    "X-Sky-Signature": self.calculate_signature('POST', path, {}, "")
                },
                data=challenge,  # expects bytes
            ).content
        return res

    @staticmethod
    def calculate_sky_header(headers: dict) -> str:
        text_headers = ""
        for key in sorted(headers.keys()):
            if key.lower().startswith("x-skyott"):
                text_headers += key + ": " + headers[key] + "\n"
        return hashlib.md5(text_headers.encode()).hexdigest()

    def calculate_signature(self, method: str, url: str, headers: dict, payload: str) -> str:
        to_hash = (
            "{method}\n{path}\n{response_code}\n{app_id}\n{version}\n{headers_md5}\n" "{timestamp}\n{payload_md5}\n"
        ).format(
            method=method,
            path=urlparse(url).path if url.startswith("http") else url,
            response_code="",
            app_id=self.config["client"]["client_sdk"],
            version="1.0",
            headers_md5=self.calculate_sky_header(headers),
            timestamp=int(time.time()),
            payload_md5=hashlib.md5(payload.encode()).hexdigest(),
        )

        signature_key = bytes(self.config["security"]["signature_hmac_key_v4"], "utf-8")
        hashed = hmac.new(signature_key, to_hash.encode("utf8"), hashlib.sha1).digest()
        signature_hmac = base64.b64encode(hashed).decode("utf8")

        return self.config["security"]["signature_format"].format(
            client=self.config["client"]["client_sdk"], signature=signature_hmac, timestamp=int(time.time())
        )

    def get_token(self) -> str:
        url = self.config["endpoints"]["tokens"]

        headers = {
            "accept": "application/vnd.tokens.v1+json",
            "content-type": "application/vnd.tokens.v1+json",
            "x-skyott-device": self.config["client"]["device"],
            "x-skyott-platform": self.config["client"]["platform"],
            "x-skyott-proposition": self.config["client"]["proposition"],
            "x-skyott-provider": self.config["client"]["provider"],
            "x-skyott-territory": self.config["client"]["territory"],
        }

        data = {
            "auth": {
                "authScheme": self.config["client"]["auth_scheme"],
                "authToken": self.session.cookies.get("skyCEsidismesso01"),
                "authIssuer": self.config["client"]["auth_issuer"],
                "provider": self.config["client"]["provider"],
                "providerTerritory": self.config["client"]["territory"],
                "proposition": self.config["client"]["proposition"],
            },
            "device": {
                "type": self.config["client"]["device"],
                "platform": self.config["client"]["platform"],
                "id": self.config["client"]["id"],
                "drmDeviceId": self.config["client"]["drm_device_id"],
            },
        }

        data = json.dumps(data)
        headers["Content-MD5"] = hashlib.md5(data.encode("utf-8")).hexdigest()

        response = self.session.post(url, headers=headers, data=data).json()
        if response.get("message"):
            self.log.error(f"{response['message']}")
            sys.exit(1)

        return response["userToken"]
