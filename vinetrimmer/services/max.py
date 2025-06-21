import base64
import json
import os.path
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from hashlib import md5

import click
import httpx
import isodate
import requests
import xmltodict
from langcodes import Language

from vinetrimmer.objects import TextTrack, Title, Tracks, VideoTrack
from vinetrimmer.objects.tracks import AudioTrack, MenuTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils import is_close_match, short_hash, try_get
from vinetrimmer.utils.widevine.device import LocalDevice


class Max(BaseService):
    """
    Service code for MAX's streaming service (https://max.com).

    \b
    Authorization: Cookies
    Security: UHD@L1 FHD@L1 HD@L3
    """

    ALIASES = ["MAX", "max"]

    TITLE_RE = r"^(?:https?://(?:www\.|play\.)?max\.com/)?(?P<type>[^/]+)/(?P<id>[^/]+)"

    VIDEO_CODEC_MAP = {
        "H264": ["avc1"],
        "H265": ["hvc1", "dvh1"]
    }

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "AC3": "ac-3",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="Max", short_help="https://max.com")
    @click.argument("title", type=str, required=False)
    # @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Max(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.title = self.parse_title(ctx, title)
        # self.movie = movie

        self.cdm = ctx.obj.cdm

        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]
        self.range = ctx.parent.params["range_"]
        self.alang = ctx.parent.params["alang"]
        # self.api_region = self.config.get(ctx.obj.profile, {}).get('api_region', 'comet-latam')

        # self.license_api = None
        # self.client_grant = None
        # self.auth_grant = None
        # self.profile_id = None
        # self.entitlements = None
        
        if self.range == 'HDR10':
            self.vcodec = "H265"
        
        self.configure()


    def get_titles(self):
        content_type = self.title['type']
        external_id = self.title['id']
        
        response = self.session.get(
            f"https://default.prd.api.max.com/cms/routes/{content_type}/{external_id}?include=default",
        )

        try:
            content_data = [x for x in response.json()["included"] if "attributes" in x and "title" in 
                               x["attributes"] and x["attributes"]["alias"] == "generic-%s-blueprint-page" % (re.sub(r"-", "", content_type))][0]["attributes"]
            content_title = content_data["title"]
        except:
            content_data = [x for x in response.json()["included"] if "attributes" in x and "alternateId" in 
                               x["attributes"] and x["attributes"]["alternateId"] == external_id and x["attributes"].get("originalName")][0]["attributes"]
            content_title = content_data["originalName"]

        
        if content_type == "sport" or content_type =="event":
            included_dt = response.json()["included"]

            for included in included_dt:
                for key, data in included.items():
                    if key == "attributes":
                        for k,d in data.items():
                            if d == "VOD":
                                event_data = included

            release_date = event_data["attributes"].get("airDate") or event_data["attributes"].get("firstAvailableDate")
            year = datetime.strptime(release_date, '%Y-%m-%dT%H:%M:%SZ').year

            return Title(
                id_=external_id,
                type_=Title.Types.MOVIE,
                name=content_title.title(),
                year=year,
                # original_lang=,
                source=self.ALIASES[0],
                service_data=event_data,
            )
        
        if content_type == "movie" or content_type == "standalone":
            metadata = self.session.get(
                url=f"https://default.prd.api.max.com/content/videos/{external_id}/activeVideoForShow?&include=edit"
            ).json()['data']
            
            try:
                edit_id = metadata['relationships']['edit']['data']['id']
            except:
                for x in response.json()["included"]:
                    if x.get("type") == "video" and x.get("relationships", {}).get("show", {}).get("data", {}).get("id") == external_id:
                        metadata = x

            release_date = metadata["attributes"].get("airDate") or metadata["attributes"].get("firstAvailableDate")
            year = datetime.strptime(release_date, '%Y-%m-%dT%H:%M:%SZ').year
            return Title(
                id_=external_id,
                type_=Title.Types.MOVIE,
                name=content_title,
                year=year,
                # original_lang=,
                source=self.ALIASES[0],
                service_data=metadata,
            )

        if content_type in ["show", "mini-series", "topical"]:
            episodes = []
            if content_type == "mini-series":
                alias = "generic-miniseries-page-rail-episodes"
            elif content_type == "topical":
                alias = "generic-topical-show-page-rail-episodes"
            else:
                alias = "-%s-page-rail-episodes-tabbed-content" % (content_type)

            included_dt = response.json()["included"]
            

            season_data = [data for included in included_dt for key, data in included.items()
                           if key == "attributes" for k,d in data.items() if alias in str(d).lower()][0]

            season_data = season_data["component"]["filters"][0]
            
            seasons = [int(season["value"]) for season in season_data["options"]]
            
            season_parameters = [(int(season["value"]), season["parameter"]) for season in season_data["options"]
                for season_number in seasons if int(season["value"]) == int(season_number)]

            if not season_parameters:
                raise self.log.exit("season(s) %s not found")

            for (value, parameter) in season_parameters:
                data = self.session.get(url="https://default.prd.api.max.com/cms/collections/generic-show-page-rail-episodes-tabbed-content?include=default&pf[show.id]=%s&%s" % (external_id, parameter)).json()
                try:
                    episodes_dt = sorted([dt for dt in data["included"] if "attributes" in dt and "videoType" in 
                                    dt["attributes"] and dt["attributes"]["videoType"] == "EPISODE" 
                                    and int(dt["attributes"]["seasonNumber"]) == int(parameter.split("=")[-1])], key=lambda x: x["attributes"]["episodeNumber"])
                except KeyError:
                    raise self.log.exit("season episodes were not found")
                
                episodes.extend(episodes_dt)
            
            titles = []
            release_date = episodes[0]["attributes"].get("airDate") or episodes[0]["attributes"].get("firstAvailableDate")
            year = datetime.strptime(release_date, '%Y-%m-%dT%H:%M:%SZ').year
            
            season_map = {int(item[1].split("=")[-1]): item[0] for item in season_parameters}

            for episode in episodes:
                titles.append(
                    Title(
                        id_=episode['id'],
                        type_=Title.Types.TV,
                        name=content_title,
                        year=year,
                        season=season_map.get(episode['attributes'].get('seasonNumber')),
                        episode=episode['attributes']['episodeNumber'],
                        episode_name=episode['attributes']['name'],
                        # original_lang=edit.get('originalAudioLanguage'),
                        source=self.ALIASES[0],
                        service_data=episode
                    )
                )

            return titles

    def get_tracks(self, title: Title):
        edit_id = title.service_data['relationships']['edit']['data']['id']
        
        response = self.session.post(
            url=self.config['endpoints']['playbackInfo'],
            json={
                'appBundle': 'beam',
                'consumptionType': 'streaming',
                'deviceInfo': {
                    'deviceId': '2dec6cb0-eb34-45f9-bbc9-a0533597303c',
                    'browser': {
                        'name': 'chrome',
                        'version': '113.0.0.0',
                    },
                    'make': 'Microsoft',
                    'model': 'XBOX-Unknown',
                    'os': {
                        'name': 'Windows',
                        'version': '113.0.0.0',
                    },
                    'platform': 'XBOX',
                    'deviceType': 'xbox',
                    'player': {
                        'sdk': {
                            'name': 'Beam Player Console',
                            'version': '1.0.2.4',
                        },
                        'mediaEngine': {
                            'name': 'GLUON_BROWSER',
                            'version': '1.20.1',
                        },
                        'playerView': {
                            'height': 1080,
                            'width': 1920,
                        },
                    },
                },
                'editId': edit_id,
                'capabilities': {
                    'manifests': {
                        'formats': {
                            'dash': {},
                        },
                    },
                'codecs': {
                    'video': {
                        'hdrFormats': [
                            'hlg',
                            'hdr10',
                            'dolbyvision5',
                            'dolbyvision8',
                        ],
                        'decoders': [
                            {
                                'maxLevel': '6.2',
                                'codec': 'h265',
                                'levelConstraints': {
                                    'width': {
                                        'min': 1920,
                                        'max': 3840,
                                    },
                                    'height': {
                                        'min': 1080,
                                        'max': 2160,
                                    },
                                    'framerate': {
                                        'min': 15,
                                        'max': 60,
                                    },
                                },
                                'profiles': [
                                    'main',
                                    'main10',
                                ],
                            },
                            {
                                'maxLevel': '4.2',
                                'codec': 'h264',
                                'levelConstraints': {
                                    'width': {
                                        'min': 640,
                                        'max': 3840,
                                    },
                                    'height': {
                                        'min': 480,
                                        'max': 2160,
                                    },
                                    'framerate': {
                                        'min': 15,
                                        'max': 60,
                                    },
                                },
                                'profiles': [
                                    'high',
                                    'main',
                                    'baseline',
                                ],
                            },
                        ],
                    },
                    'audio': {
                        'decoders': [
                            {
                                'codec': 'aac',
                                'profiles': [
                                    'lc',
                                    'he',
                                    'hev2',
                                    'xhe',
                                ],
                            },
                        ],
                    },
                },
                'devicePlatform': {
                    'network': {
                        'lastKnownStatus': {
                            'networkTransportType': 'unknown',
                        },
                        'capabilities': {
                            'protocols': {
                                'http': {
                                    'byteRangeRequests': True,
                                },
                            },
                        },
                    },
                    'videoSink': {
                        'lastKnownStatus': {
                            'width': 1290,
                            'height': 2796,
                        },
                        'capabilities': {
                            'colorGamuts': [
                                'standard',
                                'wide',
                            ],
                            'hdrFormats': [
                                'dolbyvision',
                                'hdr10plus',
                                'hdr10',
                                'hlg',
                            ],
                        },
                    },
                },
                },
                'gdpr': False,
                'firstPlay': False,
                'playbackSessionId': str(uuid.uuid4()),
                'applicationSessionId': str(uuid.uuid4()),
                'userPreferences': {},
                'features': [],
            }
        )

        playback_data = response.json()
        
        # TEST
        video_info = next(x for x in playback_data['videos'] if x['type'] == 'main')
        title.original_lang = Language.get(video_info['defaultAudioSelection']['language'])

        fallback_url = playback_data["fallback"]["manifest"]["url"]
        fallback_url = fallback_url.replace('fly', 'akm').replace('gcp', 'akm')

        try:
            self.wv_license_url = playback_data["drm"]["schemes"]["widevine"]["licenseUrl"]
            drm_protection_enabled = True
        except (KeyError, IndexError):
            drm_protection_enabled = False
            
        try:
            self.pr_license_url = playback_data["drm"]["schemes"]["playready"]["licenseUrl"]
            drm_protection_enabled = True
        except (KeyError, IndexError):
            drm_protection_enabled = False

        manifest_url = fallback_url.replace('_fallback', '')
        print("MPD URL -", manifest_url)

        tracks: Tracks = Tracks.from_mpd(
            url=manifest_url,
            source=self.ALIASES[0]
        )
        
        tracks.videos = self.dedupe(tracks.videos)
        tracks.audios = self.dedupe(tracks.audios)
        
        # remove partial subs
        tracks.subtitles.clear()

        subtitles = self.get_subtitles(manifest_url, fallback_url)
        
        subs = []
        for subtitle in subtitles:
            subs.append(
                TextTrack(
                    id_=md5(subtitle["url"].encode()).hexdigest(),
                    source=self.ALIASES[0],
                    url=subtitle["url"],
                    codec=subtitle['format'],
                    language=subtitle["language"],
                    forced=subtitle['name'] == 'Forced',
                    sdh=subtitle['name'] == 'SDH'
                )
            )

        tracks.add(subs)

        if self.vcodec:
            tracks.videos = [x for x in tracks.videos if (x.codec or "")[:4] in self.VIDEO_CODEC_MAP[self.vcodec]]

        if self.acodec:
            tracks.audios = [x for x in tracks.audios if (x.codec or "")[:4] == self.AUDIO_CODEC_MAP[self.acodec]]

        for track in tracks:
            # track.needs_proxy = True
            if isinstance(track, VideoTrack):
                codec = track.extra[0].get("codecs")
                #track.hdr10 = codec[0:4] in ("hvc1", "hev1") and codec[5] == "2"
                track.hdr10 = track.dvhdr
                track.dv = codec[0:4] in ("dvh1", "dvhe")
            if isinstance(track, TextTrack) and track.codec == "":
                track.codec = "webvtt"

        title.service_data['info'] = video_info
        
        for track in tracks.audios:
            role = track.extra[1].find("Role")
            if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                track.descriptive = True

        return tracks

    def get_chapters(self, title: Title):
        chapters = []
        video_info = title.service_data['info']
        if 'annotations' in video_info:
            chapters.append(MenuTrack(number=1, title='Chapter 1', timecode='00:00:00.0000'))
            chapters.append(MenuTrack(number=2, title='Credits', timecode=self.convert_timecode(video_info['annotations'][0]['start'])))
            chapters.append(MenuTrack(number=3, title='Chapter 2', timecode=self.convert_timecode(video_info['annotations'][0]['end'])))

        return chapters

    def certificate(self, challenge, **_):
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            return None
        else:
            return self.license(challenge)

    def license(self, challenge, **_):
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            decoded_challenge = str(challenge.decode())
            return self.session.post(
                url=self.pr_license_url,
                data=decoded_challenge  # expects XML
            ).content
        else:
            return self.session.post(
                url=self.wv_license_url,
                data=challenge  # expects bytes
            ).content
        

    def configure(self):
        token = self.session.cookies.get_dict()["st"]
        device_id = json.loads(self.session.cookies.get_dict()["session"])
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            self.session.headers.update({
                'User-Agent': 'BEAM-Android/1.0.0.104 (SONY/XR-75X95EL)',
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'x-disco-client': 'SAMSUNGTV:124.0.0.0:beam:4.0.0.118',
                'x-disco-params': 'realm=bolt,bid=beam,features=ar',
                'x-device-info': 'beam/4.0.0.118 (Samsung/Samsung-Unknown; Tizen/124.0.0.0; f198a6c1-c582-4725-9935-64eb6b17c3cd/87a996fa-4917-41ae-9b6d-c7f521f0cb78)',
                'traceparent': '00-315ac07a3de9ad1493956cf1dd5d1313-988e057938681391-01',
                'tracestate': f'wbd=session:{device_id}',
                'Origin': 'https://play.max.com',
                'Referer': 'https://play.max.com/',
            })
        else:
            self.session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/113.0',
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'x-disco-client': 'WEB:NT 10.0:beam:0.0.0',
                'x-disco-params': 'realm=bolt,bid=beam,features=ar',
                'x-device-info': 'beam/0.0.0 (desktop/desktop; Windows/NT 10.0; b3950c49-ed17-49d0-beb2-11b1d61e5672/da0cdd94-5a39-42ef-aa68-54cbc1b852c3)',
                'traceparent': '00-053c91686df1e7ee0b0b0f7fda45ee6a-f5a98d6877ba2515-01',
                'tracestate': f'wbd=session:{device_id}',
                'Origin': 'https://play.max.com',
                'Referer': 'https://play.max.com/',
            })

        auth_token = self.get_device_token()
        self.session.headers.update({
            "x-wbd-session-state": auth_token
        })

    def get_device_token(self):
        response = self.session.post(
            'https://default.prd.api.max.com/session-context/headwaiter/v1/bootstrap',
        )
        response.raise_for_status()

        return response.headers.get('x-wbd-session-state')

    @staticmethod
    def convert_timecode(time):
        secs, ms = divmod(time, 1)
        mins, secs = divmod(secs, 60)
        hours, mins = divmod(mins, 60)
        ms = ms * 10000
        chapter_time = '%02d:%02d:%02d.%04d' % (hours, mins, secs, ms)

        return chapter_time

    def get_subtitles(self, mpd_url, fallback_url):
        base_url = "/".join(fallback_url.split("/")[:-1]) + "/"
        xml = xmltodict.parse(requests.get(mpd_url).text)

        try:
            tracks = xml["MPD"]["Period"][0]["AdaptationSet"]
        except KeyError:
            tracks = xml["MPD"]["Period"]["AdaptationSet"]

        subs_tracks_js = []
        for subs_tracks in tracks:
            if subs_tracks['@contentType'] == 'text':
                for x in self.force_instance(subs_tracks, "Representation"):
                    try:
                        path = re.search(r'(t/\w+/)', x["SegmentTemplate"]["@media"])[1]
                    except AttributeError:
                        path = 't/sub/'

                    is_sdh = False
                    text = ""
                    if subs_tracks["Role"]["@value"] == "caption":
                        #url = base_url + path + subs_tracks['@lang'] + '_cc.vtt'
                        url = base_url + path + subs_tracks['@lang'] + ('_sdh.vtt' if 'sdh' in subs_tracks["Label"].lower() else '_cc.vtt')
                        is_sdh = True
                        text = " (SDH)"
                    
                    is_forced = False
                    text = ""
                    if subs_tracks["Role"]["@value"] == "forced-subtitle":
                        url = base_url + path + subs_tracks['@lang'] + '_forced.vtt'
                        text = " (Forced)"
                        is_forced = True
                    
                    if subs_tracks["Role"]["@value"] == "subtitle":
                        url = base_url + path + subs_tracks['@lang'] + '_sub.vtt'

                    subs_tracks_js.append({
                        "url": url,
                        "format": "vtt",
                        "language": subs_tracks["@lang"],
                        "languageDescription": Language.make(language=subs_tracks["@lang"].split('-')[0]).display_name() + text,
                        "name": "SDH" if is_sdh else "Forced" if is_forced else "Full",
                    })

        subs_tracks_js = self.remove_dupe(subs_tracks_js)

        return subs_tracks_js

    @staticmethod
    def force_instance(data, variable):
        if isinstance(data[variable], list):
            X = data[variable]
        else:
            X = [data[variable]]
        return X

    @staticmethod
    def remove_dupe(items):
        valores_chave = set()
        new_items = []

        for item in items:
            valor = item['url']
            if valor not in valores_chave:
                new_items.append(item)
                valores_chave.add(valor)

        return new_items
        
    @staticmethod
    def dedupe(items: list) -> list:
        if isinstance(items[0].url, list):
            return items

        filtered_items = list({item.url: item for item in items}.values())

        return filtered_items
