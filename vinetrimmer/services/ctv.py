import json

import click

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class CTV(BaseService):
    """
    Service code for CTV Television Network's free streaming platform (https://ctv.ca).

    \b
    Authorization: None (Free Service)
    Security: UHD@-- HD@L3, doesn't care about releases.

    TODO: Movies are not yet supported
    """

    ALIASES = ["CTV"]
   # GEOFENCE = ["ca"]
    TITLE_RE = r"^(?:https?://(?:www\.)?ctv\.ca(?:/[a-z]{2})?)?/(?P<type>movies|shows)/(?P<id>[a-z0-9-]+)(?:/(?P<episode>[a-z0-9-]+))?$"

    @staticmethod
    @click.command(name="CTV", short_help="https://ctv.ca")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return CTV(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.parse_title(ctx, title)

        self.configure()

    def get_titles(self):
        title_information = self.session.post(
            url="https://api.ctv.ca/space-graphql/graphql",
            json={
                "operationName": "axisMedia",
                "variables": {
                    "axisMediaId": self.title
                },
                "query": """
                query axisMedia($axisMediaId: ID!) {
                    contentData: axisMedia(id: $axisMediaId) {
                        title
                        originalSpokenLanguage
                        mediaType
                        firstAirYear
                        seasons {
                            title
                            id
                            seasonNumber
                        }
                    }
                }
                """
            }
        ).json()["data"]["contentData"]
        titles = []
        for season in title_information["seasons"]:
            titles.extend(self.session.post(
                url="https://api.ctv.ca/space-graphql/graphql",
                json={
                    "operationName": "season",
                    "variables": {
                        "seasonId": season["id"]
                    },
                    "query": """
                    query season($seasonId: ID!) {
                        axisSeason(id: $seasonId) {
                            episodes {
                                axisId
                                title
                                contentType
                                seasonNumber
                                episodeNumber
                                axisPlaybackLanguages {
                                    language
                                }
                            }
                        }
                    }
                    """
                }
            ).json()["data"]["axisSeason"]["episodes"])
        return [Title(
            id_=self.title,
            type_=Title.Types.TV,
            name=title_information["title"],
            year=None,  # TODO: Implement year
            season=x.get("seasonNumber"),
            episode=x.get("episodeNumber"),
            episode_name=x.get("title"),
            original_lang=title_information["originalSpokenLanguage"],
            source=self.ALIASES[0],
            service_data=x
        ) for x in titles]

    def get_tracks(self, title):
        package_id = self.session.get(
            url=self.config["endpoints"]["content_packages"].format(title_id=title.service_data["axisId"]),
            params={"$lang": "en"}
        ).json()["Items"][0]["Id"]

        mpd_url = self.config["endpoints"]["manifest"].format(
            title_id=title.service_data["axisId"],
            package_id=package_id
        )
        r = self.session.get(mpd_url)
        try:
            mpd_data = r.json()
        except json.JSONDecodeError:
            # awesome, probably no error, should be an MPD
            mpd_data = r.text
        else:
            if "ErrorCode" in mpd_data:
                raise Exception(
                    "CTV reported an error when obtaining the MPD Manifest.\n" +
                    f"{mpd_data['Message']} ({mpd_data['ErrorCode']})"
                )

        tracks = Tracks.from_mpd(
            data=mpd_data,
            url=mpd_url,
            source=self.ALIASES[0]
        )

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **kwargs):
        # TODO: Hardcode the certificate
        return self.license(**kwargs)

    def license(self, challenge, **_):
        return self.session.post(
            url=self.config["endpoints"]["license"],
            data=challenge  # expects bytes
        ).content

    # Service specific functions

    def configure(self):
        print("Fetching real title id...")
        self.title = self.session.post(
            url="https://api.ctv.ca/space-graphql/graphql",
            json={
                "operationName": "resolvePath",
                "variables": {
                    "path": f"/shows/{self.title}"
                },
                "query": """
                query resolvePath($path: String!) {
                    resolvedPath(path: $path) {
                        lastSegment {
                            content {
                                id
                            }
                        }
                    }
                }
                """
            }
        ).json()["data"]["resolvedPath"]["lastSegment"]["content"]["id"]
        print(f"Got axis title id: {self.title}")
