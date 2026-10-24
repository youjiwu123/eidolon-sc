"""生成局域网用的自签证书：一张根证书 + 一张服务器证书（含局域网 IP 的 SAN）。

在 backend 容器里跑（那里有 cryptography），产物写到 /tmp/certs，再用
`docker compose cp backend:/tmp/certs/. ./certs/` 取出来。

手机装一次 `lan-ca.crt` 之后，`https://localhost` 就是可信来源，麦克风可用。
"""

import datetime
import ipaddress
import pathlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

OUT = pathlib.Path("/tmp/certs")
OUT.mkdir(exist_ok=True)
NOW = datetime.datetime.now(datetime.UTC)
ADDRESSES = ["127.0.0.1"]  # 局域网 IP 自己加，例如 "192.168.1.10"
NAMES = ["localhost", "eidolon.local"]  # 有自己的域名就加在这里，例如 "eidolon.example.com"


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem_key(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


ca_key = new_key()
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Eidolon Local CA")])
ca = (
    x509.CertificateBuilder()
    .subject_name(ca_name)
    .issuer_name(ca_name)
    .public_key(ca_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(NOW - datetime.timedelta(days=1))
    .not_valid_after(NOW + datetime.timedelta(days=3650))
    .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    .add_extension(
        x509.KeyUsage(
            digital_signature=True,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=True,
            crl_sign=True,
            encipher_only=False,
            decipher_only=False,
        ),
        critical=True,
    )
    .sign(ca_key, hashes.SHA256())
)

server_key = new_key()
san = x509.SubjectAlternativeName(
    [x509.DNSName(name) for name in NAMES]
    + [x509.IPAddress(ipaddress.ip_address(address)) for address in ADDRESSES]
)
server = (
    x509.CertificateBuilder()
    .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NAMES[0])]))
    .issuer_name(ca_name)
    .public_key(server_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(NOW - datetime.timedelta(days=1))
    .not_valid_after(NOW + datetime.timedelta(days=825))
    .add_extension(san, critical=False)
    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    .sign(ca_key, hashes.SHA256())
)

(OUT / "lan-ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
(OUT / "lan-ca.key").write_bytes(pem_key(ca_key))
(OUT / "lan.crt").write_bytes(server.public_bytes(serialization.Encoding.PEM))
(OUT / "lan.key").write_bytes(pem_key(server_key))
print("写好:", sorted(p.name for p in OUT.iterdir()))
