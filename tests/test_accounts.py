"""Tests for multi-account support: the accounts index, legacy-token migration,
permissionId dedupe, credentials_for, and per-target credential selection.

No real Google calls — the interactive flow, account identification, and the
google ``Credentials`` loader are stubbed so the index/migration/selection logic
is exercised deterministically against a temp directory.
"""

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from doctopdf import accounts, config, drive


class FakeCreds:
    """Minimal stand-in for google's Credentials (only what the code touches)."""

    def __init__(self, tag="t", valid=True, expired=False, refresh_token="r"):
        self._tag = tag
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token

    def to_json(self):
        return json.dumps({"token": self._tag, "refresh_token": self.refresh_token})


class StubCreds:
    """Patched in for ``accounts.Credentials``; returns a test-set token."""

    to_return = None

    @staticmethod
    def from_authorized_user_file(path, scopes):
        if StubCreds.to_return is None:
            raise ValueError("unreadable token")
        return StubCreds.to_return


class AccountsStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = {
            "TOKENS_DIR": accounts.TOKENS_DIR,
            "ACCOUNTS_PATH": accounts.ACCOUNTS_PATH,
            "TOKEN_PATH": config.TOKEN_PATH,
            "run_install_flow": drive.run_install_flow,
            "identify": accounts.identify,
            "Credentials": accounts.Credentials,
        }
        accounts.TOKENS_DIR = self.tmp / "tokens"
        accounts.ACCOUNTS_PATH = self.tmp / "accounts.json"
        config.TOKEN_PATH = self.tmp / "token.json"
        accounts.Credentials = StubCreds
        StubCreds.to_return = FakeCreds(valid=True)

        # identity map: creds tag -> (email, permission_id); tests populate it.
        self.identity = {}
        accounts.identify = lambda creds: {
            "email": self.identity[creds._tag][0],
            "permission_id": self.identity[creds._tag][1],
        }

    def tearDown(self):
        accounts.TOKENS_DIR = self._orig["TOKENS_DIR"]
        accounts.ACCOUNTS_PATH = self._orig["ACCOUNTS_PATH"]
        config.TOKEN_PATH = self._orig["TOKEN_PATH"]
        drive.run_install_flow = self._orig["run_install_flow"]
        accounts.identify = self._orig["identify"]
        accounts.Credentials = self._orig["Credentials"]
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def _flow_returns(self, *creds):
        """Make drive.run_install_flow yield the given creds across calls."""
        it = iter(creds)
        drive.run_install_flow = lambda: next(it)

    # -- add / dedupe ------------------------------------------------------
    def test_first_account_becomes_default(self):
        self.identity["c1"] = ("alice@x.com", "PID-A")
        self._flow_returns(FakeCreds(tag="c1"))
        acct = accounts.authorize_new_account()
        self.assertEqual(acct["email"], "alice@x.com")
        self.assertTrue(acct["is_default"])
        self.assertEqual(len(accounts.list_accounts()), 1)
        self.assertTrue((accounts.TOKENS_DIR / acct["token_file"]).exists())

    def test_readd_same_account_dedupes_by_permission_id(self):
        self.identity["c1"] = ("alice@x.com", "PID-A")
        self.identity["c2"] = ("alice@x.com", "PID-A")   # same account, new token
        self._flow_returns(FakeCreds(tag="c1"), FakeCreds(tag="c2"))
        accounts.authorize_new_account()
        accounts.authorize_new_account()
        idx = accounts.list_accounts()
        self.assertEqual(len(idx), 1, "re-adding the same account must not duplicate")
        # token was overwritten with the newer creds (tag c2)
        stored = json.loads((accounts.TOKENS_DIR / idx[0]["token_file"]).read_text())
        self.assertEqual(stored["token"], "c2")

    def test_distinct_accounts_coexist_first_is_default(self):
        self.identity["c1"] = ("alice@x.com", "PID-A")
        self.identity["c2"] = ("bob@y.com", "PID-B")
        self._flow_returns(FakeCreds(tag="c1"), FakeCreds(tag="c2"))
        accounts.authorize_new_account()
        accounts.authorize_new_account()
        idx = accounts.list_accounts()
        self.assertEqual({a["email"] for a in idx}, {"alice@x.com", "bob@y.com"})
        defaults = [a for a in idx if a["is_default"]]
        self.assertEqual([a["email"] for a in defaults], ["alice@x.com"])

    # -- remove ------------------------------------------------------------
    def test_remove_default_promotes_another(self):
        self.identity["c1"] = ("alice@x.com", "PID-A")
        self.identity["c2"] = ("bob@y.com", "PID-B")
        self._flow_returns(FakeCreds(tag="c1"), FakeCreds(tag="c2"))
        a1 = accounts.authorize_new_account()
        accounts.authorize_new_account()
        token1 = accounts.TOKENS_DIR / a1["token_file"]
        self.assertTrue(token1.exists())
        remaining = accounts.remove_account("alice@x.com")
        self.assertEqual([a["email"] for a in remaining], ["bob@y.com"])
        self.assertTrue(remaining[0]["is_default"], "a new default must be promoted")
        self.assertFalse(token1.exists(), "the removed account's token is deleted")

    def test_set_default_moves_the_marker(self):
        self.identity["c1"] = ("alice@x.com", "PID-A")
        self.identity["c2"] = ("bob@y.com", "PID-B")
        self._flow_returns(FakeCreds(tag="c1"), FakeCreds(tag="c2"))
        accounts.authorize_new_account()
        accounts.authorize_new_account()
        self.assertEqual(accounts.default_key(), "alice@x.com")
        accounts.set_default("bob@y.com")
        self.assertEqual(accounts.default_key(), "bob@y.com")
        # exactly one default remains
        self.assertEqual(sum(1 for a in accounts.list_accounts() if a["is_default"]), 1)

    # -- credentials_for ---------------------------------------------------
    def test_credentials_for_no_accounts_raises(self):
        with self.assertRaises(accounts.AccountAuthError):
            accounts.credentials_for()

    def test_credentials_for_missing_token_file_raises(self):
        # Register an account by hand whose token file does not exist.
        accounts._save([{ "email": "ghost@x.com", "permission_id": "P",
                          "token_file": "ghost_x.com.json", "added_at": "now",
                          "is_default": True}])
        with self.assertRaises(accounts.AccountAuthError):
            accounts.credentials_for("ghost@x.com")

    def test_credentials_for_returns_valid_token(self):
        self.identity["c1"] = ("alice@x.com", "PID-A")
        self._flow_returns(FakeCreds(tag="c1"))
        accounts.authorize_new_account()
        want = FakeCreds(tag="loaded", valid=True)
        StubCreds.to_return = want
        got = accounts.credentials_for("alice@x.com")
        self.assertIs(got, want)
        # default selection (email=None) resolves to the only account too
        self.assertIs(accounts.credentials_for(), want)

    def test_credentials_for_unreadable_token_raises(self):
        self.identity["c1"] = ("alice@x.com", "PID-A")
        self._flow_returns(FakeCreds(tag="c1"))
        accounts.authorize_new_account()
        StubCreds.to_return = None   # loader raises ValueError → AccountAuthError
        with self.assertRaises(accounts.AccountAuthError):
            accounts.credentials_for("alice@x.com")

    # -- migration ---------------------------------------------------------
    def test_migrate_no_legacy_is_noop(self):
        self.assertIsNone(accounts.migrate_legacy_token())
        self.assertEqual(accounts.list_accounts(), [])

    def test_migrate_legacy_token_then_idempotent(self):
        # Seed a legacy token.json and make it identify as alice.
        config.TOKEN_PATH.write_text(FakeCreds(tag="legacy").to_json())
        self.identity["legacy"] = ("alice@x.com", "PID-A")
        StubCreds.to_return = FakeCreds(tag="legacy", valid=True)

        acct = accounts.migrate_legacy_token()
        self.assertIsNotNone(acct)
        self.assertEqual(acct["email"], "alice@x.com")
        self.assertTrue(acct["is_default"])
        self.assertEqual(len(accounts.list_accounts()), 1)
        self.assertTrue((accounts.TOKENS_DIR / acct["token_file"]).exists())
        self.assertFalse(config.TOKEN_PATH.exists(), "legacy token removed after migration")

        # Idempotent: nothing left to migrate.
        self.assertIsNone(accounts.migrate_legacy_token())
        self.assertEqual(len(accounts.list_accounts()), 1)

        # If a legacy file reappears for an already-registered account, it is
        # cleaned up without creating a duplicate.
        config.TOKEN_PATH.write_text(FakeCreds(tag="legacy").to_json())
        again = accounts.migrate_legacy_token()
        self.assertEqual(again["permission_id"], "PID-A")
        self.assertEqual(len(accounts.list_accounts()), 1)
        self.assertFalse(config.TOKEN_PATH.exists())

    def test_migrate_defers_and_preserves_token_when_identify_fails(self):
        config.TOKEN_PATH.write_text(FakeCreds(tag="legacy").to_json())
        StubCreds.to_return = FakeCreds(tag="legacy", valid=True)
        # Identify fails (offline / transient) → defer, never delete the token.
        accounts.identify = lambda creds: (_ for _ in ()).throw(drive.DriveError("offline"))
        self.assertIsNone(accounts.migrate_legacy_token())
        self.assertTrue(config.TOKEN_PATH.exists(),
                        "a working legacy token must not be deleted before migration")
        self.assertEqual(accounts.list_accounts(), [])


class PerTargetCredentialTests(unittest.TestCase):
    """The watch loop must fetch each Google target with its own account's
    credential, and one bad account must not abort the others."""

    def setUp(self):
        from doctopdf.app import DocToPDFController
        self.Controller = DocToPDFController
        self._orig = {
            "build_service": drive.build_service,
            "get_file_metadata": drive.get_file_metadata,
            "credentials_for": accounts.credentials_for,
            "default_key": accounts.default_key,
            "persist_if_refreshed": accounts.persist_if_refreshed,
            "list_accounts": accounts.list_accounts,
        }
        # Each account's "service" is a marker dict tagged with the account key.
        drive.build_service = lambda creds: {"acct": creds._tag}
        accounts.credentials_for = lambda key: FakeCreds(tag=key)
        accounts.default_key = lambda: "A@x.com"
        accounts.persist_if_refreshed = lambda email, creds: None
        accounts.list_accounts = lambda: [{"email": "A@x.com", "is_default": True},
                                          {"email": "B@x.com", "is_default": False}]
        self.calls = []

        def meta(service, fid):
            self.calls.append((service["acct"], fid))
            return {"id": fid, "name": f"Doc {fid}", "modifiedTime": "T0",
                    "mimeType": "application/vnd.google-apps.document"}

        drive.get_file_metadata = meta

    def tearDown(self):
        drive.build_service = self._orig["build_service"]
        drive.get_file_metadata = self._orig["get_file_metadata"]
        accounts.credentials_for = self._orig["credentials_for"]
        accounts.default_key = self._orig["default_key"]
        accounts.persist_if_refreshed = self._orig["persist_if_refreshed"]
        accounts.list_accounts = self._orig["list_accounts"]

    def _ctl(self):
        ctl = type("Ctl", (), {})()
        ctl._lock = threading.RLock()
        ctl._svc = {}
        ctl._svc_creds = {}
        ctl._account_errors = {}
        ctl._entry_names = {}
        ctl._source_names = {}
        ctl._service_for = lambda key: self.Controller._service_for(ctl, key)
        ctl._drop_account = lambda key, msg=None: self.Controller._drop_account(ctl, key, msg)
        return ctl

    def test_service_for_caches_per_account(self):
        ctl = self._ctl()
        a = ctl._service_for("A@x.com")
        b = ctl._service_for("B@x.com")
        self.assertEqual(a["acct"], "A@x.com")
        self.assertEqual(b["acct"], "B@x.com")
        self.assertIs(ctl._service_for("A@x.com"), a, "service is cached per account")
        self.assertIs(ctl._service_for(None), a, "None resolves to the default account")

    def test_service_for_propagates_account_auth_error(self):
        ctl = self._ctl()
        accounts.credentials_for = lambda key: (_ for _ in ()).throw(
            accounts.AccountAuthError("expired"))
        with self.assertRaises(accounts.AccountAuthError):
            ctl._service_for("A@x.com")

    def test_resolve_targets_uses_each_targets_account(self):
        ctl = self._ctl()
        watch = [{"id": "d1", "account": "A@x.com"},
                 {"id": "d2", "account": "B@x.com"}]
        targets, errors = self.Controller._resolve_targets(ctl, watch)
        self.assertEqual(errors, [])
        by_id = {t["id"]: t for t in targets}
        self.assertEqual(by_id["d1"]["account"], "A@x.com")
        self.assertEqual(by_id["d2"]["account"], "B@x.com")
        # Each metadata fetch used the matching account's service.
        self.assertIn(("A@x.com", "d1"), self.calls)
        self.assertIn(("B@x.com", "d2"), self.calls)

    def test_folder_children_inherit_parent_account(self):
        ctl = self._ctl()
        folder_mime = "application/vnd.google-apps.folder"
        doc_mime = "application/vnd.google-apps.document"

        def meta(service, fid):
            self.calls.append((service["acct"], fid))
            mime = folder_mime if fid == "F" else doc_mime
            return {"id": fid, "name": f"N {fid}", "modifiedTime": "T", "mimeType": mime}

        drive.get_file_metadata = meta
        list_calls = []

        def listf(service, fid):
            list_calls.append((service["acct"], fid))
            return [{"id": "c1", "name": "C1", "modifiedTime": "T", "mimeType": doc_mime},
                    {"id": "c2", "name": "C2", "modifiedTime": "T", "mimeType": doc_mime}]

        orig_list = drive.list_folder
        drive.list_folder = listf
        self.addCleanup(lambda: setattr(drive, "list_folder", orig_list))

        targets, errors = self.Controller._resolve_targets(ctl, [{"id": "F", "account": "B@x.com"}])
        self.assertEqual(errors, [])
        self.assertEqual({t["id"]: t["account"] for t in targets},
                         {"c1": "B@x.com", "c2": "B@x.com"})
        self.assertIn(("B@x.com", "F"), list_calls)  # folder listed with its account

    def test_one_bad_account_does_not_abort_the_others(self):
        ctl = self._ctl()
        good = accounts.credentials_for

        def creds(key):
            if key == "B@x.com":
                raise accounts.AccountAuthError("B expired")
            return good(key)

        accounts.credentials_for = creds
        watch = [{"id": "d1", "account": "A@x.com"},
                 {"id": "d2", "account": "B@x.com"}]
        targets, errors = self.Controller._resolve_targets(ctl, watch)
        ids = {t["id"] for t in targets}
        self.assertIn("d1", ids, "the healthy account still resolves")
        self.assertNotIn("d2", ids)
        self.assertTrue(any("d2" in e for e in errors))

    def test_orphaned_target_unknown_account_is_flagged_not_fetched(self):
        ctl = self._ctl()
        # Z@x.com is not in the known set → orphaned (its account was removed).
        watch = [{"id": "d1", "account": "A@x.com"},
                 {"id": "d2", "account": "Z@x.com"}]
        targets, errors = self.Controller._resolve_targets(ctl, watch)
        ids = {t["id"] for t in targets}
        self.assertIn("d1", ids)
        self.assertNotIn("d2", ids, "an orphaned target is not fetched under another identity")
        self.assertTrue(any("no longer authorized" in e for e in errors))
        # The orphan was never fetched (no Drive call for d2).
        self.assertNotIn("d2", [fid for _, fid in self.calls])


class OrphanHandlingTests(unittest.TestCase):
    """Reassign / remove logic for targets bound to a removed account."""

    def setUp(self):
        from doctopdf.app import DocToPDFController
        self.C = DocToPDFController
        from doctopdf import config as _config
        self._config = _config
        self._save = _config.save_config
        self.saved = []
        _config.save_config = lambda c: self.saved.append({k: list(v) if isinstance(v, list) else v
                                                           for k, v in c.items()})

    def tearDown(self):
        self._config.save_config = self._save

    def _ctl(self, watch):
        ctl = type("Ctl", (), {})()
        ctl._lock = threading.RLock()
        ctl._watch = list(watch)
        ctl._config = {"watch": list(watch)}
        ctl._reassign_orphans = lambda email, dest: self.C._reassign_orphans(ctl, email, dest)
        ctl._remove_orphan_entries = lambda email: self.C._remove_orphan_entries(ctl, email)
        return ctl

    def test_remove_orphans_drops_only_that_accounts_targets(self):
        ctl = self._ctl([{"id": "d1", "account": "A"},
                         {"id": "d2", "account": "B"},
                         {"kind": "web", "id": "u"}])
        ctl._remove_orphan_entries("A")
        self.assertEqual([e.get("id") for e in ctl._watch], ["d2", "u"])
        self.assertTrue(self.saved, "the watch list is persisted")

    def test_reassign_orphans_moves_only_that_accounts_targets(self):
        ctl = self._ctl([{"id": "d1", "account": "A"},
                         {"id": "d2", "account": "B"}])
        ctl._reassign_orphans("A", "B")
        self.assertEqual([e.get("account") for e in ctl._watch], ["B", "B"])


if __name__ == "__main__":
    unittest.main()
