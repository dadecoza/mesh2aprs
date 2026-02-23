import m2a_meshtastic as meshtastic
import m2a_aprs as aprsis
from m2a_config import Config
from m2a_nodedb import NodeDB
import time
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

conf = Config()
nodedb = NodeDB()


def update_position_on_aprs(data):
    node_id = data.get("node_id", None)
    if not node_id:
        return
    callsign = conf.get("nodes", {}).get(node_id, {}).get("callsign", None)
    if not callsign:
        return
    update_interval = conf.get("update_interval", 10)
    seen = time.time() - (update_interval+1)
    lat = data.get("latitude", None)
    lon = data.get("longitude", None)
    comment = f"Meshtastic Node !{node_id}"
    node_info = nodedb.get_node(node_id)
    if node_info:
        hardware = node_info.get("hw_model", None)
        seen = node_info.get("seen", seen)
        if hardware:
            comment += f" | {hardware}"

    if lat is not None and lon is not None and seen < time.time() - update_interval:
        aprs.send_position_packet(
            callsign=callsign,
            latitude=lat,
            longitude=lon,
            comment=comment
        )
        logging.info(f"Sent position for node {node_id} ({callsign}) to APRS-IS")
    nodedb.update_node(node_id, {"seen": time.time()})


def on_mesh_received(data):
    type = data.get("type", "Unknown")
    node_id = data.get("node_id", "Unknown")
    if node_id not in conf.get("nodes", {}):
        return
    if type == "user":
        node = data.copy()
        del node["type"]
        nodedb.update_node(node_id, node)
        logging.debug(f"Updated node {node_id} with user data: {data}")
    elif type == "position":
        update_position_on_aprs(data)
        logging.debug(f"Received position data for node {node_id}: {data}")


mesh = meshtastic.M2AMeshtastic(on_mesh_received)
aprs = aprsis.M2AAPRS()

if __name__ == "__main__":
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Bye.")
