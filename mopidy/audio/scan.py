from __future__ import unicode_literals

import pygst
pygst.require('0.10')
import gst

import datetime
import os
import time

from mopidy import exceptions
from mopidy.models import Track, Artist, Album
from mopidy.utils import path


class Scanner(object):
    """
    Helper to get tags and other relevant info from URIs.

    :param timeout: timeout for scanning a URI in ms
    :type event: int
    :param min_duration: minimum duration of scanned URI in ms, -1 for all.
    :type event: int
    """

    def __init__(self, timeout=1000, min_duration=100):
        self._timeout_ms = timeout
        self._min_duration_ms = min_duration

        sink = gst.element_factory_make('fakesink')

        audio_caps = gst.Caps(b'audio/x-raw-int; audio/x-raw-float')
        pad_added = lambda src, pad: pad.link(sink.get_pad('sink'))

        self._uribin = gst.element_factory_make('uridecodebin')
        self._uribin.set_property('caps', audio_caps)
        self._uribin.connect('pad-added', pad_added)

        self._pipe = gst.element_factory_make('pipeline')
        self._pipe.add(self._uribin)
        self._pipe.add(sink)

        self._bus = self._pipe.get_bus()
        self._bus.set_flushing(True)

    def scan(self, uri):
        """
        Scan the given uri collecting relevant metadata.

        :param uri: URI of the resource to scan.
        :type event: string
        :return: Dictionary of tags, duration, mtime and uri information.
        """
        try:
            self._setup(uri)
            data = self._collect()
            # Make sure uri, mtime and duration does not come from tags.
            data[b'uri'] = uri
            data[b'mtime'] = self._query_mtime(uri)
            data[gst.TAG_DURATION] = self._query_duration()
        finally:
            self._reset()

        if self._min_duration_ms is None:
            return data
        elif data[gst.TAG_DURATION] >= self._min_duration_ms * gst.MSECOND:
            return data

        raise exceptions.ScannerError('Rejecting file with less than %dms '
                                      'audio data.' % self._min_duration_ms)

    def _setup(self, uri):
        """Primes the pipeline for collection."""
        self._pipe.set_state(gst.STATE_READY)
        self._uribin.set_property(b'uri', uri)
        self._bus.set_flushing(False)
        result = self._pipe.set_state(gst.STATE_PAUSED)
        if result == gst.STATE_CHANGE_NO_PREROLL:
            # Live sources don't pre-roll, so set to playing to get data.
            self._pipe.set_state(gst.STATE_PLAYING)

    def _collect(self):
        """Polls for messages to collect data."""
        start = time.time()
        timeout_s = self._timeout_ms / float(1000)
        data = {}

        while time.time() - start < timeout_s:
            if not self._bus.have_pending():
                continue
            message = self._bus.pop()

            if message.type == gst.MESSAGE_ERROR:
                raise exceptions.ScannerError(message.parse_error()[0])
            elif message.type == gst.MESSAGE_EOS:
                return data
            elif message.type == gst.MESSAGE_ASYNC_DONE:
                if message.src == self._pipe:
                    return data
            elif message.type == gst.MESSAGE_TAG:
                taglist = message.parse_tag()
                for key in taglist.keys():
                    data[key] = taglist[key]

        raise exceptions.ScannerError('Timeout after %dms' % self._timeout_ms)

    def _reset(self):
        """Ensures we cleanup child elements and flush the bus."""
        self._bus.set_flushing(True)
        self._pipe.set_state(gst.STATE_NULL)

    def _query_duration(self):
        try:
            return self._pipe.query_duration(gst.FORMAT_TIME, None)[0]
        except gst.QueryError:
            return None

    def _query_mtime(self, uri):
        if not uri.startswith('file:'):
            return None
        return os.path.getmtime(path.uri_to_path(uri))


def audio_data_to_track(data):
    """Convert taglist data + our extras to a track."""
    albumartist_kwargs = {}
    album_kwargs = {}
    artist_kwargs = {}
    composer_kwargs = {}
    performer_kwargs = {}
    track_kwargs = {}

    def _retrieve(source_key, target_key, target):
        if source_key in data:
            target.setdefault(target_key, data[source_key])

    _retrieve(gst.TAG_ALBUM, 'name', album_kwargs)
    _retrieve(gst.TAG_TRACK_COUNT, 'num_tracks', album_kwargs)
    _retrieve(gst.TAG_ALBUM_VOLUME_COUNT, 'num_discs', album_kwargs)
    _retrieve(gst.TAG_ARTIST, 'name', artist_kwargs)
    _retrieve(gst.TAG_COMPOSER, 'name', composer_kwargs)
    _retrieve(gst.TAG_PERFORMER, 'name', performer_kwargs)
    _retrieve(gst.TAG_ALBUM_ARTIST, 'name', albumartist_kwargs)
    _retrieve(gst.TAG_TITLE, 'name', track_kwargs)
    _retrieve(gst.TAG_TRACK_NUMBER, 'track_no', track_kwargs)
    _retrieve(gst.TAG_ALBUM_VOLUME_NUMBER, 'disc_no', track_kwargs)
    _retrieve(gst.TAG_GENRE, 'genre', track_kwargs)
    _retrieve(gst.TAG_BITRATE, 'bitrate', track_kwargs)

    # Following keys don't seem to have TAG_* constant.
    _retrieve('comment', 'comment', track_kwargs)
    _retrieve('musicbrainz-trackid', 'musicbrainz_id', track_kwargs)
    _retrieve('musicbrainz-artistid', 'musicbrainz_id', artist_kwargs)
    _retrieve('musicbrainz-albumid', 'musicbrainz_id', album_kwargs)
    _retrieve(
        'musicbrainz-albumartistid', 'musicbrainz_id', albumartist_kwargs)

    # For streams, will not override if a better value has already been set.
    _retrieve(gst.TAG_ORGANIZATION, 'name', track_kwargs)
    _retrieve(gst.TAG_LOCATION, 'comment', track_kwargs)
    _retrieve(gst.TAG_COPYRIGHT, 'comment', track_kwargs)

    if gst.TAG_DATE in data and data[gst.TAG_DATE]:
        date = data[gst.TAG_DATE]
        try:
            date = datetime.date(date.year, date.month, date.day)
        except ValueError:
            pass  # Ignore invalid dates
        else:
            track_kwargs['date'] = date.isoformat()

    if albumartist_kwargs:
        album_kwargs['artists'] = [Artist(**albumartist_kwargs)]

    if data['mtime']:
        track_kwargs['last_modified'] = int(data['mtime'])

    if data[gst.TAG_DURATION]:
        track_kwargs['length'] = data[gst.TAG_DURATION] // gst.MSECOND

    track_kwargs['uri'] = data['uri']
    track_kwargs['album'] = Album(**album_kwargs)

    if ('name' in artist_kwargs
            and not isinstance(artist_kwargs['name'], basestring)):
        track_kwargs['artists'] = [Artist(name=artist)
                                   for artist in artist_kwargs['name']]
    else:
        track_kwargs['artists'] = [Artist(**artist_kwargs)]

    if ('name' in composer_kwargs
            and not isinstance(composer_kwargs['name'], basestring)):
        track_kwargs['composers'] = [Artist(name=artist)
                                     for artist in composer_kwargs['name']]
    else:
        track_kwargs['composers'] = \
            [Artist(**composer_kwargs)] if composer_kwargs else ''

    if ('name' in performer_kwargs
            and not isinstance(performer_kwargs['name'], basestring)):
        track_kwargs['performers'] = [Artist(name=artist)
                                      for artist in performer_kwargs['name']]
    else:
        track_kwargs['performers'] = \
            [Artist(**performer_kwargs)] if performer_kwargs else ''

    return Track(**track_kwargs)
