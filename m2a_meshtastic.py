import paho.mqtt.client as mqtt
import meshtastic.protobuf.mesh_pb2 as mesh_pb2
import meshtastic.protobuf.mqtt_pb2 as mqtt_pb2
import meshtastic.protobuf.portnums_pb2 as portnums_pb2
from google.protobuf.message import DecodeError
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
import base64
import logging
from m2a_config import Config


class M2AMeshtastic:

    def __init__(self, on_receive_callback=None):
        if not on_receive_callback:
            raise ValueError("on_receive_callback must be provided")
        self.config = Config()
        self.client = mqtt.Client()
        username = self.config.get("mqtt", {}).get("username", None)
        password = self.config.get("mqtt", {}).get("password", None)
        if username and password:
            self.client.username_pw_set(username, password)
        self.client.connect(
            self.config.get("mqtt", {}).get("host", "localhost"),
            self.config.get("mqtt", {}).get("port", 1883)
        )
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.on_receive_callback = on_receive_callback
        self.client.loop_start()

    def __del__(self):
        self.client.loop_stop()
        self.client.disconnect()

    def decrypt_packet(self, packet_bytes: bytes):
        envelope = mqtt_pb2.ServiceEnvelope()
        envelope.ParseFromString(packet_bytes)
        packet = envelope.packet
        if not packet.HasField("encrypted"):
            return packet  # Return packet if not encrypted
        key = self.config.get("meshtastic", {}).get("key", None)
        if not key:
            raise ValueError("Meshtastic encryption "
                             "key not specified in config")
        key_bytes = base64.b64decode(key)
        nonce_packet_id = getattr(packet, "id").to_bytes(8, "little")
        nonce_from_node = getattr(packet, "from").to_bytes(8, "little")
        nonce = nonce_packet_id + nonce_from_node
        cipher = Cipher(
            algorithms.AES(key_bytes),
            modes.CTR(nonce),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()
        decrypted_bytes = decryptor.update(
            getattr(packet, "encrypted")
        ) + decryptor.finalize()
        data = mesh_pb2.Data()
        data.ParseFromString(decrypted_bytes)
        packet.decoded.CopyFrom(data)
        return packet

    def decode_position(self, plaintext: bytes) -> dict:
        pos = mesh_pb2.Position()
        try:
            pos.ParseFromString(plaintext)
        except DecodeError as e:
            raise ValueError(f"Error decoding POSITION_APP payload: {e}")
        return {
            "latitude": pos.latitude_i / 1e7,  # Convert from integer to float
            "longitude": pos.longitude_i / 1e7,
            "altitude": pos.altitude,
            "time": pos.time,
            "type": "position"
        }

    def decode_user(self, plaintext: bytes) -> dict:
        user = mesh_pb2.User()
        try:
            user.ParseFromString(plaintext)
        except DecodeError as e:
            raise ValueError(f"Error decoding NODEINFO_APP payload: {e}")

        hw_id = user.hw_model
        try:
            hw_name = mesh_pb2.HardwareModel.Name(hw_id)
        except ValueError:
            hw_name = f"{hw_id}"
        return {
            "long_name": user.long_name,
            "short_name": user.short_name,
            "hw_model": hw_name,
            "type": "user"
        }

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logging.info("MQTT connected successfully")
            topic = self.config.get("mqtt", {}).get("topic", None)
            if not topic:
                raise ValueError("MQTT topic not specified in config")
            client.subscribe(topic)
            logging.debug(f"Subscribed to topic: {topic}")
        else:
            logging.error(f"Connection failed with code {rc}")

    def on_message(self, client, userdata, msg):
        raw_payload = msg.payload  # from paho-mqtt
        try:
            decoded = self.decrypt_packet(raw_payload)
            portnum = getattr(decoded.decoded, "portnum", None)
            node_id = format(getattr(decoded, "from", 0), '08x')
            if portnum == portnums_pb2.POSITION_APP:
                position_data = self.decode_position(decoded.decoded.payload)
                data = position_data.copy()
                data["node_id"] = node_id
                self.on_receive_callback(data)
            elif portnum == portnums_pb2.NODEINFO_APP:
                user_data = self.decode_user(decoded.decoded.payload)
                data = user_data.copy()
                data["node_id"] = node_id
                self.on_receive_callback(data)
        except DecodeError as e:
            pass
        except Exception as e:
            logging.error(f"Unexpected error in on_message: {e}")
