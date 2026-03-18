"""Device family plugins for smart home device control.

Each adapter implements the IJarvisDeviceProtocol interface for a specific
manufacturer's device API — LAN (LIFX, Kasa), cloud (Govee, Schlage),
or hybrid. Families are auto-discovered at startup via pkgutil.
"""
