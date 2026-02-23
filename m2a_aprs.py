import socket
import logging
import threading
from m2a_config import Config
import time


class M2AAPRS:
    def __init__(self):
        self.connected = False
        self.config = Config()
        self.aprsis = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.callsign = self.config.get("aprs", {}).get("callsign", None)
        if not self.callsign:
            raise ValueError("APRS callsign not specified in config")
        self.passcode = self.aprs_passcode(self.callsign.split("-")[0])  # Remove SSID for passcode calculation
        self.connect()
        self.aprs_thread = threading.Thread(target=self.aprsis_manager, daemon=True)
        self.aprs_thread.start()

    def aprs_passcode(self, callsign: str) -> int:
        callsign = callsign.upper()
        hash_val = 0x73e2  # starting seed

        for i, c in enumerate(callsign):
            if i % 2 == 0:
                hash_val ^= ord(c) << 8
            else:
                hash_val ^= ord(c)

        return hash_val & 0x7fff  # keep only 15 bits

    def send_position_packet(self, callsign: str, latitude: float, longitude: float,
                             comment: str = "") -> str:
        lat_deg = int(abs(latitude))
        lat_min = (abs(latitude) - lat_deg) * 60
        lat_hem = "N" if latitude >= 0 else "S"
        lat_str = f"{lat_deg:02d}{lat_min:05.2f}{lat_hem}"
        lon_deg = int(abs(longitude))
        lon_min = (abs(longitude) - lon_deg) * 60
        lon_hem = "E" if longitude >= 0 else "W"
        lon_str = f"{lon_deg:03d}{lon_min:05.2f}{lon_hem}"
        position = f"!{lat_str}\\{lon_str}a"
        packet = f"{callsign}>APRS,TCPIP*:{position}{comment}"
        logging.debug(f"Constructed APRS packet: {packet}")
        self.send_packet(packet)

    def connect(self):
        self.aprsis.connect((
            self.config.get("aprs", {}).get("host", "localhost"),
            self.config.get("aprs", {}).get("port", 14580)
        ))
        default_filter = "filter a/-21.0/16.45/-35.5/33.5"  # approximate South Africa bbox
        filter_cmd = self.config.get("aprs", {}).get("filter", default_filter)
        credentials = f"user {self.callsign} pass {self.passcode} vers meshtastic2aprs 1.0 {filter_cmd}\r\n"
        logging.debug(f"Logging into APRS-IS with filter: {filter_cmd}")
        self.aprsis.send(credentials.encode())
        f = self.aprsis.makefile('rb')
        for raw in f:
            try:
                line = raw.decode('utf-8', errors='replace')
            except Exception:
                line = raw.decode('latin-1', errors='replace')
            logging.debug(f"APRS-IS response: {line.strip()}")
            if line.startswith("# logresp"):
                logging.info("APRS-IS connected successfully")
                self.connected = True
                break

    def send_packet(self, packet: str):
        """Send a packet to APRS-IS with auto reconnect + retry on failure."""

        for attempt in range(3):  # Try up to 3 times
            try:
                # Check if socket is valid
                if not self.aprsis or self.aprsis.fileno() == -1:
                    raise BrokenPipeError("APRS-IS socket is not connected")

                # Try sending
                self.aprsis.sendall((packet + "\r\n").encode("utf-8"))
                logging.debug(f"Sent packet to APRS-IS: {packet}")
                return True

            except (BrokenPipeError, ConnectionResetError, socket.error) as e:
                logging.error(f"Send failed (attempt {attempt+1}/3): {e}")

                # Try reconnecting
                if hasattr(self, "connect_aprsis"):
                    logging.info("Reconnecting to APRS-IS...")
                    try:
                        self.connect_aprsis()
                        logging.info("Reconnect successful.")
                    except Exception as e2:
                        logging.error(f"Reconnect failed: {e2}")
                        time.sleep(1)
                        continue  # Try again on next loop

                # Short delay before retry
                time.sleep(1)

        logging.error("Giving up: packet could not be sent after 3 attempts.")
        return False

    def aprsis_manager(self):
        """Background manager that ensures the APRS-IS socket is connected and
        prints incoming lines. Reconnects on failure.
        """
        while True:
            try:
                if not self.aprsis or self.aprsis.fileno() == -1:
                    try:
                        self.connect()
                    except Exception as e:
                        logging.error(f"APRS-IS connection failed: {e}")
                        time.sleep(10)
                        continue
                try:
                    f = self.aprsis.makefile('rb')
                    for raw in f:
                        try:
                            line = raw.decode('utf-8', errors='replace')
                        except Exception:
                            line = raw.decode('latin-1', errors='replace')
                        logging.debug(f"APRS-IS: {line.strip()}")
                except Exception as e:
                    logging.error(f"APRS-IS read loop ended: {e}")
                    try:
                        self.aprsis.close()
                    except Exception:
                        pass
                    self.aprsis = None
                    time.sleep(5)
            except Exception as e:
                logging.error(f"APRS manager error: {e}")
                time.sleep(5)
