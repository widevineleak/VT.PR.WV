import html
import logging
import re

import bs4
from srt import Subtitle

from subby.converters.base import BaseConverter
from subby.subripfile import SubRipFile
from subby.utils.time import timedelta_from_timestamp, timestamp_from_ms


class SMPTEConverter(BaseConverter):
    """DFXP/TTML/TTML2 subtitle converter"""

    def parse(self, stream):
        data = stream.read().decode('utf-8-sig')

        if data.count('</tt>') == 1:
            return _SMPTEConverter(data).srt

        # Support for multiple XML documents in a single file
        smpte_subs = [s + '</tt>' for s in data.strip().split('</tt>') if s]
        srt = SubRipFile([])

        for sub in smpte_subs:
            srt.extend(_SMPTEConverter(sub).srt)

        return srt


# Internal converter class as we need to handle multiple subs in one stream
class _SMPTEConverter:
    def __init__(self, data):
        self.logger = logging.getLogger(__name__)
        self.root = bs4.BeautifulSoup(data, 'lxml-xml')
        # Unescape only if necessary (parsing fails)
        if not self.root:
            self.root = bs4.BeautifulSoup(html.unescape(data), 'lxml-xml')

        self.srt = SubRipFile([])

        self.tickrate = int(self.root.tt.get('ttp:tickRate', 0))
        self.frame_duration = 1
        if (rate := self.root.tt.get('ttp:frameRate')) is not None:
            num, denom = map(int, self.root.tt.get('ttp:frameRateMultiplier', '1 1').split())
            framerate = (int(rate) * num) / denom
            self.frame_duration = (1 / framerate) * 1000  # ms

        self.italics = {}
        self.an8 = {}
        self.all_span_italics = '<span tts:fontStyle="italic">' not in data

        self._parse_styles()
        self._convert()

    def _convert(self):
        try:
            assert self.root.tt.body.div is not None
        except (AttributeError, AssertionError):
            return

        for num, line in enumerate(self.root.tt.body.div.find_all('p'), 1):
            line_text = ''

            try:
                for time in ('begin', 'end'):
                    if line[time].endswith('t'):
                        line[time] = self._convert_ticks(line[time])
                    elif line[time].endswith('ms'):
                        line[time] = timestamp_from_ms(line[time][:-2])
                    else:
                        line[time] = self._parse_timestamp(line[time])
            except (AttributeError, KeyError):
                self.logger.warning(
                    'Could not parse %s timestamp for line %02d, skipping',
                    time, num
                )
                continue

            srt_line = Subtitle(
                index=num,
                start=timedelta_from_timestamp(line['begin']),
                end=timedelta_from_timestamp(line['end']),
                content=''
            )

            for element in line:
                line_text += self._parse_element(element)

            if self._is_italic(line) and line_text.strip():
                line_text = line_text.replace('<i>', '')
                line_text = line_text.replace('</i>', '')
                line_text = '<i>%s</i>' % line_text.strip()

            if self._is_an8(line) and line_text.strip():
                line_text = '{\\an8}%s' % line_text.strip()

            srt_line.content = line_text.strip().strip('\n')
            if srt_line.content:
                self.srt.append(srt_line)

    def _parse_styles(self):
        for style in self.root.find_all('style'):
            if style.get('xml:id'):
                self.italics[style['xml:id']] = self._is_italic(style)
        for region in self.root.find_all('region'):
            if region.get('xml:id'):
                self.an8[region['xml:id']] = self._is_an8(region)

    def _parse_element(self, element):
        element_text = ''
        if isinstance(element, bs4.element.NavigableString):
            element_text += element
        elif isinstance(element, bs4.element.Tag):
            subelement_text = ''
            for subelement in element:
                subelement_text += self._parse_element(subelement)
            element_text += subelement_text
            if element.name == 'br':
                element_text += '\n'

            if self._is_italic(element) and element_text.strip():
                element_text = element_text.replace('<i>', '')
                element_text = element_text.replace('</i>', '')
                element_text = '<i>%s</i>' % element_text

            if self._is_an8(element) and element_text.strip():
                element_text = '{\\an8}%s' % element_text

        return element_text

    def _is_italic(self, element):
        if element.get('tts:fontStyle'):
            return element.get('tts:fontStyle') == 'italic'
        elif element.get('style'):
            return self.italics.get(element['style'])
        elif element.name == 'span' and not element.attrs and self.all_span_italics:
            return not self._is_italic(element.parent)

        return False

    def _is_an8(self, element):
        if element.get('tts:displayAlign'):
            return element.get('tts:displayAlign') == 'before'
        elif element.get('region'):
            return self.an8.get(element['region'])

        return False

    def _convert_ticks(self, ticks):
        ticks = int(ticks[:-1])
        offset = 1.0 / self.tickrate
        seconds = (offset * ticks) * 1000

        return timestamp_from_ms(seconds)

    def _parse_timestamp(self, timestamp):
        regex = r'([0-9]{2}):([0-9]{2}):([0-9]{2})[:\.,]?([0-9]{0,3})?'
        parsed = re.search(regex, timestamp)
        hours = int(parsed.group(1))
        minutes = int(parsed.group(2))
        seconds = int(parsed.group(3))
        miliseconds = 0
        if frames := parsed.group(4):
            miliseconds = self.frame_duration * int(frames)

        return "%02d:%02d:%02d.%03d" % (hours, minutes, seconds, miliseconds)
