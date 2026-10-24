"""Create the Web Push (VAPID) key pair once: the private key goes straight into Secrets Manager, the public key is printed.

Usage: AWS_PROFILE=sendpidge python scripts/create_vapid_key.py [--secret-name sendpidge/vapid-private-key]
Paste the printed public key into pwa/js/config.js as VAPID_PUBLIC_KEY. The private key is never printed or written to disk.
Refuses to overwrite an existing secret: replacing the key invalidates every saved browser subscription.
"""

import argparse
import base64

import boto3
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

DEFAULT_SECRET_NAME = "sendpidge/vapid-private-key"


def base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--secret-name", default=DEFAULT_SECRET_NAME)
    arguments = parser.parse_args()
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_raw = private_key.private_numbers().private_value.to_bytes(32, "big")
    public_point = private_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    boto3.client("secretsmanager", region_name="us-east-1").create_secret(
        Name=arguments.secret_name, SecretString=base64url(private_raw),
        Description="SendPidge Web Push VAPID private key (raw P-256, base64url)",
    )
    print(f"stored the private key in {arguments.secret_name}")
    print(f"VAPID_PUBLIC_KEY = {base64url(public_point)}")


if __name__ == "__main__":
    main()
