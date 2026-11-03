"""Receive meshcoretomqtt/LetsMesh raw packets and decode TEAM tracking."""

import base64
import hashlib
import hmac
import json
import logging
import math
import re
import struct
import time
from collections import OrderedDict

import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from m2a_config import Config, node_type


WARDRIVING_CHANNEL = "#wardriving"
WARDRIVING_KEY = hashlib.sha256(WARDRIVING_CHANNEL.encode("utf-8")).digest()[:16]
MESHMAPPER_PATTERN = re.compile(
    r"MM:([A-Za-z0-9_-]{10}):([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)",
    re.ASCII,
)


def decode_meshmapper(body):
    """Decode broadcast coordinates; the wire tag is opaque, not a node ID."""
    match = MESHMAPPER_PATTERN.fullmatch(body)
    if match is None:
        raise ValueError("Not a MeshMapper coordinate message")
    tag, lat, lon = match.groups()
    latitude, longitude = float(lat), float(lon)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Coordinates outside geographic bounds")
    return {"type": "position", "latitude": latitude, "longitude": longitude,
            "meshmapper_tag": tag}


def decode_telemetry(body):
    """Decode TEAM beta6 #TEL: or #T: text, independently of transport."""
    prefix, sep, encoded = body.partition(":")
    if not sep or prefix not in ("#TEL", "#T"):
        raise ValueError("Not TEAM tracking telemetry")
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
    if len(raw) < 11:
        raise ValueError("Truncated TEAM telemetry")
    if prefix == "#TEL" and len(raw) != 11:
        raise ValueError("Expected exactly 11 #TEL bytes")
    if prefix == "#T" and len(raw) != 11 + (raw[10] + 7) // 8:
        raise ValueError("Invalid #T neighbor bitmap length")
    lat, lon = struct.unpack_from(">ii", raw)
    latitude, longitude = lat / 1e7, lon / 1e7
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Coordinates outside geographic bounds")

    def voltage(value):
        return None if value <= 1 else 2750 + (value - 2) * 6

    companion = voltage(raw[8])
    autonomous = prefix == "#TEL" and raw[9] == 255
    result = {
        "type": "position",
        "latitude": latitude,
        "longitude": longitude,
        "companion_battery_mv": companion,
        "phone_battery_mv": None if autonomous else voltage(raw[9]),
        "autonomous": autonomous,
        "voltage": companion / 1000 if companion is not None else None,
    }
    if prefix == "#TEL":
        value = raw[10] - 1
        result.update(needs_forwarding=bool(value & 1),
                      max_path_observed=(value >> 1) & 127)
    else:
        result.update(node_count=raw[10], neighbor_bitmap=raw[11:].hex())
    return result


class M2AMeshCore:
    def __init__(self, on_receive_callback=None, config=None):
        if not callable(on_receive_callback):
            raise ValueError("on_receive_callback must be provided")
        self.config = config if config is not None else Config()
        self.on_receive_callback = on_receive_callback
        settings = self.config.get("meshcore", {})
        self.channel = settings.get("channel")
        if not isinstance(self.channel, str) or not self.channel:
            raise ValueError("MeshCore channel must be configured")
        self.key = bytes.fromhex(settings.get("key", ""))
        if len(self.key) != 16:
            raise ValueError("MeshCore channel key must contain 32 hex characters")
        self.channel_hash = hashlib.sha256(self.key).digest()[0]
        # Keep every candidate: different channels can share a one-byte hash.
        self.channels = {self.key: self.channel, WARDRIVING_KEY: WARDRIVING_CHANNEL}
        self.max_age = float(settings.get("max_message_age", 300))
        self.future_tolerance = float(settings.get("future_tolerance", 60))
        self.dedupe_seconds = float(settings.get("dedupe_seconds", 300))
        if any(not math.isfinite(v) or v < 0 for v in
               (self.max_age, self.future_tolerance, self.dedupe_seconds)):
            raise ValueError("MeshCore age/deduplication settings must be finite and nonnegative")
        self.senders = {}
        for public_key, node in self.config.get("nodes", {}).items():
            if node_type(node) != "meshcore":
                continue
            if not 8 <= len(public_key) <= 64 or len(public_key) % 2:
                raise ValueError("MeshCore node keys must be 8–64 hex characters (full key preferred)")
            bytes.fromhex(public_key)
            sender = node.get("sender_name", node.get("callsign"))
            if not isinstance(sender, str) or not sender:
                raise ValueError("MeshCore node requires sender_name or callsign")
            if sender in self.senders:
                raise ValueError(f"Ambiguous MeshCore sender_name: {sender!r}")
            self.senders[sender] = public_key
        self.seen = OrderedDict()
        settings = settings.get("mqtt", {})
        if not settings.get("host") or not settings.get("topic"):
            raise ValueError("MeshCore MQTT host and topic must be configured")
        self.topic = settings["topic"]
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        if settings.get("username"):
            self.client.username_pw_set(settings["username"], settings.get("password"))
        try:
            self.client.connect(settings["host"], settings.get("port", 1883), keepalive=60)
            self.client.loop_start()
        except Exception:
            self.close()
            raise

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            client.subscribe(self.topic)
            logging.info("MeshCore MQTT connected; subscribed to %s", self.topic)
        else:
            logging.error("MeshCore MQTT connection failed: %s", reason_code)

    def decode_packet(self, raw):
        """Authenticate a v1 GRP_TXT packet; observer IDs are never sender IDs."""
        if len(raw) < 2 or raw[0] >> 6 or ((raw[0] >> 2) & 15) != 5:
            return None
        offset = 5 if (raw[0] & 3) in (0, 3) else 1
        if len(raw) <= offset:
            raise ValueError("Missing path metadata")
        path = raw[offset]
        hops, width = path & 63, (path >> 6) + 1
        if width == 4 or hops * width > 64:
            raise ValueError("Invalid MeshCore path")
        payload = raw[offset + 1 + hops * width:]
        if not 19 <= len(payload) <= 184:
            raise ValueError("Invalid group payload length")
        ciphertext = payload[3:]
        if len(ciphertext) % 16:
            raise ValueError("Unaligned group ciphertext")
        channel = None
        for key, name in self.channels.items():
            if payload[0] != hashlib.sha256(key).digest()[0]:
                continue
            expected = hmac.new(key, ciphertext, hashlib.sha256).digest()[:2]
            if hmac.compare_digest(payload[1:3], expected):
                channel = name
                break
        if channel is None:
            return None
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        if plaintext[4] >> 2 != 0:
            return None  # Only plain channel text, no CLI or signed-text variants.
        timestamp = struct.unpack_from("<I", plaintext)[0]
        text = plaintext[5:].split(b"\0", 1)[0].decode("utf-8")
        sender, sep, body = text.partition(": ")
        node_id = self.senders.get(sender)
        if not sep or node_id is None:
            return None
        if channel == WARDRIVING_CHANNEL:
            if not body.startswith("MM:") or body.count(":") == 1:
                return None  # Privacy-mode tokens carry no location.
            position = decode_meshmapper(body)
        else:
            if not body.startswith(("#TEL:", "#T:")):
                return None
            position = decode_telemetry(body)
        return {**position, "node_id": node_id,
                "sender_name": sender, "channel": channel,
                "timestamp": timestamp, "hops": hops,
                "packet_hash": hashlib.sha256(payload).hexdigest()}

    def on_message(self, client, userdata, msg):
        try:
            # Status, adverts and TX echoes must never become tracker positions.
            if msg.retain:
                return
            packet = json.loads(msg.payload)
            if not isinstance(packet, dict) or packet.get("direction", "rx").lower() != "rx":
                return
            raw = packet.get("raw")
            if not isinstance(raw, str):
                return
            data = self.decode_packet(bytes.fromhex(raw))
            if data is None:
                return
            if not data["timestamp"]:
                return
            now = time.time()
            age = now - data["timestamp"]
            if age > self.max_age or age < -self.future_tolerance:
                return
            # The same encrypted body arrives through different RF paths/observers.
            identity = (data["node_id"], data["channel"], data["packet_hash"])
            monotonic_now = time.monotonic()
            while self.seen and next(iter(self.seen.values())) <= monotonic_now - self.dedupe_seconds:
                self.seen.popitem(last=False)
            if identity in self.seen:
                return
            for source, target in (("SNR", "rx_snr"), ("RSSI", "rx_rssi")):
                value = packet.get(source, packet.get(source.lower()))
                if value is not None:
                    value = float(value)
                    if not math.isfinite(value):
                        raise ValueError("Non-finite RF metric")
                    data[target] = value
            data["received_at"] = now
            self.on_receive_callback(data)
            self.seen[identity] = monotonic_now
            if len(self.seen) > 4096:
                self.seen.popitem(last=False)
        except (ValueError, TypeError, AttributeError) as exc:
            logging.debug("Ignoring invalid MeshCore MQTT packet: %s", exc)
        except Exception:
            logging.exception("Unexpected error in MeshCore MQTT callback")
