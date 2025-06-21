from __future__ import annotations

import hashlib
import os
import re
import sys
import uuid
from collections.abc import Generator
from typing import Any

import click
from langcodes import Language

from vinetrimmer.objects import TextTrack, Title, Tracks
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.objects import AudioTrack, TextTrack, Title, Tracks, VideoTrack, MenuTrack
from vinetrimmer.utils import Cdm
from vinetrimmer.vendor.pymp4.parser import Box
from vinetrimmer.utils.widevine.device import LocalDevice

class TUBI(BaseService):
    """
    Service code for TubiTV streaming service (https://tubitv.com/)

    \b
    Version: 1.0.1
    Author: stabbedbybrick
    Authorization: None
    Robustness:
      Widevine:
        L3: 720p, AAC2.0

    \b
    Tips:
        - Input can be complete title URL or just the path:
            /series/300001423/gotham
            /tv-shows/200024793/s01-e01-pilot
            /movies/589279/the-outsiders

    \b
    Notes:
        - Due to the structure of the DASH manifest and requests downloader failing to output progress,
          aria2c is used as the downloader no matter what downloader is specified in the config.
        - Search is currently disabled.
    """
    ALIASES = ["TUBI", "tubi", "tubitv", "TubiTV"]
    TITLE_RE = r"^(?:https?://(?:www\.)?tubitv\.com?)?/(?P<type>movies|series|tv-shows)/(?P<id>[a-z0-9-]+)"

    @staticmethod
    @click.command(name="TUBI", short_help="https://tubitv.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return TUBI(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.title = title
        super().__init__(ctx)
        self.licenseurl = None
        self.cdm = ctx.obj.cdm

    def get_titles(self):
        try:
            kind, content_id = (re.match(self.TITLE_RE, self.title).group(i) for i in ("type", "id"))
        except Exception:
            raise ValueError("Could not parse ID from title - is the URL correct?")

        params = {
            "platform": "android",
            "content_id": content_id,
            "device_id": str(uuid.uuid4()),
            "video_resources[]": [
                "dash",
                "dash_playready",
            ] if self.cdm.device.type == LocalDevice.Types.PLAYREADY else [
                "dash",
                "dash_widevine",
            ],
        }

        if kind == "tv-shows":
            content = self.session.get(self.config["endpoints"]["content"], params=params)
            content.raise_for_status()
            series_id = "0" + content.json().get("series_id")
            params.update({"content_id": int(series_id)})
            data = self.session.get(self.config["endpoints"]["content"], params=params).json()
            
            return [
                        Title(
                            id_=episode["id"],
                            type_=Title.Types.TV,
                            name=data["title"],
                            year=data["year"],
                            season=int(season["id"]),
                            episode=int(episode["episode_number"]),
                            episode_name=episode["title"].split("-")[1],
                            original_lang="en",
                            source=self.ALIASES[0],
                            service_data=episode
                        ) 
                        for season in data["children"]
                        for episode in season["children"]
                    ]

        if kind == "series":
            r = self.session.get(self.config["endpoints"]["content"], params=params)
            r.raise_for_status()
            data = r.json()

            return [
                        Title(
                            id_=episode["id"],
                            type_=Title.Types.TV,
                            name=data["title"],
                            year=data["year"],
                            season=int(season["id"]),
                            episode=int(episode["episode_number"]),
                            episode_name=episode["title"].split("-")[1],
                            original_lang="en",
                            source=self.ALIASES[0],
                            service_data=episode
                        ) 
                        for season in data["children"]
                        for episode in season["children"]
                    ]

        if kind == "movies":
            r = self.session.get(self.config["endpoints"]["content"], params=params)
            r.raise_for_status()
            data = r.json()
            
            return Title(
                    id_=data["id"],
                    type_=Title.Types.MOVIE,
                    name=data["title"],
                    year=data["year"],
                    original_lang="en",  # TODO: Get original language
                    source=self.ALIASES[0],
                    service_data=data,
                )

    def get_tracks(self, title):
        if not title.service_data.get("video_resources"):
            self.log.error(" - Failed to obtain video resources. Check geography settings.")
            self.log.info(f"Title is available in: {title.service_data.get('country')}")
            sys.exit(1)

        self.manifest = title.service_data["video_resources"][0]["manifest"]["url"]
        self.licenseurl = title.service_data["video_resources"][0].get("license_server", {}).get("url")
        
        tracks = Tracks.from_mpd(
            url=self.manifest,
            session=self.session,
            source=self.ALIASES[0]
        )
        
        for track in tracks:
            rep_base = track.extra[1].find("BaseURL")
            if rep_base is not None:
                base_url = os.path.dirname(track.url)
                track_base = rep_base.text
                track.url = f"{base_url}/{track_base}"
                track.descriptor = Track.Descriptor.URL
        #        track.downloader = aria2c

                
        for track in tracks.audios:
            role = track.extra[1].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

            
        if title.service_data.get("subtitles"):
            tracks.add(
                TextTrack(
                    id_=hashlib.md5(title.service_data["subtitles"][0]["url"].encode()).hexdigest()[0:6],
                    source=self.ALIASES[0],
                    url=title.service_data["subtitles"][0]["url"],
                    codec=title.service_data["subtitles"][0]["url"][-3:],
                    language= title.service_data["subtitles"][0].get("lang_alpha3"),
                    forced=False,
                    sdh=False,
                )
            )
            
        return tracks       

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        # TODO: Hardcode the certificate
        return self.license(**_)

        
    def license(self, challenge, **_):
        if not self.licenseurl:
            return None
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            r = self.session.post(url=self.licenseurl, data=challenge)
            if r.status_code != 200:
                raise ConnectionError(r.content)

            return r.content
        else:
            r = self.session.post(url=self.licenseurl, data=challenge)
            if r.status_code != 200:
                raise ConnectionError(r.content)

            return r.content

