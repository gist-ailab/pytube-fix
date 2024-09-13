#!/usr/bin/env python
from os import sep, path, makedirs, listdir
from sys import stderr
from platform import system
import logging
from datetime import date, datetime
from time import time, sleep
from json import dumps, dump, loads
from contextlib import closing
from enum import Flag, auto
from typing import Optional, Dict, List, Any
from pathlib import Path
import re
from urllib.request import urlopen
import urllib.error
from http.client import IncompleteRead

import pytube
import pytube.cipher
# from pytube import Youtube

from livestream_saver import exceptions
from livestream_saver import extract
from livestream_saver import util
from livestream_saver.request import YoutubeUrllibSession

SYSTEM = system()
ISPOSIX = SYSTEM == 'Linux' or SYSTEM == 'Darwin'
ISWINDOWS = SYSTEM == 'Windows'
COPY_BUFSIZE = 1024 * 1024 if ISWINDOWS else 64 * 1024

# logger = logging.getLogger(__name__)
# logger.setLevel(logging.DEBUG)

# Temporary backport from pytube 11.0.1
def get_throttling_function_name(js: str) -> str:
    """Extract the name of the function that computes the throttling parameter.

    :param str js:
        The contents of the base.js asset file.
    :rtype: str
    :returns:
        The name of the function used to compute the throttling parameter.
    """
    function_patterns = [
        # https://github.com/ytdl-org/youtube-dl/issues/29326#issuecomment-865985377
        # a.C&&(b=a.get("n"))&&(b=Dea(b),a.set("n",b))}};
        # In above case, `Dea` is the relevant function name
        r'a\.[A-Z]&&\(b=a\.get\("n"\)\)&&\(b=([^(]+)\(b\)',
    ]
    print('Finding throttling function name')
    for pattern in function_patterns:
        regex = re.compile(pattern)
        function_match = regex.search(js)
        if function_match:
            print("finished regex search, matched: %s", pattern)
            return function_match.group(1)


    raise pytube.RegexMatchError(
        caller="get_throttling_function_name", pattern="multiple"
    )
pytube.cipher.get_throttling_function_name = get_throttling_function_name


class YoutubeLiveStream():
    def __init__(
        self,
        url: str,
        output_dir: Path,
        session: YoutubeUrllibSession,
        video_id: Optional[str] = None,
        max_video_quality: Optional[str] = None,
        log_level = logging.INFO) -> None:

        self.session = session
        self.url = url
        self.max_video_quality = max_video_quality
        self.video_id = video_id if video_id is not None \
                                 else extract.get_video_id(url)

        self._js: Optional[str] = None  # js fetched by js_url
        self._js_url: Optional[str] = None  # the url to the js, parsed from watch html

        self._watch_html: Optional[str] = None
        self._embed_html: Optional[str] = None

        self._json: Optional[Dict] = {}

        # NOTE if "www" is omitted, it might force a redirect on YT's side
        # (with &ucbcb=1) and force us to update cookies again. YT is very picky
        # about that. Let's just avoid it.
        self.watch_url = f"https://www.youtube.com/watch?v={self.video_id}"
        self.embed_url = f"https://www.youtube.com/embed/{self.video_id}"

        self._author: Optional[str] = None
        self._title: Optional[str] = None
        self._publish_date: Optional[datetime] = None
        self.video_itag = None
        self.audio_itag = None

        self._player_config_args: Optional[Dict] = None
        self._player_response: Optional[Dict] = None
        self._fmt_streams: Optional[List[pytube.Stream]] = None

        self._chosen_itags: Dict = {}

        self._download_date: Optional[str] = None
        self._scheduled_timestamp = None
        self._start_time: Optional[str] = None

        self._age_restricted: Optional[bool] = None

        self.video_base_url = None
        self.audio_base_url = None
        self.seg = 0
        self.status = Status.OFFLINE
        self.done = False
        self.error = None

        self.output_dir = output_dir
        if not self.output_dir.exists:
            util.create_output_dir(
                output_dir=output_dir, video_id=None
            )

        # self.output_dir = output_dir \
        #     if output_dir.exists() \
        #     else util.create_output_dir(
        #         output_dir=output_dir, video_id=None
        #     )

        self.logger = self.setup_logger(self.output_dir, log_level)

        self.video_outpath = self.output_dir / 'vid'
        self.audio_outpath = self.output_dir / 'aud'

    def setup_logger(self, output_path, log_level):
        if isinstance(log_level, str):
            log_level = str.upper(log_level)

        # We need to make an independent logger (with no parent) in order to
        # avoid using the parent logger's handlers, although we are writing
        # to the same file.
        logger = logging.getLogger("download" + "." + self.video_id)

        if logger.hasHandlers():
            logger.debug(
                f"Logger {logger} already had handlers!"
            )
            return logger

        logger.setLevel(logging.DEBUG)
        # File output
        logfile = logging.FileHandler(
            filename=output_path / "download.log", delay=True, encoding='utf-8'
        )
        logfile.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
        )
        logfile.setFormatter(formatter)
        logger.addHandler(logfile)

        # Console output
        conhandler = logging.StreamHandler()
        conhandler.setLevel(log_level)
        conhandler.setFormatter(formatter)

        def dumb_filter(record):
            # if "Downloading segment" in record.msg:
            # Only filter logRecords that came from our function
            if record.funcName == "print_progress":
                return False
            return True

        confilter = logging.Filter()
        confilter.filter = dumb_filter
        conhandler.addFilter(confilter)
        logger.addHandler(conhandler)
        return logger

    def get_first_segment(self, paths):
        """
        Create each path in paths. If one already existed, return the last
        segment already downloaded, otherwise return 1.
        """
        # If one of the directories exists, assume we are resuming a previously
        # failed download attempt.
        dir_existed = False
        for path in paths:
            try:
                makedirs(path, 0o766)
            except FileExistsError:
                dir_existed = True

        # The sequence number to start downloading from (acually starts at 0).
        seg = 0

        if dir_existed:
            # Get the latest downloaded segment number,
            # unless one directory holds an earlier segment than the other.
            # video_last_segment = max([int(f[:f.index('.')]) for f in listdir(paths[0])])
            # audio_last_segment = max([int(f[:f.index('.')]) for f in listdir(paths[1])])
            # seg = min(video_last_segment, audio_last_segment)
            seg = min([
                    max([int(f[:f.index('.')].split('_')[0])
                    for f in listdir(p)], default=1)
                    for p in paths
                ])

            # Step back one file just in case the latest segment got only partially
            # downloaded (we want to overwrite it to avoid a corrupted segment)
            if seg > 0:
                self.logger.warning(f"An output directory already existed. \
We assume a failed download attempt. Last segment available was {seg}.")
                seg -= 1
        return seg

    def is_live(self) -> None:
        if not self.json:
            return

        isLive = self.json.get('videoDetails', {}).get('isLive')
        if isLive is not None and isLive is True:
            self.status |= Status.LIVE
        else:
            self.status &= ~Status.LIVE

        # Is this actually being streamed live?
        val = None
        for _dict in self.json.get('responseContext', {}).get('serviceTrackingParams', []):
            param = _dict.get('params', [])
            for key in param:
                if key.get('key') == 'is_viewed_live':
                    val = key.get('value')
                    break
        if val and val == "True":
            self.status |= Status.VIEWED_LIVE
        else:
            self.status &= ~Status.VIEWED_LIVE
        self.logger.debug(f"is_live() status {self.status}")


    @property
    def watch_html(self):
        # TODO get the DASH manifest (MPD) instead?
        if self._watch_html:
            return self._watch_html
        try:
            self._watch_html = self.session.make_request(url=self.watch_url)
        except:
            self._watch_html = None

        return self._watch_html

    @property
    def embed_html(self):
        if self._embed_html:
            return self._embed_html
        self._embed_html = pytube.request.get(url=self.embed_url)
        return self._embed_html


    @property
    def json(self):
        if self._json:
            return self._json
        try:
            json_string = extract.initial_player_response(self.watch_html)
            self._json = extract.str_as_json(json_string)
            self.session.is_logged_out(self._json)

            remove_useless_keys(self._json)
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(
                    "Extracted JSON from html:\n"
                    + dumps(self._json, indent=4, ensure_ascii=False)
                )
        except Exception as e:
            self.logger.debug(f"Error extracting JSON from HTML: {e}")
            self._json = {}

        if not self._json:
            self.logger.critical(
                f"WARNING: invalid JSON for {self.watch_url}: {self._json}"
            )
            self.status &= ~Status.AVAILABLE

        return self._json

    @property
    def publish_date(self):
        """Get the publish date.

        :rtype: datetime
        """
        if self._publish_date:
            return self._publish_date
        self._publish_date = extract.publish_date(self.watch_html)
        return self._publish_date

    @publish_date.setter
    def publish_date(self, value):
        """Sets the publish date."""
        self._publish_date = value

    @property
    def streams(self):
        """Interface to query both adaptive (DASH) and progressive streams.

        :rtype: :class:`StreamQuery <StreamQuery>`.
        """
        # self.update_status()
        return pytube.StreamQuery(self.fmt_streams)


    @property
    def age_restricted(self):
        if self._age_restricted:
            return self._age_restricted
        self._age_restricted = pytube.extract.is_age_restricted(self.watch_html)
        return self._age_restricted

    @property
    def js_url(self):
        if self._js_url:
            return self._js_url

        if self.age_restricted:
            self._js_url = pytube.extract.js_url(self.embed_html)
        else:
            self._js_url = pytube.extract.js_url(self.watch_html)

        return self._js_url

    @property
    def js(self):
        if self._js:
            return self._js

        # If the js_url doesn't match the cached url, fetch the new js and update
        #  the cache; otherwise, load the cache.
        if pytube.__js_url__ != self.js_url:
            self._js = pytube.request.get(self.js_url)
            pytube.__js__ = self._js
            pytube.__js_url__ = self.js_url
        else:
            self._js = pytube.__js__

        return self._js

    @property
    def player_response(self) -> Optional[Dict]:
        """The player response contains subtitle information and video details."""
        if self._player_response:
            return self._player_response

        if isinstance(self.player_config_args["player_response"], str):
            self._player_response = loads(
                self.player_config_args["player_response"]
            )
        else:
            self._player_response = self.player_config_args["player_response"]
        return self._player_response

    @property
    def title(self) -> Optional[str]:
        """Get the video title."""
        if self._title:
            return self._title

        try:
            # FIXME decode unicode escape sequences if any
            self._title = self.player_response['videoDetails']['title']
        except KeyError as e:
            self.logger.debug(f"KeyError in {self.video_id}.title: {e}")
            # Check_availability will raise the correct exception in most cases
            #  if it doesn't, ask for a report.
            # self.check_availability()
            self.update_status()
            raise pytube.exceptions.PytubeError(
                (
                    f'Exception while accessing title of {self.watch_url}. '
                    'Please file a bug report at https://github.com/pytube/pytube'
                )
            )
        return self._title

    @title.setter
    def title(self, value):
        """Sets the title value."""
        self._title = value

    @property
    def description(self) -> Optional[str]:
        """Get the video description.

        :rtype: str
        """
        return self.player_response.get("videoDetails", {}).get("shortDescription")



    # # NOT USED
    # def populate_info(self):
    #     if not self.json:
    #         return

    #     self.video_title = self.json.get('videoDetails', {}).get('title')
    #     self.author =  self.json.get('videoDetails', {}).get('author')

    #     if not self.thumbnail_url:
    #         tlist = self.json.get('videoDetails', {}).get('thumbnail', {}).get('thumbnails', [])
    #         if tlist:
    #             # Grab the last one, probably always highest resolution
    #             # FIXME grab the best by comparing width/height key-values.
    #             self.thumbnail_url = tlist[-1].get('url')

    #     # self.scheduled_time = self.get_scheduled_time(self.json.get('playabilityStatus', {}))

    #     if self.logger.isEnabledFor(logging.DEBUG):
    #         self.logger.debug(f"Video ID: {self.video_id}")
    #         self.logger.debug(f"Video title: {self.title}")
    #         self.logger.debug(f"Video author: {self.author}")


    @property
    def thumbnail_url(self) -> str:
        """Get the thumbnail url image.

        :rtype: str
        """
        thumbnail_details = (
            self.player_response.get("videoDetails", {})
            .get("thumbnail", {})
            .get("thumbnails")
        )
        if thumbnail_details:
            thumbnail_details = thumbnail_details[-1]  # last item has max size
            return thumbnail_details["url"]

        return f"https://img.youtube.com/vi/{self.video_id}/maxresdefault.jpg"

    @property
    def start_time(self):
        if self._start_time:
            return self._start_time
        try:
            # String reprensentation in UTC format
            self._start_time = self.player_response \
                .get("microformat", {}) \
                .get("playerMicroformatRenderer", {}) \
                .get("liveBroadcastDetails", {}) \
                .get("startTimestamp", None)
        except Exception as e:
            self.logger.debug(f"Error getting start_time: {e}")
        return self._start_time

    @property
    def scheduled_timestamp(self):
        if self._scheduled_timestamp:
            return self._scheduled_timestamp
        try:
            timestamp = self.player_response.get("playabilityStatus", {}) \
                .get('liveStreamability', {})\
                .get('liveStreamabilityRenderer', {}) \
                .get('offlineSlate', {}) \
                .get('liveStreamOfflineSlateRenderer', {}) \
                .get('scheduledStartTime', None) # unix timestamp
            if timestamp is not None:
                self._scheduled_timestamp = int(timestamp)
            else:
                self._scheduled_timestamp = None
        except Exception as e:
            self.logger.debug(f"Error getting scheduled_timestamp: {e}")
        return self._scheduled_timestamp

    @property
    def author(self) -> str:
        """Get the video author.
        :rtype: str
        """
        if self._author:
            return self._author
        self._author = self.player_response.get(
            "videoDetails", {}).get("author", "unknown")
        return self._author

    @author.setter
    def author(self, value):
        """Set the video author."""
        self._author = value

    def download_thumbnail(self):
        # TODO write more thumbnail files in case the first one somehow
        #  got updated.
        thumbnail_path = self.output_dir / 'thumbnail'
        if self.thumbnail_url and not path.exists(thumbnail_path):
            with closing(urlopen(self.thumbnail_url)) as in_stream:
                self.write_to_file(in_stream, thumbnail_path)

    def update_metadata(self):
        if self.video_itag:
            if info := pytube.itags.ITAGS.get(self.video_itag):
                self.video_resolution = info[0]
        if self.audio_itag:
            if info := pytube.itags.ITAGS.get(self.audio_itag):
                self.audio_bitrate = info[0]

        self.download_thumbnail()

        # TODO get the description once the stream has started

        metadata_file = self.output_dir / 'metadata.json'
        if path.exists(metadata_file):
            # FIXME this avoids writing this file more than once for now.
            # No further updates.
            return
        with open(metadata_file, 'w', encoding='utf8') as fp:
            dump(obj=self.video_info, fp=fp, indent=4, ensure_ascii=False)

    @property
    def video_info(self):
        """Return current metadata for writing to disk as JSON."""
        info = {
            "id": self.video_id,
            "title": self.title,
            "author": self.author,
            "publish_date": str(self.publish_date),
            "start_time": self.start_time,
            "download_date": date.fromtimestamp(time()).__str__(),
            "video_itag": self.video_itag,
            "audio_itag": self.audio_itag,
            "description": self.description,
        }
        if self.scheduled_timestamp is not None:
            info["scheduled_time"] = datetime.utcfromtimestamp(
                self.scheduled_timestamp
            ).__str__()

        if self.video_itag:
            info["video_itag"] = self.video_itag.itag
            info["video_resolution"] = self.video_itag.resolution
        if self.audio_itag:
            info["audio_itag"] = self.audio_itag.itag
            info["audio_bitrate"] = self.audio_itag.abr
        return info

    def update_status(self):
        self.logger.debug("update_status()...")
        # force update
        self._watch_html = None
        self._json = None
        self._player_config_args = None
        _json = self.json

        if not _json:
            return

        self.is_live()
        self.logger.info("Stream seems to be viewed live. Good.") \
        if self.status & Status.VIEWED_LIVE else \
        self.logger.warning(
            "Stream is not being viewed live. This might not work!"
        )

        # Check if video is indeed available through its reported status.
        playabilityStatus = _json.get('playabilityStatus', {})
        status = playabilityStatus.get('status')

        if status == 'LIVE_STREAM_OFFLINE':
            self.status |= Status.OFFLINE

            scheduled_time = self.scheduled_timestamp
            if scheduled_time is not None:
                self.status |= Status.WAITING

                # self._scheduled_timestamp = scheduled_time
                reason = playabilityStatus.get('reason', 'No reason found.')

                self.logger.info(f"Scheduled start time: {scheduled_time} \
({datetime.utcfromtimestamp(scheduled_time)} UTC). We wait...")
                # FIXME use local time zone for more accurate display of time
                # for example: https://dateutil.readthedocs.io/
                self.logger.warning(f"{reason}")

                raise exceptions.WaitingException(
                    self.video_id, reason, scheduled_time
                )

            elif (Status.LIVE | Status.VIEWED_LIVE) not in self.status:
                raise exceptions.WaitingException(
                    self.video_id,
                    playabilityStatus.get('reason', 'No reason found.')
                )

            raise exceptions.OfflineException(
                self.video_id,
                playabilityStatus.get('reason', 'No reason found.')
            )

        elif status == 'LOGIN_REQUIRED':
            raise exceptions.NoLoginException(
                self.video_id,
                playabilityStatus.get('reason', 'No reason found.')
            )

        elif status == 'UNPLAYABLE':
            raise exceptions.UnplayableException(
                self.video_id,
                playabilityStatus.get('reason', 'No reason found.')
            )

        elif status != 'OK':
            subreason = playabilityStatus.get('errorScreen', {})\
                                         .get('playerErrorMessageRenderer', {})\
                                         .get('subreason', {})\
                                         .get('simpleText', \
                                              'No subreason found in JSON.')
            self.logger.warning(f"Livestream {self.video_id} \
playability status is: {status} \
{playabilityStatus.get('reason', 'No reason found')}. Sub-reason: {subreason}")
            self.status &= ~Status.AVAILABLE
            # return
        else: # status == 'OK'
            self.status |= Status.AVAILABLE
            self.status &= ~Status.OFFLINE
            self.status &= ~Status.WAITING

        self.logger.info(f"Stream status {self.status}")


    # TODO get itag by quality first, and then update the itag download url
    # if needed by selecting by itag (the itag we have chosen by best quality)
    def update_download_urls(self):
        video_quality, audio_quality = self.get_best_streams(
            maxq=self.max_video_quality
        )

        self.logger.debug(
            f"Selected video itag {video_quality} / "
            f"Selected audio itag:{audio_quality}"
        )

        if ((self.video_itag is not None
        and self.video_itag != video_quality)
        or
        (self.audio_itag is not None
        and self.audio_itag != audio_quality)):
            # Probably should fail if we suddenly get a different format than the
            # one we had before to avoid problems during merging.
            self.logger.critical(
                "Got a different format after refresh of download URL!\n"
                f"Previous video itag: {self.video_itag}. New: {video_quality}.\n"
                f"Previous audio itag: {self.audio_itag}. New: {audio_quality}"
            )
            raise Exception("Format mismatch after update of base URL.")

        self.video_itag = video_quality
        self.audio_itag = audio_quality

        # self.video_base_url = extract.get_base_url_from_itag(self.json, video_quality)
        # self.audio_base_url = extract.get_base_url_from_itag(self.json, audio_quality)
        self.video_base_url = self.video_itag.url
        self.audio_base_url = self.audio_itag.url

        self.logger.debug(f"Video base url: {self.video_base_url}")
        self.logger.debug(f"Audio base url: {self.audio_base_url}")

    def download(self, wait_delay=2.0):
        self.seg = self.get_first_segment((self.video_outpath, self.audio_outpath))
        self.logger.info(f'Will start downloading from segment number {self.seg}.')

        attempt = 0
        while not self.done and not self.error:
            try:
                self.update_status()
                self.logger.debug(f"Status is {self.status}.")

                if not self.status == Status.OK:
                    self.logger.critical(
                        f"Could not download \"{self.url}\": "
                        "stream unavailable or not a livestream.")
                    return

            except exceptions.WaitingException as e:
                self.logger.warning(
                    f"Status is {self.status}. "
                    f"Waiting for {wait_delay} minutes...")
                sleep(wait_delay * 60)
                continue
            except exceptions.OfflineException as e:
                self.logger.critical(f"{e}")
                raise e
            except Exception as e:
                self.logger.critical(f"{e}")
                raise e

            self.update_download_urls()
            self.update_metadata()

            while True:
                try:
                    self.do_download()
                except (exceptions.EmptySegmentException,
                        exceptions.ForbiddenSegmentException,
                        IncompleteRead,
                        ValueError) as e:
                    self.logger.info(e)
                    # force update
                    self._watch_html = None
                    self._json = None
                    self._player_config_args = None
                    self.json
                    self.is_live()
                    if Status.LIVE | Status.VIEWED_LIVE in self.status:

                        if attempt >= 15:
                            self.logger.critical(
                                f"Too many attempts on segment {self.seg}. "
                                "Skipping it.")
                            self.seg += 1
                            attempt = 0
                            continue

                        self.logger.warning(
                            "It seems the stream has not really ended. "
                            f"Retrying in 10 secs... (attempt {attempt}/15)")
                        attempt += 1
                        sleep(10)
                        try:
                            self.update_download_urls()
                        except Exception as e:
                            self.error = f"{e}"
                            break
                        continue
                    self.logger.warning(f"The stream is not live anymore. Done.")
                    self.done = True
                    break
                except Exception as e:
                    self.logger.exception(f"Unhandled exception. Aborting.")
                    self.error = f"{e}"
                    break
        if self.done:
            self.logger.info(f"Finished downloading {self.video_id}.")
        if self.error:
            self.logger.critical(f"Some kind of error occured during download? {self.error}")

    def do_download(self):
        if not self.video_base_url or not self.audio_base_url:
            raise Exception("Missing video or audio base url!")

        wait_sec = 60
        attempt = 0
        while True:
            try:
                video_segment_url = f'{self.video_base_url}&sq={self.seg}'
                audio_segment_url = f'{self.audio_base_url}&sq={self.seg}'
                self.print_progress(self.seg)

                # To have zero-padded filenames (not compatible with
                # merge.py from https://github.com/mrwnwttk/youtube_stream_capture
                # as it doesn't expect any zero padding )
                video_segment_filename = f'{self.video_outpath}{sep}{self.seg:0{10}}_video.ts'
                audio_segment_filename = f'{self.audio_outpath}{sep}{self.seg:0{10}}_audio.ts'

                # urllib.request.urlretrieve(video_segment_url, video_segment_filename)
                # # Assume stream has ended if last segment is empty
                # if stat(video_segment_filename).st_size == 0:
                #     unlink(video_segment_filename)
                #     break

                # TODO pass proper user-agent headers to server (construct Request)
                with closing(urlopen(video_segment_url)) as in_stream:
                    headers = in_stream.headers
                    status = in_stream.status
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(f"Seg {self.seg} video URL: {video_segment_url}")
                        self.logger.debug(f"Seg status: {status}")
                        self.logger.debug(f"Seg headers:\n{headers}")

                    if not self.write_to_file(in_stream, video_segment_filename):
                        if status == 204 and headers.get('X-Segment-Lmt', "0") == "0":
                            raise exceptions.EmptySegmentException(\
                                f"Segment {self.seg} (video) is empty, stream might have ended...")
                        self.logger.warning(f"Waiting for {wait_sec} seconds before retrying...")
                        sleep(wait_sec)
                        continue

                # urllib.request.urlretrieve(audio_segment_url, audio_segment_filename)
                with closing(urlopen(audio_segment_url)) as in_stream:
                    headers = in_stream.headers
                    status = in_stream.status
                    if self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(f"Seg {self.seg} audio URL: {audio_segment_url}")
                        self.logger.debug(f"Seg status: {status}")
                        self.logger.debug(f"Seg headers:\n{headers}")

                    if not self.write_to_file(in_stream, audio_segment_filename):
                        if status == 204 and headers.get('X-Segment-Lmt', "0") == "0":
                            raise exceptions.EmptySegmentException(\
                                f"Segment {self.seg} (audio) is empty, stream might have ended...")
                        self.logger.warning(f"Waiting for {wait_sec} seconds before retrying...")
                        sleep(wait_sec)
                        continue

                attempt = 0
                self.seg += 1

            except urllib.error.URLError as e:
                self.logger.critical(f'{type(e)}: {e}')
                if e.reason == 'Forbidden':
                    # Usually this means the stream has ended and parts
                    # are now unavailable.
                    raise exceptions.ForbiddenSegmentException(e.reason)
                if attempt >= 30:
                    raise e
                attempt += 1
                self.logger.warning(
                    f"Waiting for {wait_sec} seconds before retrying... "
                    f"(attempt {attempt}/30)")
                sleep(wait_sec)
                continue
            except (IncompleteRead, ValueError) as e:
                # This is most likely signaling the end of the stream
                self.logger.exception(e)
                raise e
            except IOError as e:
                self.logger.exception(e)
                raise e

    def print_progress(self, seg: int) -> None:
        # TODO display rotating wheel in interactive mode
        fullmsg = f"Downloading segment {seg}..."
        if ISWINDOWS:
            prev_len = getattr(self, '_report_progress_prev_line_length', 0)
            if prev_len > len(fullmsg):
                fullmsg += ' ' * (prev_len - len(fullmsg))
            self._report_progress_prev_line_length = len(fullmsg)
            clear_line = '\r'
        else:
            clear_line = ('\r\x1b[K' if stderr.isatty() else '\r')

        print(clear_line + fullmsg, end='')
        self.logger.info(fullmsg)

    # OBSOLETE
    def print_found_quality(self, item, datatype):
        if datatype == "video":
            keys = ["itag", "qualityLabel", "mimeType", "bitrate", "quality", "fps"]
        else:
            keys = ["itag", "audioQuality", "mimeType", "bitrate", "audioSampleRate"]
        try:
            result = f"Available {datatype} quality: "
            for k in keys:
                result += f"{k}: {item.get(k)}\t"
            self.logger.info(result)
        except Exception as e:
            self.logger.critical(
                f"Exception while trying to print found {datatype} quality: {e}"
            )

    def print_available_streams(self, stream_list):
        if not self.logger.isEnabledFor(logging.INFO):
            return
        for s in stream_list:
            self.logger.info(
                "Available {}".format(s.__repr__().replace(' ', '\t'))
            )

    @property
    def player_config_args(self):
        if self._player_config_args:
            return self._player_config_args

        # FIXME this is redundant with json property
        self._player_config_args = {}
        # self._player_config_args["player_response"] = self.json["responseContext"]
        self._player_config_args["player_response"] = self.json

        if 'streamingData' not in self._player_config_args["player_response"]:
            self.logger.critical("Missing streamingData key in json!")
            # TODO add fallback strategy with get_ytplayer_config()?

        return self._player_config_args

    @property
    def fmt_streams(self):
        """Returns a list of streams if they have been initialized.

        If the streams have not been initialized, finds all relevant
        streams and initializes them.
        """
        # self.check_availability()
        if self._fmt_streams:
            return self._fmt_streams

        self._fmt_streams = []
        stream_maps = ["url_encoded_fmt_stream_map"]

        # unscramble the progressive and adaptive stream manifests.
        for fmt in stream_maps:
            # if not self.age_restricted and fmt in self.vid_info:
            #     extract.apply_descrambler(self.vid_info, fmt)
            pytube.extract.apply_descrambler(self.player_config_args, fmt)

            pytube.extract.apply_signature(self.player_config_args, fmt, self.js)

            # build instances of :class:`Stream <Stream>`
            # Initialize stream objects
            stream_manifest = self.player_config_args[fmt]
            for stream in stream_manifest:
                video = pytube.Stream(
                    stream=stream,
                    player_config_args=self.player_config_args,
                    monostate=None,
                )
                self._fmt_streams.append(video)

        return self._fmt_streams

    def get_best_streams(self, maxq=None, codec="mp4", fps="60"):
        """Return a tuple of pytube.Stream objects, first one for video
        second one for audio.
        If only progressive streams are available, the second item in tuple
        will be None.
        :param str maxq:
        :param str codec: mp4, webm
        :param str fps: 30, 60"""
        video_stream = None
        audio_stream = None

        def as_int(res_or_abr: str) -> Optional[int]:
            if res_or_abr is None:
                return None
            as_int = None
            # looks for "1080p" or "48kbps", either a resolution or abr
            if match := re.search(r"(\d{3,4})(p)?|(\d{2,4})(kpbs)?", res_or_abr):
                as_int = int(match.group(1))
            return as_int

        if maxq is not None and not isinstance(maxq, int):
            maxq = as_int(maxq)
            # if match := re.search(r"(\d{3,4})(p)?|(\d{2,4}(kpbs)?", maxq):
            #     maxq = int(match.group(1))
            if maxq is None:
                self.logger.warning(
                    f"Max quality setting \"{maxq}\" is incorrect. "
                    "Defaulting to best video quality available."
                )

        custom_filters = None
        if maxq is not None and isinstance(maxq, int):
            def filter_maxq(s):
                res_int = as_int(s.resolution)
                if res_int is None:
                    return False
                return res_int <= maxq
            custom_filters = [filter_maxq]

        avail_streams = self.streams
        self.print_available_streams(avail_streams)
        video_streams = avail_streams.filter(file_extension=codec,
            custom_filter_functions=custom_filters
            ) \
            .order_by('resolution') \
            .desc()

        video_stream = video_streams.first()
        self.logger.info(f"Selected video {video_stream}")

        audio_streams = avail_streams.filter(
            only_audio=True
            ) \
            .order_by('abr') \
            .desc()
        audio_stream = audio_streams.first()
        self.logger.info(f"selected audio {audio_stream}")

        # FIXME need a fallback in case we didn't have an audio stream
        # TODO need a strategy if progressive has better audio quality:
        # use progressive stream's audio track only? Would that work with the
        # DASH stream video?
        if len(audio_streams) == 0:
            self.streams.filter(
                progressive=False,
                file_extension=codec
                ) \
                .order_by('abr') \
                .desc() \
                .first()

        return (video_stream, audio_stream)


    # # TODO close but UNFINISHED, superceded by pytube. OBSOLETE
    # def get_best_quality(self, datatype, maxq=None, codec="mp4", fps="60"):
    #     # Select the best possible quality, with maxq (str) as the highest possible
    #     label = 'qualityLabel' if datatype == 'video' else 'audioQuality'
    #     streamingData = self.json.get('streamingData', {})
    #     adaptiveFormats = streamingData.get('adaptiveFormats', {})

    #     if not streamingData or not adaptiveFormats:
    #         raise Exception(f"Could not get {datatype} quality format. \
    # Missing streamingData or adaptiveFormats.")

    #     available_stream_by_itags = []
    #     for stream in adaptiveFormats:
    #         if stream.get(label, None) is not None:
    #             available_stream_by_itags.append(stream)
    #             self.print_found_quality(stream, datatype)

    #     if maxq is not None and isinstance(maxq, str):
    #         if match := re.search(r"(\d{3,4})p?", maxq):
    #             maxq = int(match.group(1))
    #         else:
    #             self.logger.warning(
    #                 f"Max quality setting \"{maxq}\" is incorrect."
    #                 " Defaulting to best video quality available."
    #             )
    #             maxq = None

    #     ranked_profiles = []
    #     for stream in available_stream_by_itags:
    #         i_itag = int(stream.get("itag"))
    #         itag_profile = pytube.itags.get_format_profile(i_itag)
    #         itag_profile["itag"] = i_itag

    #         # Filter None values, we don't know what bitrate they represent.
    #         if datatype == "audio" and itag_profile.get("abr"):
    #             ranked_profiles.append(itag_profile)
    #             # strip kpbs for sorting. Not really necessary anymore since
    #             # None values are filtered already.
    #             # audio_streams[-1]["abr"] = abr.split("kpbs")[0]
    #         elif datatype == "video" and (res := itag_profile.get("resolution")):
    #             if maxq:
    #                 res_int = int(res.split("p")[0])
    #                 if res_int > maxq:
    #                     continue
    #             ranked_profiles.append(itag_profile)

    #     if datatype == "audio":
    #         ranked_profiles.sort(key=lambda s: s.get("abr"))
    #     else:
    #         ranked_profiles.sort(key=lambda s: s.get("resolution"))

    #     # Add back information from the json for further ranking
    #     # because pytube doesn't keep track of those
    #     if datatype == "video":
    #         for avail in adaptiveFormats:
    #             itag = avail.get("itag")
    #             for profile in ranked_profiles:
    #                 if profile.get("itag") == itag:
    #                     # fps: 60/30
    #                     profile["fps"] = avail.get("fps", "")
    #                     # mimeType: video/mp4; codecs="avc1.42c00b"
    #                     # mimeType: video/webm; codecs="vp9"
    #                     profile["mimeType"] = avail.get("mimeType", "").split(";")[0]
    #                     continue
    #         ranked_profiles.sort(key=lambda s: s.get("fps"))

    #     # select mp4 or webm depending on "mimeType" container type
    #     ranked_profiles.sort(key=lambda s: s.get("mimeType"))

    #     filters = []


    #     best_itag = ranked_streams[0].get('itag')

    #     chosen_itag = None
    #     chosen_quality_labels = ""
    #     for i in ranked_streams:
    #         if i in available_itags:
    #             chosen_itag = i
    #             for s in adaptiveFormats:
    #                 if chosen_itag == s.get('itag'):
    #                     if datatype == "video":
    #                         chosen_quality_labels = f"{d.get('qualityLabel')} \
    # type: {d.get('mimeType')} bitrate: {d.get('bitrate')} codec: {d.get('codecs')}"
    #                     else:
    #                         chosen_quality_labels = f"{d.get('audioQuality')} \
    # type: {d.get('mimeType')} bitrate: {d.get('bitrate')} codec: {d.get('codecs')}"
    #             break

    #     self.logger.warning(f"Chosen {datatype} quality: \
    # itag {chosen_itag}; height: {chosen_quality_labels}")

    #     if chosen_itag is None:
    #         raise Exception(f"Failed to get chosen quality from adaptiveFormats.")
    #     return chosen_itag


    def write_to_file(self, fsrc, fdst, length=0):
        """Copy data from file-like object fsrc to file-like object fdst.
        If no bytes are read from fsrc, do not create fdst and return False.
        Return True when file has been created and data has been written."""
        # Localize variable access to minimize overhead.
        if not length:
            length = COPY_BUFSIZE
        fsrc_read = fsrc.read

        try:
            buf = fsrc_read(length)
        except Exception as e:
            # FIXME handle these errors better, for now we just ignore and move on:
            # ValueError: invalid literal for int() with base 16: b''
            # http.client.IncompleteRead: IncompleteRead
            self.logger.exception(e)
            buf = None

        if not buf:
            return False
        with open(fdst, 'wb') as out_file:
            fdst_write = out_file.write
            while buf:
                fdst_write(buf)
                buf = fsrc_read(length)
        return True


class Status(Flag):
    OFFLINE = auto()
    AVAILABLE = auto()
    LIVE = auto()
    VIEWED_LIVE = auto()
    WAITING = auto()
    OK = AVAILABLE | LIVE | VIEWED_LIVE


def remove_useless_keys(_dict):
    for keyname in ['heartbeatParams', 'playerAds', 'adPlacements', 'playbackTracking',
    'annotations', 'playerConfig', 'storyboards',
    'trackingParams', 'attestation', 'messages', 'frameworkUpdates']:
        try:
            _dict.pop(keyname)
        except KeyError:
            continue

    # remove this annoying long list
    try:
        _dict.get('microformat', {})\
             .get('playerMicroformatRenderer', {})\
             .pop('availableCountries')
    except KeyError:
        pass
