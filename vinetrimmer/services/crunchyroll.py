from __future__ import annotations

import re
import uuid
import json
from pathlib import Path
from hashlib import md5, sha1
from langcodes import Language
from typing import Any, Optional, Union
from copy import copy

import click
import requests

from vinetrimmer.objects import Title, Tracks, AudioTrack, MenuTrack, TextTrack, Track, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.widevine.device import LocalDevice
import requests
from requests.adapters import HTTPAdapter, Retry
from vinetrimmer.config import config


class Crunchyroll(BaseService):
    """
    Service code for Crunchyroll (https://www.crunchyroll.com).

    \b
    Authorization: Credentials
    Security:
      Widevine:
        L3: 1080p

    \b
    Tips:
    - Subs, dubs, OVAs, and some series movies are considered seasons/episodes internally.
    - View the series page on the website to map extra season numbers to proper season/title.
    - If a movie is incorrectly listed as a series season/episode,
      use movie option (if only one episode), year can be incorrect.

    \b
    Notes:
    - The series year is based on the release season (i.e winter-2023) because there is no series release year API data.
    - This means dubs will have a different release year if they weren't simulcasted together.
    - This issue does not affect movies as movie_release_year API data exists.
    """
    ALIASES = ["CR"]
    TITLE_RE = [
        r"^https?://(?:www\.)?crunchyroll\.com(?:/[a-z0-9-]+)?/(?:watch/)?(?P<type>series|watch|artist|musicvideo|concert)/(?P<id>[A-Z0-9]+)",
        r"^(?P<id>[A-Z0-9]+)"
    ]

    LANGUAGE_MAP: dict = {
        "es-LA": "es-419",
        "ar-ME": "ar-SA",
        "de-DE": "de",
        "en-US": "en",
        "es-ES": "es",
        "fr-FR": "fr",
        "hi-IN": "hi",
        "it-IT": "it",
        "ja-JP": "ja",
        "ko-KR": "ko",
        "ru-RU": "ru",
        "zh-CN": "zh",
    }

    @staticmethod
    @click.command(name="Crunchyroll", short_help="https://crunchyroll.com", help=__doc__)
    @click.argument("title", type=str, required=False)
    @click.option("-d", "--default-sub-lang", type=str, default="en", help="Default subtitle language (default: en).")
    @click.option("-all", "--show-all-video", is_flag=True, default=False, help="Show all video track.")
    @click.option("-sm", "--skip-merge", type=str, default=None, help="Show dub versions as individual eps.")
    @click.option("-c", "--concert", is_flag=True, default=False, help="Select concert to download.")
    @click.option("-m", "--movie", is_flag=True, default=False, help="Download series as movie if listed as a series.)")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Crunchyroll(ctx, **kwargs)

    def __init__(self, ctx, title, default_sub_lang, show_all_video, concert, movie, skip_merge):
        super().__init__(ctx)
        data = self.parse_title(ctx, title)
        self.title = data.get("id")
        self.type: str = data.get("type") if data else "series"
        self.movie: bool = False
        self.music: bool = False
        self.default_sub: str = default_sub_lang
        self.show_all_video: bool = show_all_video
        self.concert: bool = concert
        self.movie_forced: bool = movie
        self.skip_merge: Optional[str] = skip_merge
        self.cdm = ctx.obj.cdm
        self.processed_ids = set()  # Track already processed content IDs
        self.slang: list = ctx.parent.params["slang"]
        self.alang: list = ctx.parent.params["alang"]
        self.chapters_only: bool = ctx.parent.params["chapters_only"]
        self.audio_only: bool = ctx.parent.params["audio_only"]
        self.subtitles_only: bool = ctx.parent.params["subs_only"]
        self.lic: int = 0

        self.configure()
        
    def get_session(self):
        """
        Creates a Python-requests Session, adds common headers
        from config, cookies, retry handler, and a proxy if available.
        :returns: Prepared Python-requests Session
        """
        session = requests.Session()
        session.mount("https://", HTTPAdapter(
            max_retries=Retry(
                total=5,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
            )
        ))
        session.hooks = {
            "response": lambda r, *_, **__: r.raise_for_status(),
        }
        session.headers.update(config.headers)
        session.cookies.update(self.cookies or {})
        return session
        
    def close_all_sessions(self):
        headers = {
            "authorization": f"Bearer {self.access_token}",
        }
        response = requests.get(self.config['endpoints']['session'], headers=headers)
        
        	        # Close all sessions
        if len (response.json()['items']) > 0:
            for ses in response.json()['items']:
                
                self.close_session(ses['contentId'], ses['token'])

    def get_titles(self) -> Union[list[Title], Title]:
    
        # Reset processed IDs when getting new titles
        self.processed_ids.clear()

        if self.type in {"concert", "musicvideo"}: # music watch link
            return self.get_title_music_watch()
        elif self.type == "artist": # music link
            return self.get_title_music()
        elif self.type == "watch": # watch link
            return self.get_title_watch()
        else: # series
            return self.get_title_series()
    def close_session(self, id, token):

        # Close session
        self.session.delete(
            self.config['endpoints']['streams_del'].format(id=id, video_token=token)
        )
    def get_tracks(self, title: Title) -> Tracks:
    
        # Check if we've already processed this title
        if title.id in self.processed_ids:
            return Tracks()
        
        # Mark this title as processed
        self.processed_ids.add(title.id)

        if self.chapters_only and not self.audio_only and not self.subtitles_only:
            return list()

        self.headers_update()

        tracks = list()
        variants = list()

        try:
            if self.music:
                playback = self.session.get(
                    self.config["endpoints"]["music"]["streams_new"].format(id=title.id)
                ).json()
            else:
                playback = self.session.get(
                    self.config["endpoints"]["streams_new"].format(id=title.id)
                ).json()
        except requests.HTTPError as e:
            self.log.debug(e.response.content)
            self.log.exit("Failed to get playback, maybe too much request in short time!")

        self.log.debug(playback)

        if playback.get("versions"):
            for x in playback["versions"]:
                if x["guid"] not in variants and x["guid"] != title.id:
                    variants.append(x["guid"])
        if title.service_data.get("versions"):
            for x in title.service_data["versions"]:
                if x["guid"] not in variants and x["guid"] != title.id:
                    variants.append(x["guid"])
        variants.insert(0, title.id)

        self.log.debug(variants)

        sdh: list = []

        for variant in variants:

            skip: bool = False

            if not self.music and variant != title.id: 
                try: 
                    headers = {
                        'authorization': f'Bearer {self.access_token}',
                        'origin': 'https://www.crunchyroll.com',
                        'referer': 'https://www.crunchyroll.com/'
                    }

                    playback = self.session.get(
                        url=self.config["endpoints"]["streams_new"].format(id=variant),
                        headers=headers,
                    ).json()
                    self.log.debug(playback)
                except requests.HTTPError as e:
                    self.log.debug(e)
                    if "420 Client Error" in str(e):
                        skip = True
                        self.log.warning("A version might not available in your region!")

            if not skip:
                track = Tracks.from_mpd(
                    url=playback["url"],
                    session=self.session,
                    source=self.ALIASES[0]
                )

                for x in track:
                    x.token = playback["token"]
                    x.real_id = variant

                if not self.music:
                    try:
                        lang_audio: str = [x for x in playback["versions"] if x["guid"] == variant][0]["audio_locale"]
                        lang_audio = self.get_lang(lang_audio)
                    except IndexError:
                        lang_audio: str = title.original_lang
                else:
                    lang_audio: str = "ja"
                if self.skip_merge and not str(lang_audio) in  str(self.skip_merge).lower():
                    continue
                for track_now in track:
                    if isinstance(track_now, AudioTrack):
                        track_now.language = Language.get(lang_audio)
                        # add different id for dubbed audio
                        track_now.id += md5(variant.encode()).hexdigest()[0:6]
                        if track_now.channels == "1.0":
                            track_now.channels = "2.0"
                    elif isinstance(track_now, VideoTrack):
                        track_now.language = Language.get(lang_audio)
                        track_now.note = lang_audio
                        if self.show_all_video:
                            track_now.id += md5(variant.encode()).hexdigest()[0:6]
                track.subtitles.clear()  # remove subs in manifest
                if playback["captions"]:
                    for subtitle in playback["captions"].values():
                        lang = self.get_lang(subtitle["language"])
                        sdh.append(lang)
                        track.add(TextTrack(
                            id_=md5(subtitle["url"].encode()).hexdigest()[0:6],
                            url=subtitle["url"],
                            codec=subtitle["format"],
                            language=Language.get(lang),
                            source=self.ALIASES[0],
                            sdh=True,
                        ))

                for subtitle in playback["subtitles"].values():
                    lang = self.get_lang(subtitle["language"])
                    track.add(TextTrack(
                        id_=md5(subtitle["url"].encode()).hexdigest()[0:6],
                        url=subtitle["url"],
                        codec=subtitle["format"],
                        language=Language.get(lang),
                        source=self.ALIASES[0],
                        forced=False if (str(lang_audio) == str(title.original_lang)) and (lang not in sdh) else True,
                    ))

                if not tracks:
                    tracks = copy(track)
                else:
                    for track in track:
                        if isinstance(track, AudioTrack):
                            tracks.audios.append(track)
                        elif isinstance(track, TextTrack):
                            tracks.subtitles.append(track)
                        elif isinstance(track, VideoTrack) and self.show_all_video:
                            tracks.subtitles.append(track)
            self.session.delete(
                url=self.config["endpoints"]["streams_stop"].format(guid=variant, token=playback["token"])
            )
        return tracks

    def get_chapters(self, title: Title) -> list[MenuTrack]:
        return []

    def license(
        self, challenge: bytes, title: Title, track: Track, *_, **__
    ) -> Optional[Union[bytes, str]]:
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            headers = {
                'authorization': f'Bearer {self.access_token}',
                'origin': 'https://www.crunchyroll.com',
                'referer': 'https://www.crunchyroll.com/'
            }
            
            playback = self.session.get(
                url=self.config["endpoints"]["streams_new"].format(id=track.real_id),
                headers=headers,
            ).json()
            self.log.debug(track.real_id)
            self.log.debug(playback["token"])

            res = self.session.post(
                url=self.config["endpoints"]["license_pr"],
                headers={
                    "x-cr-content-id": track.real_id,
                    "x-cr-video-token": playback["token"],
                    "content-type": "text/xml",
                    "accept-encoding": "gzip, deflate, br",
                },
                data=challenge,  # expects bytes
            ).content       

            self.session.delete(
                url=self.config["endpoints"]["streams_stop"].format(guid=track.real_id, token=playback["token"])
            )
            return res
        else:
            headers = {
                'authorization': f'Bearer {self.access_token}',
                'origin': 'https://www.crunchyroll.com',
                'referer': 'https://www.crunchyroll.com/'
            }
            
            playback = self.session.get(
                url=self.config["endpoints"]["streams_new"].format(id=track.real_id),
                headers=headers,
            ).json()

            # headers = {
            #     "authorization": f"Bearer {self.access_token}",
            # }
            # response = requests.get(self.config['endpoints']['session'], headers=headers)
            # for ses in response.json()['items']:
            #     self.close_session(ses['contentId'], ses['token'])
            self.log.debug(track.real_id)
            self.log.debug(playback["token"])

            r: dict = self.session.post(
                url=self.config["endpoints"]["license_wv"],
                headers={
                    "x-cr-content-id": track.real_id,
                    "x-cr-video-token": playback["token"],
                    "content-type": "application/octet-stream",
                    "accept-encoding": "gzip, deflate, br",
                },
                data=challenge,
            ).json()

            self.session.delete(
                url=self.config["endpoints"]["streams_stop"].format(guid=track.real_id, token=playback["token"])
            )

            return r["license"]


    # Service-specific functions

    def configure(self) -> None:

        self.log.info(" + Logging in")
        self.session.headers.update(
            {
                "origin": "https://www.crunchyroll.com",
                "referer": "https://www.crunchyroll.com",
            }
        )

        if self.credentials:
            cache_path = Path(self.get_cache(
                "tokens_{hash}.json".format(
                    hash=sha1(f"{self.credentials.username}".encode()).hexdigest(),
                ),
            ))

            if cache_path.is_file():
                tokens = json.loads(cache_path.read_text())
                self.refresh_token = tokens["refresh_token"]
                self.log.info("Using cached tokens")
                self.headers_update()
            else:
                cache_path.parent.mkdir(exist_ok=True, parents=True)
                self.log.info(" + Getting logging tokens")
                res = self.session.post(
                    url=self.config["endpoints"]["token"],
                    headers={
                        **self.config["headers_login"],
                    },
                    data={
                        "scope": "offline_access",
                        "grant_type": "password",
                        "device_id": str(uuid.uuid4()),
                        "username": self.credentials.username,
                        "password": self.credentials.password,
                        "device_name": "Google sdk_gphone64_x86_64",
                        "device_type": "sdk_gphone64_x86_64",
                    }
                ).json()

                self.log.debug(res)

                self.refresh_token: str = res["refresh_token"]
                self.access_token: str = res["access_token"]
                cache_path.write_text(json.dumps(res))
                self.update_auth()
                self.key_pair_id, self.policy, self.signature, self.bucket = self.get_bucket_info()
                self.account_id: str = res['account_id']
        else:
            self.get_token_cookies()

    def convert_timecode(self, time):
        secs, ms = divmod(time, 1)
        mins, secs = divmod(secs, 60)
        hours, mins = divmod(mins, 60)
        ms = ms * 10000
        chapter_time = '%02d:%02d:%02d.%04d' % (hours, mins, secs, ms)
        return chapter_time

    def update_auth(self):
        self.session.headers.update({"authorization": f"Bearer {self.access_token}"})

    def headers_update(self):
        if self.credentials:
            self.access_token, self.account_id = self.get_token_password()
        else:
            self.access_token, self.account_id = self.get_token_cookies()
        self.update_auth()
        self.key_pair_id, self.policy, self.signature, self.bucket = self.get_bucket_info()

    def get_bucket_info(self):
        try:
            res = self.session.get(self.config["endpoints"]["bucket"]).json()
            return res["cms"]["key_pair_id"], res["cms"]["policy"], res["cms"]["signature"], res["cms"]["bucket"]
        except requests.HTTPError as e:
            self.log.debug(e.response)
            self.headers_update()

    def get_token_password(self) -> str:
        try:
            res = self.session.post(
                url=self.config["endpoints"]["token"],
                headers={
                    **self.config["headers_login"],
                },
                data={
                    "refresh_token": self.refresh_token,
                    "device_id": str(uuid.uuid4()),
                    "grant_type": "refresh_token",
                    "scope": "offline_access",
                    "device_type": "sdk_gphone64_x86_64",
                }
            ).json()
            return res["access_token"], res["account_id"]
        except requests.HTTPError as e:
            self.log.debug(e.response)
            self.log.exit(f"Failed to get token.")

    def get_token_cookies(self) -> str:
        try:
            res = self.session.post(
                url=self.config["endpoints"]["token"],
                headers={
                    "Authorization": "Basic bm9haWhkZXZtXzZpeWcwYThsMHE6"
                },
                data={
                    "device_id": str(uuid.uuid4()),
                    "device_type": "Chrome on Windows",
                    "grant_type": "etp_rt_cookie"
                }
            ).json()
            return res["access_token"], res["account_id"]
        except requests.HTTPError as e:
            self.log.debug(e.response)
            if self.credentials:
                self.log.warning(
                    f"Failed to get token, maybe cookies expired.")
                self.cookies_cache_path.unlink()
                self.configure()
            else:
                self.log.exit(f"Failed to get token, maybe cookies expired.")

    def get_lang(self, lang: str):
        return self.LANGUAGE_MAP.get(lang, lang)
    
    def get_title_music_watch(self) -> Title:
        
        self.music = True

        type: str = "concerts" if self.type == "concert" else "music_videos"
        data: dict = self.session.get(
            url=self.config["endpoints"]["music"]["music"].format(id=self.title, type=type),
        ).json()["data"][0]
        self.log.debug(data)

        
        return Title(
                id_ = data["id"],
                type_ = Title.Types.MUSIC,
                artist = data["artist"]["name"],
                year = data["originalRelease"][:4],
                album = data["type"],  # TODO
                disc_number = None,  # TODO
                track_number = data["sequenceNumber"],
                source=self.ALIASES[0],
                original_lang = "ja", # not specified but needed in service
                name = data["title"],
                service_data = data,
            )

    def get_title_music(self) -> list[Title]:

        data = self.session.get(
            url=self.config["endpoints"]["music"]["artist"].format(
                id=self.title, type="concerts" if self.concert else "music_videos"
            ),
        ).json()["data"]

        self.log.debug(data)
        self.music = True

        return [
            Title(
                id_ = x["id"],
                type_ = Title.Types.MUSIC,
                artist = x["artist"]["name"],
                year = x["originalRelease"][:4],
                album = x["type"],  # TODO
                disc_number = None,  # TODO no info
                track_number = y,
                source=self.ALIASES[0],
                original_lang = "ja", # not specified, but needed in service
                name = x["title"],
                service_data = x,
            ) for y, x in enumerate(data, 1)
        ]
    def get_title_watch(self) -> Title:

        data = self.session.get(
            url=self.config["endpoints"]["objects"].format(id=self.title),
        ).json()["data"][0]

        self.log.debug(data)

        if data["type"] == "episode": # series, episode
            if self.movie_forced:
                return Title(
                    id_=data["id"],
                    type_=Title.Types.MOVIE,
                    name=data["episode_metadata"]["series_title"],
                    year=data["episode_metadata"]["episode_air_date"][:4],
                    source=self.ALIASES[0],
                    original_lang=self.get_lang(data["episode_metadata"]["audio_locale"]),
                    service_data=data,
                )
            else:
                return Title(
                    id_=data["id"],
                    type_=Title.Types.TV,
                    name=data["episode_metadata"]["series_title"],
                    season=data["episode_metadata"]["season_number"],
                    episode=data["episode_metadata"]["episode_number"],
                    year=data["episode_metadata"]["episode_air_date"][:4],
                    source=self.ALIASES[0],
                    original_lang=self.get_lang(data["episode_metadata"]["audio_locale"]),
                    episode_name=data["title"],
                    service_data=data,
            )

        else: # movie
            self.movie = True
            metadata = self.session.get(
                url=self.config["endpoints"]["metadata"].format(title_id=self.title),
            ).json()["data"][0]

            self.log.debug(metadata)

            return Title(
                id_=data["movie_listing_metadata"]["first_movie_id"],
                type_=Title.Types.MOVIE,
                name=metadata["title"],
                year=metadata["movie_release_year"],
                source=self.ALIASES[0],
                original_lang=self.get_lang(metadata["audio_locale"]),
                service_data=data,
            )
    def get_title_series(self) -> list[Title]:
        titles = list()
        var = dict()

        series_info: dict = self.session.get(
                url=self.config["endpoints"]["series"]["series"].format(
                    id=self.title
                ),
            ).json()["data"][0]

        del series_info["availability_notes"]
        self.log.debug(series_info)

        seasons: dict = self.session.get(
            url=self.config["endpoints"]["series"]["seasons"].format(bucket=self.bucket),
            params={
                "series_id": self.title,
                "locale": "en-US",
                "Signature": self.signature,
                "Policy": self.policy,
                "Key-Pair-Id": self.key_pair_id
            }
        ).json()["items"]

        for season in seasons:
            del season["availability_notes"]

        self.log.debug(seasons)

        unique_episodes = {} 
        
        for season in seasons:
            is_dub = bool(re.search(r" Dub", season["title"]))
            
            if is_dub:
                continue
                
            episodes: dict = self.session.get(
                url=self.config["endpoints"]["series"]["episodes"].format(
                    id=season["id"]
                ),
            ).json()["data"]
            
            for episode in episodes:
                key = f"{episode['season_number']}:{episode['episode_number']}"
                if key not in unique_episodes:
                    unique_episodes[key] = episode
        
        for episode in unique_episodes.values():
            if self.movie_forced:
                titles.append(Title(
                    id_=episode["id"],
                    type_=Title.Types.MOVIE,
                    name=series_info["title"],
                    year=episode["episode_air_date"][:4],
                    source=self.ALIASES[0],
                    original_lang=self.get_lang(episode["audio_locale"]),
                    service_data=episode,
                ))
            else:
                titles.append(Title(
                    id_=episode["id"],
                    type_=Title.Types.TV,
                    name=series_info["title"],
                    season=episode["season_number"],
                    episode=episode["episode_number"] or float(
                    episode["sequence_number"] if '.' not in str(
                    episode["sequence_number"]) else str(
                    episode["sequence_number"]).replace('.', "")),
                    year=episode["episode_air_date"][:4],
                    source=self.ALIASES[0],
                    original_lang=self.get_lang(episode["audio_locale"]),
                    episode_name=episode["title"],
                    service_data=episode,
                ))

        for season in seasons:
            if re.search(r" Dub", season["title"]):
                episodes: dict = self.session.get(
                    url=self.config["endpoints"]["series"]["episodes"].format(
                        id=season["id"]
                    ),
                ).json()["data"]
                
                for episode in episodes:
                    if episode["season_number"] not in var:
                        var[episode["season_number"]] = dict()
                    if episode["episode_number"] not in var[episode["season_number"]]:
                        var[episode["season_number"]][episode["episode_number"]] = []
                    var[episode["season_number"]][episode["episode_number"]].append(episode["id"])

        season_count = {}
        for title in titles:
            if title.season not in season_count:
                season_count[title.season] = 0
            season_count[title.season] += 1
        
        self.log.debug(f"Actual episode counts after deduplication: {season_count}")

        for s, x in var.items():
            for ep, y in x.items():
                for x in titles:
                    if x.season == s and x.episode == ep:
                        for z in y:
                            x.service_data["versions"].append({'guid': f'{z}'})

        return titles

