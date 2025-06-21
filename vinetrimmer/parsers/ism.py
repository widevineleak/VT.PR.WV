import asyncio
import hashlib
import urllib.parse

import requests
from langcodes import Language
from langcodes.tag_parser import LanguageTagError
from vinetrimmer.utils.widevine.pssh import convert_playready_pssh

from vinetrimmer.objects import AudioTrack, TextTrack, Track, Tracks, VideoTrack
import base64
from vinetrimmer import config
from vinetrimmer.utils.io import aria2c
from vinetrimmer.utils.xml import load_xml


def parse(url, data=None, source=None, session=None, downloader=None):
    """
        Convert a Microsoft Smooth Streaming ISM document to a Tracks object
        with video, audio and subtitle track objects where available.

        :param url: URL of the ISM document.
        :param data: The ISM document as a string.
        :param source: Source tag for the returned tracks.
        :param session: Used for any remote calls, e.g. getting the ISM document from an URL.
            Can be useful for setting custom headers, proxies, etc.
        :param downloader: Downloader to use. Accepted values are None (use requests to download)
            and aria2c.

        Don't forget to manually handle the addition of any needed or extra information or values
        like `encrypted`, `pssh`, `hdr10`, `dv`, etc. Essentially anything that is per-service
        should be looked at. Some of these values like `pssh` will be attempted to be set automatically
        if possible but if you definitely have the values in the service, then set them.

        Examples:
            url = "http://playready.directtaps.net/smoothstreaming/SSWSS720H264/SuperSpeedway_720.ism/Manifest"
            self.session = requests.Session(headers={"X-Example": "foo"})
            tracks = self.tracks_from_ism(url)

            url = "http://playready.directtaps.net/smoothstreaming/SSWSS720H264/SuperSpeedway_720.ism/Manifest"
            self.session = requests.Session(headers={"X-Example": "foo"})
            tracks = self.tracks_from_ism(url, data=session.get(url).text)
        """
    tracks = []

    if not data:
        if downloader is None:
            r = (session or requests).get(url)
            # Resolve final redirect URL
            url = r.url
            data = r.content
        elif downloader == "aria2c":
            out = config.directories.temp / url.split("/")[-1]
            asyncio.run(aria2c((url, out)))

            data = out.read_bytes()

            out.unlink(missing_ok=True)
        else:
            raise ValueError(f"Unsupported downloader: {downloader}")

    root = load_xml(data)
    if root.tag != "SmoothStreamingMedia":
        raise ValueError("Non-ISM document provided to tracks_from_ism")

    base_url = url
    duration = int(root.attrib["Duration"])

    for stream_index in root.findall("StreamIndex"):
        for ql in stream_index.findall("QualityLevel"):
            # content type
            if not (content_type := stream_index.get("Type")):
                raise ValueError("No content type value could be found")
            # codec
            codec = ql.get("FourCC")
            if codec == "TTML":
                codec = "STPP"
            # language
            track_lang = None
            if lang := (stream_index.get("Language") or "").strip():
                try:
                    t = Language.get(lang.split("-")[0])
                    if t == Language.get("und") or not t.is_valid():
                        raise LanguageTagError()
                except LanguageTagError:
                    pass
                else:
                    track_lang = Language.get(lang)
            # content protection
            protections = root.xpath(".//ProtectionHeader")
            # wv_protections = [x for x in protections if (x.get("SystemID") or "").lower() == CDM.uuid]
            pr_protections = [
                x for x in protections
                if (x.get("SystemID") or "").lower() == "9a04f079-9840-4286-ab92-e65be0885f95"
            ]
            protections = pr_protections
            encrypted = bool(protections)
            pssh = None
            pr_pssh=None
            kid = None
            if not pr_pssh:
                for protection in pr_protections:
                    if pr_pssh := "".join(protection.itertext()):
                        pr_pssh = pr_pssh
                        pssh,kid=convert_playready_pssh(pr_pssh)
                        break

            track_url = []
            fragment_ctx = {
                "time": 0,
            }
            stream_fragments = stream_index.findall("c")
            for stream_fragment_index, stream_fragment in enumerate(stream_fragments):
                fragment_ctx["time"] = int(stream_fragment.get("t", fragment_ctx["time"]))
                fragment_repeat = int(stream_fragment.get("r", 1))
                fragment_ctx["duration"] = int(stream_fragment.get("d"))
                if not fragment_ctx["duration"]:
                    try:
                        next_fragment_time = int(stream_fragment[stream_fragment_index + 1].attrib["t"])
                    except IndexError:
                        next_fragment_time = duration
                    fragment_ctx["duration"] = (next_fragment_time - fragment_ctx["time"]) / fragment_repeat
                for _ in range(fragment_repeat):
                    track_url += [
                        urllib.parse.urljoin(
                            base_url, stream_index.get("Url").format_map({
                                "bitrate": ql.get("Bitrate"),
                                "start time": str(fragment_ctx["time"]),
                            }),
                        ),
                    ]
                    fragment_ctx["time"] += fragment_ctx["duration"]

            # For some reason it's incredibly common for services to not provide
            # a good and actually unique track ID, sometimes because of the lang
            # dialect not being represented in the id, or the bitrate, or such.
            # This combines all of them as one and hashes it to keep it small(ish).
            track_id = hashlib.md5(
                f"{codec}-{track_lang}-{ql.get('Bitrate') or 0}-{ql.get('Index') or 0}".encode(),
            ).hexdigest()

            if content_type == "video":
                tracks.append(VideoTrack(
                    id_=track_id,
                    source=source,
                    url=track_url,
                    # metadata
                    codec=codec or "",
                    language=track_lang,
                    bitrate=ql.get("Bitrate"),
                    width=int(ql.get("MaxWidth") or 0) or stream_index.get("MaxWidth"),
                    height=int(ql.get("MaxHeight") or 0) or stream_index.get("MaxHeight"),
                    fps=None,  # TODO
                    hdr10=False,  # TODO
                    hlg=False,  # TODO
                    dv=codec and codec.lower() in ("dvhe", "dvh1"),
                    # switches/options
                    descriptor=Track.Descriptor.ISM,
                    # decryption
                    encrypted=encrypted,
                    pr_pssh=pr_pssh,
                    smooth=True,
                    pssh=pssh,
                    kid=kid,
                    # extra
                    extra=(ql, stream_index, root),
                ))
    # Add tracks, but warn only. Assume any duplicate track cannot be handled.
    # Since the custom track id above uses all kinds of data, there realistically would
    # be no other workaround.
    tracks_obj = Tracks()
    tracks_obj.add(tracks, warn_only=True)

    return tracks_obj  # , warn_only=True
