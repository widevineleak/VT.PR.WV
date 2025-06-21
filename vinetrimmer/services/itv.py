from __future__ import annotations

import os
import time
import hmac
import uuid
from datetime import datetime
from hashlib import sha1
from datetime import timedelta
from hashlib import md5
import hashlib
import json
import re
import sys
from collections.abc import Generator
from langcodes import Language
import click
from bs4 import BeautifulSoup
from vinetrimmer.objects import Title, Tracks, MenuTrack, TextTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.widevine.device import LocalDevice

class ITV(BaseService):
    """
    Service code for ITVx streaming service (https://www.itv.com/).

    \b
    Author: stabbedbybrick
    Authorization: Cookies (Optional for free content | Required for premium content)
    Robustness:
      L3: 1080p

    \b
    Tips:
        - Use complete title URL as input (pay attention to the URL format):
            SERIES: https://www.itv.com/watch/bay-of-fires/10a5270
            EPISODE: https://www.itv.com/watch/bay-of-fires/10a5270/10a5270a0001
            FILM: https://www.itv.com/watch/mad-max-beyond-thunderdome/2a7095
        - Some shows aren't listed as series, only as "Latest episodes"
            Download by SERIES URL for those titles, not by EPISODE URL

    \b
    Examples:
        - SERIES: devine dl -w s01e01 itv https://www.itv.com/watch/bay-of-fires/10a5270
        - EPISODE: devine dl itv https://www.itv.com/watch/bay-of-fires/10a5270/10a5270a0001
        - FILM: devine dl itv https://www.itv.com/watch/mad-max-beyond-thunderdome/2a7095

    \b
    Notes:
        ITV seem to detect and throttle multiple connections against the server.
        It's recommended to use requests as downloader, with few workers.

    """

    #GEOFENCE = ["gb"]
    ALIASES = ["ITV", "itvx"]

    @staticmethod
    @click.command(name="ITV", short_help="https://www.itv.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return ITV(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = title
        super().__init__(ctx)

        self.profile = ctx.parent.params.get("profile")
        if not self.profile:
            self.profile = "default"

        self.configure()
        self.cdm = ctx.obj.cdm
        self.session.headers.update(self.config["headers"])

    def configure(self):
        self.log.info(f"Logging into ITV...")
        
        self.authorization = None
        if self.credentials and not self.cookies:
            self.log.error(" - Error: This service requires cookies for authentication.")
            sys.exit(1)

        if self.cookies is not None:
            self.log.info(f" + Cookies for '{self.profile}' profile found, authenticating...")
            itv_session = next((cookie.value for cookie in self.session.cookies if cookie.name == "Itv.Session"), None)
            if not itv_session:
                self.log.error(" - Error: Session cookie not found. Cookies may be invalid.")
                sys.exit(1)

            itv_session = json.loads(itv_session)
            refresh_token = itv_session["tokens"]["content"].get("refresh_token")
            tokens_cache_path = self.get_cache("tokens_{}.json".format(self.profile))
            
            os.makedirs(os.path.dirname(tokens_cache_path), exist_ok=True)
            with open(tokens_cache_path, "w", encoding="utf-8") as fd:
                json.dump(refresh_token, fd)
            if not refresh_token:
                self.log.error(" - Error: Access tokens not found. Try refreshing your cookies.")
                sys.exit(1)

            if os.path.isfile(tokens_cache_path):
                with open(tokens_cache_path, encoding="utf-8") as fd:
                    tokens = json.load(fd)

            headers = {
                "Host": "auth.prd.user.itv.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
                "Accept": "application/vnd.user.auth.v2+json",
                "Accept-Language": "en-US,en;q=0.8",
                "Origin": "https://www.itv.com",
                "Connection": "keep-alive",
                "Referer": "https://www.itv.com/",
            }

            params = {"refresh": tokens} if tokens else {"refresh": refresh_token}

            r = self.session.get(
                url='https://auth.prd.user.itv.com/token',
                headers=headers,
                params=params,
            )
            if r.status_code != 200:
                raise ConnectionError(f"Failed to refresh tokens: {r.text}")

            tokens = r.json()
            
            with open(tokens_cache_path, "w", encoding="utf-8") as fd:
                json.dump(tokens, fd)

            self.log.info(" + Tokens refreshed and placed in cache")

            self.authorization = tokens["access_token"]



    def get_titles(self):
        data = self.get_data(self.title)
        kind = next(
            (x.get("seriesType") for x in data.get("seriesList") if x.get("seriesType") in ["SERIES", "FILM"]), None
        )

        # Some shows are not listed as "SERIES" or "FILM", only as "Latest episodes"
        if not kind and next(
            (x for x in data.get("seriesList") if x.get("seriesLabel").lower() in ("latest episodes", "other episodes")), None
        ):
            titles = data["seriesList"][0]["titles"]
            return [Title(
                        id_=episode["episodeId"],
                        type_=Title.Types.TV,
                        name=data["programme"]["title"],
                        season=episode.get("series") if isinstance(episode.get("series"), int) else 0,
                        episode=episode.get("episode") if isinstance(episode.get("episode"), int) else 0,
                        episode_name=episode["episodeTitle"],
                        source=self.ALIASES[0],
                        original_lang="en",  # TODO: language detection
                        service_data=episode,
                    )
                    for episode in titles
                ]
            # Assign episode numbers to special seasons
            counter = 1
            for episode in episodes:
                if episode.season == 0 and episode.number == 0:
                    episode.number = counter
                    counter += 1
            return Series(episodes)

        if kind == "SERIES" and data.get("episode"):
            episode = data.get("episode")
            return [Title(
                        id_=episode["episodeId"],
                        type_=Title.Types.TV,
                        name=data["programme"]["title"],
                        season=episode.get("series") if isinstance(episode.get("series"), int) else 0,
                        episode=episode.get("episode") if isinstance(episode.get("episode"), int) else 0,
                        episode_name=episode["episodeTitle"],
                        source=self.ALIASES[0],
                        original_lang="en",  # TODO: language detection
                        service_data=episode,
                    )
                ]


        elif kind == "SERIES":
            episode = data.get("episode")
            return [Title(
                        id_=episode["episodeId"],
                        type_=Title.Types.TV,
                        name=data["programme"]["title"],
                        season=episode.get("series") if isinstance(episode.get("series"), int) else 0,
                        episode=episode.get("episode") if isinstance(episode.get("episode"), int) else 0,
                        episode_name=episode["episodeTitle"],
                        source=self.ALIASES[0],
                        original_lang="en",  # TODO: language detection
                        service_data=episode,
                    )
                    for series in data["seriesList"]
                    if "Latest episodes" not in series["seriesLabel"]
                    for episode in series["titles"]
                ]


        elif kind == "FILM":
            return [Title(
                        id_=movie["episodeId"],
                        type_=Title.Types.MOVIE,
                        name=data["programme"]["title"],
                        year=movie.get("productionYear"),
                        original_lang="en",  # TODO: language detection
                        source=self.ALIASES[0],
                        service_data=movie,
                    )
                    for movies in data["seriesList"]
                    for movie in movies["titles"]
                ]


    def get_tracks(self, title):
        playlist = title.service_data.get("playlistUrl")

        headers = {
            "Accept": "application/vnd.itv.vod.playlist.v4+json",
            "Accept-Language": "en-US,en;q=0.9,da;q=0.8",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
        }
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            payload = {
                "client": {
                    "id": "lg",
                },
                "device": {
                    "deviceGroup": "ctv",
                },
                "variantAvailability": {
                    "player": "dash",
                    "featureset": [
                        "mpeg-dash",
                        "playready",
                        "outband-webvtt",
                        "hd",
                        "single-track",
                    ],
                    "platformTag": "ctv",
                    "drm": {
                        "system": "playready",
                        "maxSupported": "SL3000",
                    },
                },
            }
        else:
            payload = {
                "client": {
                    "id": "lg",
                },
                "device": {
                    "deviceGroup": "ctv",
                },
                "variantAvailability": {
                    "player": "dash",
                    "featureset": [
                        "mpeg-dash",
                        "widevine",
                        "outband-webvtt",
                        "hd",
                        "single-track",
                    ],
                    "platformTag": "ctv",
                    "drm": {
                        "system": "widevine",
                        "maxSupported": "L3",
                    },
                },
            }
            
        if self.authorization:
            payload["user"] = {"token": self.authorization}

        r = self.session.post(playlist, headers=headers, json=payload)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        r = self.session.post(playlist, headers=headers, json=payload)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        data = r.json()
        video = data["Playlist"]["Video"]
        subtitles = video.get("Subtitles")
        self.manifest = video["MediaFiles"][0].get("Href")
        self.license_url = video["MediaFiles"][0].get("KeyServiceUrl")

        tracks = Tracks.from_mpd(
            url=self.manifest,
            session=self.session,
            source=self.ALIASES[0]
        )
        tracks.videos[0].data = data

        if subtitles is not None:
            for subtitle in subtitles:
                tracks.add(
                    TextTrack(
                        id_=hashlib.md5(subtitle.get("Href", "").encode()).hexdigest()[0:6],
                        url=subtitle.get("Href", ""),
                        #codec=Subtitle.Codec.from_mime(subtitle.get("Href", "")[-3:]),
                        codec='vtt',
                        #language=title.language,
                        language='en',
                        source=self.ALIASES[0],
                        forced=False,
                    )
                )
                
        for track in tracks.audios:
            role = track.extra[1].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        return tracks
        
        
    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, **_):
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            r = self.session.post(url=self.license_url, data=challenge)
            if r.status_code != 200:
                raise ConnectionError(r.text)
            return r.content
        else:
            r = self.session.post(url=self.license_url, data=challenge)
            if r.status_code != 200:
                raise ConnectionError(r.text)
            return r.content

    # Service specific functions

    def get_data(self, url: str):
        # TODO: Find a proper endpoint for this

        r = self.session.get(url)
        if r.status_code != 200:
            raise ConnectionError(r.text)

        soup = BeautifulSoup(r.text, "html.parser")
        props = soup.select_one("#__NEXT_DATA__").text

        try:
            data = json.loads(props)
        except Exception as e:
            raise ValueError(f"Failed to parse JSON: {e}")

        return data["props"]["pageProps"]

    @staticmethod
    def _sanitize(title: str) -> str:
        title = title.lower()
        title = title.replace("&", "and")
        title = re.sub(r"[:;/()]", "", title)
        title = re.sub(r"[ ]", "-", title)
        title = re.sub(r"[\\*!?¿,'\"<>|$#`’]", "", title)
        title = re.sub(rf"[{'.'}]{{2,}}", ".", title)
        title = re.sub(rf"[{'_'}]{{2,}}", "_", title)
        title = re.sub(rf"[{'-'}]{{2,}}", "-", title)
        title = re.sub(rf"[{' '}]{{2,}}", " ", title)
        return title
