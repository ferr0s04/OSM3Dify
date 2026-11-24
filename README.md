<p align="center">
  <img src="src/osm3d/app/static/favicon.svg" alt="OSM3Dify" width="128">
</p>

<h1 align="center">OSM3Dify</h1>

<p align="center">
  <strong>Edifícios 3D simples para o OpenStreetMap, a partir de LiDAR, modelos de terreno e ortofotos.</strong><br>
  Tags <a href="https://wiki.openstreetmap.org/wiki/Simple_3D_Buildings">Simple 3D Buildings</a> no estilo do F4 Map, revistas por uma pessoa antes de chegarem ao OSM.
</p>

<p align="center">
  <a href="LICENSE"><img alt="Licença MIT" src="https://img.shields.io/badge/licen%C3%A7a-MIT-2563eb"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&amp;logoColor=white">
  <a href="https://docs.astral.sh/uv/"><img alt="uv" src="https://img.shields.io/badge/gestor-uv-5C3EE8"></a>
  <img alt="Prova de conceito" src="https://img.shields.io/badge/estado-prova%20de%20conceito-c2410c">
</p>

<p align="center">
  <a href="#o-que-faz">O que faz</a> ·
  <a href="#como-funciona">Como funciona</a> ·
  <a href="#começar">Começar</a> ·
  <a href="#configuração">Configuração</a> ·
  <a href="#roadmap">Roadmap</a> ·
  <a href="#licenças-e-responsabilidade">Licenças</a>
</p>

> [!WARNING]
> **Prova de conceito.** Projeto experimental, sem garantias (ver [Licença](#licença)).
> Quem usa a ferramenta é responsável pelas licenças dos dados que usa e pelo que
> contribui para o OSM: ver [Licenças e responsabilidade](#licenças-e-responsabilidade).

## O que faz

O resultado não é um modelo fotorealista nem uma malha 3D: são **tags**
(`height`, `roof:shape`, `roof:height`, `roof:orientation`, `roof:colour`,
`building:part`…), que é o que os mapas 3D (F4 Map, OSM2World, Streets GL…)
interpretam. Servem para os edifícios já mapeados e para os **edifícios em falta no
OSM**, que vêm da cartografia das câmaras municipais. Desta cartografia vêm também
propostas de **muros** (`barrier=wall`).

| | |
| --- | --- |
| **Telhados e alturas** | `height`, `roof:shape`, `roof:height`, `roof:orientation` a partir do LiDAR. Exatidão de `roof:shape` face às tags do OSM: **87 %**, com 67 % de cobertura; os casos incertos ficam para revisão ([resultados](docs/validacao.md#primeiros-resultados-quadrículas-155480--156480-2026-11-11)). |
| **Cor** | `roof:colour` da ortofoto, confirmada pelo RGB do LiDAR. |
| **Partes** | Divisão em `building:part` na revisão, exportadas com nós partilhados. |
| **Edifícios em falta no OSM** | Da cartografia municipal (geoportais MapGuide, serviços ArcGIS REST, GeoPackages de dados abertos), confirmados pelo LiDAR. Testado com Vila do Conde, Porto e Póvoa de Varzim; uma área na fronteira usa as várias câmaras. |
| **Muros** | Propostas de `barrier=wall` da cartografia municipal, ligadas aos limites de `landuse` na exportação. |
| **Revisão** | Aplicação local com mapa, 3D sobre o LiDAR, edição de tags e de partes, e edição de geometria (muros e `landuse`) ao estilo do iD. |

O pipeline corre de ponta a ponta sobre dados reais da DGT no norte de Portugal, até à
exportação `.osm` para o JOSM. **Nada é publicado automaticamente**: a submissão ao OSM
é sempre manual, no JOSM.

## Como funciona

```text
fetch → prepare → heights → parts → roofs → colours → qa → candidates → export
 OSM     footprints   nDSM     volumes  modelos    cor de    flags  cartografia  .osm (JOSM)
 LiDAR   + co-registo                   paramétr.  telhado          municipal    GeoPackage / GLB
```

Cada fase grava um artefacto em `data/processed/<área>/` e pode ser re-executada
isoladamente. A fase `candidates` é opcional: traz os edifícios da cartografia
municipal em falta no OSM, confirmados pelo LiDAR, e ajusta-lhes o telhado e a cor
como aos restantes. **O export é sempre explícito**: gera um ficheiro `.osm` para
rever e carregar manualmente no JOSM.

## Começar

### Requisitos

- Python 3.12+ e [uv](https://docs.astral.sh/uv/)
- Opcional: [QGIS](https://qgis.org/), [JOSM](https://josm.openstreetmap.de/),
  [OSM2World](https://osm2world.org/), [PDAL](https://pdal.io/) (CLI, para
  pré-processamento pesado de LAZ)

### Instalação

```bash
uv sync                    # dependências + ferramentas de desenvolvimento
uv sync --extra mesh       # + exportação GLB (trimesh)
uv sync --extra osm        # + leitura de extratos PBF (pyosmium)
```

### Utilização

```bash
uv run osm3d review                    # aplicação de revisão: mapa, 3D vs LiDAR, edição
uv run osm3d info                      # área ativa e estado dos artefactos
uv run osm3d area                      # listar áreas; `osm3d area area-2` ativa outra
uv run osm3d area --new --relation <id>  # nova área (id area-N): quadrículas de um limite OSM
uv run osm3d area --new --tiles 155480,156480 --label Teste  # nova área: quadrículas indicadas
uv run osm3d stage fetch               # edifícios OSM da área (Overpass)
uv run osm3d tiles                     # edifícios por quadrícula LiDAR (com id DGT)
uv run osm3d dgt -t 155480 --dry-run   # verificar disponibilidade no Centro de Dados DGT
uv run osm3d dgt -t 155480             # descarregar LAZ + MDT (credenciais em .env)
uv run osm3d run --from prepare --to qa  # reconstrução → GeoPackage
uv run osm3d stage candidates          # edifícios da cartografia municipal em falta no OSM
uv run osm3d evaluate                  # comparar com as tags roof:shape/building:levels do OSM
uv run osm3d stage export              # .osm para rever no JOSM (+ relatório de conflitos)
```

Quase tudo se faz também na aplicação: escolher a área, descarregar as quadrículas e
correr o pipeline em segundo plano (separador "Dados"), rever e exportar.

### Aplicação de revisão

`osm3d review` abre a aplicação em http://127.0.0.1:8765/ (só no próprio computador).
Lista os edifícios da cartografia municipal em falta no OSM (os do OSM aparecem como
camada do mapa) e as propostas de muros. Permite aceitar, rejeitar ou corrigir: tipo,
forma e altura do telhado, cor, partes. No mapa, editam-se os vértices dos muros e dos
limites de `landuse`, como no iD. As revisões e as edições ficam em
`data/processed/<área>/reviews.sqlite`, sobrevivem a novas corridas do pipeline e são
aplicadas na exportação. Ver [frontend-revisao.md](docs/architecture/frontend-revisao.md).

### Exportação para o JOSM

Só são exportados os edifícios e os muros **aceites** na aplicação de revisão, com as
correções aplicadas. Com `export.include_pending: true`, saem também os não revistos sem
sinalizações (é assim que se exportam as tags 3D dos edifícios que já estão no OSM).

O `.osm` exportado preenche as alterações. A submissão é sempre manual: abre-se o
ficheiro no JOSM, ajusta-se ou descarta-se o que for preciso e só então se faz o upload.
Por omissão o JOSM **avisa antes do upload** (`export.josm_upload: "false"`), para
confirmar as licenças das fontes (ver
[Licenças e responsabilidade](#licenças-e-responsabilidade)); `"never"` recusa o upload
e `"true"` tira o aviso (no `config/local.yaml`).

## Configuração

A configuração por omissão está em [config/config.yaml](config/config.yaml). O que é
desta instalação (credenciais, câmaras) vai para `config/local.yaml`, que não é
versionado e se junta por cima.

- **Áreas.** Uma área de estudo é **um conjunto de quadrículas LiDAR de 1 km**: o
  LAZ/MDT descarregado e os edifícios OSM processados (centróide dentro das quadrículas)
  são sempre os mesmos. A área do `config.yaml` é só a inicial. As outras criam-se e
  ativam-se no separador "Dados" ou com `osm3d area`, e ficam em `data/areas.json` (não
  versionado). A área ativa vale para a aplicação e para todos os comandos da CLI.
- **DGT.** O download precisa de `DGT_USER` e `DGT_PASSWORD` num ficheiro `.env` na
  raiz do repositório, que não é versionado.
- **Câmaras municipais.** Configuram-se em `sources.municipalities`, no
  `config/local.yaml`. Os tipos de fonte e os campos estão descritos no
  [config.yaml](config/config.yaml). Cada área usa as câmaras cuja extensão a cobre. A
  cópia de todo o concelho pode ser descarregada uma vez, no separador "Dados", e serve
  todas as áreas.

## Dados

| Dados | Fonte | Formato | Local |
| --- | --- | --- | --- |
| Edifícios | OpenStreetMap (Overpass, `out meta`) | OSM XML | `data/raw/` |
| Nuvem de pontos | DGT — LiDAR Portugal continental (≥10 pts/m²) | LAZ, EPSG:3763 | `data/raw/lidar/` |
| MDT | DGT (mesmo levantamento LiDAR) | GeoTIFF | `data/raw/dtm/` |
| Ortofotos | DGT | GeoTIFF | `data/raw/orthophotos/` |
| Cartografia municipal | Câmaras (MapGuide, ArcGIS REST, GeoPackage) | GeoJSON (cópia do concelho) | `data/raw/municipal/` |
| Referência | Edifícios OSM com tags S3DB + anotação manual | GeoJSON/CSV | `data/reference/` |

Os dados não são versionados (ver [.gitignore](.gitignore)). Ver
[Licenças e responsabilidade](#licenças-e-responsabilidade) antes de usar dados destas
fontes no OSM.

## Desenvolvimento

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
uv run mypy src
```

<details>
<summary><strong>Estrutura do código</strong></summary>

```text
src/osm3d/
├── acquisition/      # Overpass, LiDAR/MDT DGT, ortofotos, cartografia municipal
├── geospatial/       # footprints, pontos (laspy), rasters, ortofotos, co-registo
├── reconstruction/   # alturas, partes, telhados, candidatos municipais, muros
├── appearance/       # cor de telhado (CIELAB + paleta)
├── domain/           # modelo intermédio: Building → BuildingPart → Roof
├── validation/       # QA, topologia face ao OSM, sinalização para revisão
├── evaluation/       # comparação com o conjunto de referência
├── export/           # tags S3DB, OSM XML (JOSM), ajustes ao OSM, GeoPackage, GLB
├── app/              # aplicação de revisão (FastAPI + MapLibre/three.js)
├── pipeline.py       # fases e artefactos
├── municipal_area.py # câmaras de cada área: recortes e junção
├── geometry_edits.py # edições de geometria feitas no mapa
├── review.py         # revisões guardadas (SQLite)
├── config.py
└── cli.py
config/               # configuração YAML
data/                 # raw / processed / output / reference (não versionado)
docs/architecture/    # arquitetura e revisão
tests/
```

</details>

## Roadmap

| Fase | Resultado | Estado |
| --- | --- | --- |
| 0. Enquadramento | Área de estudo, conjunto de referência (licenças: do utilizador) | ✅ |
| 1. Ingestão | Footprints OSM + pontos LiDAR co-registados por edifício | ✅ |
| 2. Alturas | `height` com avaliação contra a referência | ✅ |
| 3. Telhados | `roof:shape/height/orientation/direction` | ✅ `flat`/`skillion` por validar |
| 3b. Export | `.osm` para JOSM + visualização OSM2World | ✅ |
| 4. Partes | Volumes independentes (GeoPackage/GLB) | ✅ |
| 5. Aparência | `roof:colour`, `roof:material` | ✅ `roof:colour` · ⬜ `roof:material` |
| 6. Partes no OSM | `building:part` com topologia partilhada | ✅ |
| 7. Edifícios em falta | Cartografia municipal confirmada pelo LiDAR, várias câmaras | ✅ |
| 8. Muros | `barrier=wall` ligados ao `landuse` | ✅ |

**Por fazer:** validação manual de `flat` e `skillion`; `roof:material`; os contornos de
algumas cartografias seguem o beiral e não as paredes (por exemplo, a NdD2 da Póvoa de
Varzim).

## Documentação

- [Arquitetura original](docs/architecture/arquitetura-osm-3d-building-generator.md)
- [Revisão crítica da arquitetura](docs/architecture/revisao-arquitetura.md) — decisões
  que prevalecem sobre o documento original (alvo S3DB, telhados model-driven,
  ferramentas, licenciamento, plano revisto).
- [Aplicação de revisão](docs/architecture/frontend-revisao.md) — âmbito e arquitetura
  da aplicação de revisão.
- [Validação em dados reais](docs/validacao.md) — medições e calibrações numa área de
  teste: dados LiDAR verificados, alinhamento com o OSM, exatidão dos telhados.

## Licenças e responsabilidade

Este repositório é uma **prova de conceito**. O código não traz dados, credenciais nem
fontes configuradas: cada utilizador escolhe as suas e é **responsável por**:

- **Licenças das fontes.** Os dados da DGT (LiDAR, MDT, ortofotos) são CC BY 4.0, que
  **não** é automaticamente compatível com a ODbL do OSM: é precisa autorização
  explícita do titular para os usar no OSM (ver
  [revisão §8](docs/architecture/revisao-arquitetura.md#8-licenciamento--passa-a-fase-0)).
  O mesmo vale para a cartografia de cada câmara municipal (geoportais, dados abertos):
  usar só com autorização e nos termos de cada uma.
- **Contribuições para o OSM.** Tudo o que for submetido a partir do `.osm` exportado é
  da responsabilidade de quem o submete, incluindo cumprir as
  [Import Guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines), o
  [Automated Edits code of conduct](https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct)
  e a atribuição na página
  [Contributors](https://wiki.openstreetmap.org/wiki/Contributors). O sistema nunca
  submete nada, e o JOSM avisa antes do upload do ficheiro exportado.
- **Uso dos serviços.** Overpass, tiles do OSM, DGT e geoportais municipais: pedidos
  espaçados e dentro das políticas de uso de cada serviço.

## Licença

Código sob a [licença MIT](LICENSE). Os dados usados ou produzidos têm as licenças das
respetivas fontes (ver acima).
