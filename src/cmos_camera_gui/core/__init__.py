"""
Vendor-agnostic core: controller, finite state machine, thermal logic.

Nothing in this package may import from the GUI; the GUI and the remote
server are both thin clients of ``CameraController``.
"""
