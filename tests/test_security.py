import base64
import os
import unittest
from pathlib import Path

from app.security import CredentialVault, hash_password, verify_password


class SecurityTest(unittest.TestCase):
    def test_password_hash_is_not_plaintext(self):
        encoded = hash_password("correct-horse-42")
        self.assertNotIn("correct-horse-42", encoded)
        self.assertTrue(verify_password(encoded, "correct-horse-42"))
        self.assertFalse(verify_password(encoded, "wrong"))

    def test_vault_binds_ciphertext_to_user(self):
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        vault = CredentialVault(Path("."), key)
        ciphertext = vault.encrypt(7, "school-password")
        self.assertEqual("school-password", vault.decrypt(7, ciphertext))
        with self.assertRaises(Exception):
            vault.decrypt(8, ciphertext)


if __name__ == "__main__":
    unittest.main()
