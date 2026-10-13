"""Authenticated OCR evidence envelopes; recipient private key stays local."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

FORMAT = "jobsheet-ocr-rsa-oaep-aes256gcm-v1"
DOMAIN = b"jobsheet-private-ocr-v1\0"


def load_recipient(pem):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise ValueError("An RSA recipient key of at least 2048 bits is required")
    return key


def seal(data, recipient, label):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    wrapped = recipient.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                algorithm=hashes.SHA256(), label=None))
    public = recipient.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    fingerprint = hashlib.sha256(public).hexdigest()
    aad = DOMAIN + label.encode("utf-8") + b"\0" + fingerprint.encode("ascii")
    encrypted = AESGCM(key).encrypt(nonce, data, aad)
    return json.dumps({"format": FORMAT, "recipient_sha256": fingerprint,
        "wrapped_key": base64.b64encode(wrapped).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(encrypted).decode("ascii")}, separators=(",", ":")).encode("ascii")


def unseal(envelope, private, label):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    body = json.loads(envelope)
    if body.get("format") != FORMAT:
        raise ValueError("Unsupported private OCR envelope")
    public = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    fingerprint = hashlib.sha256(public).hexdigest()
    if body.get("recipient_sha256") != fingerprint:
        raise ValueError("Wrong OCR recipient key")
    decode = lambda name: base64.b64decode(body[name], validate=True)
    key = private.decrypt(decode("wrapped_key"), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                            algorithm=hashes.SHA256(), label=None))
    aad = DOMAIN + label.encode("utf-8") + b"\0" + fingerprint.encode("ascii")
    return AESGCM(key).decrypt(decode("nonce"), decode("ciphertext"), aad)


def generate_keys(directory):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_path = directory / "recipient.private.pem"
    with private_path.open("xb") as stream:
        stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    private_path.chmod(0o600)
    (directory / "recipient.public.pem").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


def decrypt_directory(source, output, private_path):
    from cryptography.hazmat.primitives import serialization
    from .private_ocr_trace import private_output
    source, output = Path(source), private_output(Path(output))
    private = serialization.load_pem_private_key(Path(private_path).read_bytes(), password=None)
    # Authenticate everything in memory before creating the destination.
    files = {}
    for item in source.iterdir():
        if item.is_symlink() or not item.is_file():
            raise ValueError("Unexpected encrypted evidence entry")
        if item.name == "events.encrypted.jsonl":
            lines = item.read_bytes().splitlines()
            files["events.private.jsonl"] = b"\n".join(
                unseal(line, private, f"event-{number}") for number, line in enumerate(lines, 1)) + b"\n"
        elif item.name.endswith(".enc"):
            name = item.name[:-4]
            files[name] = unseal(item.read_bytes(), private, name)
        else:
            raise ValueError("Unexpected unencrypted file in OCR artifact")
    if "events.private.jsonl" not in files:
        raise ValueError("Missing OCR events")
    output.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        (output / name).write_bytes(data)
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    generate = sub.add_parser("generate")
    generate.add_argument("directory", type=Path)
    decrypt = sub.add_parser("decrypt")
    decrypt.add_argument("source", type=Path)
    decrypt.add_argument("output", type=Path)
    decrypt.add_argument("--private-key", required=True, type=Path)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        raise ValueError("Private-key operations are local-only")
    from .private_ocr_trace import private_output
    if args.action == "generate":
        generate_keys(private_output(args.directory))
        print(json.dumps({"status": "LOCAL_KEYS_CREATED"}))
    else:
        print(json.dumps({"status": "DECRYPTED", "files": decrypt_directory(
            args.source, args.output, args.private_key)}))


if __name__ == "__main__":
    main()
