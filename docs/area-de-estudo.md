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

| Quadrícula (x0_y0, EPSG:3763) | Edifícios | `building:levels` | `roof:shape` | Formas |
| --- | ---: | ---: | ---: | --- |
| **`-45000_179000`** ← desenvolvimento | 328 | 246 | 42 | hipped 25, gabled 13, lean_to 3, pyramidal 1 |
| `-44000_179000` ← 2.ª (adjacente, a este) | 196 | 161 | 46 | hipped 42, gabled 3, pyramidal 1 |
| `-46000_180000` | 518 | 247 | 37 | gabled 35, hipped 2 |
| `-46000_179000` | 486 | 277 | 20 | gabled 10, hipped 9, lean_to 1 |

Bbox WGS84 da quadrícula de desenvolvimento: `-8.67031, 41.27898, -8.65830, 41.28804`.

Notas:

- **`lean_to` aparece no OSM.** É um valor alternativo para `skillion`, e a avaliação
  tem de os tratar como equivalentes.
- **A grelha de 1 km a múltiplos de 1000 m em PT-TM06 é uma suposição.** Confirmar
  com a bbox dos ficheiros LAZ descarregados.
- **Edifícios que atravessam o limite entre quadrículas:** 11 na `-45000_179000`.
  Descarregar também a quadrícula adjacente resolve-os e duplica a referência de
  `hipped`.

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
