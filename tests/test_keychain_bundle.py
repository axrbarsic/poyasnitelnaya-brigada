from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import keychain_bundle


class KeychainBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = self.root / "XMentionKeychainHelper.app"
        self.executable = (
            self.app
            / "Contents"
            / "MacOS"
            / "XMentionKeychainHelper"
        )
        self.executable.parent.mkdir(parents=True)
        self.executable.write_bytes(b"binary")
        self.executable.chmod(0o755)
        (self.app / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": keychain_bundle.EXPECTED_BUNDLE_ID,
                }
            )
        )
        (self.app / "Contents" / "embedded.provisionprofile").write_bytes(
            b"profile"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def valid_entitlements() -> dict[str, object]:
        return {
            "com.apple.application-identifier": (
                keychain_bundle.EXPECTED_APPLICATION_ID
            ),
            "keychain-access-groups": [
                keychain_bundle.EXPECTED_APPLICATION_ID
            ],
        }

    @staticmethod
    def valid_profile() -> dict[str, object]:
        return {
            "Name": "Mac Team Provisioning Profile",
            "Platform": ["OSX"],
            "TeamIdentifier": [keychain_bundle.EXPECTED_TEAM_ID],
            "ApplicationIdentifierPrefix": [
                keychain_bundle.EXPECTED_TEAM_ID
            ],
            "ExpirationDate": datetime.now(timezone.utc)
            + timedelta(days=180),
            "Entitlements": {
                "com.apple.application-identifier": (
                    keychain_bundle.EXPECTED_APPLICATION_ID
                ),
                "keychain-access-groups": [
                    f"{keychain_bundle.EXPECTED_TEAM_ID}.*"
                ],
            },
        }

    @mock.patch("scripts.keychain_bundle._plist_from_command")
    @mock.patch("scripts.keychain_bundle._run")
    def test_valid_signed_bundle_passes(
        self,
        run: mock.Mock,
        plist_command: mock.Mock,
    ) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess(
                [],
                0,
                b"",
                (
                    "TeamIdentifier="
                    + keychain_bundle.EXPECTED_TEAM_ID
                    + "\n"
                ).encode(),
            ),
        ]
        plist_command.side_effect = [
            (self.valid_entitlements(), None),
            (self.valid_profile(), None),
        ]

        result = keychain_bundle.verify_bundle(self.executable)

        self.assertTrue(result.ok)
        self.assertEqual(result.errors, ())
        self.assertEqual(
            result.application_id,
            keychain_bundle.EXPECTED_APPLICATION_ID,
        )

    def test_standalone_executable_fails_closed(self) -> None:
        standalone = self.root / "keychain-helper"
        standalone.write_bytes(b"binary")

        result = keychain_bundle.verify_bundle(standalone)

        self.assertFalse(result.ok)
        self.assertIn(
            "helper is not inside an app-like bundle",
            result.errors,
        )

    @mock.patch("scripts.keychain_bundle._plist_from_command")
    @mock.patch("scripts.keychain_bundle._run")
    def test_profile_without_keychain_authority_fails(
        self,
        run: mock.Mock,
        plist_command: mock.Mock,
    ) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess(
                [],
                0,
                b"",
                (
                    "TeamIdentifier="
                    + keychain_bundle.EXPECTED_TEAM_ID
                    + "\n"
                ).encode(),
            ),
        ]
        profile = self.valid_profile()
        profile["Entitlements"] = {
            "com.apple.application-identifier": "OTHER.*",
            "keychain-access-groups": ["OTHER.*"],
        }
        plist_command.side_effect = [
            (self.valid_entitlements(), None),
            (profile, None),
        ]

        result = keychain_bundle.verify_bundle(self.executable)

        self.assertFalse(result.ok)
        self.assertIn(
            "profile does not authorize the application ID",
            result.errors,
        )
        self.assertIn(
            "profile does not authorize the keychain group",
            result.errors,
        )

    @mock.patch("scripts.keychain_bundle._plist_from_command")
    @mock.patch("scripts.keychain_bundle._run")
    def test_signed_keychain_group_must_be_exact(
        self,
        run: mock.Mock,
        plist_command: mock.Mock,
    ) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess(
                [],
                0,
                b"",
                (
                    "TeamIdentifier="
                    + keychain_bundle.EXPECTED_TEAM_ID
                    + "\n"
                ).encode(),
            ),
        ]
        entitlements = self.valid_entitlements()
        entitlements["keychain-access-groups"] = [
            f"{keychain_bundle.EXPECTED_TEAM_ID}.*"
        ]
        plist_command.side_effect = [
            (entitlements, None),
            (self.valid_profile(), None),
        ]

        result = keychain_bundle.verify_bundle(self.executable)

        self.assertFalse(result.ok)
        self.assertIn(
            "expected keychain access group is missing",
            result.errors,
        )

    @mock.patch("scripts.keychain_bundle._plist_from_command")
    @mock.patch("scripts.keychain_bundle._run")
    def test_wrong_signing_and_profile_team_fail_closed(
        self,
        run: mock.Mock,
        plist_command: mock.Mock,
    ) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 0, b"", b""),
            subprocess.CompletedProcess(
                [],
                0,
                b"",
                b"TeamIdentifier=OTHERTEAM\n",
            ),
        ]
        profile = self.valid_profile()
        profile["TeamIdentifier"] = ["OTHERTEAM"]
        profile["ApplicationIdentifierPrefix"] = ["OTHERTEAM"]
        plist_command.side_effect = [
            (self.valid_entitlements(), None),
            (profile, None),
        ]

        result = keychain_bundle.verify_bundle(self.executable)

        self.assertFalse(result.ok)
        self.assertIn("unexpected code-signing team", result.errors)
        self.assertIn(
            "provisioning profile has an unexpected team",
            result.errors,
        )
        self.assertIn(
            "provisioning profile has an unexpected app prefix",
            result.errors,
        )

    def test_malformed_info_plist_fails_closed(self) -> None:
        (self.app / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps(["not", "a", "dictionary"])
        )

        result = keychain_bundle.verify_bundle(self.executable)

        self.assertFalse(result.ok)
        self.assertIn("bundle Info.plist is unreadable", result.errors)

    @mock.patch("scripts.keychain_bundle._run")
    def test_plist_parser_accepts_codesign_stderr_prefix(
        self,
        run: mock.Mock,
    ) -> None:
        payload = plistlib.dumps(self.valid_entitlements())
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            b"",
            b"Executable=/tmp/helper\n" + payload,
        )

        decoded, error = keychain_bundle._plist_from_command(
            ["/usr/bin/codesign"]
        )

        self.assertIsNone(error)
        self.assertEqual(decoded, self.valid_entitlements())

    def test_profile_authority_accepts_expected_wildcard_only(self) -> None:
        self.assertTrue(
            keychain_bundle._profile_allows(
                keychain_bundle.EXPECTED_APPLICATION_ID,
                [f"{keychain_bundle.EXPECTED_TEAM_ID}.*"],
            )
        )

    def test_subprocess_timeout_is_converted_to_failure(self) -> None:
        with mock.patch(
            "scripts.keychain_bundle.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["codesign"], 20),
        ):
            result = keychain_bundle._run(["codesign"])

        self.assertEqual(result.returncode, 125)

    def test_installer_migrates_before_switching_live_helper(self) -> None:
        installer = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "install_keychain_helper.sh"
        ).read_text(encoding="utf-8")

        migration = installer.index(
            '"${installed_executable}" \\\n'
            "      migrate-if-present"
        )
        switch = installer.index(
            '/bin/mv -f "${temporary_link}" '
            '"${project_root}/var/keychain-helper"'
        )
        self.assertLess(migration, switch)
        self.assertIn("-allowProvisioningDeviceRegistration", installer)
        self.assertFalse(
            keychain_bundle._profile_allows(
                keychain_bundle.EXPECTED_APPLICATION_ID,
                ["OTHER.*"],
            )
        )


if __name__ == "__main__":
    unittest.main()
