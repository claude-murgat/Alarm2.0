"""Logique pure : noeud injoignable ou qui ne suit plus le leader (INV-189). RED : stub."""
from datetime import timedelta

NODE_DOWN_AFTER = timedelta(minutes=10)


def decide_node_down(cluster, expected, state, now):
    return (), (), {}
