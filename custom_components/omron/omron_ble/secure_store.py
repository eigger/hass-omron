"""Persistence for the application-layer bond key.

The modern-stack cuffs keep a bond of their own, above SMP. A phone btsnoop
of a HEM-7188T1-LEO shows the official app running the ECDH pairing
(``0x70 0x01``) exactly once, when the cuff is in pairing mode, and every
later session skipping straight to ``0x70 0x05`` with the key it derived
then. That key is what makes the host a registered one; without it the only
thing a reconnect can do is ask to pair again, which the cuff refuses outside
pairing mode.

Nothing here touches Home Assistant: the driver takes a store, and the
integration layer supplies one backed by the config entry.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

# 16-byte LTK, or the 80-byte extended token some firmware returns.
VALID_KEY_LENGTHS = (16, 80)


class SecureBondStore:
    """In-memory bond key for one device.

    Also the base class for persistent stores, and the whole implementation
    for the config flow — pairing happens before a config entry exists, so
    the flow keeps the key here and writes it into the entry it creates.
    """

    def __init__(self, key: bytes | None = None) -> None:
        self._key = key

    def load(self) -> bytes | None:
        """Return the stored key, or None when this host is not bonded yet."""
        return self._key

    async def save(self, key: bytes) -> None:
        """Record a key derived by a successful pairing."""
        if len(key) not in VALID_KEY_LENGTHS:
            raise ValueError(
                f"Secure bond key must be one of {VALID_KEY_LENGTHS} bytes, "
                f"got {len(key)}"
            )
        self._key = bytes(key)

    async def clear(self) -> None:
        """Forget the key, so the next session pairs from scratch.

        Called when the device rejects it: a key the cuff no longer honours
        is worse than none, because it sends every reconnect down the
        encryption path instead of letting one pairing-mode session fix it.
        """
        self._key = None
