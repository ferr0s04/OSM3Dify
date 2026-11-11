# OSM 3D Building Generator

## Arquitetura e stack tecnológica

**Objetivo:** desenvolver um sistema que gere representações 3D
simplificadas de edifícios para OpenStreetMap (OSM), semelhantes às
visualizações urbanas do F4 Map. O foco é reconstruir volumes
arquitetónicos simples, telhados básicos e cores aproximadas --- não
modelos fotorealistas.

------------------------------------------------------------------------

## 1. Objetivo e âmbito

O sistema deverá:

-   Utilizar edifícios existentes no OSM como base geométrica.
-   Integrar nuvens de pontos LiDAR, modelos digitais de
    terreno/superfície e ortofotos.
-   Estimar alturas e identificar volumes distintos dentro de cada
    edifício.
-   Reconhecer formas simples de telhado: plano, duas águas e quatro
    águas.
-   Estimar cores aproximadas de telhados e, quando existam imagens
    adequadas, de fachadas.
-   Produzir modelos 3D compostos por volumes independentes.
-   Permitir revisão humana antes de exportar alterações para o OSM.

A representação pretendida é do tipo *building massing*: edifícios
construídos a partir de sólidos simples, com diferentes alturas,
geometrias e coberturas.

## 2. Arquitetura geral

A arquitetura divide-se em quatro grandes responsabilidades:

1.  **Data Acquisition** --- obtenção dos dados OSM, LiDAR, MDT/MDS e
    ortofotos.
2.  **Geospatial Processing** --- processamento e associação dos dados
    geográficos.
3.  **3D Reconstruction** --- deteção de volumes, alturas e formas de
    telhado.
4.  **OSM Integration & Visualization** --- validação, visualização e
    exportação dos resultados.

### Fluxo de processamento

``` text
┌─────────────────────────────────────────────┐
│              1. DATA ACQUISITION            │
│       OSM · LiDAR · MDT/MDS · Ortofotos     │
└──────────────────────┬──────────────────────┘
                       ▼
┌─────────────────────────────────────────────┐
│            2. GEOSPATIAL PROCESSING          │
│ Footprints · Point clouds · Raster · Matching│
└──────────────────────┬──────────────────────┘
                       ▼
┌─────────────────────────────────────────────┐
│             3. 3D RECONSTRUCTION             │
│                                             │
│  Volume Detection     Roof Detection         │
│  Geometry Builder    Appearance Estimation  │
└──────────────────────┬──────────────────────┘
                       ▼
┌─────────────────────────────────────────────┐
│          4. BUILDING MODEL & VALIDATION      │
│     Modelo intermédio · Confiança · QA      │
└──────────────────────┬──────────────────────┘
                       ▼
             ┌─────────────────┐
             │   EXPORTAÇÃO    │
             └────────┬────────┘
                      ▼
          ┌───────────────────────┐
          │ OSM XML / GeoPackage  │
          │ GLB / visualização 3D │
          └───────────────────────┘
```

Cada fase deve poder ser executada independentemente. Por exemplo, deve
ser possível voltar a executar a deteção de telhados sem descarregar e
processar novamente toda a nuvem LiDAR.

## 3. Stack tecnológica

A recomendação é utilizar Python como linguagem principal do
processamento. O ecossistema Python tem bibliotecas maduras para dados
geoespaciais, nuvens de pontos, geometria computacional e análise de
imagem.

### 3.1. Aquisição e armazenamento de dados

  -----------------------------------------------------------------------
  Tecnologia                          Responsabilidade
  ----------------------------------- -----------------------------------
  OSMnx                               Obtenção de edifícios e elementos
                                      geográficos do OSM

  Overpass API                        Consultas a edifícios e respetivas
                                      tags

  PDAL                                Leitura, filtragem e processamento
                                      de LAS/LAZ

  Rasterio                            Leitura e processamento de MDT, MDS
                                      e ortofotos

  laspy                               Acesso aos pontos e atributos RGB
                                      da nuvem LiDAR

  PostGIS                             Armazenamento e consultas
                                      geoespaciais, numa fase posterior
  -----------------------------------------------------------------------

Para o MVP, PostGIS não é obrigatório. GeoPackage e ficheiros LAS/LAZ
são suficientes para uma pequena área de estudo.

### 3.2. Processamento geoespacial

  Tecnologia   Responsabilidade
  ------------ ------------------------------------------------
  GeoPandas    Manipulação de footprints e geometrias
  Shapely      Operações geométricas, interseções e validação
  NumPy        Operações numéricas sobre pontos
  SciPy        Vizinhança, clustering e análise espacial
  Open3D       Processamento e segmentação de nuvens 3D

### 3.3. Reconstrução geométrica

  Tecnologia / algoritmo   Responsabilidade
  ------------------------ -----------------------------------------------------------
  RANSAC                   Identificação de planos dos telhados
  DBSCAN                   Agrupamento de pontos por proximidade
  PCA                      Análise da orientação e geometria dos conjuntos de pontos
  Shapely                  Divisão e combinação de footprints
  NumPy                    Cálculo de alturas, inclinações e coordenadas
  Trimesh                  Construção e validação de sólidos e meshes 3D

RANSAC, DBSCAN e PCA são algoritmos, não bibliotecas. Podem ser
utilizados através de Open3D, SciPy e scikit-learn.

### 3.4. Cores e aparência

  Tecnologia     Responsabilidade
  -------------- --------------------------------------------
  OpenCV         Processamento de imagens e amostras de cor
  NumPy          Estatísticas RGB
  scikit-learn   Clustering de cores
  Rasterio       Amostragem de ortofotos

Recomenda-se utilizar o espaço de cor CIELAB para agrupar cores
semelhantes e calcular uma cor dominante. O sistema deve guardar a
confiança da estimativa e evitar atribuir cores quando os dados são
insuficientes.

As ortofotos e os pontos LiDAR com RGB podem ajudar a estimar cores dos
telhados. As cores das fachadas exigem imagens oblíquas ou ao nível da
rua com condições de utilização adequadas, ou validação manual. Não se
deve inferir a cor da fachada diretamente a partir de uma imagem aérea.

### 3.5. Visualização e edição

  -----------------------------------------------------------------------
  Tecnologia                          Responsabilidade
  ----------------------------------- -----------------------------------
  QGIS                                Visualização geográfica, inspeção
                                      de footprints, LiDAR e resultados

  JOSM                                Revisão e edição dos edifícios e
                                      tags antes da publicação no OSM

  OSM2World                           Conversão de dados OSM com tags 3D
                                      numa representação visual

  Trimesh                             Inspeção e exportação de geometrias
                                      3D, incluindo GLB
  -----------------------------------------------------------------------

Links:

-   [QGIS](https://qgis.org/)
-   [JOSM](https://josm.openstreetmap.de/)
-   [OSM2World](https://osm2world.org/)
-   [PDAL](https://pdal.io/)
-   [Open3D](https://www.open3d.org/)

Para a primeira versão, utilizar QGIS e OSM2World em vez de desenvolver
imediatamente um visualizador 3D próprio.

## 4. Modelo de dados intermédio

Uma decisão importante é não gerar diretamente tags OSM a partir dos
algoritmos. Deve existir um modelo intermédio independente do formato de
exportação. Isto permite corrigir geometrias, comparar algoritmos e
gerar diferentes formatos sem repetir o processamento.

Exemplo ilustrativo:

``` json
{
  "osm_id": 123456,
  "footprint": "POLYGON(...)",
  "parts": [
    {
      "id": "part_1",
      "geometry": "POLYGON(...)",
      "height": 8.5,
      "roof": {
        "shape": "gabled",
        "height": 2.2,
        "colour": "#A65F35"
      },
      "facade": {
        "colour": "#E8DDC8"
      },
      "confidence": {
        "height": 0.96,
        "roof_shape": 0.91,
        "roof_colour": 0.85,
        "facade_colour": 0.35
      },
      "sources": {
        "height": "lidar",
        "roof_shape": "lidar",
        "roof_colour": "orthophoto",
        "facade_colour": "manual"
      }
    }
  ]
}
```

Os valores são meramente ilustrativos.

### Conceitos do modelo

-   **Building:** edifício completo e respetivo footprint.
-   **Building Part:** cada volume arquitetónico independente.
-   **Roof:** geometria e características da cobertura de cada volume.

O telhado não precisa de ser uma entidade independente no modelo de
dados: pode ser uma propriedade geométrica da respetiva parte.

## 5. Módulos de reconstrução

### A. Footprint Processing

-   Obter polígonos dos edifícios OSM.
-   Corrigir geometrias inválidas.
-   Relacionar footprints com pontos LiDAR.
-   Detetar edifícios sem correspondência adequada.

**Output:** footprints preparados e associados aos dados de origem.

### B. Height Estimation

-   Selecionar pontos classificados como construções.
-   Estimar a cota do terreno através do MDT.
-   Calcular a altura usando percentis dos pontos do telhado.
-   Detetar alterações de altura dentro do footprint.

Uma estimativa inicial pode ser:

`altura ≈ P95(Z dos pontos do telhado) - Z do terreno`

A cota do terreno deve considerar o relevo local. A utilização de uma
média simples dos pontos dentro do footprint pode produzir erros,
especialmente em terrenos inclinados.

**Output:** altura estimada e possíveis subdivisões do edifício.

### C. Roof Segmentation

-   Agrupar pontos LiDAR.
-   Detetar planos com RANSAC.
-   Calcular inclinação e orientação.
-   Classificar a forma do telhado.

Formas a considerar:

-   `flat` --- plano;
-   `gabled` --- duas águas;
-   `hipped` --- quatro águas;
-   `unknown` --- forma não identificada com confiança suficiente.

**Output:** planos identificados e classificação da cobertura.

### D. Volume Reconstruction

-   Identificar volumes com alturas distintas.
-   Dividir footprints quando necessário.
-   Construir volumes independentes.
-   Gerar a geometria dos telhados.

A divisão de volumes não deve ser feita apenas com base em alterações de
altura. Um telhado pode ter vários planos sem que existam vários volumes
arquitetónicos. A segmentação deve combinar altura, continuidade
geométrica, planos e footprint.

**Output:** geometria 3D composta por partes.

### E. Appearance Estimation

-   Extrair cores dominantes dos telhados.
-   Estimar cores das fachadas apenas com imagens adequadas.
-   Atribuir confiança e proveniência a cada atributo.

**Output:** cores aproximadas e respetiva confiança.

### F. Quality Control

-   Verificar geometrias inválidas.
-   Detetar partes sobrepostas ou inconsistentes.
-   Verificar alturas e telhados anómalos.
-   Sinalizar edifícios que precisam de revisão.

## 6. Backend e execução

Para o processamento, recomenda-se inicialmente uma arquitetura Python
modular, sem microserviços.

  Componente                 Tecnologia
  -------------------------- ---------------------------------------------------------
  Linguagem principal        Python 3.12+
  API                        FastAPI, quando existir interface ou integração externa
  Validação de dados         Pydantic
  Processamento              Bibliotecas geoespaciais Python
  Execução de tarefas        Celery + Redis, numa fase posterior
  Armazenamento geográfico   GeoPackage / PostGIS
  Ficheiros de origem        LAS/LAZ e GeoTIFF
  Configuração               YAML
  Testes                     pytest
  Ambiente                   Docker

O processamento LiDAR pesado não deve ser executado diretamente num
pedido HTTP. Quando existir uma API, esta deverá criar uma tarefa que
será executada por um worker.

### Fluxo de execução

1.  O utilizador seleciona uma área.
2.  O backend obtém os dados OSM e identifica os dados geoespaciais
    disponíveis.
3.  É criado um job de processamento.
4.  Um worker executa a reconstrução.
5.  Os resultados são guardados.
6.  O utilizador consulta e revê os edifícios.
7.  O sistema exporta os resultados aprovados.

Para o MVP, pode existir apenas uma CLI Python. FastAPI, Celery e Redis
podem ser adicionados quando houver necessidade de uma interface de
utilização.

## 7. Estrutura do repositório

``` text
osm-3d-building-generator/
│
├── src/
│   ├── acquisition/
│   │   ├── osm.py
│   │   ├── lidar.py
│   │   └── orthophotos.py
│   │
│   ├── geospatial/
│   │   ├── footprints.py
│   │   ├── point_clouds.py
│   │   └── raster.py
│   │
│   ├── reconstruction/
│   │   ├── heights.py
│   │   ├── segmentation.py
│   │   ├── roof_detection.py
│   │   ├── volume_detection.py
│   │   └── geometry_builder.py
│   │
│   ├── appearance/
│   │   ├── roof_colours.py
│   │   └── facade_colours.py
│   │
│   ├── domain/
│   │   ├── building.py
│   │   ├── building_part.py
│   │   └── roof.py
│   │
│   ├── validation/
│   │   ├── geometry.py
│   │   ├── quality.py
│   │   └── confidence.py
│   │
│   ├── export/
│   │   ├── geopackage.py
│   │   ├── osm_xml.py
│   │   └── glb.py
│   │
│   ├── api/
│   │   └── main.py
│   │
│   └── cli.py
│
├── data/
│   ├── raw/
│   ├── processed/
│   └── output/
│
├── tests/
├── config/
│   └── config.yaml
│
├── docker/
├── pyproject.toml
└── README.md
```

## 8. Stack final recomendada para o MVP

  Área                Escolha
  ------------------- --------------------------------
  Linguagem           Python 3.12+
  Dados geográficos   GeoPandas + Shapely + Rasterio
  LiDAR               PDAL + laspy
  Nuvens 3D           Open3D
  Algoritmos          NumPy + SciPy + scikit-learn
  Geometria 3D        Trimesh
  Cores               OpenCV
  Modelo de dados     Pydantic
  Armazenamento       GeoPackage
  Visualização        QGIS + OSM2World
  Exportação OSM      PyOsmium ou geração de OSM XML
  Testes              pytest
  Ambiente            Docker

### Tecnologias a introduzir mais tarde

-   **PostGIS:** quando for necessário processar várias localidades e
    gerir grandes volumes de dados.
-   **FastAPI:** quando existir uma interface ou outros clientes a
    consumir o sistema.
-   **Celery + Redis:** quando for necessário executar vários jobs em
    background.
-   **PyTorch:** quando os algoritmos geométricos tradicionais já não
    forem suficientes.
-   **React + TypeScript:** para uma interface web própria.
-   **MapLibre GL JS:** para visualização do mapa.
-   **Three.js:** para visualização e interação com os modelos 3D.

## 9. Plano de desenvolvimento

  -----------------------------------------------------------------------
  Fase                    Objetivo                Resultado
  ----------------------- ----------------------- -----------------------
  1\. Data ingestion      OSM + LiDAR + MDT       Dados preparados por
                                                  edifício

  2\. Basic 3D            Alturas e extrusão      Edifícios com volumes
                                                  simples

  3\. Roof reconstruction Deteção de planos e     Telhados planos, duas e
                          formas                  quatro águas

  4\. Building parts      Separação de volumes    Edifícios compostos por
                                                  várias partes

  5\. Appearance & export Cores e integração OSM  Modelos completos e
                                                  exportáveis
  -----------------------------------------------------------------------

### Critérios de sucesso do primeiro MVP

-   [ ] Processar automaticamente 50--100 edifícios.
-   [ ] Estimar alturas com LiDAR.
-   [ ] Gerar volumes com footprints existentes.
-   [ ] Identificar telhados planos e inclinados.
-   [ ] Reconstruir telhados de duas e quatro águas.
-   [ ] Identificar edifícios com mais de um volume.
-   [ ] Exportar GeoPackage e GLB.
-   [ ] Produzir um relatório de confiança por edifício.
-   [ ] Permitir revisão manual antes da exportação OSM.

## 10. Visualizador 3D: evolução futura

Inicialmente, recomenda-se utilizar QGIS e OSM2World. Se for necessária
uma interface web para rever edifícios individualmente, pode ser
adicionada uma stack frontend:

-   React + TypeScript;
-   MapLibre GL JS para o mapa;
-   Three.js para visualização e interação com os modelos 3D;
-   FastAPI para comunicar com o backend.

Não é necessário desenvolver um visualizador próprio para validar o
primeiro protótipo.

## 11. Licenciamento e integração com OSM

Os dados de origem devem ser avaliados antes de qualquer publicação. Em
particular, é necessário esclarecer a compatibilidade das licenças dos
dados LiDAR e ortofotos da DGT com a ODbL do OpenStreetMap, incluindo os
dados derivados.

A publicação de alterações em massa no OSM também exige preparação,
documentação, discussão comunitária, revisão e cumprimento das import
guidelines. O sistema deve, por isso, separar a geração de resultados da
sua publicação.

A exportação OSM deve ser uma etapa explícita e controlada, nunca uma
consequência automática do processamento.

## 12. Recomendação final

A recomendação é começar pelo motor Python de reconstrução 3D, sem
frontend e sem machine learning.

O principal desafio técnico é transformar uma nuvem de pontos numa
decomposição geométrica coerente de edifícios: identificar volumes
independentes, estimar alturas e reconstruir telhados simples.

Quando esse processo funcionar bem numa pequena área de estudo, será
possível acrescentar uma API, uma interface de revisão e uma integração
mais completa com o OpenStreetMap.

**Prioridade do MVP:** reconstruir corretamente 50--100 edifícios,
exportar as geometrias em GeoPackage/GLB e permitir a sua validação
visual antes de avançar para a publicação no OSM.
