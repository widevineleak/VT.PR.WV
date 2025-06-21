import json
import re
import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from typing import Any, Optional
from urllib.parse import unquote, urlparse
import uuid
import click
import requests
from vinetrimmer.objects import Title, Tracks, MenuTrack, TextTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.widevine.device import LocalDevice

class ROKU(BaseService):
    """
    Service code for The Roku Channel (https://therokuchannel.roku.com)

    \b
    Version: 1.0.1
    Author: stabbedbybrick
    Authorization: Cookies
    Robustness:
      Widevine:
        L3: 1080p, DD5.1

    \b
    Tips:
        - Use complete title/episode URL or id as input:
            https://therokuchannel.roku.com/details/e05fc677ab9c5d5e8332f123770697b9/paddington
            OR
            e05fc677ab9c5d5e8332f123770697b9
        - Supports movies, series, and single episodes
        - Search is geofenced
    """

    #GEOFENCE = ("us",)
    ALIASES = ["ROKU", "Roku"]
    TITLE_RE = r"^(?:https?://(?:www.)?therokuchannel.roku.com/(?:details|watch)/)?(?P<id>[a-z0-9-]+)"

    @staticmethod
    @click.command(name="ROKU", short_help="https://therokuchannel.roku.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return ROKU(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = re.match(self.TITLE_RE, title).group("id")
        super().__init__(ctx)

        self.licenseurl = None
        self.cdm = ctx.obj.cdm
        self.configure()


    def get_titles(self):
        data = self.session.get(self.config["endpoints"]["content"] + self.title).json()
        if not data["isAvailable"]:
            self.log.error("This title is temporarily unavailable or expired")
            sys.exit(1)

        if data["type"] in ["movie", "tvspecial", "shortformvideo"]:
            return Title(
                        id_=data["meta"]["id"],
                        type_=Title.Types.MOVIE,
                        name=data["title"],
                        year=data["releaseYear"],
                        original_lang=data["viewOptions"][0]["media"].get("originalAudioLanguage", "en"),
                        source=self.ALIASES[0],
                        service_data=data,
                    )


        elif data["type"] == "series":
            episodes = self.fetch_episodes(data)
            return [
                    Title(
                        id_=episode["meta"]["id"],
                        type_=Title.Types.TV,
                        name=data["title"],
                        season=int(episode["seasonNumber"]),
                        episode=int(episode["episodeNumber"]),
                        episode_name=episode["title"],
                        year=data["releaseYear"],
                        original_lang=episode["viewOptions"][0]["media"].get("originalAudioLanguage", "en"),
                        source=self.ALIASES[0],
                        service_data=data,
                    )
                    for episode in episodes
                ]


        elif data["type"] == "episode":
            return [
                    Title(
                        id_=data["meta"]["id"],
                        type_=Title.Types.TV,
                        name=data["title"],
                        season=int(data["seasonNumber"]),
                        episode=int(data["episodeNumber"]),
                        episode_name=data["title"],
                        year=data["releaseYear"],
                        original_lang=data["viewOptions"][0]["media"].get("originalAudioLanguage", "en"),
                        source=self.ALIASES[0],
                        service_data=data,
                    )
                ]


    def get_tracks(self, title):
        token = self.session.get(self.config["endpoints"]["token"]).json()["csrf"]

        options = title.service_data["viewOptions"]
        subscription = options[0].get("license", "").lower()
        authenticated = next((x for x in options if x.get("isAuthenticated")), None)

        if subscription == "subscription" and not authenticated:
            self.log.error("This title is only available to subscribers")
            sys.exit(1)

        play_id = authenticated.get("playId") if authenticated else options[0].get("playId")
        provider_id = authenticated.get("providerId") if authenticated else options[0].get("providerId")

        headers = {
            "csrf-token": token,
        }
        payload = {
            "rokuId": title.id,
            "playId": play_id,
            "mediaFormat": "mpeg-dash",
            "drmType": "playready" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "widevine",
            "quality": "fhd",
            "providerId": provider_id,
        }

        r = self.session.post(
            self.config["endpoints"]["vod"],
            headers=headers,
            json=payload,
        )
        r.raise_for_status()

        videos = r.json()["playbackMedia"]["videos"]
        self.licenseurl = next(
            (
                x["drmParams"]["licenseServerURL"]
                for x in videos
            ),
            None,
        )       

        url = next((x["url"] for x in videos if x["streamFormat"] == "dash"), None)
        if url and "origin" in urlparse(url).query:
            url = unquote(urlparse(url).query.split("=")[1]).split("?")[0]

        tracks = Tracks.from_mpd(
            url=url,
            session=self.session,
            source=self.ALIASES[0]
        )
        #tracks.videos[0].data["playbackMedia"] = r.json()["playbackMedia"]

        for track in tracks.audios:
            label = track.extra[1].find("Label")
            if label is not None and "description" in label.text:
                track.descriptive = True

        for track in tracks.subtitles:
            label = track.extra[1].find("Label")
            if label is not None and "caption" in label.text:
                track.cc = True

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        # TODO: Hardcode the certificate
        return self.license(**_)

    def license(self, challenge, **_) -> bytes:
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            r = self.session.post(url=self.licenseurl, data=challenge)
            if r.status_code != 200:
                self.log.error(r.content)
                sys.exit(1)
            return r.content
        else:
            r = self.session.post(url=self.licenseurl, data=challenge)
            if r.status_code != 200:
                self.log.error(r.content)
                sys.exit(1)
            return r.content
        

    # service specific functions

    def fetch_episode(self, episode: dict) -> json:
        try:
            r = self.session.get(self.config["endpoints"]["content"] + episode["meta"]["id"])
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            self.log.error(f"An error occurred while fetching episode {episode['meta']['id']}: {e}")
            return None

    def fetch_episodes(self, data: dict) -> list:
        """TODO: Switch to async once https proxies are fully supported"""
        with ThreadPoolExecutor(max_workers=10) as executor:
            tasks = list(executor.map(self.fetch_episode, data["episodes"]))
        return [task for task in tasks if task is not None]
        
    def configure(self):
        #if cookies is not None:
        #    self.session.cookies.update(cookies)
        self.session.get('https://therokuchannel.roku.com/')
        self.cookies = json.loads(json.dumps(self.session.cookies.get_dict()))
        self.cookies['_usn'] = str(uuid.uuid4())