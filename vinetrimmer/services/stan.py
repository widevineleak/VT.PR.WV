import base64
import json
import sys
import time
import urllib.parse
from hashlib import md5
from uuid import UUID

import click
import requests
from bs4 import BeautifulSoup
from Cryptodome.Hash import HMAC, SHA256

from vinetrimmer.objects import AudioTrack, TextTrack, Title, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils import Cdm, try_get
from vinetrimmer.utils.collections import as_list
from vinetrimmer.vendor.pymp4.parser import Box
from vinetrimmer.utils.widevine.device import LocalDevice

class Stan(BaseService):
    """
    Service code for Nine Digital's Stan. streaming service (https://stan.com.au).

    \b
    Authorization: Cookies
    Security: UHD@L1, SD-FDH@L3 doesn't care about releases.
    """

    ALIASES = ["STAN"]
    #GEOFENCE = ["au"]
    TITLE_RE = [
        r"^(?:https?://play\.stan\.com\.au/programs/)?(?P<id>\d+)",
        r"^(?:https?://(?:www\.)?stan\.com\.au/watch/)?(?P<id>[a-z0-9-]+)",
    ]

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "AC3": "ac-3",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="Stan", short_help="https://stan.com.au")
    @click.argument("title", type=str, required=False)
    @click.option("-t", "--device-type", default="tv", type=click.Choice(["tv", "web"]),
                  help="Device type.")
    @click.option("-q", "--vquality", default="uhd", type=click.Choice(["uhd", "hd"]),
                  help="Quality to request from the manifest, combine --vquality uhd with --device-type tv for UHD L1.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Stan(ctx, **kwargs)

    def __init__(self, ctx, title, device_type, vquality):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.device_type = device_type
        self.vquality = vquality
        self.cdm = ctx.obj.cdm
        self.vcodec = ctx.parent.params["vcodec"].lower()
        self.acodec = ctx.parent.params["acodec"]
        self.range = ctx.parent.params["range_"]

        self.api_config = {}
        self.login_data: dict[str, str] = {}
        self.license_api = None
        self.license_cd = None

        self.configure()

    def get_titles(self):
        if not self.title.isnumeric():
            r = self.session.get(self.config["endpoints"]["watch"].format(title_id=self.title))
            soup = BeautifulSoup(r.text, "lxml-html")
            data = json.loads(soup.select_one("script[type='application/ld+json']").text)
            self.title = data["@id"]

        r = self.session.get(f"{self.api_config['cat']['v12']}/programs/{self.title}.json")
        try:
            res = r.json()
        except json.JSONDecodeError:
            raise self.log.exit(f" - Failed to load title manifest: {r.text}")
        if "audioTracks" in res:
            res["original_language"] = [x["language"]["iso"] for x in res["audioTracks"] if x["type"] == "main"]
            if len(res["original_language"]) > 0:
                res["original_language"] = res["original_language"][0]

        original_language = res["original_language"]
        if not original_language:
            original_language = [x for x in res["audioTracks"] if x["type"] == "main"]
            if original_language:
                original_language = original_language[0]["language"]["iso"]
            else:
                original_language = res["languages"][0]

        if not res.get("seasons"):
            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=res["title"],
                year=res.get("releaseYear"),
                original_lang=original_language,
                source=self.ALIASES[0],
                service_data=res,
            )
        else:
            titles = []
            for season in res["seasons"]:
                r = self.session.get(season["url"])
                try:
                    season_res = r.json()
                except json.JSONDecodeError:
                    raise self.log.exit(f" - Failed to load season manifest: {r.text}")
                for episode in season_res["entries"]:
                    episode["title_year"] = res["releaseYear"]
                    episode["original_language"] = res.get("original_language")
                    titles.append(episode)
            return [Title(
                id_=x["id"],
                type_=Title.Types.TV,
                name=res["title"],
                year=x.get("title_year", x.get("releaseYear")),
                season=x.get("tvSeasonNumber"),
                episode=x.get("tvSeasonEpisodeNumber"),
                episode_name=x.get("title"),
                original_lang=original_language,
                source=self.ALIASES[0],
                service_data=x
            ) for x in titles]

    def get_tracks(self, title: Title) -> Tracks:
        program_data = self.session.get(
            f"{self.api_config['cat']['v12']}/programs/{title.service_data['id']}.json"
        ).json()

        res = self.session.get(
            url=program_data["streams"][self.vquality]["dash"]["auto"]["url"],
            params={
                "jwToken": self.login_data['jwToken'],
                "format": "json",
                "capabilities.drm":  "playready" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "widevine",
                "videoCodec": self.vcodec
            }
        )
        try:
            stream_data = res.json()
        except json.JSONDecodeError:
            raise ValueError(f"Failed to load stream data: {res.text}")
        if "media" not in stream_data:
            raise ValueError(f"Failed to load stream data: {stream_data}")
        stream_data = stream_data["media"]

        if self.vquality == "uhd":
            try:
                self.license_api = stream_data["fallbackDrm"]["licenseServerUrl"]
            except:
                self.license_api = stream_data["drm"]["licenseServerUrl"]
                self.license_cd = stream_data["drm"]["customData"]
        else:
            self.license_api = stream_data["drm"]["licenseServerUrl"]
            self.license_cd = stream_data["drm"]["customData"]

        tracks = Tracks.from_mpd(
            data=self.session.get(
                url=self.config["endpoints"]["manifest"],
                params={
                    "url": stream_data["videoUrl"],
                    "audioType": "all"
                }
            ).text,
            url=self.config["endpoints"]["manifest"],
            source=self.ALIASES[0]
        )
        if self.acodec:
            tracks.audios = [
                x for x in tracks.audios
                if x.codec[:4] == self.AUDIO_CODEC_MAP[self.acodec]
            ]
        if "captions" in stream_data:
            for sub in stream_data["captions"]:
                tracks.add(TextTrack(
                    id_=md5(sub["url"].encode()).hexdigest()[0:6],
                    source=self.ALIASES[0],
                    url=sub["url"],
                    # metadata
                    codec=sub["type"].split("/")[-1],
                    language=sub["language"],
                    cc="(cc)" in sub["name"].lower()
                ))

        # craft pssh with the key_id
        # TODO: is doing this still necessary? since the code now tries grabbing PSSH from
        #       the first chunk of data of the track, it might be available from that.
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            for track in tracks:
                track.needs_proxy = True
                if isinstance(track, VideoTrack):
                    track.hdr10 = self.range == "HDR10"
                if isinstance(track, (VideoTrack, AudioTrack)):
                    track.encrypted = True
            video_pssh = next((x.pr_pssh for x in tracks.videos if x.pr_pssh), None)

            for track in tracks.audios:
                if not track.pr_pssh:
                    track.pr_pssh = video_pssh
                    
        if not self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            pssh = Box.parse(Box.build(dict(
                type=b"pssh",
                version=0,
                flags=0,
                system_ID=Cdm.uuid,
                # \x12\x10 is decimal ascii representation of \f\n (\r\n)
                init_data=b"\x12\x10" + UUID(stream_data["drm"]["keyId"]).bytes
            )))

            for track in tracks:
                track.needs_proxy = True
                if isinstance(track, VideoTrack):
                    track.hdr10 = self.range == "HDR10"
                if isinstance(track, (VideoTrack, AudioTrack)):
                    track.encrypted = True
                    if not track.pssh:
                        track.pssh = pssh

        return tracks

    def get_chapters(self, title: Title):
        return []

    def certificate(self, **kwargs):
        # TODO: Hardcode the certificate
        return self.license(**kwargs)

    def license(self, challenge: bytes, **_):
        assert self.license_api is not None
        try:
            lic = self.session.post(
                url=self.license_api,
                headers={} if self.device_type == "tv" else {
                    "dt-custom-data": self.license_cd
                },
                data=challenge  # expects bytes
            )
            # print(f"lic 1: {lic}")
        except:
            lic = self.session.post(
                url=self.license_api,
                headers={
                    "dt-custom-data": self.license_cd
                },
                data=challenge  # expects bytes
            )
            # print(f"lic 2: {lic.json()}")
        try:
            if "license" in lic.json():
                return lic.json()["license"]  # base64 str?
        except json.JSONDecodeError:
            return lic.content  # bytes

        raise ValueError(f"Failed to obtain license: {lic.text}")

    # Service specific functions

    def configure(self) -> None:
        self.log.info("Retrieving API configuration...")
        self.api_config = self.get_config()
        
        self.log.info("Logging in...")
        self.login_data = self.check_cookie()

        if self.device_type == "tv":
            self.login_data = self.get_tv_token(self.login_data)

    def get_config(self) -> dict:
        res = self.session.get(
            self.config["endpoints"]["config"].format(type='web/app' if self.device_type == 'web' else 'tv/android'))
        try:
            return res.json()
        except json.JSONDecodeError:
            raise ValueError(f"Failed to obtain Stan API configuration: {res.text}")

    def check_cookie(self) -> dict:
        res = self.session.post(
            url=self.api_config["login"]["v1"] + self.config["endpoints"]["login"].format(type="web/app"),
            data={
                "jwToken": self.session.cookies['streamco_token'],
                "profileId": self.session.cookies['streamco_profileid'],
                "source": "play"
            }
        )
        try:
            data = res.json()
        except json.JSONDecodeError:
            self.log.exit(f"Failed to log in: {res.text}")
            raise
        if "errors" in data:
            self.log.exit(f"An error occurred while logging in: {data['errors']}")
            raise
        return data

    def get_tv_token(self, login_data) -> dict:
        res = self.session.post(
            url=self.api_config["login"]["v1"] + self.config["endpoints"]["login"].format(type="web/app"),
            data={
                "jwToken": login_data['jwToken'],
                "profileId": login_data['userId'],
                "manufacturer": "NVIDIA",
                "os": "Android-9",
                "model": "SHIELD Android TV",
                "stanName": "Stan-AndroidTV",
                "stanVersion": "4.10.1",
                "type": "console",
                "videoCodecs": "h264,decode,h263,h265,hevc,mjpeg,mpeg2v,mp4,mpeg4,vc1,vp8,vp9",
                "audioCodecs": "omx.dolby.ac3.decoder,omx.dolby.eac3.decoder,aac",
                "drm": "playready" if self.cdm.device.type == LocalDevice.Types.PLAYREADY  else "widevine",
                "captions": "ttml",
                "screenSize": "3840x2160",
                "hdcpVersion": "2.2",
                "colorSpace": "hdr10,hdr,sdr",
                "features": "hevc"
            },
            headers={
                "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SHIELD Android TV Build/PPR1.180610.011)"
            }
        )
        try:
            data = res.json()
        except json.JSONDecodeError:
            raise ValueError(f"Failed to create token: {res.text}")
        if "errors" in data:
            raise ValueError(f"An error occurred while creating token: {data['errors']}")
        return data