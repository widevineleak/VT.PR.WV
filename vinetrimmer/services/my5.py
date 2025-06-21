from __future__ import annotations

import base64
import re
import tempfile
import os
from collections.abc import Generator
from typing import Any, Union
from urllib.parse import urlparse, urlunparse
from vinetrimmer.utils.sslciphers import SSLCiphers
import click
import requests
from click import Context
from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService

class MY5(BaseService):
    """
    \b
    Service code for Channel 5's My5 streaming service (https://channel5.com).

    \b
    Author: stabbedbybrick
    Authorization: None
    Robustness:
      L3: 1080p, AAC2.0

    \b
    Tips:
        - Input for series/films/episodes can be either complete URL or just the slug/path:
          https://www.channel5.com/the-cuckoo OR the-cuckoo OR the-cuckoo/season-1/episode-1

    \b
    Known bugs:
        - The progress bar is broken for certain DASH manifests
          See issue: https://github.com/devine-dl/devine/issues/106

    """

    ALIASES = ["channel5", "ch5", "c5"]
    #GEOFENCE = ["gb"]
    TITLE_RE = r"^(?:https?://(?:www\.)?channel5\.com(?:/show)?/)?(?P<id>[a-z0-9-]+)(?:/(?P<sea>[a-z0-9-]+))?(?:/(?P<ep>[a-z0-9-]+))?"

    @staticmethod
    @click.command(name="MY5", short_help="https://channel5.com", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return MY5(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)
        self.title = title
        self.license_api = None

        self.session.headers.update({"user-agent": self.config["user_agent"]})



    def get_titles(self):
        title, season, episode = (re.match(self.TITLE_RE, self.title).group(i) for i in ("id", "sea", "ep"))
        if not title:
            raise ValueError("Could not parse ID from title - is the URL correct?")

        if season and episode:
            r = self.session.get(
                self.config["endpoints"]["single"].format(
                    show=title,
                    season=season,
                    episode=episode,
                )
            )
            r.raise_for_status()
            episode = r.json()

            return [Title(
                id_=episode.get("id"),
                type_=Title.Types.TV,
                name=episode.get("sh_title"),
                season=int(episode.get("sea_num")) if episode.get("sea_num") else 0,
                episode=int(episode.get("ep_num")) if episode.get("ep_num") else 0,
                service_data=episode,
                episode_name=episode.get("title"),
                source=self.ALIASES[0]
                )]


        r = self.session.get(self.config["endpoints"]["episodes"].format(show=title))
        r.raise_for_status()
        data = r.json()

        if data["episodes"][0]["genre"] == "Film":
            return [Title(
                        id_=movie.get("id"),
                        #service=self.__class__,
                        type_=Title.Types.MOVIE,
                        year=None,
                        name=movie.get("sh_title"),
                        source=self.ALIASES[0],
                        service_data=movie
                        #language="en",  # TODO: don't assume
                    )
                    for movie in data.get("episodes")
                ]

        else:
            return [Title(
                id_=episode.get("id"),
                type_=Title.Types.TV,
                name=episode.get("sh_title"),
                season=int(episode.get("sea_num")) if episode.get("sea_num") else 0,
                episode=int(episode.get("ep_num")) if episode.get("sea_num") else 0,
                source=self.ALIASES[0],
                service_data=episode,
                episode_name=episode.get("title")
                #language="en",  # TODO: don't assume
                    )for episode in data["episodes"]]
                    
                    
                    


    def get_tracks(self, title):
        self.manifest, self.license_api = self.get_playlist(title.id)

        tracks = Tracks.from_mpd(
            url=self.manifest,
            session=self.session,
            source=self.ALIASES[0]
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

    def license(self, challenge: bytes, **_):
        r = self.session.post(self.license_api, data=challenge)
        r.raise_for_status()

        return r.content

    # Service specific functions

    def get_playlist(self, asset_id: str) -> tuple:
        session = self.session
        for prefix in ("https://", "http://"):
            session.mount(prefix, SSLCiphers())

        cert_binary = base64.b64decode(self.config["certificate"])
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pem") as cert_file:
            cert_file.write(cert_binary)
            cert_path = cert_file.name
        try:
            r = session.get(url=self.config["endpoints"]["auth"].format(title_id=asset_id), cert=cert_path)
        except requests.RequestException as e:
            if "Max retries exceeded" in str(e):
                raise ConnectionError(
                    "Permission denied. If you're behind a VPN/proxy, you might be blocked"
                )
            else:
                raise ConnectionError(f"Failed to request assets: {str(e)}")
        finally:
            os.remove(cert_path)

        data = r.json()
        if not data.get("assets"):
            raise ValueError(f"Could not find asset: {data}")

        asset = [x for x in data["assets"] if x["drm"] == "widevine"][0]
        rendition = asset["renditions"][0]
        mpd_url = rendition["url"]
        lic_url = asset["keyserver"]

        parse = urlparse(mpd_url)
        path = parse.path.split("/")
        path[-1] = path[-1].split("-")[0].split("_")[0]
        manifest = urlunparse(parse._replace(path="/".join(path)))
        manifest += ".mpd" if not manifest.endswith("mpd") else ""

        return manifest, lic_url
