# Área de estudo

## Área inicial: Vilar e Mosteiró (Vila do Conde)

Mosteiró deixou de ser freguesia autónoma na reorganização administrativa de 2013.
Hoje faz parte da **União das Freguesias de Vilar e Mosteiró**, que é a unidade
administrativa usada aqui.

| | |
| --- | --- |
| Relação OSM | [5989576](https://www.openstreetmap.org/relation/5989576) (`admin_level=8`) |
| Bbox WGS84 | `-8.69911, 41.27348, -8.63541, 41.29553` |
| Bbox EPSG:3763 | `-47423, 178371, -42072, 180853` |
| Extensão | ~5,4 × 2,5 km |
| Quadrículas LiDAR (1 km) | ~18 na bbox (menos dentro do polígono) |

### Quadrícula de desenvolvimento

Resultado de `osm3d tiles`: 2 425 edifícios com ≥ 20 m² em 12 quadrículas de 1 km.
Cada edifício conta na quadrícula onde fica o seu centróide.

| DGT | x0_y0 (EPSG:3763) | Edifícios | `building:levels` | `roof:shape` | Formas |
| --- | --- | ---: | ---: | ---: | --- |
| **`155480`** | `-45000_179000` ← desenvolvimento | 328 | 246 | 42 | hipped 25, gabled 13, lean_to 3, pyramidal 1 |
| **`156480`** | `-44000_179000` ← 2.ª (a este) | 196 | 161 | 46 | hipped 42, gabled 3, pyramidal 1 |
| `154481` | `-46000_180000` | 518 | 247 | 37 | gabled 35, hipped 2 |
| `154480` | `-46000_179000` | 486 | 277 | 20 | gabled 10, hipped 9, lean_to 1 |

Bbox WGS84 da quadrícula de desenvolvimento: `-8.67031, 41.27898, -8.65830, 41.28804`.

Notas:

- **`lean_to` aparece no OSM.** É um valor alternativo para `skillion`, e a avaliação
  tem de os tratar como equivalentes.
- **Grelha confirmada.** A grelha oficial `LiDAR2024_2025_Seccionamento.gpkg`
  (em `data/raw/dgt/`, não versionada) tem 91 196 quadrículas de 1 km, todas alinhadas
  a múltiplos de 1000 m em EPSG:3763. O identificador DGT é
  `(x0/1000 + 200)(y_topo/1000 + 300)`, regra verificada em toda a grelha e
  implementada em `TileKey.dgt_name`.
- **Disponibilidade.** Nessa grelha (versão de 04/07/2025), as quadrículas de
  Vila do Conde aparecem como `DISPONIVEL=nao`. Essa informação está desatualizada:
  a pesquisa no portal (2026-11-11) devolve dados para `155480` e `156480`, com voo
  de julho de 2025 (`LO-155480-07-2025` v5 e `MDT-50cm-155480-07-2025` v3). Para
  saber o que existe, usar `osm3d dgt --dry-run` e não a coluna `DISPONIVEL`.
- **Download:** `osm3d dgt -t 155480 -t 156480` (LAZ + MDT-50cm, credenciais no `.env`).
- **Edifícios que atravessam o limite entre quadrículas:** 11 na `-45000_179000`.
  Descarregar também a quadrícula adjacente resolve-os e duplica a referência de
  `hipped`.

### Dados LiDAR verificados (quadrícula `155480`)

| | |
| --- | --- |
| Ficheiros | `LO-155480-07-2025_v05.laz` (163 MB), `MDT-50cm-155480-07-2025_v03.tif` (13 MB). A `156480` tem tamanhos semelhantes (143 MB + 13 MB). |
| Formato | LAS 1.4, formato de ponto 8, EPSG:3763 no cabeçalho, escala 1 mm |
| Pontos | 23,8 M em 1 km², ou seja **23,8 pts/m²** (18,4 pts/m² em 1.º retorno): mais do dobro dos 10 pts/m² da especificação |
| Classes | solo 42 %, vegetação (3–5) 54 %, **edifício (6) 3,9 % ≈ 19 pts/m² sobre os telhados**, ruído (7) 10 k pontos |
| Cor | RGB em 16 bits **+ NIR** (dá para calcular um índice de vegetação, NDVI, e limpar vegetação sobre telhados). Tem também campos extra: Amplitude, Reflectance, Deviation. |
| Outliers | Z máximo de 2 010 m (aves/nuvens): filtrar pela classe e pela cota |
| MDT-50cm | 2000 × 2000 float32, nodata −999, cotas de 28,5 a 74,9 m, mesma referência vertical do LAZ |

Densidade em edifícios pequenos: um anexo de 20 m² tem ~380 pontos de telhado, o
que chega para ajustar modelos de telhado.

### Alinhamento OSM ↔ LiDAR (quadrícula `155480`, 322 edifícios)

| | |
| --- | --- |
| IoU sem deslocamento (raster 0,25 m) | **0,784** |
| Área OSM coberta por pontos de classe 6 | **94,9 %** |
| Melhor deslocamento global | dx 0,00 m, dy −0,50 m, que só sobe o IoU para 0,798 |
| Edifícios OSM com < 20 % de cobertura LiDAR | 5 (2 %): demolidos, construídos depois do voo, ou mal classificados |

Conclusões:

- **Os contornos OSM desta zona estão bem alinhados com o LiDAR.** Basta um
  deslocamento global por quadrícula, que é pequeno. A pesquisa de deslocamento
  edifício a edifício (maximizar o IoU, ±3 m) **não é fiável**: 34 % dos edifícios
  "saltam" para o limite da pesquisa, atraídos por vizinhos e beirais, e o ganho
  mediano de IoU é de apenas 0,012. Não usar por edifício.
- **A área de classe 6 é 16 % maior do que a do OSM.** A diferença vem dos beirais
  e de edifícios que não estão mapeados no OSM. Detetar estes edifícios em falta
  seria um subproduto possível, mas está fora do âmbito.

## Primeiros resultados (quadrículas `155480` + `156480`, 2026-11-11)

`osm3d run --from prepare --to qa` seguido de `osm3d evaluate`:

- **Volume:** 509 edifícios dentro da cobertura, 504 reconstruídos, ~45 s.
- **Por rever:** 11 % dos edifícios têm sinalizações.
- **Co-registo:** deslocamento global de (0,00, −0,50) m em ambas as quadrículas.

| `roof:shape` vs. OSM (n = 82) | v1 (águas iguais) | v2 (cumeeira livre) | **v3 (RMSE ≤ 0,20 m)** |
| --- | ---: | ---: | ---: |
| Exatidão — todos | 66 % | 79 % | **87 %** |
| Exatidão — plantas retangulares (n = 60) | 72 % | 80 % | **87 %** |
| Cobertura (forma exportável) | 89 % | 85 % | 67 % |
| `hipped` → `pyramidal` | 13 | 5 | 3 |
| Edifícios a rever | 8 % | 11 % | 26 % |

**v3: limite de RMSE.** Os erros concentravam-se em RMSE entre 0,24 e 0,27 m: telhados
complexos (mansardas, vários níveis, cumeeira descentrada) para os quais `skillion`
era só o modelo "menos mau". Com `max_rmse_m: 0.20` esses casos passam a `unknown` e
vão para revisão em vez de serem exportados com a forma errada. Troca-se cobertura por
precisão, que é o lado certo para edições no OSM. **O limite foi calibrado em apenas
82 edifícios** e tem de ser confirmado noutras quadrículas.

**Telhados partilhados.** Para grupos de edifícios encostados cuja planta conjunta é
retangular, ajusta-se também um telhado único ao grupo. Esse telhado é usado se
explicar os pontos melhor do que os telhados individuais, pelo mesmo critério
penalizado. Funciona no caso clássico (casa geminada dividida ao longo da cumeeira,
ver testes), mas aqui só mudou 2 edifícios. Os erros "→ `skillion`" que motivaram
a ideia não eram telhados contínuos: eram conjuntos com planta não retangular
(retangularidade da união 0,40–0,54) ou telhados realmente diferentes.

**Formas por validar.** A referência OSM tem só 3 `skillion` e **nenhum `flat`**. As
previsões destas formas (110 e 112 edifícios exportáveis) são sobretudo anexos baixos:
mediana de ~50 m² e 3–4 m de altura. É plausível, mas não está validado. Precisa de
amostragem manual.

**Lição da v1.** Nos `hipped` desta zona as águas das pontas são muitas vezes mais
inclinadas do que as laterais, e por isso a cumeeira é mais curta do que `2·(a − b)`.
Com todas as águas à mesma inclinação, a pirâmide ajustava melhor. A v2 procura o
comprimento da cumeeira em grelha.

**Erros restantes.**

| Situação | Exatidão |
| --- | ---: |
| Edifícios isolados | **88 %** |
| Edifícios encostados a vizinhos | 76 % |

- 5 dos 6 erros "→ `skillion`" são casas geminadas ou em banda. O contorno OSM
  cobre só uma parte de um telhado contínuo, e essa parte parece uma água só.
  Próximo passo: ajustar o telhado sobre o grupo de edifícios encostados e
  atribuir o resultado a cada um, ou tratar o caso na Fase 4 (partes).
- Os `unknown` são sobretudo edifícios grandes e não retangulares
  (retangularidade < 0,65), que precisam de partes.
- **A referência OSM também tem erros.** A série `way/92165xxxx` tem tags que
  parecem atribuídas por defeito. A exatidão real deve ser algo superior à medida.

**Alturas.** Não há `height` no OSM. Comparando com `building:levels` (n = 391), a
altura por piso implícita tem mediana de 3,33 m, P10 de 2,39 m e P90 de 5,18 m;
64 % dos casos ficam entre 2,4 e 4,0 m. É coerente, mas a referência é fraca:
`building:levels` costuma omitir caves, pisos recuados e pés-direitos altos.

**Exportação JOSM** (`osm3d stage export`):

- **377 de 509 edifícios exportados**; os 132 sinalizados ficam de fora.
- **Tags acrescentadas:**

  | Tag | Edifícios |
  | --- | ---: |
  | `height` | 377 |
  | `roof:shape` | 322 |
  | `roof:height` | 265 |
  | `roof:direction` | 110 |
  | `roof:orientation` | 29 |

- **8 conflitos com tags existentes**, quase todos `roof:shape`. O valor OSM é
  mantido e o conflito fica registado em `data/output/vilar-e-mosteiro_export.csv`.
- **O ficheiro `data/output/vilar-e-mosteiro_josm.osm` tem `upload="never"`.** O JOSM
  abre-o e valida-o, mas recusa o upload até a licença estar resolvida.

**Ver no QGIS:** abrir `data/processed/vilar-e-mosteiro/06_qa.gpkg`.

- Camada `building_parts`: simbolizar por `roof_shape`. Na vista 3D, extrudir por
  `height`.
- Camada `buildings`: filtrar `needs_review = 1` para ver o que precisa de revisão.
- Detalhe por edifício: `data/output/vilar-e-mosteiro_avaliacao.csv`.

## Área futura: concelho de Vila do Conde

| | |
| --- | --- |
| Relação OSM | [3892769](https://www.openstreetmap.org/relation/3892769) (`admin_level=7`) |
| Bbox WGS84 | `-8.76805, 41.25549, -8.61062, 41.42571` |
| Extensão | ~13,3 × 19,0 km |
| Quadrículas LiDAR (1 km) | ~300 na bbox |

## Estado atual do OSM (levantamento Overpass, 2026-11-11)

| | Vilar e Mosteiró | Concelho |
| --- | ---: | ---: |
| Edifícios | 2 679 | 28 246 |
| com `height` | 4 | 13 |
| com `building:levels` | 1 545 (58 %) | 11 681 (41 %) |
| com `roof:shape` | 173 (6 %) | 3 831 (14 %) |
| `building:part` | 8 | 64 |

### O que estes números mostram

- **`height` praticamente não existe.** É aqui que o projeto acrescenta mais valor,
  mas não há referência de altura no OSM. Para avaliar as alturas são precisas
  outras referências:
  - `building:levels`, como referência fraca (pisos × ~3 m + telhado);
  - medições manuais sobre o próprio LiDAR para um subconjunto.
- **Há 173 `roof:shape` mapeados à mão na área inicial e 3 831 no concelho.** Formam
  um conjunto de referência para avaliar a classificação de telhados sem custo de
  anotação.
- **A taxa de `building:levels` é alta**, o que indica mapeadores locais ativos em 3D.
  Daqui resultam duas consequências:
  - a política de merge tem de respeitar estes valores (`overwrite_existing_tags: false`);
  - vale a pena contactar estes mapeadores antes de qualquer edição automatizada.
- **A dimensão está acima do MVP de 50–100 edifícios.** Proposta:
  1. desenvolver numa quadrícula de 1 km com a maior mistura de formas;
  2. avaliar na união de freguesias completa (~2 700 edifícios).

### Implicações de escala (concelho)

- ~28 000 edifícios e ~300 quadrículas LAZ (~10 pts/m² ⇒ ordem de 10⁷ pontos por
  quadrícula). O processamento tem de ser feito **por quadrícula**, em streaming,
  sem carregar a nuvem toda em memória. Os edifícios que atravessam o limite entre
  quadrículas precisam dos pontos das quadrículas vizinhas.
- GeoPackage continua a chegar a esta escala. PostGIS só passa a ser necessário se
  houver vários concelhos ou acesso concorrente.
- Para o concelho, usar um extrato PBF (Geofabrik Portugal + `osmium extract` pelo
  polígono) em vez de Overpass. A instância pública do Overpass esteve instável
  durante o levantamento (respostas 502 e 504).
