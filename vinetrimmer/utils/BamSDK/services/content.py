from requests import Request

from vinetrimmer.utils.BamSDK.services import Service


# noinspection PyPep8Naming
class content(Service):
    def getDmcEpisodes(self, region, season_id, page, access_token):
        endpoint = self.client.endpoints["getDmcEpisodes"]
        req = Request(
            method=endpoint.method,
            url=f"https://disney.content.edge.bamgrid.com/svc/content/DmcEpisodes/version/5.1/region/{region}/audience/false/maturity/1850/language/en/seasonId/{season_id}/pageSize/15/page/{page}",
            headers=endpoint.get_headers(accessToken=access_token)
        ).prepare()
        res = self.session.send(req)
        return res.json()

    def getDmcSeriesBundle(self, region, media_id, access_token):
        endpoint = self.client.endpoints["getDmcSeriesBundle"]
        req = Request(
            method=endpoint.method,
            url=f"https://disney.content.edge.bamgrid.com/svc/content/DmcSeriesBundle/version/5.1/region/{region}/audience/false/maturity/1850/language/en/encodedSeriesId//{media_id}",
            headers=endpoint.get_headers(accessToken=access_token)
        ).prepare()
        res = self.session.send(req)
        return res.json()

    def getDmcVideoBundle(self, region, media_id, access_token):
        endpoint = self.client.endpoints["getDmcVideoBundle"]
        req = Request(
            method=endpoint.method,
            url=f"https://disney.content.edge.bamgrid.com/svc/content/DmcVideoBundle/version/5.1/region/{region}/audience/false/maturity/1850/language/en/encodedFamilyId/{media_id}",
            headers=endpoint.get_headers(accessToken=access_token)
        ).prepare()
        res = self.session.send(req)
        return res.json()
