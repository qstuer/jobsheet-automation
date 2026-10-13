"""Optional encrypted diagnostic dependency tests; synthetic evidence only."""
import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import private_ocr_trace as trace, private_trace_crypto as crypto


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "Diagnostic-only crypto dependency not installed")
class EncryptedTraceTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public = self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)

    def tearDown(self):
        self.temp.cleanup()

    def test_roundtrip_and_randomized_ciphertext(self):
        a = crypto.seal(b"synthetic-private-serial", self.key.public_key(), "event-1")
        b = crypto.seal(b"synthetic-private-serial", self.key.public_key(), "event-1")
        self.assertNotEqual(a, b)
        self.assertNotIn(b"synthetic-private-serial", a)
        self.assertEqual(b"synthetic-private-serial", crypto.unseal(a, self.key, "event-1"))

    def test_tamper_or_reordering_fails_authentication(self):
        from cryptography.exceptions import InvalidTag
        encrypted = crypto.seal(b"synthetic", self.key.public_key(), "event-1")
        with self.assertRaises(InvalidTag):
            crypto.unseal(encrypted, self.key, "event-2")
        body = json.loads(encrypted)
        ciphertext = bytearray(base64.b64decode(body["ciphertext"]))
        ciphertext[0] ^= 1
        body["ciphertext"] = base64.b64encode(ciphertext).decode()
        with self.assertRaises(InvalidTag):
            crypto.unseal(json.dumps(body), self.key, "event-1")

    def test_cloud_capture_contains_no_plaintext(self):
        output = self.root / "capture"
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}), trace.Capture(
                output, root=self.root, recipient_public_key=self.public) as capture:
            capture.emit("response", content="private-OCR-sentinel")
        self.assertEqual(["events.encrypted.jsonl"], [path.name for path in output.iterdir()])
        saved = (output / "events.encrypted.jsonl").read_bytes().strip()
        self.assertNotIn(b"private-OCR-sentinel", saved)
        self.assertEqual("private-OCR-sentinel", json.loads(crypto.unseal(saved, self.key, "event-1"))["content"])

    def test_wrong_recipient_or_small_key_rejected(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(ValueError):
            crypto.unseal(crypto.seal(b"synthetic", self.key.public_key(), "x"), other, "x")
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key()
        with self.assertRaises(ValueError):
            crypto.load_recipient(weak.public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo))

    def test_encrypted_image_roundtrip_preserves_exact_bytes(self):
        from cryptography.hazmat.primitives import serialization
        source = self.root / "encrypted"
        source.mkdir()
        (source / "events.encrypted.jsonl").write_bytes(crypto.seal(b'{"event":"synthetic"}', self.key.public_key(), "event-1") + b"\n")
        (source / "call-001-image-1.jpeg.enc").write_bytes(crypto.seal(b"exact-image-bytes", self.key.public_key(), "call-001-image-1.jpeg"))
        keyfile = self.root / "private.pem"
        keyfile.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        destination = self.root / "decoded"
        with patch.object(trace, "PRIVATE_ROOT", self.root):
            # private_output's default was bound at definition; explicit test
            # root lets this synthetic fixture use a disposable private folder.
            with patch.object(trace, "private_output", side_effect=lambda path: trace.Path(path)):
                self.assertEqual(2, crypto.decrypt_directory(source, destination, keyfile))
        self.assertEqual(b"exact-image-bytes", (destination / "call-001-image-1.jpeg").read_bytes())

    def test_refuses_unencrypted_artifact_entries(self):
        from cryptography.hazmat.primitives import serialization
        source = self.root / "invalid"
        source.mkdir()
        (source / "customer.pdf").write_bytes(b"must not be accepted")
        keyfile = self.root / "private.pem"
        keyfile.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        with patch.object(trace, "private_output", side_effect=lambda path: Path(path)):
            with self.assertRaises(ValueError):
                crypto.decrypt_directory(source, self.root / "not-created", keyfile)
        self.assertFalse((self.root / "not-created").exists())
