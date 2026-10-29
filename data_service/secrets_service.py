"""The one secret this service owns, in the OS keyring.

PORTED FROM McTrader'S ``services/secrets_service.py``, CUT DOWN TO THE EODHD KEY

McTrader's version is a generic store: a ``CredentialDTO`` carrying an arbitrary
``secret_key_path``/``secret_name``/``secret_value``, plus broker-specific helpers. This service
owns exactly one secret, so the DTO and the broker helpers are gone and the two identifiers are
class constants. What is kept is the part that was hard-won.

WHY THIS EXISTS AT ALL

``config.resolve_api_key()`` already falls back to the keyring, so why an endpoint? Because the
keyring is **per user**. A service running as ``NT AUTHORITY\\\\SYSTEM`` cannot see an entry stored
under an interactive account, so the key has to get into *SYSTEM's* vault. The only process that can
write there is the service itself — so it needs a way to be told the key at runtime.

That is also why this is better than passing ``EODHD_API_KEY`` in the service environment: NSSM
would store that value in plaintext in the registry, whereas the credential store is encrypted by
Windows and only readable by the account that owns it.

THE DELETE-THEN-SET PATTERN IS NOT INCIDENTAL

``write`` deletes any existing entry before setting the new one. Windows Credential Manager treats a
write over an existing entry inconsistently and can end up holding *both* the old and the new value
under the same target, after which a read may return either. McTrader's service carries the same
guard for the same reason; it is the kind of bug that shows up months later as "the key is wrong and
then it is right again".
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import config

log = logging.getLogger(__name__)


class KeyringUnavailable(RuntimeError):
    """No keyring backend is installed or reachable."""


class EodhdKeyStore:
    """Read, write and delete the EODHD API key in the OS keyring.

    The identifiers are taken from :mod:`data_service.config` so this service and McTrader address
    the same entry — one key, one quota, one place to measure it.
    """

    #: Windows Credential Manager target. Venue-scoped, not service-scoped: the same string McTrader
    #: and Mini-McTrader use.
    SERVICE = config.KEYRING_SERVICE
    #: The entry within that target.
    USERNAME = config.KEYRING_USERNAME

    def __init__(self, keyring_module: Any = None) -> None:
        """*keyring_module* is injectable so a test can run without a real credential store."""
        if keyring_module is not None:
            self._keyring = keyring_module
            self._available = True
            return
        try:
            import keyring

            self._keyring = keyring
            self._available = True
        except Exception as exc:  # noqa: BLE001 - a missing backend is a state, not a crash
            log.warning("keyring backend unavailable: %s", exc)
            self._keyring = None
            self._available = False

    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    def _backend_name(self) -> Optional[str]:
        if not self._available:
            return None
        try:
            getter = getattr(self._keyring, "get_keyring", None)
            if getter is None:
                return "injected"
            return type(getter()).__name__
        except Exception:  # noqa: BLE001
            return None

    def read(self) -> Optional[str]:
        """The stored key, or ``None``. Never logs or returns it anywhere else."""
        if not self._available:
            return None
        try:
            value = self._keyring.get_password(self.SERVICE, self.USERNAME)
        except Exception as exc:  # noqa: BLE001
            log.warning("keyring read failed: %s", exc)
            return None
        return value.strip() if value and value.strip() else None

    def write(self, value: str) -> None:
        """Store (or replace) the key. Deletes first — see the module docstring."""
        if not self._available:
            raise KeyringUnavailable(
                "no keyring backend is available, so the key cannot be stored; "
                "set EODHD_API_KEY in the service environment instead")
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("an empty API key is not a key")
        try:
            # Delete first so Windows cannot keep both the old and the new value.
            try:
                self._keyring.delete_password(self.SERVICE, self.USERNAME)
            except Exception:  # noqa: BLE001 - not found is the normal case
                pass
            self._keyring.set_password(self.SERVICE, self.USERNAME, cleaned)
        except Exception as exc:  # noqa: BLE001
            raise KeyringUnavailable(f"could not write to the keyring: {exc}") from exc
        log.info("EODHD key written to the keyring (%s/%s)", self.SERVICE, self.USERNAME)

    def delete(self) -> bool:
        """Remove the key. ``True`` when something was there."""
        if not self._available:
            return False
        try:
            self._keyring.delete_password(self.SERVICE, self.USERNAME)
            return True
        except Exception:  # noqa: BLE001 - absent is not an error
            return False

    def status(self) -> Dict[str, Any]:
        """Whether a key is stored, and a mask — never the key itself.

        The mask shows the last four characters. That is enough for a human to tell "this is the
        key I think it is" against another system, and useless to anyone who intercepts it.
        """
        key = self.read()
        return {
            "stored": bool(key),
            "masked": f"{'*' * max(0, len(key) - 4)}{key[-4:]}" if key else None,
            "length": len(key) if key else 0,
            "keyring_service": self.SERVICE,
            "keyring_username": self.USERNAME,
            "backend": self._backend_name(),
            "keyring_available": self._available,
        }
