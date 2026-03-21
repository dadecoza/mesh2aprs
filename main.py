import m2a_meshtastic as meshtastic
import m2a_aprs as aprsis
from m2a_aprs import DEFAULT_SYMBOL
from m2a_config import Config
from m2a_nodedb import NodeDB
import time
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

conf = Config()
nodedb = NodeDB()


def ok_to_tx(node_id):
    node_info = nodedb.get_node(node_id)
    if not node_info:
        return True
    update_interval = conf.get("update_interval", 10)
    last_tx = node_info.get("last_tx", time.time() - (update_interval+1))
    txok = last_tx < time.time() - update_interval
    nodedb.update_node(node_id, {"last_tx": time.time()})
    return txok


def comment_string(node_id):
    node_info = nodedb.get_node(node_id)
    stats = []
    rssi = round(node_info.get("rx_rssi", 0), 2)
    snr = round(node_info.get("rx_snr", 0), 2)
    battery = round(node_info.get("voltage", 0), 2)
    batlvl = node_info.get("battery_level", 0)
    sats = node_info.get("sats", 0)
    utl = round(node_info.get("channel_utilization", 0), 3)
    air = round(node_info.get("air_util_tx", 0), 3)
    if rssi:
        stats.append(f"RSSI:{rssi}")
    if snr:
        stats.append(f"SNR:{snr}")
    if sats:
        stats.append(f"Sats:{sats}")
    if battery:
        stats.append(f"Bat:{battery}v")
    if batlvl:
        stats.append(f"BatLvl:{batlvl}%")
    if air:
        stats.append(f"Air:{air}%")
    if utl:
        stats.append(f"Utl:{utl}%")
    return " ".join(stats)


def update_position_on_aprs(node: dict, data: dict):
    callsign = node.get("callsign", None)
    node_id = data.get("node_id", None)
    if not node_id or not callsign:
        return
    node = data.copy()
    del node["type"]
    nodedb.update_node(node_id, node)
    symbol = node.get("symbol", DEFAULT_SYMBOL)
    lat = data.get("latitude", None)
    lon = data.get("longitude", None)
    alt = data.get("altitude", None)

    if lat is not None and lon is not None and ok_to_tx(node_id):
        comment = comment_string(node_id)
        aprs.send_position_packet(
            callsign=callsign,
            latitude=lat,
            longitude=lon,
            altitude=alt,
            comment=comment,
            symbol=symbol
        )
        logging.info(f"Sent position for node {node_id} ({callsign}) to APRS-IS")


def update_user(callsign: str, data: dict):
    node_id = data.get("node_id", None)
    if not node_id:
        return
    node = data.copy()
    del node["type"]
    long_name = node.get("long_name", None)
    short_name = node.get("short_name", None)
    if long_name and short_name and ok_to_tx(node_id):
        status = f"{long_name} ({short_name})"
        aprs.send_status_packet(callsign=callsign, status=status)
        logging.info(f"Sent status for node {node_id} ({callsign}) to APRS-IS")
    nodedb.update_node(node_id, node)


def update_telemetry(callsign: str, data: dict):
    node_id = data.get("node_id", None)
    if not node_id:
        return
    telemetry = data.copy()
    del telemetry["type"]
    nodedb.update_node(node_id, telemetry)


def on_mesh_received(data):
    type = data.get("type", "Unknown")
    node_id = data.get("node_id", "Unknown")
    node = conf.get("nodes", {}).get(node_id, {})
    if not node:
        return
    callsign = node.get("callsign", None)
    if not callsign:
        return
    if type == "user":
        update_user(callsign, data)
        logging.debug(f"Updated node {node_id} with user data: {data}")
    elif type == "position":
        update_position_on_aprs(node, data)
        logging.debug(f"Received position data for node {node_id}: {data}")
    elif type == "telemetry":
        update_telemetry(callsign, data)
        logging.info(f"Received telemetry data for node {node_id}: {data}")


mesh = meshtastic.M2AMeshtastic(on_mesh_received)
aprs = aprsis.M2AAPRS()

if __name__ == "__main__":
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Bye.")
