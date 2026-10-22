"""Preenche chaves ausentes/vazias sem substituir credenciais já configuradas."""
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values, set_key


GENERATORS = {
    "DJANGO_SECRET_KEY": lambda: secrets.token_urlsafe(64),
    "OLT_CREDENTIAL_KEY": lambda: Fernet.generate_key().decode(),
}


def initialize_env(path):
    path = Path(path)
    if not path.exists():
        content = "".join(f"{name}={generate()}\n" for name, generate in GENERATORS.items())
        content += (
            "DJANGO_DEBUG=0\nDJANGO_ALLOWED_HOSTS=localhost,127.0.0.1\n"
            "CSRF_TRUSTED_ORIGINS=\nCOOKIE_SECURE=0\nENABLE_LIVE_OLT=0\n"
        )
        with open(path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
            handle.write(content)
        return list(GENERATORS)

    # A cópia de .env.example contém chaves vazias. Não basta testar existência.
    # Não interpolar mantém referências e valores fornecidos pelo usuário.
    values = dotenv_values(path, interpolate=False)
    generated = []
    for name, generate in GENERATORS.items():
        if not (values.get(name) or "").strip():
            set_key(path, name, generate(), quote_mode="never")
            generated.append(name)
    return generated


if __name__ == "__main__":
    generated = initialize_env(Path(__file__).resolve().parent.parent / ".env")
    if generated:
        print("Chaves ausentes/vazias geradas: " + ", ".join(generated) + ".")
        print("Valores existentes preservados. Não adicione .env ao Git.")
    else:
        print(".env já configurado; chaves e demais valores preservados.")
