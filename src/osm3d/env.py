"""Leitura mínima de ``.env`` (sem dependências) e acesso a credenciais."""

from __future__ import annotations

import os
from pathlib import Path


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_dotenv(path: Path = Path(".env")) -> None:
    """Carrega ``.env`` para ``os.environ`` sem sobrescrever variáveis já definidas."""
    if path.exists():
        for key, value in parse_dotenv(path.read_text(encoding="utf-8")).items():
            os.environ.setdefault(key, value)


def require_env(*names: str) -> tuple[str, ...]:
    load_dotenv()
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"Variáveis em falta (definir em .env): {', '.join(missing)}")
    return tuple(os.environ[n] for n in names)
