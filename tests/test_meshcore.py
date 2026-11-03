import base64
import copy
import hashlib
import hmac
import json
import socket
import struct
import threading
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from meshtastic.protobuf import mesh_pb2, mqtt_pb2, portnums_pb2

import main
from m2a_aprs import M2AAPRS
from m2a_config import Config, node_type
from m2a_meshcore import M2AMeshCore, decode_telemetry
from m2a_meshtastic import M2AMeshtastic
from m2a_nodedb import NodeDB


KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
PUBLIC_KEY = "12" * 32
NOW = 1804140000
BODY = "#TEL:68yUiAsPFKf9/gE"


def config():
    return {
        "meshcore": {"channel": "team-sar", "key": KEY.hex(),
                     "mqtt": {"host": "localhost", "topic": "meshcore/#",
                              "username": "test", "password": "test"}},
        "nodes": {PUBLIC_KEY: {"type": "meshcore", "callsign": "ZR1RF-16",
                               "sender_name": "ZR1RF 📟", "symbol": "/>"}},
        "update_interval": 10,
    }


def packet(body=BODY, sender="ZR1RF 📟", timestamp=NOW, route=1,
           path=b"", width=1, key=KEY, flags=0):
    plaintext = struct.pack("<IB", timestamp, flags) + f"{sender}: {body}".encode()
    plaintext += b"\0" * (-len(plaintext) % 16)
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    payload = hashlib.sha256(key).digest()[:1]
    payload += hmac.new(key, ciphertext, hashlib.sha256).digest()[:2] + ciphertext
    transport = b"\x11\x22\x33\x44" if route in (0, 3) else b""
    return bytes([(5 << 2) | route]) + transport + bytes([
        ((width - 1) << 6) | (len(path) // width)]) + path + payload


def message(raw=None, retain=False, **fields):
    raw = raw if raw is not None else packet()
    payload = {"type": "PACKET", "direction": "rx", "raw": raw.hex() if isinstance(raw, bytes) else raw,
               "SNR": "4.5", "RSSI": "-93", "origin_id": "observer-key"}
    payload.update(fields)
    return SimpleNamespace(payload=json.dumps(payload).encode(), retain=retain,
                           topic="meshcore/CPT/observer-key/packets")


class TelemetryTests(unittest.TestCase):
    def test_documented_team_vector(self):
        data = decode_telemetry(BODY)
        self.assertEqual(data["latitude"], -33.8914168)
        self.assertEqual(data["longitude"], 18.5537703)
        self.assertEqual(data["companion_battery_mv"], 4256)
        self.assertEqual(data["phone_battery_mv"], 4262)
        self.assertEqual(data["voltage"], 4.256)
        self.assertFalse(data["needs_forwarding"])
        self.assertEqual(data["max_path_observed"], 0)

    def test_zero_coordinates_unavailable_battery_and_autonomous(self):
        raw = struct.pack(">iiBBB", 0, 0, 1, 255, 255)
        data = decode_telemetry("#TEL:" + base64.b64encode(raw).decode())
        self.assertEqual(data["latitude"], 0)
        self.assertEqual(data["longitude"], 0)
        self.assertIsNone(data["voltage"])
        self.assertIsNone(data["phone_battery_mv"])
        self.assertTrue(data["autonomous"])
        self.assertEqual(data["max_path_observed"], 127)

    def test_forwarding_byte_wrap(self):
        raw = struct.pack(">iiBBB", 0, 0, 0, 0, 0)
        data = decode_telemetry("#TEL:" + base64.b64encode(raw).decode())
        self.assertTrue(data["needs_forwarding"])
        self.assertEqual(data["max_path_observed"], 127)

    def test_topology(self):
        raw = struct.pack(">iiBBB", -338914168, 185537703, 253, 254, 9) + b"\x01\x80"
        data = decode_telemetry("#T:" + base64.b64encode(raw).decode().rstrip("="))
        self.assertEqual(data["node_count"], 9)
        self.assertEqual(data["neighbor_bitmap"], "0180")
        self.assertEqual(data["longitude"], 18.5537703)

    def test_malformed_telemetry(self):
        invalid = ["", "#TEL:!!!!", "#TEL:A", "#TEL:AA", "#T:", "#CAP:AAAA"]
        for lat, lon in ((900000001, 0), (0, -1800000001)):
            invalid.append("#TEL:" + base64.b64encode(struct.pack(">iiBBB", lat, lon, 1, 1, 1)).decode())
        for raw in (b"\0" * 12,):
            invalid.append("#TEL:" + base64.b64encode(raw).decode())
        for count in (1, 9, 255):
            invalid.append("#T:" + base64.b64encode(b"\0" * 10 + bytes([count])).decode())
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ValueError):
                decode_telemetry(body)


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.client_patch = patch("m2a_meshcore.mqtt.Client")
        self.client = self.client_patch.start().return_value
        self.addCleanup(self.client_patch.stop)
        self.cfg = config()
        self.callback = Mock()
        self.receiver = M2AMeshCore(self.callback, self.cfg)
        self.addCleanup(self.receiver.close)
        self.time_patch = patch("m2a_meshcore.time.time", return_value=NOW)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    def test_connection_subscription_and_close(self):
        self.client.username_pw_set.assert_called_once_with("test", "test")
        self.client.connect.assert_called_once_with("localhost", 1883, keepalive=60)
        self.client.loop_start.assert_called_once()
        self.receiver.on_connect(self.client, None, None, 0)
        self.client.subscribe.assert_called_once_with("meshcore/#")
        self.receiver.close()
        self.client.disconnect.assert_called_once()
        self.client.loop_stop.assert_called_once()

    def test_connection_failure_cleans_up(self):
        self.client.connect.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            M2AMeshCore(self.callback, self.cfg)
        self.client.disconnect.assert_called_once()

    def test_all_routes_and_path_widths(self):
        for route in range(4):
            for width in (1, 2, 3):
                with self.subTest(route=route, width=width):
                    data = self.receiver.decode_packet(packet(route=route, path=b"\xab" * width * 2, width=width))
                    self.assertEqual(data["hops"], 2)
                    self.assertEqual(data["node_id"], PUBLIC_KEY)
                    self.assertEqual(data["channel"], "team-sar")

    def test_decodes_rx_and_uses_configured_identity(self):
        self.receiver.on_message(None, None, message())
        data = self.callback.call_args.args[0]
        self.assertEqual(data["node_id"], PUBLIC_KEY)
        self.assertEqual(data["rx_rssi"], -93)
        self.assertEqual(data["rx_snr"], 4.5)
        self.assertEqual(data["received_at"], NOW)

    def test_rejects_wrong_key_channel_mac_and_sender(self):
        changed_mac = bytearray(packet())
        changed_mac[3] ^= 1
        for raw in (packet(key=b"x" * 16), bytes(changed_mac),
                    packet(sender="Unknown"), packet(body="hello"), packet(flags=8)):
            self.receiver.on_message(None, None, message(raw))
        self.callback.assert_not_called()

    def test_rejects_stale_future_tx_and_retained(self):
        for msg in (message(packet(timestamp=NOW-301)), message(packet(timestamp=NOW+61)),
                    message(direction="tx"), message(retain=True)):
            self.receiver.on_message(None, None, msg)
        self.callback.assert_not_called()

    def test_duplicate_paths_observers_and_fresh_stationary_positions(self):
        self.receiver.on_message(None, None, message())
        self.receiver.on_message(None, None, message(packet(path=b"\xab\xcd"), origin_id="other"))
        self.callback.assert_called_once()
        self.receiver.on_message(None, None, message(packet(timestamp=NOW+1)))
        self.assertEqual(self.callback.call_count, 2)

    def test_dedupe_expiry_and_bound(self):
        with patch("m2a_meshcore.time.monotonic", return_value=1):
            self.receiver.on_message(None, None, message())
        with patch("m2a_meshcore.time.monotonic", return_value=302):
            self.receiver.on_message(None, None, message())
        self.assertEqual(self.callback.call_count, 2)
        self.receiver.seen.clear()
        for i in range(4096):
            self.receiver.seen[(i,)] = 302
        with patch("m2a_meshcore.time.monotonic", return_value=302):
            self.receiver.on_message(None, None, message())
        self.assertEqual(len(self.receiver.seen), 4096)

    def test_bad_input_does_not_kill_receiver(self):
        for payload in (b"broken", b"[]", b"null", b"42", b'"text"', b"\xff"):
            self.receiver.on_message(None, None, SimpleNamespace(payload=payload, retain=False))
        for raw in (b"\x15", b"\x15\xc0", b"\x15\x3f", b"\x15\x00\x00", packet()[:-1]):
            self.receiver.on_message(None, None, message(raw))
        for fields in ({"raw": "xyz"}, {"SNR": "nan"}, {"RSSI": {}}, {"direction": 123}):
            self.receiver.on_message(None, None, message(**fields))
        self.callback.assert_not_called()
        self.receiver.on_message(None, None, message())
        self.callback.assert_called_once()

    def test_missing_battery_does_not_break_aprs(self):
        raw = struct.pack(">iiBBB", 0, 0, 1, 1, 1)
        body = "#TEL:" + base64.b64encode(raw).decode()
        self.receiver.on_message(None, None, message(packet(body=body)))
        self.assertIsNone(self.callback.call_args.args[0]["voltage"])

    def test_invalid_config(self):
        for section, key, value in (("meshcore", "key", ""), ("meshcore", "channel", ""),
                                    ("meshcore", "max_message_age", -1),
                                    ("meshcore", "dedupe_seconds", float("nan"))):
            cfg = copy.deepcopy(self.cfg)
            cfg[section][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                M2AMeshCore(self.callback, cfg)
        cfg = copy.deepcopy(self.cfg)
        cfg["nodes"]["34"*32] = cfg["nodes"][PUBLIC_KEY].copy()
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            M2AMeshCore(self.callback, cfg)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.db = NodeDB.__new__(NodeDB)
        self.db.nodes = {}
        self.db.save = Mock()
        self.cfg = config()
        self.aprs = M2AAPRS.__new__(M2AAPRS)
        self.aprs.send_packet = Mock(return_value=True)
        for attr, value in (("conf", self.cfg), ("nodedb", self.db), ("aprs", self.aprs)):
            p = patch.object(main, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def test_meshcore_mqtt_to_aprs_and_rate_limit(self):
        with patch("m2a_meshcore.mqtt.Client"), patch("time.time", return_value=NOW):
            receiver = M2AMeshCore(partial(main.on_mesh_received, mesh_type="meshcore"), self.cfg)
            receiver.on_message(None, None, message())
            receiver.on_message(None, None, message(packet(timestamp=NOW+1)))
            receiver.close()
        self.aprs.send_packet.assert_called_once()
        self.assertEqual(self.aprs.send_packet.call_args.args[0],
                         "ZR1RF-16>APM2A0,TCPIP*:!3353.49S/01833.23E>RSSI:-93.0 SNR:4.5 Bat:4.26v")
        self.assertEqual(self.db.nodes[PUBLIC_KEY]["latitude"], -33.8914168)

    def test_cross_transport_identity_is_rejected(self):
        data = {**decode_telemetry(BODY), "node_id": PUBLIC_KEY}
        main.on_mesh_received(data, mesh_type="meshtastic")
        self.aprs.send_packet.assert_not_called()
        self.assertEqual(self.db.nodes, {})

    def test_unknown_sender_is_rejected(self):
        main.on_mesh_received({**decode_telemetry(BODY), "node_id": "unknown"}, "meshcore")
        self.aprs.send_packet.assert_not_called()

    def test_meshtastic_protobuf_regression(self):
        self.cfg["nodes"]["12345678"] = {"type": "meshtatic", "callsign": "ZR1RF-14"}
        receiver = M2AMeshtastic.__new__(M2AMeshtastic)
        receiver.config = self.cfg
        receiver.on_receive_callback = main.on_mesh_received
        pos = mesh_pb2.Position(latitude_i=-338914168, longitude_i=185537703,
                                altitude=20, sats_in_view=8)
        envelope = mqtt_pb2.ServiceEnvelope()
        setattr(envelope.packet, "from", 0x12345678)
        envelope.packet.decoded.portnum = portnums_pb2.POSITION_APP
        envelope.packet.decoded.payload = pos.SerializeToString()
        receiver.on_message(None, None, SimpleNamespace(payload=envelope.SerializeToString()))
        self.aprs.send_packet.assert_called_once()
        self.assertTrue(self.aprs.send_packet.call_args.args[0].startswith("ZR1RF-14>"))
        self.assertEqual(self.db.nodes["12345678"]["sats"], 8)

    def test_zero_coordinate_position_with_unknown_battery(self):
        main.on_mesh_received({"type": "position", "node_id": PUBLIC_KEY,
                               "latitude": 0, "longitude": 0, "voltage": None}, "meshcore")
        self.aprs.send_packet.assert_called_once()
        self.assertIn("0000.00N/00000.00E>", self.aprs.send_packet.call_args.args[0])

    def test_type_defaults_and_config_false_values(self):
        self.assertEqual(node_type({}), "meshtastic")
        self.assertEqual(node_type({"type": "meshtatic"}), "meshtastic")
        with self.assertRaises(ValueError):
            node_type({"type": "typo"})
        cfg = Config.__new__(Config)
        cfg.config_dict = {"enabled": False, "interval": 0}
        self.assertFalse(cfg.get("enabled"))
        self.assertEqual(cfg.get("interval"), 0)
        self.assertEqual(cfg.get("missing", {}), {})

    def test_startup_orders_aprs_before_mqtt_and_closes_both(self):
        self.cfg["nodes"]["12345678"] = {"type": "meshtatic"}
        events = []
        meshtastic = Mock()
        meshcore = Mock()
        with patch.object(main, "Config", return_value=self.cfg), \
             patch.object(main, "NodeDB", return_value=self.db), \
             patch.object(main.aprsis, "M2AAPRS", side_effect=lambda: events.append("aprs") or self.aprs), \
             patch.object(main.meshtastic, "M2AMeshtastic", side_effect=lambda cb: events.append("meshtastic") or meshtastic), \
             patch.object(main.meshcore, "M2AMeshCore", side_effect=lambda cb, config: events.append("meshcore") or meshcore), \
             patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
            main.main()
        self.assertEqual(events, ["aprs", "meshtastic", "meshcore"])
        meshtastic.close.assert_called_once()
        meshcore.close.assert_called_once()

    def test_real_mqtt_wire_to_aprs_socket(self):
        """Use real paho and APRS sendall against local sockets, with no RF/APRS publication."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        self.addCleanup(listener.close)
        self.cfg["meshcore"]["mqtt"]["host"] = "127.0.0.1"
        self.cfg["meshcore"]["mqtt"]["port"] = listener.getsockname()[1]
        finished = threading.Event()
        errors = []

        def read_exact(conn, count):
            result = b""
            while len(result) < count:
                chunk = conn.recv(count-len(result))
                if not chunk:
                    raise EOFError("MQTT client disconnected")
                result += chunk
            return result

        def read_frame(conn):
            header = read_exact(conn, 1)[0]
            length, multiplier = 0, 1
            while True:
                byte = read_exact(conn, 1)[0]
                length += (byte & 127) * multiplier
                if byte < 128:
                    break
                multiplier *= 128
            return header, read_exact(conn, length)

        def broker():
            try:
                with listener.accept()[0] as conn:
                    conn.settimeout(5)
                    header, _ = read_frame(conn)
                    self.assertEqual(header, 0x10)  # CONNECT
                    conn.sendall(b"\x20\x02\x00\x00")
                    header, subscribe = read_frame(conn)
                    self.assertEqual(header, 0x82)  # SUBSCRIBE
                    self.assertIn(b"meshcore/#", subscribe)
                    conn.sendall(b"\x90\x03" + subscribe[:2] + b"\x00")
                    topic = b"meshcore/CPT/observer/packets"
                    payload = struct.pack(">H", len(topic)) + topic + message().payload
                    length, remaining = len(payload), bytearray()
                    while True:
                        byte = length % 128
                        length //= 128
                        remaining.append(byte | (128 if length else 0))
                        if not length:
                            break
                    conn.sendall(b"\x30" + remaining + payload)
                    finished.wait(5)
            except Exception as exc:
                errors.append(exc)

        aprs_tx, aprs_rx = socket.socketpair()
        self.addCleanup(aprs_tx.close)
        self.addCleanup(aprs_rx.close)
        aprs_rx.settimeout(5)
        del self.aprs.send_packet  # Restore the real APRS writer.
        self.aprs.aprsis = aprs_tx
        worker = threading.Thread(target=broker, daemon=True)
        worker.start()
        receiver = None
        try:
            with patch("time.time", return_value=NOW):
                receiver = M2AMeshCore(partial(main.on_mesh_received, mesh_type="meshcore"), self.cfg)
                emitted = aprs_rx.recv(1024).decode()
            self.assertTrue(emitted.startswith("ZR1RF-16>APM2A0,TCPIP*:!3353.49S/01833.23E>"))
            self.assertTrue(emitted.endswith("\r\n"))
        finally:
            finished.set()
            if receiver:
                receiver.close()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
