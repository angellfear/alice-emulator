"""Локальный сертификат glagol-сервера колонки (glagold, P2P мультирум/тандем).

Образец — generateGlagolSecurity() в quasar-server (storage/defaults.ts): RSA 2048,
самоподписанный X.509 — serialNumber '01', CN=localhost, O=Yandex, C=RU, срок 30 лет,
подпись SHA-256; PEM сертификата + приватного ключа (PKCS#1, как pki.privateKeyToPem).
Выдаётся в /get_sync_info (glagol.security) и /glagol/device_list. Приватный ключ —
секрет: колонке в ответе идёт настоящим, в API/UI отдаётся маской, в журнал не пишется.
"""
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

VALIDITY_DAYS = 30 * 365  # срок из образца: 30 лет


def generate():
    """{certificate, private_key} — PEM-пара локального glagol-сервера по параметрам образца."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "RU"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Yandex"),
    ])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder()
                   .subject_name(name)
                   .issuer_name(name)
                   .public_key(key.public_key())
                   .serial_number(1)  # '01' из образца
                   .not_valid_before(now)
                   .not_valid_after(now + timedelta(days=VALIDITY_DAYS))
                   .sign(key, hashes.SHA256()))
    return {
        "certificate": certificate.public_bytes(serialization.Encoding.PEM).decode(),
        "private_key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,  # PKCS#1, как в образце
            serialization.NoEncryption()).decode(),
    }
