from __future__ import annotations

import hashlib
import json
import base64
import re
import sys
import warnings
from collections.abc import Generator
from typing import Any, Union
import m3u8
import click
import requests
from click import Context
from vinetrimmer.config import directories
from vinetrimmer.objects import AudioTrack, TextTrack, Title, Track, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.collections import as_list
from vinetrimmer.utils.regex import find
from vinetrimmer.utils.widevine.device import LocalDevice

class BritBox(BaseService):
    """
    \b
    Service code for the BritBox streaming service (https://www.britbox.com).
    This code is a modified BBC iPlayer code for Devine by stabbedbybrick, credit to original author

    \b
    Author: stabbedbybrick
    Author of modification: ST02
    Authorization: Login
    Security: None

    \b
    Tips:
        - Use full title URL as input for best results.
        - Use --list-titles before anything, BBlayer's listings are often messed up.
    \b
        - Use --range HLG to request H265 UHD tracks
    """

    ALIASES = ["BB"]
    #GEOFENCE = () #"ca", "us", "au", "dk", "fi", "se", "no", "za")
    TITLE_RE = r"^(?:https?://(?:www\.)?britbox\.com/)(?P<geo>\w{2})/(?P<kind>show|movie|episode)/(?P<id>[a-zA-Z0-9_()-]+)(?:/.*)?$"

    @staticmethod
    @click.command(name="BritBox", short_help="https://www.britbox.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("-mt", "--manifest-type", default="m3u8", type=str,
                  help="Temporary solution until we find a way to get PSSH from m3u8")
    @click.pass_context
    def cli(ctx, **kwargs):
        return BritBox(ctx, **kwargs)

    def __init__(self, ctx, title: str, manifest_type):
        self.title = title
        self.vcodec = ctx.parent.params.get("vcodec")
        self.range = ctx.parent.params.get("range_")
        super().__init__(ctx)
        self.manifest_type = manifest_type
        self.cdm = ctx.obj.cdm
        if self.range == "HLG":
            self.vcodec = "H265"
        self.configure()
    
    def configure(self):

        if not self.credentials:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        cache = self.get_cache(f"tokens_{self.credentials.sha1}.json")
        
            # new
        json_data = {
            "deviceName": "AOSP TV on x86",
            "email": self.credentials.username,
            "id": "d39266ce-8028-3e5c-9788-580149375320",
            "password": self.credentials.password,
            "scopes": [
                "Catalog",
                "Settings"
            ]
        }
        r = self.session.post("https://rocket.us.britbox.com/authorization?ff=ldp%2Cidp&lang=en-US&device=tv_android", 
            headers={"User-Agent": self.config["headers"]["user_agent"]}, json=json_data)
        try:
            res = r.json()
        except json.JSONDecodeError:
            raise ValueError(f"Failed to log in: {r.text}")

        if "error" in res:
            self.log.error(f"Failed to log in: {res['errorMessage']}")
            sys.exit(1)

        tokens = res[0]["value"]
        self.log.info(" + Acquired tokens...")

        exp = json.loads(base64.b64decode(tokens.split(".")[1] + "==").decode("utf-8"))["exp"]
        #cache.set(tokens, expiration=exp)

        self.bearer = tokens

    def get_titles(self):
        self.geo, kind, pid = (re.match(self.TITLE_RE, self.title).group(i) for i in ("geo", "kind", "id"))
        if not pid:
            raise ValueError("Unable to parse title ID - is the URL or id correct?")

        data = self.get_data(pid=pid, cpid=None, kind=kind)
        if kind == "episode":
            return self.get_single_episode(pid, kind)
        elif data is None:
            raise ValueError(f"Metadata was not found - if {pid} is an episode, use full URL as input")

        if kind == "movie":           
            return Title(
                id_=data["item"]["id"],
                type_=Title.Types.MOVIE,
                name=data["item"]["title"],
                year=data["item"]["releaseYear"],
                source=self.ALIASES[0],
                service_data=data
            )
            
        else:
            seasons = [self.get_data(pid=None, cpid=x["path"].split("/")[-1], kind="season") for x in data["item"]["show"]["seasons"]["items"]]
            episodes = [
                self.create_episode(episode, season) for season in seasons for episode in season["item"]["episodes"]["items"]
            ]
            return episodes

    def get_tracks(self, title):
        
        quality = [
            connection.get("height")
            for connection in self.check_all_versions(title.id)
            if connection.get("height")
        ]

        max_quality = max((h for h in quality if h <= 1080), default=None)

        media = self.check_all_versions(title.id)

        if not media:
            raise self.log.error("No media found. If you're behind a VPN/proxy, you might be blocked")

        connection = {}
        for video in [x for x in media if x["kind"] == "video"]:
            connections = sorted(video["connection"], key=lambda x: x["dpw"], reverse=True)
            if self.vcodec == "H265":
                connection = connections[0]
            else:
                connection = next(
                    x for x in connections if x["supplier"] == "mf_akamai" and x["transferFormat"] == "dash"
                )
            break

        if self.manifest_type == "m3u8":
            if not self.vcodec == "H265":
                
                if connection["transferFormat"] == "dash":
                    connection["href"] = "/".join(
                        connection["href"].replace("dash", "hls").replace(".hlsv2.ism", "").split("?")[0].split("/")[0:-1] + ["hls", "master.m3u8"]
                    )
                    connection["transferFormat"] = "hls"
                elif connection["transferFormat"] == "hls":
                    connection["href"] = "/".join(
                        connection["href"].replace(".hlsv2.ism", "").split("?")[0].split("/")[0:-1] + ["hls", "master.m3u8"]
                    )

                if connection["transferFormat"] != "hls":
                    raise ValueError(f"Unsupported video media transfer format {connection['transferFormat']!r}")
        self.log.info(f"Manifest: {connection['href']}")
        if connection["transferFormat"] == "dash":
            tracks = Tracks.from_mpd(
                url=connection["href"],
                session=self.session,
                source=self.ALIASES[0]
            )
        elif connection["transferFormat"] == "hls":
            tracks = Tracks.from_m3u8(
                m3u8.loads(self.session.get(connection["href"]).text, connection["href"]),
                source=self.ALIASES[0]
            )
        else:
            raise ValueError(f"Unsupported video media transfer format {connection['transferFormat']!r}")
        
        for video in tracks.videos:
            # UHD DASH manifest has no range information, so we add it manually
            #if video.codec == Video.Codec.HEVC:
            #    video.range = Video.Range.HLG
            video.hlg = video.codec and video.codec.startswith("hev1") and not (video.hdr10 or video.dv)

            if any(re.search(r"-audio_\w+=\d+", x) for x in as_list(video.url)):
                # create audio stream from the video stream
                audio_url = re.sub(r"-video=\d+", "", as_list(video.url)[0])
                audio = AudioTrack(
                    # use audio_url not video url, as to ignore video bitrate in ID
                    id_=hashlib.md5(audio_url.encode()).hexdigest()[0:7],
                    url=audio_url,
                    #codec=Audio.Codec.from_codecs(video.data["hls"]["playlist"].stream_info.codecs.split(",")[0]),
                    codec=video.extra.stream_info.codecs.split(",")[0],
                    #language=video.data["hls"]["playlist"].media[0].language,
                    language='en',  # TODO: Get from `#EXT-X-MEDIA` audio groups section
                    bitrate=int(self.find(r"-audio_\w+=(\d+)", as_list(video.url)[0]) or 0),
                    #channels=video.data["hls"]["playlist"].media[0].channels,
                    #channels=video.extra["hls"]["playlist"].media[0].channels,
                    descriptive=False,  # Not available
                    #descriptor=Audio.Descriptor.HLS,
                    descriptor=Track.Descriptor.M3U,
                    source=self.ALIASES[0],
                    #drm=video.drm,
                    #data=video.data,
                    extra=video.extra,
                )
                if not tracks.exists(by_id=audio.id):
                    # some video streams use the same audio, so natural dupes exist
                    tracks.add(audio)
                # remove audio from the video stream
                video.url = [re.sub(r"-audio_\w+=\d+", "", x) for x in as_list(video.url)][0]
               # video.codec = Video.Codec.from_codecs(video.data["hls"]["playlist"].stream_info.codecs)
                video.codec = video.extra.stream_info.codecs.split(",")[1]
                video.bitrate = int(self.find(r"-video=(\d+)", as_list(video.url)[0]) or 0)
        
        for caption in [x for x in media if x["kind"] == "captions"]:
            connection = caption["connection"][0]
            tracks.add(TextTrack(
                    id_=hashlib.md5(connection["href"].encode()).hexdigest()[0:6],
                    url=connection["href"],
                    source=self.ALIASES[0],
                    #codec=Subtitle.Codec.from_codecs("ttml"),
                    codec=caption["type"].split("/")[-1].replace("ttaf+xml", "ttml"),
                    language=caption["language"],#title.language,
                    #is_original_lang=True if str(title.language) in caption["language"] else False,
                    forced=False,
                    sdh=True if caption["purpose"] == "hard-of-hearing" else False,
                )
            )

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, challenge, **_):
        return None if self.cdm.device.type == LocalDevice.Types.PLAYREADY else self.license(challenge)

    def license(self, challenge, **_):
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            try:
                try:
                    res_params = requests.get(self.config["endpoints"]["licence_pr"].format(vpid=self.vpid), 
                        headers = {
                            "User-Agent": "okhttp/3.14.9",
                            "Authorization": f"britbox x={self.token}",
                            "Accept-Encoding": "gzip, deflate",
                            "Connection": "Keep-Alive"
                        }
                    ).json()
                except:
                    raise self.log.exit("BritBox didn't return JSON data")
                
                res = self.session.post(res_params["licence_server"],
                    headers={
                        'origin': 'https://www.britbox.com',
                        'referer': 'https://www.britbox.com/',
                        'x-axdrm-message': res_params["token"]
                    },
                    data=challenge
                )
                return res.content
            except requests.HTTPError as e:
                if not e.response.content:
                    raise self.log.exit(" - No license returned!")
                raise self.log.exit(f" - Unable to obtain license (error code: {e.response.json()['errorCode']})")
        else:
            try:
                try:
                    res_params = requests.get(self.config["endpoints"]["licence_wv"].format(vpid=self.vpid), 
                        headers = {
                            "User-Agent": "okhttp/3.14.9",
                            "Authorization": f"britbox x={self.token}",
                            "Accept-Encoding": "gzip, deflate",
                            "Connection": "Keep-Alive"
                        }
                    ).json()
                except:
                    raise self.log.exit("BritBox didn't return JSON data")
                
                res = self.session.post(res_params["licence_server"],
                    headers={
                        'origin': 'https://www.britbox.com',
                        'referer': 'https://www.britbox.com/',
                        'x-axdrm-message': res_params["token"]
                    },
                    data=challenge
                )
                return res.content
            except requests.HTTPError as e:
                if not e.response.text:
                    raise self.log.exit(" - No license returned!")
                raise self.log.exit(f" - Unable to obtain license (error code: {e.response.json()['errorCode']})")
        
        return None  # Unencrypted


    # service specific functions

    def get_data(self, pid: str, cpid: str, kind: str) -> dict:
        
        if cpid == None:
            params = {
                'path': f"/{kind}/{pid}",
                'useCustomId': 'true',
                'listPageSize': '100',
                'maxListPrefetch': '15',
                'itemDetailExpand': 'all',
                'textEntryFormat': 'html',
                'device': 'web_browser',
                'sub': 'Subscriber',
                'segments': self.geo,
            }

            contentid = self.session.get('https://api.britbox.com/v1/content/Page', params=params, 
            headers={"Referer": self.config["headers"]["referer"]})

            contentid.raise_for_status()

            contentid = contentid.json()["externalResponse"]["entries"][0]["item"]["id"]
        
        params = {
            'path': f"/{kind}/{pid.replace(pid.split('_')[-1], contentid) if cpid == None else cpid}",
            'list_page_size_large': '100',
            'item_detail_expand': 'all',
            'item_detail_select_season': 'first',
            'related_items_count': 'false',
            'device': 'tv_android',
            'sub': 'Subscriber',
            'segments': [self.geo.upper(), 'supportTA'],
            'ff': ['ldp', 'idp'],
            'lang': f'en-{self.geo.upper()}',
            'c': 'tv_firetv',
            'v': '1.0.0',
        }

        r = self.session.get(self.config["endpoints"]["metadata"], headers={"User-Agent": self.config["headers"]["user_agent"]}, params=params)
        r.raise_for_status()

        return r.json()

    def get_token(self, id_):
    
        headers_token = {
            "User-Agent": "okhttp/3.14.9",
            "Authorization": f"Bearer {self.bearer}"
            }
        
        params = {
            'delivery': 'stream',
            'resolution': 'HD-1080',
            'device': 'tv_android',
            'sub': 'Subscriber',
            'segments': self.geo.upper(),
            'ff': ['ldp','idp'],
            'lang': f'en-{self.geo.upper()}',
        }

        metadata = self.session.get(url=self.config["endpoints"]["token"].format(id_=id_), 
        headers={"User-Agent": self.config["headers"]["user_agent"],
                 "Authorization": f"Bearer {self.bearer}"}, params=params).json()

        return metadata[0]["token"], metadata[0]["name"]

    def check_all_versions(self, vpid: str) -> list:
        
        self.token, self.vpid = self.get_token(vpid)
        url = self.config["endpoints"]["manifest"].format(
            vpid=self.vpid,
            mediaset="iptv-uhd" if self.vcodec == "H265" else ("iptv-all-drm" if self.manifest_type == "mpd" else "iptv-all"),
        )

        session = self.session
        manifest = session.get(
            url, headers={"User-Agent": self.config["headers"]["user_agent"],
                          "Authorization": f"britbox x={self.token}"}
        ).json()

        if "result" in manifest:
            return {}
        #print(manifest)
        return manifest["media"]

    def create_episode(self, episode: dict, season: dict):
        title = episode["showTitle"]
        season_num = int(season["item"]["seasonNumber"])
        ep_num = int(episode["episodeNumber"])
        ep_name = episode["episodeName"]

        return Title(
            id_=episode["id"],
            type_=Title.Types.TV,
            name=title,
            episode_name=ep_name,
            season=season_num,
            episode=ep_num,
            source=self.ALIASES[0],
            service_data=episode
        )

    def get_single_episode(self, pid: str, kind: str) -> Series:
        
        params = {
            'path': '/{kind}}/{pid}}'.format(kind=kind, pid=pid),
            'useCustomId': 'true',
            'listPageSize': '100',
            'maxListPrefetch': '15',
            'itemDetailExpand': 'all',
            'textEntryFormat': 'html',
            'device': 'web_browser',
            'sub': 'Subscriber',
            'segments': self.geo,
        }

        contentid = self.session.get('https://api.britbox.com/v1/content/Page', params=params,
        headers={"User-Agent": self.config["user_agent_web"],
                "Referer": self.config["user_agent_web"]}).json()["externalResponse"]["entries"][0]["item"]["id"]

        params = {
            'path': f'/{kind}/{pid}',
            'list_page_size_large': '100',
            'item_detail_expand': 'all',
            'item_detail_select_season': 'first',
            'related_items_count': 'false',
            'device': 'tv_android',
            'sub': 'Subscriber',
            'segments': [self.geo.upper(), 'supportTA'],
            'ff': ['ldp', 'idp'],
            'lang': f'en-{self.geo.upper()}',
            'c': 'tv_firetv',
            'v': '1.0.0',
        }
        r = self.session.get(self.config["endpoints"]["metadata"], headers={"User-Agent": self.config["user_agent"]}, params=params)
        r.raise_for_status()

        data = json.loads(r.content)

        season = int(data["item"]["season"]["seasonNumber"])
        number = int(data["item"]["episodeNumber"])
        name = data["item"]["episodeName"]
        
        return Title(
            id_=data["item"]["id"],
            type_=Title.Types.TV,
            name=data["item"]["showTitle"],
            episode_name=name,
            season=season,
            episode=number,
            source=self.ALIASES[0],
            service_data=episode
        )

    def find(self, pattern, string, group=None):
        if group:
            m = re.search(pattern, string)
            if m:
                return m.group(group)
        else:
            return next(iter(re.findall(pattern, string)), None)
