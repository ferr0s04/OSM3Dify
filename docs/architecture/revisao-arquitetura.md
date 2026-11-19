# Revisão crítica da arquitetura

Este documento revê o [documento de arquitetura original](arquitetura-osm-3d-building-generator.md)
e regista as decisões que divergem dele. O documento original mantém-se inalterado
como referência; **quando houver conflito, prevalece este documento**.

---

## 0. Veredicto

O projeto é **tecnicamente viável** para o objetivo declarado (edifícios 3D simples
no estilo F4 Map). Os dados existem e têm qualidade suficiente:

- O LiDAR da DGT para Portugal continental tem densidade média especificada de
  **≥ 10 pontos/m²**, em LAZ (LAS 1.4) classificado, em quadrículas de 1 km × 1 km,
  com download gratuito no Centro de Dados da DGT (conta necessária, há API STAC).
  Isto chega para ajustar telhados simples em edifícios a partir de ~30–40 m² de
  área de cobertura (centenas de pontos por edifício).
- A stack Python proposta é, no essencial, adequada.

Mas o plano tem **quatro problemas estruturais** que convém corrigir antes de escrever
código:

1. **O produto final não é um modelo 3D, são tags OSM.** O F4 Map (e o OSM2World,
   Streets GL, OSM Buildings…) não lê GLB: renderiza as tags do esquema
   [Simple 3D Buildings (S3DB)](https://wiki.openstreetmap.org/wiki/Simple_3D_Buildings).
   O modelo intermédio e os algoritmos têm de ser desenhados para *produzir parâmetros
   S3DB*, e não malhas arbitrárias.
2. **A abordagem de telhados deve ser orientada a modelos, não a planos.** O plano
   propõe RANSAC → planos → classificação. Para formas S3DB é mais robusto ajustar
   diretamente modelos paramétricos (flat, skillion, gabled, hipped, pyramidal) e
   escolher o melhor.
3. **A licença é um bloqueador potencial e está no fim do plano.** CC BY 4.0 **não**
   é automaticamente compatível com a ODbL. Tem de ser resolvido na Fase 0, não na 5.
4. **A divisão em `building:part` é o passo mais caro e mais invasivo**, tanto
   algoritmicamente como na edição do OSM. A maior parte do valor visual obtém-se
   antes disso, com tags no próprio contorno do edifício.

---

## 1. O alvo real: Simple 3D Buildings

O que o F4 Map mostra é a interpretação de um conjunto pequeno de tags. O sistema
deve gerar exatamente isso:

| Tag | Significado | Nota |
| --- | --- | --- |
| `height` | Altura do chão ao **topo do telhado** (inclui o telhado) | O exemplo JSON original (`height: 8.5` + `roof.height: 2.2`) é ambíguo — fixar esta convenção. |
| `min_height` | Cota da base da parte acima do chão | Necessário para partes suspensas/sobrepostas. Faltava no modelo. |
| `roof:shape` | `flat`, `skillion`, `gabled`, `hipped`, `pyramidal`, … | Ver §2. |
| `roof:height` | Altura do telhado (beiral → cumeeira) | Só para telhados não planos. |
| `roof:orientation` | `along` / `across` em relação ao lado maior | **Faltava.** Sem isto metade dos telhados de duas águas sai com a cumeeira rodada 90°. |
| `roof:direction` | Azimute para onde o telhado "olha" | Necessário para `skillion`. Faltava. |
| `roof:colour`, `building:colour` | Cor (nome CSS ou `#rrggbb`) | Ver §6. |
| `building:part=*` | Volume independente | Ver §3 e §7. |

Consequências:

- O **modelo intermédio** (`src/osm3d/domain/models.py`) passa a ter `min_height`,
  azimute da cumeeira e azimute de orientação, e cada atributo estimado é um
  `Estimate(value, confidence, source)` em vez de dicionários paralelos
  `confidence`/`sources`.
- O mapeamento modelo → tags vive num único sítio (`export/s3db_tags.py`) e é testado.
- `building:levels` **não** deve ser inferido automaticamente a partir da altura
  (é informação verificável no terreno; adivinhá-la é ruído). Pode ser sugerido
  para revisão manual.
- Arredondar valores (p.ex. `height` a 0,5 m) — precisão centimétrica é falsa
  precisão e gera ruído no histórico do OSM.

## 2. Reconstrução de telhados: model-driven

**Proposta original:** DBSCAN + RANSAC para extrair planos, depois classificar a forma.

**Problema:** a extração de planos em dados reais produz planos espúrios (chaminés,
claraboias, painéis solares, vegetação), planos em falta (águas pequenas) e uma
topologia que depois tem de ser "traduzida" de volta para uma das 5–6 formas S3DB.
É o caminho certo para reconstrução LoD2 genérica, mas é excessivo para S3DB.

**Proposta revista:** para cada parte (inicialmente = edifício inteiro):

1. Calcular o retângulo mínimo orientado do polígono → eixo principal.
2. Ajustar por mínimos quadrados robustos (Huber/RANSAC sobre *parâmetros*, não sobre
   planos) cada modelo paramétrico candidato, com cumeeira ao longo e através do eixo:
   - `flat` (1 parâmetro), `skillion` (plano inclinado), `gabled`, `hipped`, `pyramidal`.
3. Escolher o modelo por critério penalizado (BIC/AIC) sobre os resíduos, e derivar a
   confiança da margem entre o melhor e o segundo melhor modelo.
4. Resíduo alto em todos os modelos → `unknown` e sinalização para revisão.

Vantagens: devolve diretamente `roof:shape`, `roof:height`, `roof:orientation` e
`roof:direction`; é robusto a ruído; é fácil de testar com nuvens sintéticas.

Alterações à lista de formas: acrescentar **`skillion`** (uma água — muito comum em
anexos e construções em Portugal) e **`pyramidal`** (quatro águas num ponto — facilmente
confundido com `hipped` em plantas quase quadradas). Ambas são baratas com esta abordagem.

RANSAC de planos (scikit-learn/Open3D) fica como ferramenta de diagnóstico e,
eventualmente, para a deteção de partes (§3).

## 3. Ordem do pipeline e partes

O documento coloca *Roof Segmentation* (C) antes de *Volume Reconstruction* (D), mas a
forma de telhado é uma propriedade **de cada parte** — as partes têm de existir antes
do ajuste de telhado. Ordem revista:

```text
acquisition → footprints + co-registo → alturas (nDSM) → partes → telhados por parte → cores → QA → export
```

O plano de fases original continua correto *se* as Fases 2–3 assumirem
"1 edifício = 1 parte". A Fase 4 introduz a segmentação.

Para a segmentação em partes, usar o **nDSM rasterizado** (0,25–0,5 m) dentro do
footprint: descontinuidades de altura (gradiente) + regiões com modelo de telhado
coerente, e depois regularização das fronteiras para polígonos retilíneos alinhados
com o eixo do edifício (o S3DB renderiza mal telhados inclinados em partes irregulares).

## 4. Dados e pré-processamento: riscos não cobertos

- **Co-registo footprint ↔ LiDAR.** Os contornos OSM são muitas vezes desenhados sobre
  imagens com desvio de 1–3 m. Com isto, os pontos de bordo de um edifício caem no
  vizinho ou no chão. É preciso estimar um deslocamento (por zona e/ou por edifício)
  maximizando a sobreposição entre o footprint e a máscara de pontos classe 6 do LiDAR.
  Novo módulo: `geospatial/registration.py`.
  *Medido em Vila do Conde (ver [validação](../validacao.md)):* o desvio é
  pequeno (≈ 0,5 m, IoU 0,78 → 0,80). Basta **um deslocamento global por
  quadrícula**. A otimização edifício a edifício não é fiável e foi descartada.
- **Sistemas de referência.** OSM está em WGS84 (EPSG:4326); a DGT em PT-TM06/ETRS89
  (EPSG:3763). Todo o processamento é feito em EPSG:3763 (métrico); só a exportação
  OSM reprojeta.
- **Diferença temporal.** Edifícios construídos/demolidos entre o voo LiDAR e o estado
  atual do OSM. O QA tem de detetar footprints sem pontos de edifício (e vice-versa).
- **Vegetação sobre telhados.** Usar a classificação (classe 6 = edifício) e excluir
  classes 3–5; o P95 é sensível a árvores sobre o telhado.
- **Cota do terreno.** Concordo com o alerta do documento. Convenção proposta:
  percentil baixo do MDT num anel à volta do footprint (buffer de 1–3 m), usando o
  MDT do *mesmo* levantamento LiDAR. Documentar a convenção no código.
  **Encosta e patamares** (`reconstruction/ground.py`): o percentil baixo apanha a
  estrada abaixo de um muro de suporte e dá alturas excessivas (exemplo
  `way/1441884233`: 9,1 m em vez de ~4,3 m). Por isso mede-se o terreno no pé das
  paredes, troço a troço. Com desnível > 2 m, escolhe-se entre "chão baixo" e
  "terreno do lote":
  1. se o beiral fica a menos de 2,2 m do lote, o edifício está encaixado na
     encosta e usa-se o chão baixo;
  2. senão, com `building:levels`, usa-se o chão que dá uma altura por piso plausível;
  3. sem evidência, usa-se o chão baixo, com sinalização `terrain_step` para revisão.

  Na aplicação de revisão, o terreno de referência pode ser trocado num clique.
- **Ortofotos não "true ortho".** Em ortofotos convencionais o telhado está deslocado
  relativamente ao footprint (inclinação do edifício). Amostrar a cor numa máscara
  derivada do LiDAR, erodida, e não diretamente no footprint OSM.

## 5. Revisão das ferramentas

| Ferramenta | Decisão | Razão |
| --- | --- | --- |
| **OSMnx** | ❌ Remover | Pensado para redes viárias; converte para GeoDataFrame e perde IDs de nós, versões e estrutura de relações — tudo indispensável para reescrever no OSM sem conflitos. |
| **Overpass API** | ✅ Manter, com `out meta` | Para áreas pequenas. Guarda `id`, `version`, nós e relações (multipolígonos com pátios). |
| **pyosmium** | ✅ Para extratos PBF (Geofabrik) | Para áreas maiores e para escrever ficheiros OSM. Opcional no MVP. |
| **laspy[lazrs]** | ✅ Leitor principal de LAZ | Instala-se com `pip`/`uv` em Windows; suficiente para recortar pontos por footprint. |
| **PDAL** | ⚠️ Opcional, como ferramenta externa | Excelente (`filters.hag_nn`, `filters.crop`, tiling), mas as bindings Python em Windows praticamente exigem conda. Usar via CLI/pipelines JSON para pré-processamento pesado, não como dependência do pacote. |
| **Rasterio** | ✅ Manter | MDT/MDS/ortofotos. |
| **GeoPandas / Shapely 2 / pyproj** | ✅ Manter | |
| **NumPy / SciPy / scikit-learn** | ✅ Manter | Ajuste de modelos, KD-tree, DBSCAN, RANSAC. |
| **Open3D** | ⏸️ Adiar | Dependência pesada, suporte a versões recentes de Python costuma atrasar; o que se usaria dele (RANSAC de planos, DBSCAN) existe em scikit-learn. |
| **OpenCV** | ❌ Substituir por scikit-image | Só é preciso conversão RGB→CIELAB e estatística de cor. |
| **Trimesh** | ⏸️ Opcional (extra `mesh`) | GLB é útil para inspeção, mas não é o produto final. |
| **OSM2World** | ✅ **Validador principal** | Lê ficheiros `.osm` locais e interpreta S3DB como os renderizadores reais — valida *exatamente* o que vai ser publicado. Mais fiel do que o nosso `geometry_builder`. |
| **QGIS / JOSM** | ✅ Manter | JOSM é o ponto de revisão humana e de upload. |
| **Pydantic** | ✅ Manter | Modelo de domínio e configuração. |
| **GeoPackage** | ✅ Também como *store* do modelo intermédio | Camadas `buildings`, `building_parts`, `qa_flags`: abre-se diretamente no QGIS. |
| **Docker** | ⏸️ Adiar | Com `uv` e wheels binárias (rasterio, pyogrio, shapely) o ambiente é reprodutível em Windows/Linux. Docker quando houver workers/API. |
| **FastAPI / Celery / Redis / PostGIS / React / Three.js** | ⏸️ Fora do MVP | Concordo com o documento. |
| **PyTorch** | ⏸️ Fora do âmbito previsível | Os métodos geométricos chegam para S3DB. |

## 6. Cores

- **Fonte principal: o RGB dos próprios pontos LiDAR.** A nuvem da DGT vem colorida
  (ver [`data/reference/dgt-lidar-rgb-sem-fachadas.png`](../../data/reference/dgt-lidar-rgb-sem-fachadas.png)),
  com cores de telhado próximas das reais. Como a cor já está associada a cada ponto,
  amostrar só os pontos que encaixam no modelo de telhado ajustado (inliers) evita o
  problema do deslocamento da ortofoto e exclui chaminés, claraboias e vegetação. A
  ortofoto passa a ser fonte secundária. Atenção: os bordos dos telhados têm alguns
  pontos com a cor do chão ou da vegetação (artefacto da colorização), por isso há que
  erodir a máscara.
- Concordo: **não** inferir cor de fachada a partir de imagem aérea. A imagem de
  referência confirma que o LiDAR aéreo **não tem pontos nas fachadas**. O módulo de
  fachadas sai do MVP (o campo existe no modelo, com `source=manual`).
- Agrupar em CIELAB, como proposto, mas **aproximar o resultado a uma paleta pequena**
  (nomes comuns no OSM: `red`, `brown`, `grey`, `darkgrey`, `white`, `black`…).
  Ortofotos têm dominantes de cor, sombras e variação entre voos; um hex preciso é falsa
  precisão e torna o mapa visualmente inconsistente.
- Considerar `roof:material` (`roof_tiles`, `concrete`, `metal`) — frequentemente
  mais fiável do que a cor e útil para o F4.

## 7. Integração com o OSM

- **Exportar ficheiros `.osm` no formato JOSM** (`action="modify"`, com `id` e
  `version` originais). O utilizador abre no JOSM, revê, e faz upload. O sistema
  nunca faz upload diretamente — reforça a separação geração/publicação pedida no
  documento.
- **Política de merge explícita:** nunca sobrescrever tags existentes mapeadas por
  humanos, salvo opção explícita; conflitos vão para o relatório. Rejeitar a edição
  se a `version` no OSM mudou desde o download.
- **Partes no OSM são caras topologicamente:** as `building:part` devem partilhar nós
  com o contorno (reutilizar IDs e inserir nós novos nos lados do contorno), ou o
  validador do JOSM queixa-se. Na Fase 4, as partes ficam apenas em GeoPackage/GLB;
  exportação de partes para OSM é uma fase posterior.
- Seguir o [Automated Edits code of conduct](https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct)
  e as [Import/Guidelines](https://wiki.openstreetmap.org/wiki/Import/Guidelines):
  conta dedicada, documentação na wiki e discussão prévia com a comunidade portuguesa
  (fórum `community.openstreetmap.org`, categoria Portugal).

## 8. Licenciamento — passa a Fase 0

- O LiDAR da DGT é disponibilizado sob **CC BY 4.0**.
- Segundo o OSMF Licensing Working Group, CC BY 4.0 **não** é compatível com a ODbL
  sem permissão adicional explícita do titular (renúncia às cláusulas de atribuição
  e de medidas técnicas). Há precedentes na lista de fontes portuguesa da wiki OSM de
  fontes CC BY 4.0 marcadas como não compatíveis.
- Alturas e formas de telhado derivadas do LiDAR são dados derivados — a questão
  aplica-se.
- **Ação:** antes de investir na Fase 5, pedir à DGT permissão explícita para uso no
  OSM (há um modelo de *waiver* da OSMF) e confirmar o estado na página
  *Contributors* / lista de fontes PT. Se não for obtida, o projeto continua útil para
  GeoPackage/GLB/visualização, mas não para publicar no OSM.
- **Estado (2026-11-11): licença obtida.** A exportação `.osm` deixou de bloquear o
  upload no JOSM (`export.josm_upload: "true"`).
  - **Por fazer:** guardar o documento da autorização e confirmar a atribuição na
    página *Contributors* do OSM.
  - **Modelo de edição (decisão do projeto):** as alterações são preenchidas pela
    importação do `.osm` gerado, mas a submissão é sempre manual, no JOSM. Antes de
    submeter, qualquer edifício pode ser ajustado ou descartado. O sistema nunca faz
    upload.
  - **Ainda assim:** trazer dados externos para o OSM em quantidade conta como
    importação nas regras da comunidade, mesmo com submissão manual. Convém documentar
    o processo na wiki e anunciá-lo à comunidade portuguesa antes de submeter em volume.

## 9. Avaliação (em falta no plano original)

Os critérios de sucesso do MVP não dizem *como* se mede "reconstruir corretamente".
Proposta:

- Usar edifícios **já mapeados no OSM com tags S3DB** (`height`, `roof:shape`) na
  área de estudo como conjunto de referência — custa zero e é exatamente o formato
  de saída.
- Complementar com ~30–50 edifícios anotados manualmente em `data/reference/`.
- Métricas: erro absoluto de `height` (mediana, P90), exatidão/matriz de confusão de
  `roof:shape`, erro angular de orientação, % de edifícios sinalizados para revisão.
- Módulo `evaluation/` + comando `osm3d evaluate`.

## 10. Plano de desenvolvimento revisto

| Fase | Objetivo | Resultado |
| --- | --- | --- |
| **0. Enquadramento** | Licença DGT↔ODbL; escolher área de estudo; conjunto de referência | Decisão go/no-go para publicação; dataset de avaliação |
| 1. Data ingestion | Overpass (`out meta`) + LAZ/MDT da DGT, recorte e co-registo | Pontos e footprints alinhados por edifício |
| 2. Alturas | nDSM, cota do terreno, `height` | GeoPackage com alturas + avaliação |
| 3. Telhados | Ajuste de modelos paramétricos (1 parte = 1 edifício) | `roof:shape/height/orientation/direction` + avaliação |
| 3b. Export & revisão | Tags S3DB → `.osm` JOSM, OSM2World | **Primeiro resultado publicável** (se licença OK) |
| 4. Partes | Segmentação em volumes | Partes em GeoPackage/GLB |
| 5. Aparência | Cor de telhado (paleta), `roof:material` | Tags de cor com confiança |
| 6. Partes no OSM | Topologia partilhada com o contorno | Export de `building:part` |

A grande mudança é a **Fase 3b**: com alturas e telhados em edifícios de uma só parte
já se obtém a maior parte do efeito visual do F4 Map, com edições pouco invasivas
(apenas tags em ways existentes). Só depois se ataca o problema difícil das partes.

## 11. Estrutura do repositório: alterações

- Pacote único `src/osm3d/` em vez de pacotes soltos em `src/` (nomes como
  `export`, `domain` ou `validation` como pacotes de topo colidem facilmente).
- `reconstruction/` reorganizado: `heights.py`, `parts.py`, `roof_models.py`
  (modelos paramétricos), `roof_fitting.py`.
- Novos: `geospatial/registration.py`, `export/s3db_tags.py`, `evaluation/`,
  `pipeline.py` (artefactos por fase, para re-executar fases isoladamente).
- Removidos do MVP: `appearance/facade_colours.py`, `api/`, `docker/`.
- `data/reference/` para o conjunto de avaliação.

## Fontes

- [DGT — Webinar LiDAR (jul. 2025)](https://www.dgterritorio.gov.pt/sites/default/files/ficheiros-artigos/Apresentacoes/2025/5-Webinar_LiDAR_15jul2025.pdf)
- [SNIG — Reunião CO-SNIG, BDNC/LiDAR (nov. 2024)](https://snig.dgterritorio.gov.pt/sites/default/files/documentos/879/R35-CO-SNIG_R25-CCC_28novembro2024_DGT_BDNC_LiDAR.pdf)
- [Tutorial de download do LiDAR DGT via STAC (GMT.jl)](https://www.generic-mapping-tools.org/GMTjl_doc/tutorials/dgt_lidar/dgt_lidar.html)
- [OSM wiki — Portugal/Lista de Fontes](https://wiki.openstreetmap.org/wiki/Lista_de_fontes)
- [talk-pt — Licença e utilização dos dados SIG no OpenStreetMap](https://lists.openstreetmap.org/pipermail/talk-pt/2021-March/001932.html)
- [OSM wiki — Simple 3D Buildings](https://wiki.openstreetmap.org/wiki/Simple_3D_Buildings)
