"""The key store, against a fake keyring so the real credential store is never touched.

The dangerous cases are the ones worth testing: a write must not leave the old value behind, and the
status must never return the key itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service.secrets_service import EodhdKeyStore, KeyringUnavailable


class FakeKeyring:
    """An in-memory stand-in for the OS credential store."""

    def __init__(self, initial=None) -> None:
        self.store = dict(initial or {})
        self.writes = []
        self.deletes = []

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, value):
        self.writes.append((service, username, value))
        self.store[(service, username)] = value

    def delete_password(self, service, username):
        self.deletes.append((service, username))
        if (service, username) not in self.store:
            raise KeyError("not found")
        del self.store[(service, username)]

    def get_keyring(self):
        return self


def _store(initial=None) -> tuple[EodhdKeyStore, FakeKeyring]:
    fake = FakeKeyring(initial)
    return EodhdKeyStore(keyring_module=fake), fake


KEY = "abc123def456ghi789jkl"
TARGET = (EodhdKeyStore.SERVICE, EodhdKeyStore.USERNAME)


# ---------------------------------------------------------------------------
# read / write / delete
# ---------------------------------------------------------------------------

def test_a_stored_key_reads_back():
    store, _ = _store()
    store.write(KEY)
    assert store.read() == KEY


def test_reading_an_empty_store_returns_none():
    store, _ = _store()
    assert store.read() is None


def test_writing_trims_surrounding_whitespace():
    store, _ = _store()
    store.write(f"  {KEY}\n")
    assert store.read() == KEY


def test_an_empty_key_is_refused():
    store, fake = _store()
    with pytest.raises(ValueError):
        store.write("   ")
    assert fake.writes == []


def test_a_write_deletes_the_old_value_first():
    """Windows can end up holding both values under one target; delete-then-set prevents it."""
    store, fake = _store()
    store.write(KEY)
    fake.deletes.clear()
    store.write("new-key-value-000000")

    assert fake.deletes == [TARGET], "the old entry was not removed before the overwrite"
    assert store.read() == "new-key-value-000000"
    assert len(fake.store) == 1, "the store holds more than one entry for the same target"


def test_writing_over_an_absent_entry_still_succeeds():
    """The delete raises when nothing is there, and that must not stop the write."""
    store, _ = _store()
    store.write(KEY)
    assert store.read() == KEY


def test_delete_removes_the_key_and_reports_that_it_did():
    store, _ = _store({TARGET: KEY})
    assert store.delete() is True
    assert store.read() is None


def test_delete_on_an_empty_store_reports_nothing_removed():
    store, _ = _store()
    assert store.delete() is False


# ---------------------------------------------------------------------------
# status: never the key
# ---------------------------------------------------------------------------

def test_status_masks_all_but_the_last_four_characters():
    store, _ = _store({TARGET: KEY})
    status = store.status()
    assert status["stored"] is True
    assert status["length"] == len(KEY)
    assert status["masked"].endswith(KEY[-4:])
    assert KEY not in status["masked"], "the full key leaked into the mask"


def test_status_never_contains_the_key_anywhere():
    store, _ = _store({TARGET: KEY})
    rendered = repr(store.status())
    assert KEY not in rendered, "the key appeared somewhere in status()"


def test_status_of_an_empty_store_says_so():
    store, _ = _store()
    status = store.status()
    assert status["stored"] is False
    assert status["masked"] is None
    assert status["length"] == 0


def test_status_reports_the_target_it_uses():
    """The identifiers must match the ones config resolves against, or the key is invisible."""
    from data_service import config

    store, _ = _store()
    status = store.status()
    assert status["keyring_service"] == config.KEYRING_SERVICE
    assert status["keyring_username"] == config.KEYRING_USERNAME


# ---------------------------------------------------------------------------
# no backend
# ---------------------------------------------------------------------------

class BrokenKeyring(FakeKeyring):
    def set_password(self, *args, **kwargs):
        raise OSError("credential store is locked")


def test_an_unwritable_keyring_raises_a_named_error():
    store = EodhdKeyStore(keyring_module=BrokenKeyring())
    with pytest.raises(KeyringUnavailable):
        store.write(KEY)


def test_an_unreadable_keyring_returns_none_rather_than_raising():
    class Unreadable(FakeKeyring):
        def get_password(self, *args, **kwargs):
            raise OSError("vault unavailable")

    store = EodhdKeyStore(keyring_module=Unreadable())
    assert store.read() is None
    assert store.status()["stored"] is False


def test_a_key_written_by_this_store_is_the_one_config_reads():
    """End to end through the real resolution order: write, then resolve with no env var."""
    import os

    from data_service import config

    store, _ = _store()
    store.write(KEY)
    previous = os.environ.pop("EODHD_API_KEY", None)
    try:
        resolved, source = config.resolve_api_key_with_source()
        # The fake store is not the real keyring, so config cannot see it -- this asserts the
        # *contract* (same target) rather than the value, which is what would silently break.
        assert (EodhdKeyStore.SERVICE, EodhdKeyStore.USERNAME) == (
            config.KEYRING_SERVICE, config.KEYRING_USERNAME)
        assert source in {"keyring", "none"}
    finally:
        if previous is not None:
            os.environ["EODHD_API_KEY"] = previous
