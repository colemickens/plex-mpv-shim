import logging
import requests
import threading
import time
import os

try:
    from xml.etree import cElementTree as et
except:
    from xml.etree import ElementTree as et

from io import BytesIO

from .conf import settings
from .player import playerManager
from .subscribers import remoteSubscriberManager
from .utils import Timer, safe_urlopen

log = logging.getLogger("timeline")

class TimelineManager(threading.Thread):
    def __init__(self):
        self.currentItems   = {}
        self.currentStates  = {}
        self.idleTimer      = Timer()
        self.subTimer       = Timer()
        self.serverTimer    = Timer()
        self.stopped        = False
        self.halt           = False
        self.trigger        = threading.Event()
        self.is_idle        = True
        self.last_video     = None
        self.last_server_url = None

        threading.Thread.__init__(self)

    def stop(self):
        self.halt = True
        self.join()

    def run(self):
        force_next = False
        while not self.halt:
            if (playerManager._player and playerManager._video) or force_next:
                playerManager.update()
                if not playerManager.is_paused() or force_next:
                    self.SendTimelineToSubscribers()
                self.delay_idle()
            force_next = False
            if self.idleTimer.elapsed() > settings.idle_cmd_delay and not self.is_idle and settings.idle_cmd:
                os.system(settings.idle_cmd)
                self.is_idle = True
            if self.trigger.wait(1):
                force_next = True
                self.trigger.clear()

    def delay_idle(self):
        self.idleTimer.restart()
        self.is_idle = False

    def SendTimelineToSubscribers(self):
        log.debug("TimelineManager::SendTimelineToSubscribers updating all subscribers")
        for sub in list(remoteSubscriberManager.subscribers.values()):
            self.SendTimelineToSubscriber(sub)
        
        # Also send timeline to plex server.
        video  = playerManager._video
        options = self.GetCurrentTimeline()
        session = None
        server_url = None
        if video:
            server_url = video.parent.server_url
            self.last_server_url = video.parent.server_url
            session = video.parent.session
            self.last_session = video.parent.session
        elif self.last_server_url and self.last_session:
            server_url = self.last_server_url
            session = self.last_session
        if server_url and session:
            options["X-Plex-Session-Identifier"] = session
            url = safe_urlopen("%s/:/timeline" % server_url, options, quiet=True)

    def SendTimelineToSubscriber(self, subscriber):
        subscriber.set_poll_evt()
        if subscriber.url == "":
            return True

        timelineXML = self.GetCurrentTimeLinesXML(subscriber)
        url = "%s/:/timeline" % subscriber.url

        log.debug("TimelineManager::SendTimelineToSubscriber sending timeline to %s" % url)

        tree = et.ElementTree(timelineXML)
        tmp  = BytesIO()
        tree.write(tmp, encoding="utf-8", xml_declaration=True)

        tmp.seek(0)
        xmlData = tmp.read()

        # TODO: Abstract this into a utility function and add other X-Plex-XXX fields
        try:
            requests.post(url, data=xmlData, headers={
                "Content-Type":             "application/x-www-form-urlencoded",
                "Connection":               "keep-alive",
                "Content-Range":            "bytes 0-/-1",
                "X-Plex-Client-Identifier": settings.client_uuid
            }, timeout=5)
            return True
        except requests.exceptions.ConnectTimeout:
            log.warning("TimelineManager::SendTimelineToSubscriber timeout sending to %s" % url)
            return False
        except Exception:
            log.warning("TimelineManager::SendTimelineToSubscriber error sending to %s" % url)
            return False

    def WaitForTimeline(self, subscriber):
        subscriber.get_poll_evt().wait(30)
        return self.GetCurrentTimeLinesXML(subscriber)

    def GetCurrentTimeLinesXML(self, subscriber):
        tlines = self.GetCurrentTimeline()

        #
        # Only "video" is supported right now
        #
        mediaContainer = et.Element("MediaContainer")
        if subscriber.commandID is not None:
            mediaContainer.set("commandID", str(subscriber.commandID))
        mediaContainer.set("location", tlines["location"])

        lineEl = et.Element("Timeline")
        for key, value in list(tlines.items()):
            lineEl.set(key, str(value))
        mediaContainer.append(lineEl)

        return mediaContainer

    def GetCurrentTimeline(self):
        # https://github.com/plexinc/plex-home-theater-public/blob/pht-frodo/plex/Client/PlexTimelineManager.cpp#L142
        # Note: location is set to "" to avoid pop-up of navigation menu. This may be abuse of the API.
        options = {
            "location": "",
            "state":    playerManager.get_state(),
            "type":     "video"
        }
        controllable = []

        video  = playerManager._video
        player = playerManager._player

        # The playback_time value can take on the value of none, probably
        # when playback is complete. This avoids the thread crashing.
        if video and not player.playback_abort and player.playback_time:
            self.last_video = video
            media = playerManager._video.parent

            options["location"]          = "fullScreenVideo"
            options["time"]              = int(player.playback_time * 1e3)
            options["autoPlay"]          = '1' if settings.auto_play else '0'
            
            if video.is_transcode:
                trs_audio, trs_subtitle = video.get_transcode_streams()
                if trs_subtitle:
                    options["subtitleStreamID"] = trs_subtitle
                if trs_audio:
                    options["audioStreamID"] = trs_audio
            else:
                if player.sub != 'no':
                    options["subtitleStreamID"] = video.subtitle_uid.get(player.sub, '')

                if player.audio != 'no':
                    options["audioStreamID"] = video.audio_uid.get(player.audio, '')

            options["ratingKey"]         = video.get_video_attr("ratingKey")
            options["key"]               = video.get_video_attr("key")
            options["containerKey"]      = video.get_video_attr("key")
            options["guid"]              = video.get_video_attr("guid")
            options["duration"]          = video.get_video_attr("duration", "0")
            options["address"]           = media.path.hostname
            options["protocol"]          = media.path.scheme
            options["port"]              = media.path.port
            options["machineIdentifier"] = media.get_machine_identifier()
            options["seekRange"]         = "0-%s" % options["duration"]

            if media.play_queue:
                options.update(media.get_queue_info())

            controllable.append("playPause")
            controllable.append("stop")
            controllable.append("stepBack")
            controllable.append("stepForward")
            controllable.append("seekTo")
            controllable.append("skipTo")
            controllable.append("autoPlay")

            if not video.is_transcode:
                controllable.append("subtitleStream")
                controllable.append("audioStream")

            if video.parent.has_next:
                controllable.append("skipNext")
            
            if video.parent.has_prev:
                controllable.append("skipPrevious")

            # If the duration is unknown, disable seeking
            if options["duration"] == "0":
                options.pop("duration")
                options.pop("seekRange")
                controllable.remove("seekTo")

            # Volume control is enabled only if output isn't HDMI,
            # although technically I'm pretty sure we can still control
            # the volume even if the output is hdmi...
            if settings.audio_output != "hdmi":
                controllable.append("volume")
                options["volume"] = str(playerManager.get_volume(percent=True)*100 or 0)

            options["controllable"] = ",".join(controllable)
        else:
            if self.last_video:
                video = self.last_video
                options["ratingKey"]         = video.get_video_attr("ratingKey")
                options["key"]               = video.get_video_attr("key")
                options["containerKey"]      = video.get_video_attr("key")
                if video.parent.play_queue:
                    options.update(video.parent.get_queue_info())
            options["state"] = "stopped"

        return options


timelineManager = TimelineManager()
