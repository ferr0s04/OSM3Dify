# OSM 3D Building Generator

Gera representações 3D simplificadas de edifícios para o OpenStreetMap — no estilo
do F4 Map — a partir de LiDAR, modelos digitais de terreno e ortofotos.

O resultado não é um modelo fotorealista nem uma malha 3D: são **tags
[Simple 3D Buildings](https://wiki.openstreetmap.org/wiki/Simple_3D_Buildings)**
(`height`, `roof:shape`, `roof:height`, `roof:orientation`, `roof:colour`,
`building:part`…) em edifícios OSM existentes, que é o que os mapas 3D (F4 Map,
OSM2World, Streets GL…) interpretam.

> **Estado:** em desenvolvimento. O pipeline já corre de ponta a ponta até QA
> sobre dados reais de Vila do Conde.
>
> - Exatidão de `roof:shape` face às tags do OSM: **79 %**.
> - Edifícios isolados: **88 %**.
>
> Ver [resultados](docs/area-de-estudo.md#primeiros-resultados-quadrículas-155480--156480-2026-11-11).
>
> Por fazer:
> - telhados partilhados por edifícios encostados;
> - partes (`building:part`);
> - cores;
> - exportação `.osm` para o JOSM.

## Documentação

- [Arquitetura original](docs/architecture/arquitetura-osm-3d-building-generator.md)
- [Revisão crítica da arquitetura](docs/architecture/revisao-arquitetura.md) — decisões
  que prevalecem sobre o documento original (alvo S3DB, telhados model-driven,
  ferramentas, licenciamento, plano revisto).
- [Frontend: aplicação de revisão](docs/architecture/frontend-revisao.md) — âmbito,
  arquitetura e faseamento do futuro frontend (proposta adiada).
- [Área de estudo](docs/area-de-estudo.md) — Vilar e Mosteiró (Vila do Conde),
  quadrículas LiDAR, dados verificados e alinhamento com o OSM.

## Pipeline

```text
fetch → prepare → heights → parts → roofs → colours → qa → export
 OSM     footprints   nDSM      volumes   modelos    cor de    flags   .osm (JOSM)
 LiDAR   + co-registo           (Fase 4)  paramétr.  telhado           GeoPackage / GLB
```

Cada fase grava um artefacto em `data/processed/<área>/` e pode ser re-executada
isoladamente. **O export é sempre explícito**: gera um ficheiro `.osm` para rever e
carregar manualmente no JOSM. Este projeto nunca publica no OSM.

## Requisitos

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Opcional: [QGIS](https://qgis.org/), [JOSM](https://josm.openstreetmap.de/),
  [OSM2World](https://osm2world.org/), [PDAL](https://pdal.io/) (CLI, para
  pré-processamento pesado de LAZ)

## Instalação

```bash
uv sync                    # dependências + ferramentas de desenvolvimento
uv sync --extra mesh       # + exportação GLB (trimesh)
uv sync --extra osm        # + leitura de extratos PBF (pyosmium)
```

## Utilização

```bash
uv run osm3d info                      # configuração ativa e estado dos artefactos
uv run osm3d stage fetch               # edifícios OSM da área (Overpass)
uv run osm3d tiles                     # edifícios por quadrícula LiDAR (com id DGT)
uv run osm3d dgt -t 155480 --dry-run   # verificar disponibilidade no Centro de Dados DGT
uv run osm3d dgt -t 155480             # descarregar LAZ + MDT (credenciais em .env)
uv run osm3d run --from prepare --to qa  # reconstrução → GeoPackage
uv run osm3d evaluate                  # comparar com as tags roof:shape/building:levels do OSM
```

O download da DGT precisa de `DGT_USER` e `DGT_PASSWORD` num ficheiro `.env` na raiz
do repositório, que não é versionado.

A configuração por omissão está em [config/config.yaml](config/config.yaml). Para
uma área de estudo própria, copiar para `config/local.yaml` (ignorado pelo git) e usar
`--config config/local.yaml`.

## Dados

| Dados | Fonte | Formato | Local |
| --- | --- | --- | --- |
| Edifícios | OpenStreetMap (Overpass, `out meta`) | OSM XML | `data/raw/` |
| Nuvem de pontos | DGT — LiDAR Portugal continental (≥10 pts/m²) | LAZ, EPSG:3763 | `data/raw/lidar/` |
| MDT | DGT (mesmo levantamento LiDAR) | GeoTIFF | `data/raw/dtm/` |
| Ortofotos | DGT | GeoTIFF | `data/raw/orthophotos/` |
| Referência | Edifícios OSM com tags S3DB + anotação manual | GeoJSON/CSV | `data/reference/` |

Os dados não são versionados (ver [.gitignore](.gitignore)).

> ⚠️ **Licenciamento.** Os dados da DGT são CC BY 4.0, que **não** é automaticamente
> compatível com a ODbL do OSM. Antes de publicar qualquer dado derivado no OSM é
> necessária permissão explícita da DGT e cumprimento das
> [Import Guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines) e do
> [Automated Edits code of conduct](https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct).
> Ver [revisão §8](docs/architecture/revisao-arquitetura.md#8-licenciamento--passa-a-fase-0).

## Estrutura

```text
src/osm3d/
├── acquisition/      # Overpass, LiDAR/MDT DGT, ortofotos
├── geospatial/       # footprints, pontos (laspy), rasters, co-registo
├── reconstruction/   # alturas, partes, modelos e ajuste de telhados
├── appearance/       # cor de telhado (CIELAB + paleta)
├── domain/           # modelo intermédio: Building → BuildingPart → Roof
├── validation/       # QA e sinalização para revisão
├── evaluation/       # comparação com o conjunto de referência
├── export/           # tags S3DB, OSM XML (JOSM), GeoPackage, GLB
├── pipeline.py       # fases e artefactos
├── config.py
└── cli.py
config/               # configuração YAML
data/                 # raw / processed / output / reference (não versionado)
docs/architecture/    # arquitetura e revisão
tests/
```

## Desenvolvimento

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
uv run mypy src
```

## Roadmap

| Fase | Resultado |
| --- | --- |
| 0. Enquadramento | Licença DGT↔ODbL resolvida, área de estudo, conjunto de referência |
| 1. Ingestão | Footprints OSM + pontos LiDAR co-registados por edifício |
| 2. Alturas | `height` com avaliação contra a referência |
| 3. Telhados | `roof:shape/height/orientation/direction` (1 parte por edifício) |
| 3b. Export | `.osm` para JOSM + visualização OSM2World |
| 4. Partes | Volumes independentes (GeoPackage/GLB) |
| 5. Aparência | `roof:colour`, `roof:material` |
| 6. Partes no OSM | `building:part` com topologia partilhada |
