import json


def node_type(node):
    """Keep existing configurations and the original 'meshtatic' spelling working."""
    value = node.get("type", "meshtastic")
    if value == "meshtatic":
        return "meshtastic"
    if value not in ("meshtastic", "meshcore"):
        raise ValueError(f"Unknown node type: {value!r}")
    return value


class Config:
    def __init__(self):
        with open('config.json', 'r') as f:
            config_dict = json.load(f)
        self.config_dict = config_dict

    def get(self, key, default=None):
        return self.config_dict.get(key, default)
