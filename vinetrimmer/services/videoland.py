from __future__ import annotations

from typing import Any
import click
import uuid
import re
from vinetrimmer.config import directories
from vinetrimmer.objects import AudioTrack, TextTrack, MenuTrack, Title, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.widevine.device import LocalDevice

class Videoland(BaseService):
    """
    Service code for RTL's Dutch streaming service Videoland (https://v2.videoland.com).

    Authorization: Credentials
    Security:
        - L1: >= 720p
        - L3: <= 576p

        They are using the license server of DRMToday with encoded streams from CastLabs.
        It accepts Non-Whitelisted Cdms so every unrevoked L1 Cdm should work.

    """

    ALIASES = ["VDL", "videoland"]

    TITLE_RE = [r"^(?:https?://(?:www\.)?v2.videoland\.com/)(?P<id>[a-zA-Z0-9_-]+)?"]

    @staticmethod
    @click.command(name="Videoland", short_help="videoland.com")
    @click.argument("title", type=str)
    @click.option(
        "-m", "--movie", is_flag=True, default=False, help="Title is a Movie."
    )
    @click.pass_context
    def cli(ctx, **kwargs):
        return Videoland(ctx, **kwargs)

    def __init__(self, ctx, title, movie):

        self.cdm = ctx.obj.cdm

        super().__init__(ctx)
        self.session = BaseService.get_session(self)
        self.configure()

        if re.match(r".+?-f_[0-9]+", title):
            title = self.get_program_title(title)

        self.title_url = title
        self.title = self.parse_title(ctx, title)["id"]
        self.title_id = self.title.split("-p_")[-1]

    def get_titles(self):
        metadata = self.session.get(
            url=self.config["endpoints"]["layout"].format(
                platform=self.platform,
                token=self.platform_token,
                endpoint=f"program/{self.title_id}",
            ),
            params={"nbPages": "10"},
        ).json()

        self.movie = "Seizoen" not in str(metadata)

        if self.movie:
            movie_info = metadata["blocks"][0]["content"]["items"][0]

            metadata["viewable"] = movie_info["itemContent"]["action"]["target"][
                "value_layout"
            ]["id"]

            titles = Title(
                id_=movie_info["ucid"],
                type_=Title.Types.MOVIE,
                name=metadata["entity"]["metadata"]["title"],
                year=0,  # TODO: see if we can find the releaseYear
                original_lang="nl",  # Will get it from the manifest
                source=self.ALIASES[0],
                service_data=metadata,
            )
        else:
            seasons = [
                block
                for block in metadata["blocks"]
                if block["featureId"] == "videos_by_season_by_program"
            ]

            totalItems = 0
            for season in seasons:
                totalItems = totalItems + season["content"]["pagination"]["totalItems"]
                while (
                    len(season["content"]["items"])
                    != season["content"]["pagination"]["totalItems"]
                ):
                    season_data = self.session.get(
                        url=self.config["endpoints"]["seasoning"].format(
                            platform=self.platform,
                            token=self.platform_token,
                            program=self.title_id,
                            season_id=season["id"],
                        ),
                        params={
                            "nbPages": "10",
                            "page": season["content"]["pagination"]["nextPage"],
                        },
                    ).json()

                    for episode in season_data["content"]["items"]:
                        if episode not in season["content"]["items"]:
                            season["content"]["items"].append(episode)

                    season["content"]["pagination"]["nextPage"] = season_data[
                        "content"
                    ]["pagination"]["nextPage"]

            episodes = []
            for season in seasons:
                for episode in season["content"]["items"]:
                    episode["seasonNumber"] = int(
                        re.sub("\D", "", season["title"]["long"])
                    )
                    try:
                        episode["episodeNumber"] = int(
                            re.sub(
                                "\D",
                                "",
                                episode["itemContent"]["action"]["target"][
                                    "value_layout"
                                ]["seo"],
                            )
                        )
                    except ValueError:
                        episode["episodeNumber"] = (
                            int(season["content"]["items"].index(episode)) + 1
                        )

                    episodes.append(episode)

            episodes = sorted(
                episodes,
                key=lambda episode: (episode["seasonNumber"], episode["episodeNumber"]),
            )

            self.total_titles = (
                len(set([episode["seasonNumber"] for episode in episodes])),
                len(episodes),
            )

            if totalItems != len(episodes):
                self.log.error_(
                    "Total episodes differs with the total episodes from all seasons."
                )

            for episode in episodes:
                episode["viewable"] = episode["itemContent"]["action"]["target"][
                    "value_layout"
                ]["id"]

                titles = [
                    Title(
                        id_=episode["ucid"],
                        type_=Title.Types.TV,
                        name=metadata["entity"]["metadata"]["title"],
                        year=0,  # TODO: see if we can find the releaseYear
                        season=episode["seasonNumber"],
                        episode=episode["episodeNumber"],
                        episode_name=episode["itemContent"]["extraTitle"],
                        original_lang="nl",  # Will get it from the manifest
                        source=self.ALIASES[0],
                        service_data=episode,
                    )
                    for episode in episodes
                ]
        return titles

    def get_tracks(self, title):
        manifest = self.session.get(
            url=self.config["endpoints"]["layout"].format(
                platform=self.platform,
                token=self.platform_token,
                endpoint=f"video/{title.service_data['viewable']}",
            ),
            params={"nbPages": "2"},
        ).json()

        playerBlock = [
            block for block in manifest["blocks"] if block["templateId"] == "Player"
        ][0]
        assets = playerBlock["content"]["items"][0]["itemContent"]["video"]["assets"]

        if not assets:
            self.log.exit(f"\nFailed to load content manifest")
            
        mpd_url = [
            asset
            for asset in assets
            if asset["quality"]
            == f"{'hd'}"
        ]

        if not mpd_url:
            mpd_url = [asset for asset in assets if asset["quality"] == "sd"][0]["path"]
        else:
            mpd_url = mpd_url[0]["path"]
            
        all_pssh = []
        r = self.session.get(mpd_url)
        psshes = re.findall(r'<cenc:pssh>.+</cenc:pssh>', r.text)
        for pssh in psshes:
            if len(pssh) > 200:
                pssh = pssh.replace("<cenc:pssh>", '').replace("</cenc:pssh>", '') 
                if pssh not in all_pssh:
                    all_pssh.append(pssh)

        for pssh_playready in all_pssh:
            self.pssh_playready = pssh_playready

        tracks = Tracks.from_mpd(
            url=mpd_url,
           # lang=title.original_lang,
            source=self.ALIASES[0],
            session=self.session,
        )

        for track in tracks:
            if isinstance(track, VideoTrack) or isinstance(track, AudioTrack):
                # As long as the API does not provide
                # original languages we should get it from the tracks
                track.language = tracks.videos[0].language
                track.is_original_lang = True
            if isinstance(track, TextTrack):
                if type(track.url) == list or "dash" in track.url:
                    track.codec = "srt"
                else:
                    self.log.exit("\nVideoland: TextTrack codec unknown.")

            for uri in track.url.copy():
                track.url[track.url.index(uri)] = re.sub(
                    r"https://.+?.videoland.bedrock.tech",
                    "https://origin.vod.videoland.bedrock.tech",
                    uri.split("?")[0],
                )

        return tracks

    def get_chapters(self, title: Title) -> list[MenuTrack]:
        return []

    def certificate(self, **_):
        return self.license(**_)

    def license(self, challenge: bytes, title, **_: Any) -> bytes:
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            res = self.session.post(
                url=self.config["endpoints"]["license_pr"],
                data=challenge,  # expects bytes
                headers={"x-dt-auth-token": self.get_license_token(title)},
            )

            if res.status_code != 200:
                raise FailedLicensing

            licensing = res.content

            return licensing
            
        else:
            res = self.session.post(
                url=self.config["endpoints"]["license_wv"],
                data=challenge,  # expects bytes
                headers={"x-dt-auth-token": self.get_license_token(title)},
            )

            if res.status_code != 200:
                raise FailedLicensing

            licensing = res.json()

            return licensing["license"]
        # Service specific functions

    def configure(self):
        self.platform = self.config["platform"]["android_tv"]
        self.platform_token = "token-androidtv-3"
        auth = VDL_AUTH(self)
        self.access_token = auth.access_token
        self.gigya = auth.authorization["UID"]

        self.session.headers.update(
            {
                "origin": "https://v2.videoland.com",
                "Authorization": f"Bearer {self.access_token}",
                "x-client-release": self.config["sdk"]["version"],
                "x-customer-name": "rtlnl",
            }
        )

    def get_license_token(self, title):
        return self.session.get(
            url=self.config["endpoints"]["license_token"].format(
                platform=self.platform,
                gigya=self.gigya,
                clip=title.service_data["viewable"],
            ),
        ).json()["token"]

    def get_program_title(self, title):
        res = self.session.get(
            url=self.config["endpoints"]["layout"].format(
                platform=self.platform,
                token=self.platform_token,
                endpoint=f"folder/{title.split('-f_')[1]}",
            ),
            params={"nbPages": "2"},
        ).json()

        p_title = res["blocks"][0]["content"]["items"][0]["itemContent"]["action"][
            "target"
        ]["value_layout"]["parent"]["seo"]
        p_id = res["blocks"][0]["content"]["items"][0]["itemContent"]["action"][
            "target"
        ]["value_layout"]["parent"]["id"]

        return f"https://v2.videoland.com/{p_title}-p_{p_id}"


class VDL_AUTH:
    def __init__(self, VDL) -> None:
        self.device_id = uuid.uuid1().int
        self.authorization = self.authorize(VDL)
        self.access_token = self.get_jwt(VDL)
        self.profile_id = self.get_profiles(VDL)
        self.access_token = self.get_jwt(VDL)

    def authorize(self, VDL):
        res = VDL.session.post(
            url=VDL.config["endpoints"]["authorization"],
            data={
                "loginID": VDL.credentials.username,
                "password": VDL.credentials.password,
                "sessionExpiration": "0",
                "targetEnv": "jssdk",
                "include": "profile,data",
                "includeUserInfo": "true",
                "lang": "nl",
                "ApiKey": VDL.config["sdk"]["apikey"],
                #"sdk": "js_latest",
                "authMode": "cookie",
                "pageURL": "https://v2.videoland.com/",
                "sdkBuild": VDL.config["sdk"]["build"],
                "format": "json",
            },
        ).json()

        if res.get("errorMessage"):
            self.log.exit(f"Could not authorize Videoland account: {res['errorMessage']!r}")

        return res

    def get_jwt(self, VDL):
        jwt_headers = {
            "x-auth-device-id": str(self.device_id),
            "x-auth-device-player-size-height": "3840",
            "x-auth-device-player-size-width": "2160",
            "X-Auth-gigya-signature": self.authorization["UIDSignature"],
            "X-Auth-gigya-signature-timestamp": self.authorization[
                "signatureTimestamp"
            ],
            "X-Auth-gigya-uid": self.authorization["UID"],
            "X-Client-Release": VDL.config["sdk"]["version"],
            "X-Customer-Name": "rtlnl",
        }

        if getattr(self, "profile_id", None):
            jwt_headers.update({"X-Auth-profile-id": self.profile_id})

        res = VDL.session.get(
            url=VDL.config["endpoints"]["jwt_tokens"].format(platform=VDL.platform),
            headers=jwt_headers,
        ).json()

        if res.get("error"):
            self.log.exit(
                f"Could not get Access Token from Videoland: {res['error']['message']!r}"
            )

        return res["token"]

    def get_profiles(self, VDL):
        res = VDL.session.get(
            url=VDL.config["endpoints"]["profiles"].format(
                platform=VDL.platform, gigya=self.authorization["UID"]
            ),
            headers={"Authorization": f"Bearer {self.access_token}"},
        ).json()

        try:
            if res.get("error"):
                self.log.exit(
                    f"Could not get profiles from Videoland: {res['error']['message']!r}"
                )
        except AttributeError:
            pass

        return res[0]["uid"]
