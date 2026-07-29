#!/usr/bin/env python3
"""dlnacast — minimal DLNA MediaRenderer for macOS, playing through VLC.

Run `./dlnacast` (or `python3 dlnacast.py`) and this Mac shows up as a
cast target in any DLNA-capable phone app. Requires VLC.app; everything
else is Python standard library.
"""

import argparse
import base64
import os
import socket
import subprocess
import sys
import threading
import time
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

VERSION = "1.0"
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SERVER_HEADER = f"Darwin/UPnP/1.0 dlnacast/{VERSION}"

DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
SERVICE_TYPES = {
    "AVTransport": "urn:schemas-upnp-org:service:AVTransport:1",
    "RenderingControl": "urn:schemas-upnp-org:service:RenderingControl:1",
    "ConnectionManager": "urn:schemas-upnp-org:service:ConnectionManager:1",
}
EVENT_NS = {
    "AVTransport": "urn:schemas-upnp-org:metadata-1-0/AVT/",
    "RenderingControl": "urn:schemas-upnp-org:metadata-1-0/RCS/",
}

SINK_PROTOCOL_INFO = ",".join(
    f"http-get:*:{mime}:*"
    for mime in (
        "video/mp4", "video/x-matroska", "video/x-msvideo", "video/avi",
        "video/mpeg", "video/quicktime", "video/x-flv", "video/3gpp",
        "video/webm", "video/x-ms-wmv",
        "audio/mpeg", "audio/mp4", "audio/x-flac", "audio/flac",
        "audio/wav", "audio/x-wav", "audio/ogg", "audio/x-ms-wma",
        "image/jpeg", "image/png", "image/gif",
        "*",
    )
)

VERBOSE = False
RT = None  # Runtime singleton, set in main()


def log(msg):
    if VERBOSE:
        print(f"[dlnacast] {msg}", flush=True)


# ---------------------------------------------------------------- helpers

def to_hms(seconds):
    """Seconds -> DLNA time string H:MM:SS."""
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def from_hms(value):
    """DLNA time string ("1:02:03", "02:03", "0:00:10.500") -> whole seconds."""
    try:
        parts = [float(p) for p in str(value).strip().split(":")]
    except (ValueError, AttributeError):
        return 0
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return int(total)


def parse_soap(body):
    """Return (action_name, {arg: value}) from a SOAP request body."""
    root = ET.fromstring(body)
    body_el = root.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")
    if body_el is None:
        return None, {}
    action_el = next(iter(body_el), None)
    if action_el is None:
        return None, {}
    action = action_el.tag.split("}")[-1]
    args = {child.tag.split("}")[-1]: (child.text or "") for child in action_el}
    return action, args


def build_ssdp_response(st, usn, location):
    return (
        "HTTP/1.1 200 OK\r\n"
        "CACHE-CONTROL: max-age=1800\r\n"
        "EXT:\r\n"
        f"LOCATION: {location}\r\n"
        f"SERVER: {SERVER_HEADER}\r\n"
        f"ST: {st}\r\n"
        f"USN: {usn}\r\n"
        "\r\n"
    )


def build_ssdp_notify(nt, usn, location, nts):
    return (
        "NOTIFY * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        "CACHE-CONTROL: max-age=1800\r\n"
        f"LOCATION: {location}\r\n"
        f"NT: {nt}\r\n"
        f"NTS: {nts}\r\n"
        f"SERVER: {SERVER_HEADER}\r\n"
        f"USN: {usn}\r\n"
        "\r\n"
    )


def ssdp_targets(udn):
    """(NT/ST, USN) pairs this device answers for."""
    pairs = [("upnp:rootdevice", f"{udn}::upnp:rootdevice"), (udn, udn),
             (DEVICE_TYPE, f"{udn}::{DEVICE_TYPE}")]
    for st in SERVICE_TYPES.values():
        pairs.append((st, f"{udn}::{st}"))
    return pairs


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def load_udn(path):
    try:
        with open(path) as f:
            value = f.read().strip()
        if value.startswith("uuid:"):
            return value
    except OSError:
        pass
    value = f"uuid:{uuidlib.uuid4()}"
    try:
        with open(path, "w") as f:
            f.write(value)
    except OSError:
        pass
    return value


# ---------------------------------------------------------------- VLC

class SoapError(Exception):
    def __init__(self, code=501, desc="Action Failed"):
        super().__init__(desc)
        self.code = code
        self.desc = desc


class VLC:
    """Drives VLC.app through its local HTTP interface."""

    BINARY = "/Applications/VLC.app/Contents/MacOS/VLC"

    def __init__(self, port, password):
        self.port = port
        self.password = password
        self.proc = None
        self.auth = "Basic " + base64.b64encode(f":{password}".encode()).decode()
        self.lock = threading.Lock()

    def available(self):
        return os.path.exists(self.BINARY)

    def _request(self, command=None, **params):
        query = ""
        if command:
            query = "?command=" + command
            for key, value in params.items():
                query += f"&{key}={quote(str(value), safe='')}"
        req = Request(
            f"http://127.0.0.1:{self.port}/requests/status.xml{query}",
            headers={"Authorization": self.auth},
        )
        with urlopen(req, timeout=5) as resp:
            return ET.fromstring(resp.read())

    def running(self):
        try:
            self._request()
            return True
        except Exception:
            return False

    def ensure_running(self):
        with self.lock:
            if self.running():
                return
            log("starting VLC")
            self.proc = subprocess.Popen(
                [
                    self.BINARY,
                    "--extraintf", "http",
                    "--http-host", "127.0.0.1",
                    "--http-port", str(self.port),
                    "--http-password", self.password,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(50):
                if self.running():
                    return
                time.sleep(0.2)
            raise SoapError(501, "VLC did not start")

    def status(self):
        try:
            root = self._request()
        except Exception:
            return {"state": "stopped", "time": 0, "length": 0, "volume": 100}
        raw_volume = int(root.findtext("volume", "256") or 0)
        return {
            "state": root.findtext("state", "stopped"),
            "time": int(root.findtext("time", "0") or 0),
            "length": max(0, int(root.findtext("length", "0") or 0)),
            "volume": max(0, min(100, round(raw_volume / 2.56))),
        }

    def play(self, url):
        self.ensure_running()
        self._request("pl_empty")
        self._request("in_play", input=url)

    def resume(self):
        self._request("pl_forceresume")

    def pause(self):
        self._request("pl_forcepause")

    def stop(self):
        try:
            self._request("pl_stop")
        except Exception:
            pass

    def seek(self, seconds):
        self._request("seek", val=int(seconds))

    def set_volume(self, percent):
        percent = max(0, min(100, int(percent)))
        self._request("volume", val=int(percent * 2.56))

    def quit(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------- eventing

class EventBus:
    """Minimal GENA: keeps subscribers and pushes LastChange events."""

    def __init__(self):
        self.subs = {}
        self.lock = threading.Lock()

    def subscribe(self, service, callback):
        sid = f"uuid:{uuidlib.uuid4()}"
        with self.lock:
            self.subs[sid] = {
                "service": service,
                "callback": callback,
                "seq": 0,
                "expires": time.time() + 1800,
            }
        return sid

    def renew(self, sid):
        with self.lock:
            sub = self.subs.get(sid)
            if sub:
                sub["expires"] = time.time() + 1800
            return sub is not None

    def unsubscribe(self, sid):
        with self.lock:
            self.subs.pop(sid, None)

    def notify(self, service, variables):
        body = self._last_change_body(EVENT_NS[service], variables)
        with self.lock:
            now = time.time()
            expired = [sid for sid, s in self.subs.items() if s["expires"] < now]
            for sid in expired:
                del self.subs[sid]
            targets = [
                (sid, sub) for sid, sub in self.subs.items()
                if sub["service"] == service
            ]
        for sid, sub in targets:
            seq = sub["seq"]
            sub["seq"] += 1
            threading.Thread(
                target=self._send, args=(sub["callback"], sid, seq, body),
                daemon=True,
            ).start()

    def notify_initial(self, service, sid):
        with self.lock:
            sub = self.subs.get(sid)
        if not sub:
            return
        body = self._last_change_body(EVENT_NS[service], initial_state_vars(service))
        seq = sub["seq"]
        sub["seq"] += 1
        self._send(sub["callback"], sid, seq, body)

    @staticmethod
    def _last_change_body(ns, variables):
        inner = "".join(
            f'<{name} val="{escape(str(value), {chr(34): "&quot;"})}"/>'
            for name, value in variables.items()
        )
        event = f'<Event xmlns="{ns}"><InstanceID val="0">{inner}</InstanceID></Event>'
        return (
            '<?xml version="1.0"?>\n'
            '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f"<e:property><LastChange>{escape(event)}</LastChange></e:property>"
            "</e:propertyset>"
        )

    @staticmethod
    def _send(callback, sid, seq, body):
        try:
            req = Request(
                callback,
                data=body.encode(),
                method="NOTIFY",
                headers={
                    "Content-Type": 'text/xml; charset="utf-8"',
                    "NT": "upnp:event",
                    "NTS": "upnp:propchange",
                    "SID": sid,
                    "SEQ": str(seq),
                },
            )
            urlopen(req, timeout=5).close()
        except Exception as exc:
            log(f"event delivery to {callback} failed: {exc}")


def initial_state_vars(service):
    if service == "AVTransport":
        status = RT.vlc.status()
        return {
            "TransportState": transport_state(status["state"]),
            "TransportStatus": "OK",
            "AVTransportURI": RT.state.uri,
            "CurrentTrackURI": RT.state.uri,
        }
    status = RT.vlc.status()
    return {"Volume": status["volume"], "Mute": int(RT.state.mute)}


def transport_state(vlc_state):
    return {
        "playing": "PLAYING",
        "paused": "PAUSED_PLAYBACK",
        "stopped": "STOPPED",
    }.get(vlc_state, "TRANSITIONING")


# ---------------------------------------------------------------- state

class RendererState:
    def __init__(self):
        self.uri = ""
        self.meta = ""
        self.loaded_uri = None
        self.mute = False
        self.premute_volume = 100


class Runtime:
    def __init__(self, name, ip, http_port, udn, vlc):
        self.name = name
        self.ip = ip
        self.http_port = http_port
        self.udn = udn
        self.vlc = vlc
        self.state = RendererState()
        self.events = EventBus()

    @property
    def location(self):
        return f"http://{self.ip}:{self.http_port}/description.xml"


# ---------------------------------------------------------------- SOAP actions

def avtransport_action(action, args):
    rt = RT
    if action == "SetAVTransportURI":
        rt.state.uri = args.get("CurrentURI", "")
        rt.state.meta = args.get("CurrentURIMetaData", "")
        log(f"SetAVTransportURI {rt.state.uri}")
        return {}
    if action == "Play":
        status = rt.vlc.status()
        if rt.state.uri and rt.state.uri == rt.state.loaded_uri:
            if status["state"] == "paused":
                rt.vlc.resume()
            elif status["state"] != "playing":
                rt.vlc.play(rt.state.uri)
        elif rt.state.uri:
            rt.vlc.play(rt.state.uri)
            rt.state.loaded_uri = rt.state.uri
        else:
            raise SoapError(701, "No media")
        rt.events.notify("AVTransport", {"TransportState": "PLAYING",
                                         "AVTransportURI": rt.state.uri})
        return {}
    if action == "Pause":
        rt.vlc.pause()
        rt.events.notify("AVTransport", {"TransportState": "PAUSED_PLAYBACK"})
        return {}
    if action == "Stop":
        rt.vlc.stop()
        rt.events.notify("AVTransport", {"TransportState": "STOPPED"})
        return {}
    if action == "Seek":
        if args.get("Unit") in ("REL_TIME", "ABS_TIME"):
            rt.vlc.seek(from_hms(args.get("Target", "0:00:00")))
        return {}
    if action == "GetTransportInfo":
        status = rt.vlc.status()
        return {
            "CurrentTransportState": transport_state(status["state"]),
            "CurrentTransportStatus": "OK",
            "CurrentSpeed": "1",
        }
    if action == "GetPositionInfo":
        status = rt.vlc.status()
        return {
            "Track": "1" if rt.state.uri else "0",
            "TrackDuration": to_hms(status["length"]),
            "TrackMetaData": rt.state.meta,
            "TrackURI": rt.state.uri,
            "RelTime": to_hms(status["time"]),
            "AbsTime": to_hms(status["time"]),
            "RelCount": "2147483647",
            "AbsCount": "2147483647",
        }
    if action == "GetMediaInfo":
        status = rt.vlc.status()
        return {
            "NrTracks": "1" if rt.state.uri else "0",
            "MediaDuration": to_hms(status["length"]),
            "CurrentURI": rt.state.uri,
            "CurrentURIMetaData": rt.state.meta,
            "NextURI": "",
            "NextURIMetaData": "",
            "PlayMedium": "NETWORK",
            "RecordMedium": "NOT_IMPLEMENTED",
            "WriteStatus": "NOT_IMPLEMENTED",
        }
    if action == "GetCurrentTransportActions":
        return {"Actions": "Play,Pause,Stop,Seek"}
    if action == "GetDeviceCapabilities":
        return {
            "PlayMedia": "NETWORK",
            "RecMedia": "NOT_IMPLEMENTED",
            "RecQualityModes": "NOT_IMPLEMENTED",
        }
    if action == "GetTransportSettings":
        return {"PlayMode": "NORMAL", "RecQualityMode": "NOT_IMPLEMENTED"}
    raise SoapError(401, "Invalid Action")


def renderingcontrol_action(action, args):
    rt = RT
    if action == "GetVolume":
        return {"CurrentVolume": str(rt.vlc.status()["volume"])}
    if action == "SetVolume":
        try:
            volume = int(args.get("DesiredVolume", "100"))
        except ValueError:
            raise SoapError(402, "Invalid Args")
        rt.vlc.ensure_running()
        rt.vlc.set_volume(volume)
        rt.state.mute = volume == 0
        rt.events.notify("RenderingControl", {"Volume": volume})
        return {}
    if action == "GetMute":
        return {"CurrentMute": "1" if rt.state.mute else "0"}
    if action == "SetMute":
        desired = args.get("DesiredMute", "0") in ("1", "true", "True")
        if desired and not rt.state.mute:
            rt.state.premute_volume = rt.vlc.status()["volume"]
            rt.vlc.set_volume(0)
        elif not desired and rt.state.mute:
            rt.vlc.set_volume(rt.state.premute_volume or 100)
        rt.state.mute = desired
        rt.events.notify("RenderingControl", {"Mute": int(desired)})
        return {}
    raise SoapError(401, "Invalid Action")


def connectionmanager_action(action, args):
    if action == "GetProtocolInfo":
        return {"Source": "", "Sink": SINK_PROTOCOL_INFO}
    if action == "GetCurrentConnectionIDs":
        return {"ConnectionIDs": "0"}
    if action == "GetCurrentConnectionInfo":
        return {
            "RcsID": "0",
            "AVTransportID": "0",
            "ProtocolInfo": "",
            "PeerConnectionManager": "",
            "PeerConnectionID": "-1",
            "Direction": "Input",
            "Status": "OK",
        }
    raise SoapError(401, "Invalid Action")


SERVICE_HANDLERS = {
    "AVTransport": avtransport_action,
    "RenderingControl": renderingcontrol_action,
    "ConnectionManager": connectionmanager_action,
}


# ---------------------------------------------------------------- XML documents

def description_xml(rt):
    services = "".join(
        f"""
   <service>
    <serviceType>{stype}</serviceType>
    <serviceId>urn:upnp-org:serviceId:{name}</serviceId>
    <SCPDURL>/scpd/{name}.xml</SCPDURL>
    <controlURL>/control/{name}</controlURL>
    <eventSubURL>/event/{name}</eventSubURL>
   </service>"""
        for name, stype in SERVICE_TYPES.items()
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <specVersion><major>1</major><minor>0</minor></specVersion>
 <device>
  <deviceType>{DEVICE_TYPE}</deviceType>
  <friendlyName>{escape(rt.name)}</friendlyName>
  <manufacturer>dlnacast</manufacturer>
  <modelDescription>Minimal DLNA renderer for macOS (VLC backend)</modelDescription>
  <modelName>dlnacast</modelName>
  <modelNumber>{VERSION}</modelNumber>
  <UDN>{rt.udn}</UDN>
  <dlna:X_DLNADOC xmlns:dlna="urn:schemas-dlna-org:device-1-0">DMR-1.50</dlna:X_DLNADOC>
  <serviceList>{services}
  </serviceList>
 </device>
</root>
"""


def _state_var(name, dtype="string", send_events=False, allowed=None):
    events = "yes" if send_events else "no"
    allowed_xml = ""
    if allowed:
        values = "".join(f"<allowedValue>{v}</allowedValue>" for v in allowed)
        allowed_xml = f"<allowedValueList>{values}</allowedValueList>"
    return (
        f'<stateVariable sendEvents="{events}"><name>{name}</name>'
        f"<dataType>{dtype}</dataType>{allowed_xml}</stateVariable>"
    )


def _action(name, in_args=(), out_args=()):
    arguments = "".join(
        f"<argument><name>{arg}</name><direction>{direction}</direction>"
        f"<relatedStateVariable>{var}</relatedStateVariable></argument>"
        for arg, var, direction in (
            [(a, v, "in") for a, v in in_args] + [(a, v, "out") for a, v in out_args]
        )
    )
    body = f"<argumentList>{arguments}</argumentList>" if arguments else ""
    return f"<action><name>{name}</name>{body}</action>"


def scpd_avtransport():
    iid = ("InstanceID", "A_ARG_TYPE_InstanceID")
    actions = "".join([
        _action("SetAVTransportURI", [iid, ("CurrentURI", "AVTransportURI"),
                                      ("CurrentURIMetaData", "AVTransportURIMetaData")]),
        _action("Play", [iid, ("Speed", "TransportPlaySpeed")]),
        _action("Pause", [iid]),
        _action("Stop", [iid]),
        _action("Seek", [iid, ("Unit", "A_ARG_TYPE_SeekMode"),
                         ("Target", "A_ARG_TYPE_SeekTarget")]),
        _action("GetTransportInfo", [iid], [
            ("CurrentTransportState", "TransportState"),
            ("CurrentTransportStatus", "TransportStatus"),
            ("CurrentSpeed", "TransportPlaySpeed")]),
        _action("GetPositionInfo", [iid], [
            ("Track", "CurrentTrack"),
            ("TrackDuration", "CurrentTrackDuration"),
            ("TrackMetaData", "CurrentTrackMetaData"),
            ("TrackURI", "CurrentTrackURI"),
            ("RelTime", "RelativeTimePosition"),
            ("AbsTime", "AbsoluteTimePosition"),
            ("RelCount", "RelativeCounterPosition"),
            ("AbsCount", "AbsoluteCounterPosition")]),
        _action("GetMediaInfo", [iid], [
            ("NrTracks", "NumberOfTracks"),
            ("MediaDuration", "CurrentMediaDuration"),
            ("CurrentURI", "AVTransportURI"),
            ("CurrentURIMetaData", "AVTransportURIMetaData"),
            ("NextURI", "NextAVTransportURI"),
            ("NextURIMetaData", "NextAVTransportURIMetaData"),
            ("PlayMedium", "PlaybackStorageMedium"),
            ("RecordMedium", "RecordStorageMedium"),
            ("WriteStatus", "RecordMediumWriteStatus")]),
        _action("GetCurrentTransportActions", [iid],
                [("Actions", "CurrentTransportActions")]),
        _action("GetDeviceCapabilities", [iid], [
            ("PlayMedia", "PossiblePlaybackStorageMedia"),
            ("RecMedia", "PossibleRecordStorageMedia"),
            ("RecQualityModes", "PossibleRecordQualityModes")]),
        _action("GetTransportSettings", [iid], [
            ("PlayMode", "CurrentPlayMode"),
            ("RecQualityMode", "CurrentRecordQualityMode")]),
    ])
    variables = "".join([
        _state_var("TransportState", allowed=[
            "STOPPED", "PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING",
            "NO_MEDIA_PRESENT"]),
        _state_var("TransportStatus", allowed=["OK", "ERROR_OCCURRED"]),
        _state_var("TransportPlaySpeed"),
        _state_var("NumberOfTracks", "ui4"),
        _state_var("CurrentTrack", "ui4"),
        _state_var("CurrentTrackDuration"),
        _state_var("CurrentMediaDuration"),
        _state_var("CurrentTrackMetaData"),
        _state_var("CurrentTrackURI"),
        _state_var("AVTransportURI"),
        _state_var("AVTransportURIMetaData"),
        _state_var("NextAVTransportURI"),
        _state_var("NextAVTransportURIMetaData"),
        _state_var("PlaybackStorageMedium"),
        _state_var("RecordStorageMedium"),
        _state_var("RecordMediumWriteStatus"),
        _state_var("PossiblePlaybackStorageMedia"),
        _state_var("PossibleRecordStorageMedia"),
        _state_var("PossibleRecordQualityModes"),
        _state_var("CurrentPlayMode"),
        _state_var("CurrentRecordQualityMode"),
        _state_var("CurrentTransportActions"),
        _state_var("RelativeTimePosition"),
        _state_var("AbsoluteTimePosition"),
        _state_var("RelativeCounterPosition", "i4"),
        _state_var("AbsoluteCounterPosition", "i4"),
        _state_var("A_ARG_TYPE_SeekMode", allowed=["REL_TIME", "ABS_TIME", "TRACK_NR"]),
        _state_var("A_ARG_TYPE_SeekTarget"),
        _state_var("A_ARG_TYPE_InstanceID", "ui4"),
        _state_var("LastChange", send_events=True),
    ])
    return _scpd(actions, variables)


def scpd_renderingcontrol():
    iid = ("InstanceID", "A_ARG_TYPE_InstanceID")
    channel = ("Channel", "A_ARG_TYPE_Channel")
    actions = "".join([
        _action("GetVolume", [iid, channel], [("CurrentVolume", "Volume")]),
        _action("SetVolume", [iid, channel, ("DesiredVolume", "Volume")]),
        _action("GetMute", [iid, channel], [("CurrentMute", "Mute")]),
        _action("SetMute", [iid, channel, ("DesiredMute", "Mute")]),
    ])
    variables = "".join([
        _state_var("Volume", "ui2"),
        _state_var("Mute", "boolean"),
        _state_var("A_ARG_TYPE_InstanceID", "ui4"),
        _state_var("A_ARG_TYPE_Channel", allowed=["Master"]),
        _state_var("LastChange", send_events=True),
    ])
    return _scpd(actions, variables)


def scpd_connectionmanager():
    actions = "".join([
        _action("GetProtocolInfo", [], [
            ("Source", "SourceProtocolInfo"), ("Sink", "SinkProtocolInfo")]),
        _action("GetCurrentConnectionIDs", [],
                [("ConnectionIDs", "CurrentConnectionIDs")]),
        _action("GetCurrentConnectionInfo",
                [("ConnectionID", "A_ARG_TYPE_ConnectionID")], [
            ("RcsID", "A_ARG_TYPE_RcsID"),
            ("AVTransportID", "A_ARG_TYPE_AVTransportID"),
            ("ProtocolInfo", "A_ARG_TYPE_ProtocolInfo"),
            ("PeerConnectionManager", "A_ARG_TYPE_ConnectionManager"),
            ("PeerConnectionID", "A_ARG_TYPE_ConnectionID"),
            ("Direction", "A_ARG_TYPE_Direction"),
            ("Status", "A_ARG_TYPE_ConnectionStatus")]),
    ])
    variables = "".join([
        _state_var("SourceProtocolInfo", send_events=True),
        _state_var("SinkProtocolInfo", send_events=True),
        _state_var("CurrentConnectionIDs", send_events=True),
        _state_var("A_ARG_TYPE_ConnectionID", "i4"),
        _state_var("A_ARG_TYPE_RcsID", "i4"),
        _state_var("A_ARG_TYPE_AVTransportID", "i4"),
        _state_var("A_ARG_TYPE_ProtocolInfo"),
        _state_var("A_ARG_TYPE_ConnectionManager"),
        _state_var("A_ARG_TYPE_Direction", allowed=["Input", "Output"]),
        _state_var("A_ARG_TYPE_ConnectionStatus", allowed=[
            "OK", "ContentFormatMismatch", "InsufficientBandwidth",
            "UnreliableChannel", "Unknown"]),
    ])
    return _scpd(actions, variables)


def _scpd(actions, variables):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion>"
        f"<actionList>{actions}</actionList>"
        f"<serviceStateTable>{variables}</serviceStateTable>"
        "</scpd>\n"
    )


SCPD_BUILDERS = {
    "AVTransport": scpd_avtransport,
    "RenderingControl": scpd_renderingcontrol,
    "ConnectionManager": scpd_connectionmanager,
}


# ---------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = f"dlnacast/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(f"http {self.address_string()} {fmt % args}")

    def _send(self, code, body=b"", ctype='text/xml; charset="utf-8"', headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/description.xml"):
            self._send(200, description_xml(RT))
            return
        if path.startswith("/scpd/") and path.endswith(".xml"):
            name = path[len("/scpd/"):-len(".xml")]
            builder = SCPD_BUILDERS.get(name)
            if builder:
                self._send(200, builder())
                return
        self._send(404, "Not Found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        if not path.startswith("/control/"):
            self._send(404, "Not Found", "text/plain")
            return
        service = path[len("/control/"):]
        handler = SERVICE_HANDLERS.get(service)
        if handler is None:
            self._send(404, "Not Found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            action, args = parse_soap(body)
        except ET.ParseError:
            self._send_fault(SoapError(402, "Invalid Args"))
            return
        if action is None:
            self._send_fault(SoapError(402, "Invalid Args"))
            return
        log(f"SOAP {service}#{action}")
        try:
            out_args = handler(action, args)
        except SoapError as exc:
            self._send_fault(exc)
            return
        except Exception as exc:  # VLC unreachable, etc.
            log(f"action {action} failed: {exc}")
            self._send_fault(SoapError(501, "Action Failed"))
            return
        stype = SERVICE_TYPES[service]
        payload = "".join(
            f"<{k}>{escape(str(v))}</{k}>" for k, v in out_args.items()
        )
        response = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action}Response xmlns:u="{stype}">{payload}'
            f"</u:{action}Response></s:Body></s:Envelope>"
        )
        self._send(200, response, headers={"EXT": ""})

    def _send_fault(self, error):
        body = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body><s:Fault>"
            "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>"
            "<detail><UPnPError xmlns=\"urn:schemas-upnp-org:control-1-0\">"
            f"<errorCode>{error.code}</errorCode>"
            f"<errorDescription>{escape(error.desc)}</errorDescription>"
            "</UPnPError></detail>"
            "</s:Fault></s:Body></s:Envelope>"
        )
        self._send(500, body)

    def do_SUBSCRIBE(self):
        path = self.path.split("?")[0]
        if not path.startswith("/event/"):
            self._send(404, "", "text/plain")
            return
        service = path[len("/event/"):]
        if service not in SERVICE_TYPES:
            self._send(404, "", "text/plain")
            return
        sid = self.headers.get("SID")
        if sid:
            if RT.events.renew(sid):
                self._send(200, headers={"SID": sid, "TIMEOUT": "Second-1800"})
            else:
                self._send(412, "", "text/plain")
            return
        callback = self.headers.get("CALLBACK", "")
        url = callback.strip("<> \t")
        if not url.startswith("http"):
            self._send(412, "", "text/plain")
            return
        sid = RT.events.subscribe(service, url)
        self._send(200, headers={"SID": sid, "TIMEOUT": "Second-1800"})
        if service in EVENT_NS:
            threading.Timer(
                0.3, RT.events.notify_initial, args=(service, sid)
            ).start()

    def do_UNSUBSCRIBE(self):
        sid = self.headers.get("SID")
        if sid:
            RT.events.unsubscribe(sid)
        self._send(200)


# ---------------------------------------------------------------- SSDP

class SSDP:
    def __init__(self, rt):
        self.rt = rt
        self.stop_event = threading.Event()
        self.targets = ssdp_targets(rt.udn)
        self.notify_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.notify_sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        # macOS local-network filtering can make multicast sendto block
        # forever; a timeout keeps announce/shutdown from hanging.
        self.notify_sock.settimeout(0.5)

    def start(self):
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._announce_loop, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        # byebye is best-effort; never let a stalled multicast send block
        # shutdown (the GUI Stop button waits on this).
        threading.Thread(
            target=self._notify_all, args=("ssdp:byebye",), daemon=True,
        ).start()

    def _listen(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", SSDP_PORT))
        mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(self.rt.ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(1)
        while not self.stop_event.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle(data, addr, sock)
        sock.close()

    def _handle(self, data, addr, sock):
        try:
            text = data.decode(errors="replace")
        except Exception:
            return
        if not text.startswith("M-SEARCH"):
            return
        headers = {}
        for line in text.split("\r\n")[1:]:
            if ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip().upper()] = value.strip()
        st = headers.get("ST", "")
        if headers.get("MAN", "").strip('"') != "ssdp:discover":
            return
        matches = []
        if st == "ssdp:all":
            matches = self.targets
        else:
            matches = [(t, usn) for t, usn in self.targets if t == st]
        for target, usn in matches:
            response = build_ssdp_response(target, usn, self.rt.location)
            try:
                sock.sendto(response.encode(), addr)
            except OSError:
                pass
        if matches:
            log(f"SSDP M-SEARCH {st} from {addr[0]} -> {len(matches)} responses")

    def _announce_loop(self):
        while not self.stop_event.is_set():
            self._notify_all("ssdp:alive")
            self.stop_event.wait(60)

    def _notify_all(self, nts):
        for target, usn in self.targets:
            message = build_ssdp_notify(target, usn, self.rt.location, nts)
            try:
                self.notify_sock.sendto(
                    message.encode(), (SSDP_ADDR, SSDP_PORT))
            except OSError:
                pass


# ---------------------------------------------------------------- service

class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Dropped/filtered connections are routine on a LAN; keep the
        # console clean instead of dumping a traceback per connection.
        exc = sys.exception()
        log(f"connection from {client_address[0]} aborted: {exc}")


class RendererService:
    """Start/stop the whole renderer (HTTP + SSDP + VLC) programmatically."""

    def __init__(self, name=None, port=8895, vlc_port=8090):
        self.name = name
        self.port = port
        self.vlc_port = vlc_port
        self.runtime = None
        self.server = None
        self.ssdp = None
        self.vlc = None
        self.running = False
        self.lock = threading.Lock()

    def start(self):
        global RT
        with self.lock:
            if self.running:
                return self.runtime
            vlc = VLC(self.vlc_port, "dlnacast")
            if not vlc.available():
                raise RuntimeError(
                    "VLC.app not found in /Applications — install VLC first")
            try:
                ip = get_local_ip()
            except OSError:
                raise RuntimeError("no network connection detected")
            name = self.name or f"dlnacast ({socket.gethostname().split('.')[0]})"
            rt = Runtime(name, ip, self.port,
                         load_udn(os.path.expanduser("~/.dlnacast-uuid")), vlc)
            RT = rt
            server = QuietServer(("", self.port), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            ssdp = SSDP(rt)
            ssdp.start()
            self.vlc, self.server, self.ssdp, self.runtime = vlc, server, ssdp, rt
            self.running = True
            return rt

    def stop(self):
        with self.lock:
            if not self.running:
                return
            self.ssdp.stop()
            self.server.shutdown()
            self.server.server_close()
            self.vlc.quit()
            self.running = False


# ---------------------------------------------------------------- main

def main():
    global VERBOSE
    parser = argparse.ArgumentParser(
        description="Minimal DLNA renderer for macOS (VLC backend)")
    parser.add_argument("--name", default=None,
                        help="friendly name shown on the phone")
    parser.add_argument("--port", type=int, default=8895,
                        help="HTTP port for the UPnP server (default 8895)")
    parser.add_argument("--vlc-port", type=int, default=8090,
                        help="local port for VLC's HTTP interface (default 8090)")
    parser.add_argument("-v", "--verbose", action="store_true")
    options = parser.parse_args()
    VERBOSE = options.verbose

    service = RendererService(options.name, options.port, options.vlc_port)
    try:
        rt = service.start()
    except RuntimeError as exc:
        sys.exit(f"error: {exc}")

    print(f"""\
▶ dlnacast {VERSION} is running
  Device name : {rt.name}
  Listening   : http://{rt.ip}:{rt.http_port}
  Player      : VLC (starts automatically on first cast)

  On your phone, open a DLNA/cast-capable app and pick "{rt.name}".
  Press Ctrl-C to stop.""")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nshutting down…")
    finally:
        service.stop()


if __name__ == "__main__":
    main()
