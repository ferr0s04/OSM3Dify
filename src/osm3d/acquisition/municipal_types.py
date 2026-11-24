"""Tipo municipal de edifício → tag ``building`` do OSM, pela classificação da fonte.

Duas classificações nacionais cobrem a maioria das câmaras:

- **catálogo** (cartografia 1:1000–1:10000 anterior ao CartTop): uma descrição por
  polígono, às vezes com o código do objeto à frente ("Vivenda Casa",
  "06020103-PRÉDIO", "Arrecadação_Anexo"). Vila do Conde (MapGuide) e Porto (ArcGIS).
- **CartTop** (modelo da DGT para a cartografia vetorial nova, NdD1/NdD2, p.ex. os
  GeoPackages do dados.gov.pt): utilização atual, forma e condição do edifício.
  Póvoa de Varzim.

As descrições comparam-se normalizadas (sem código, acentos, maiúsculas nem
pontuação). ``None``: não é edifício a mapear (escadas, piscinas, reservatórios,
ruínas, partes de outro edifício). Um tipo desconhecido também dá ``None``: fica de fora
e é contado no registo da fase, para se acrescentar aqui.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

_CODE = re.compile(r"^\s*\d{6,8}\s*[-_ ]\s*")


def normalize(text: object) -> str:
    """ "06020103-PRÉDIO" → "predio"; "Arrecadação_Anexo" → "arrecadacao anexo"."""
    s = _CODE.sub("", str(text or ""))
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def strip_code(text: object) -> str:
    """Descrição para mostrar: sem o código do objeto à frente."""
    return _CODE.sub("", str(text or "")).strip()


# ------------------------------------------------------------------- catálogo

_CATALOGO: dict[str, str | None] = {
    # habitação e anexos
    "Vivenda Casa": "house",
    "Prédio": "yes",
    "Construções em Geral": "yes",
    "Outras Construções": "yes",
    "Arrecadação Anexo": "shed",
    "Garagem": "garage",
    "Telheiro Alpendre": "roof",
    "Barraca": "shed",
    "Edifício em Construção": "construction",
    # partes de outro edifício (desenhadas à parte na cartografia): não são edifícios
    "Edifício Corpo Balançado": None,
    "Edifício Corpo Elevado": None,
    "Edifício Corpo Recuado": None,
    # indústria, comércio, serviços
    "Fábrica": "industrial",
    "Armazém": "warehouse",
    "Posto de Transformação": "service",
    "Centro Comercial": "retail",
    "Mercado": "retail",
    "Hipermercado": "supermarket",
    "Hotel": "hotel",
    "Pensão Residencial Albergaria": "hotel",
    "Albergue Hostel": "hotel",
    # equipamentos
    "Universidade Faculdade Politécnico": "university",
    "Escola EB1": "school",
    "Escola EB23 Secundária": "school",
    "Escola Formação Profissional": "school",
    "Escola Ensino Especial": "school",
    "Escola Primária": "school",
    "Jardim Infância": "kindergarten",
    "Infantário": "kindergarten",
    "Creche": "kindergarten",
    "Hospital": "hospital",
    "Centro Saúde": "yes",
    "Centro Social": "yes",
    "Lar 3ª Idade Centro Dia": "yes",
    "Lar Infância Juventude": "yes",
    "ATL": "yes",
    "Quartel": "military",
    "Esquadra PSP": "public",
    "Edifício Administrativo": "public",
    "Câmara Municipal": "public",
    "Câmara Municipal Secretaria Regional": "public",
    "Junta Freguesia": "public",
    "Tribunal": "public",
    "Banco de Portugal": "public",
    "Museu": "civic",
    "Teatro": "civic",
    "Cinema": "civic",
    "Biblioteca": "civic",
    "Centro Cultural": "civic",
    "Coliseu": "civic",
    "Oceanário": "civic",
    "Palácio": "palace",
    "Mosteiro Convento": "monastery",
    # culto
    "Igreja": "church",
    "Capela": "chapel",
    "Basílica Catedral": "cathedral",
    "Mesquita": "mosque",
    "Sinagoga": "synagogue",
    # não são edifícios a mapear
    "Edifício em Ruínas": None,
    "Edificio Em Ruínas": None,
    "Escadas": None,
    "Piscinas": None,
    "Pelourinho": None,
    "Depósito Água Elevado": None,
    "Depósito Água de Superfície": None,
    "Depósito de Gás (Gasómetro)": None,
}
CATALOGO: dict[str, str | None] = {normalize(k): v for k, v in _CATALOGO.items()}


def catalogo_tag(descr: object) -> str | None:
    return CATALOGO.get(normalize(descr))


def catalogo_known(descr: object) -> bool:
    return normalize(descr) in CATALOGO


# -------------------------------------------------------------------- CartTop

_FORMA: dict[str, str | None] = {
    "Estufa": "greenhouse",
    "Telheiro": "roof",
    "Coreto": "roof",
    "Construção precária": "shed",
    "Barracão": "shed",
    "Silo": "silo",
    "Posto transformador": "service",
    "Capela": "chapel",
    "Igreja": "church",
    "Sinagoga": "synagogue",
    "Torre": "tower",
    "Quiosque fixo": "kiosk",
    "Estádio": "stadium",
    "Moinho de vento": "yes",
    "Azenha": "yes",
    "Farol": "yes",
    "Forte": "yes",
    "Praça de touros": "yes",
    "Reservatório de armazenamento": None,
    "Anfiteatro ao ar livre": None,
}
_UTILIZACAO: dict[str, str] = {
    "Residencial": "residential",
    "Associado à residência": "shed",  # anexos (arrumos, garagens)
    "Agricultura e pescas": "farm_auxiliary",
    "Comércio": "retail",
    "Indústria": "industrial",
    "Alojamento e restauração": "commercial",
    "Outros - Serviços": "commercial",
    "Serviços da Administração Pública": "public",
    "Outro - Transportes": "transportation",
    "Culto e inumação": "religious",
    "Atividades desportivas e de lazer": "sports_hall",
    "Atividades recreativas e culturais": "civic",
    "Atividades associativas": "yes",
    "Parque de estacionamento em edifício": "parking",
    "Serviços de utilização coletiva": "civic",
}
_EQUIPAMENTO: dict[str, str] = {
    "Ensino básico ou secundário": "school",
    "Creche, infantário ou ensino pré-escolar": "kindergarten",
    "Ensino superior e investigação": "university",
    "Hospital": "hospital",
}
FORMA = {normalize(k): v for k, v in _FORMA.items()}
UTILIZACAO = {normalize(k): v for k, v in _UTILIZACAO.items()}
EQUIPAMENTO = {normalize(k): v for k, v in _EQUIPAMENTO.items()}


def _norm_typo(s: str) -> str:
    return s.replace("asssociado", "associado")  # gralha em alguns GeoPackages


def carttop_tag(attrs: Mapping[str, Any]) -> tuple[str | None, bool]:
    """(tag ``building``, se o tipo é conhecido) de um edifício CartTop."""
    cond = normalize(attrs.get("valor_condicao_const"))
    if cond == "ruina":
        return None, True
    if cond == "em construcao":
        return "construction", True
    forma = normalize(attrs.get("valor_forma_edificio"))
    if forma:
        return FORMA.get(forma), forma in FORMA
    equip = normalize(attrs.get("valor_tipo_equipamento_coletivo"))
    if equip in EQUIPAMENTO:
        return EQUIPAMENTO[equip], True
    uses = [
        _norm_typo(normalize(u)) for u in str(attrs.get("valor_utilizacao_atual") or "").split("|")
    ]
    uses = [u for u in uses if u]
    if not uses:
        return "yes", True  # edifício sem utilização registada
    first = uses[0]  # "Residencial | Comércio": manda a primeira
    return UTILIZACAO.get(first, "yes"), first in UTILIZACAO


def carttop_label(attrs: Mapping[str, Any]) -> str:
    """Descrição para a revisão ("Telheiro", "Residencial | Comércio (Em construção)")."""
    parts = [
        str(attrs.get(k)).strip()
        for k in ("valor_forma_edificio", "valor_utilizacao_atual")
        if attrs.get(k) not in (None, "") and str(attrs.get(k)) != "nan"
    ]
    label = parts[0] if parts else "Edifício"
    cond = attrs.get("valor_condicao_const")
    if cond not in (None, "") and str(cond) not in ("nan", "verificado"):
        label += f" ({cond})"
    return label.replace("Asssociado", "Associado")
