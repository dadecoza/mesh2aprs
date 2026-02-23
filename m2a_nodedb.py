
import json


class NodeDB:
    def __init__(self):
        self.nodes = {}
        try:
            with open("nodes.json", "r") as f:
                self.nodes = json.load(f)
        except FileNotFoundError:
            pass

    def save(self):
        with open("nodes.json", "w") as f:
            json.dump(self.nodes, f, indent=2)

    def update_node(self, node_id, data):
        if node_id not in self.nodes:
            self.nodes[node_id] = {}
        self.nodes[node_id].update(data)
        self.save()

    def get_node(self, node_id):
        return self.nodes.get(node_id, None)
