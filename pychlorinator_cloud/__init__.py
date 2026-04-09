"""Async AstralPool Halo cloud and local connectivity library.

Quick start (recommended — cloud WebSocket):
    from pychlorinator_cloud.websocket_client import HaloWebSocketClient

Legacy (DTLS P2P):
    from pychlorinator_cloud.client import HaloCloudClient
"""

# Lazy imports only — do NOT import at module level.
# Importing eagerly causes select.py shadowing issues when this package
# is installed inside a HA custom_components directory.
