import json


class Config:
    def __init__(self):
        with open('config.json', 'r') as f:
            config_dict = json.load(f)
        self.config_dict = config_dict

    def get(self, key, default=None):
        val = self.config_dict.get(key, default)
        if not val:
            raise ValueError(f"Config key '{key}' not found and no default provided")
        return val
