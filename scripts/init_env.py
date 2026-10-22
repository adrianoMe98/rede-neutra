"""Gera segredos locais apenas uma vez, sem expor os valores no terminal."""
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

root = Path(__file__).resolve().parent.parent
path = root / ".env"
if path.exists():
    print(".env já existe; configuração preservada.")
else:
    content = (
        f"DJANGO_SECRET_KEY={secrets.token_urlsafe(64)}\n"
        f"OLT_CREDENTIAL_KEY={Fernet.generate_key().decode()}\n"
        "DJANGO_DEBUG=0\nDJANGO_ALLOWED_HOSTS=localhost,127.0.0.1\n"
        "CSRF_TRUSTED_ORIGINS=\nCOOKIE_SECURE=0\nENABLE_LIVE_OLT=0\n"
    )
    with open(path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
        handle.write(content)
    print(".env criado com chaves exclusivas. Não adicione este arquivo ao Git.")
