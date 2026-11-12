# Frontend: aplicação de revisão

**Estado:** Fase A em implementação (`uv run osm3d review`). Ver §8 para o que foi
implementado e para as decisões tomadas pelo caminho.

## 1. Contexto

Pôs-se a hipótese de um frontend que gerisse tudo:

- visualização dos GeoPackages, sem depender do QGIS;
- seleção da área de estudo;
- edição de edifícios;
- gestão de downloads.

A conclusão é que **vale a pena ter um frontend, mas centrado na revisão dos
resultados**, e só depois de o pipeline produzir resultados reais. Algumas das
funções propostas já são bem resolvidas por ferramentas existentes e não devem
ser reimplementadas.

## 2. Decisão

### O que entra

| Função | Decisão | Justificação |
| --- | --- | --- |
| **Revisão de edifícios** | ✅ Núcleo da aplicação | Nenhuma ferramenta existente compara o modelo 3D proposto com os pontos LiDAR de cada edifício nem gere uma fila de revisão. A revisão humana é também uma exigência para publicar no OSM (Import Guidelines / Automated Edits). |
| **Visualização de resultados** | ✅ Integrada na revisão | O QGIS abre os GeoPackages, mas a inspeção edifício a edifício (modelo vs. nuvem de pontos) é lenta. |
| **Seleção de área** | ✅ Separador "Dados" | Áreas criadas, editadas e ativadas na aplicação (registo `data/areas.json`); o `config.yaml` só dá a área inicial. |
| **Gestão de downloads/processamento** | ✅ | Downloads em segundo plano, eliminação de ficheiros raw por quadrícula e pipeline (fetch → qa) da área ativa em segundo plano (ver §8). |

### O que não entra

| Função | Decisão | Justificação |
| --- | --- | --- |
| **Edição geométrica de edifícios** (contornos, nós, partes) | ❌ Não implementar | O JOSM já resolve isto: topologia com nós partilhados, validador, deteção de conflitos, upload com changesets. Reimplementá-lo levaria meses e editar contornos não é objetivo do projeto. |
| **Upload para o OSM** | ❌ Não implementar | A publicação passa sempre pelo JOSM, com revisão humana explícita (ver [revisão da arquitetura §7](revisao-arquitetura.md#7-integração-com-o-osm)). |

A aplicação **edita tags propostas** (forma, altura, orientação, cor), não
geometria. O JOSM continua a ser o último passo antes da publicação.

## 3. Fluxo de revisão

```text
pipeline (CLI)                aplicação de revisão                    JOSM
──────────────                ─────────────────────                   ────
fetch → … → roofs → qa  ──▶  fila ordenada por confiança/flags  ──▶  .osm (action=modify)
     GeoPackage              ├─ mapa (contorno + vizinhança)          revisão final
                             ├─ 3D: modelo proposto + pontos LiDAR    upload pelo utilizador
                             ├─ tags propostas vs. existentes
                             └─ aceitar / rejeitar / corrigir
                                      │
                                      ▼
                             estado de revisão no GeoPackage
```

Ecrã principal de revisão:

- **Fila:** edifícios por rever. Primeiro os sinalizados e os de baixa confiança,
  com filtros por forma, flag e quadrícula.
- **Mapa:** contorno OSM, vizinhança e ortofoto, se disponível.
- **Vista 3D:**
  - modelo S3DB proposto (volumes e telhado);
  - pontos LiDAR do edifício com RGB, sobrepostos;
  - resíduos do ajuste por cor, opcional.
- **Painel de tags:**
  - tags propostas vs. tags existentes no OSM, com os conflitos assinalados;
  - campos editáveis para forma, `height`, `roof:height`, orientação e cor;
  - pré-visualização imediata do modelo depois de cada correção.
- **Ações:** aceitar, rejeitar, corrigir e aceitar, ou saltar. Há atalhos de
  teclado para rever centenas de edifícios depressa.

## 4. Arquitetura proposta

| Camada | Tecnologia | Notas |
| --- | --- | --- |
| Backend | **FastAPI** | Fina, por cima do pacote `osm3d`, sem duplicar lógica. |
| Tarefas longas | Mecanismo simples em processo (fila + worker local) | Downloads (~160 MB/quadrícula) e processamento não correm dentro de pedidos HTTP. **Sem Celery/Redis** enquanto houver um só utilizador local. |
| Dados | **GeoPackage** (o mesmo do pipeline) | O estado de revisão vive no modelo intermédio, não numa base de dados à parte. |
| Mapa | **MapLibre GL JS** | Contornos, vizinhança, ortofotos. |
| 3D | **three.js** | Modelo S3DB + nuvem de pontos do edifício. Alternativa: deck.gl (`PointCloudLayer` + `SimpleMeshLayer`). |
| UI | React + TypeScript, ou uma alternativa mais leve | A decidir quando a aplicação avançar. |

Pontos de atenção:

- **A geometria 3D do telhado tem de ser gerada a partir do mesmo modelo
  paramétrico** (`reconstruction/roof_models.py`) que o pipeline usa, idealmente
  no backend como mesh/GLB. Senão, o que se revê não é o que se exporta.
- **Os pontos LiDAR por edifício** são servidos sob pedido, recortados do LAZ e
  subamostrados. Nunca se envia a quadrícula inteira para o browser.
- **A vista 3D não substitui o OSM2World.** O OSM2World continua a ser a
  verificação final de como as tags são renderizadas.

## 5. Implicações imediatas no modelo de dados

Uma alteração pequena, que serve já a exportação para JOSM e mais tarde a aplicação:

- `review_status`: `pending` | `accepted` | `rejected`
- `review_overrides`: valores corrigidos manualmente (forma, alturas, orientação,
  cor), com `source=manual`
- `reviewed_at`, `review_note`

A exportação `.osm` inclui **apenas edifícios `accepted`**, com as correções
aplicadas (`export.include_pending: false`, por omissão desde 2026-11-11).

## 6. Faseamento

| Fase | Entrega | Condição para avançar |
| --- | --- | --- |
| **Agora** | Pipeline com resultados reais: alturas, telhados, avaliação contra os `roof:shape` existentes. Inspeção via QGIS ou um relatório HTML estático por quadrícula (mapa + tabela + 3D, sem servidor). Estado de revisão no modelo (§5). | — |
| **A. Aplicação de revisão** | Fila, mapa, 3D modelo vs. pontos, edição de tags, exportação dos aceites para `.osm` JOSM. | Ajuste de telhados com exatidão aceitável nos dados reais (a definir após a primeira avaliação). |
| **B. Gestão de áreas e tarefas** | Seleção de área no mapa, estado por quadrícula (descarregada/processada/revista), lançamento de downloads e processamento em segundo plano. | Avançar para o concelho de Vila do Conde (~28 000 edifícios, ~170 quadrículas). |

## 8. Implementação (Fase A, 2026-11-11)

`uv run osm3d review` arranca a aplicação em `http://127.0.0.1:8765/`.

### Componentes

| Componente | Ficheiro | Notas |
| --- | --- | --- |
| API | `src/osm3d/app/server.py` | FastAPI; `/api/docs` com a especificação. |
| Lógica | `src/osm3d/app/service.py` | Junta resultados (GeoPackage da fase QA), revisões, pontos e malhas. |
| Revisões | `src/osm3d/review.py` | Decisões e correções num SQLite próprio (`reviews.sqlite`). |
| Malha 3D | `src/osm3d/export/mesh.py` | Gerada no backend a partir do **mesmo** modelo paramétrico do ajuste (`Roof.model`). |
| Pontos | `src/osm3d/app/points.py` | Cache `.npz` por quadrícula (~38 MB): classe 6 completa + 1/8 de solo/vegetação. LRU de 6 quadrículas em memória. A 1.ª leitura de cada quadrícula demora ~15 s. |
| Frontend | `src/osm3d/app/static/` | Módulos ES sem build. MapLibre GL 4.7.1 e three.js 0.169.0 via CDN. |

### Decisões tomadas na implementação

- **Sem passo de build (sem React nem npm).** Arranca com um só comando Python. Se a
  aplicação crescer, migra-se para React + TypeScript.
- **Revisões fora do GeoPackage das fases** (diverge do §5). Voltar a correr o pipeline
  substituiria o artefacto e apagaria o trabalho humano. As revisões aplicam-se por
  cima dos resultados, na aplicação e em `osm3d stage export`. Uma revisão feita sobre
  outra versão OSM do edifício fica marcada `review_stale` e volta a "por rever".
- **Cores reais na visualização.** O mapa e a vista 3D usam `roof:colour` e
  `building:colour` **finais**: o valor do OSM se existir, senão o estimado ou o
  corrigido. Sem tag, usa-se a cor por omissão dos renderizadores (`#c8c8c8` no
  telhado, `#e9e4da` nas fachadas). A forma do telhado continua disponível como
  modo de análise no mapa.
- **Relação com o OSM destacada.** Cada edifício é classificado em:
  - **novo:** sem 3D no OSM;
  - **complementa:** acrescenta tags em falta;
  - **diverge:** propõe valores diferentes dos mapeados;
  - **igual:** não acrescenta nada.

  O mapa destaca a roxo o contorno dos que já têm 3D no OSM e tem um modo de cor
  próprio para esta classificação. A lista tem filtro e um selo "OSM 3D ≠ / + / =",
  e o detalhe mostra as tags OSM existentes e as divergências. Ver
  `src/osm3d/export/osm_changes.py`.
- **Edição:** forma (incluindo `side_hipped`), `height`, `roof:height`,
  `roof:orientation`, `roof:direction`, `roof:colour`, `building:colour` e nota. A
  pré-visualização é imediata, e o modelo 3D é regenerado a partir das correções.
- **Atalhos:**

  | Tecla | Ação |
  | --- | --- |
  | `J` / `K` | seguinte / anterior |
  | `A` | aceitar |
  | `R` | rejeitar |
  | `U` | repor |

  Botão "carregar no JOSM" via controlo remoto (`127.0.0.1:8111`).
- **Separador "Resultados":**
  - indicadores e matriz de confusão contra o OSM (exatidão estrita e "compatível");
  - formas e cores, relação com o OSM, sinalizações e motivos de exportação.

- **Arquivo** (menu "Arquivo…" na barra de topo):
  - **arquivar aceites** depois de os submeter ao OSM;
  - **arquivar rejeitados** que não se quer voltar a ver.

  Os arquivados saem da lista, do mapa e das exportações, e a decisão fica guardada
  (`archived_at` em `reviews.sqlite`). Um aceite arquivado não fica "desatualizado"
  quando a versão OSM muda, porque essa mudança é a própria submissão. O filtro
  "arquivados" volta a mostrá-los, e o menu permite desarquivar.

- **Layout sem lista lateral:** o mapa ocupa a área principal e o detalhe fica à
  direita. A linha de filtros sobre o mapa tem:
  - id OSM;
  - relação com o OSM (novo / complementa / diverge / igual);
  - estado (por rever / aceites / rejeitados);
  - sinalizados e arquivados.

  Os filtros mostram e escondem os edifícios no próprio mapa, em 2D e em 3D. `J`/`K`
  percorrem os edifícios visíveis, sinalizados e de baixa confiança primeiro.
- **Mapa em 3D:** telhados e paredes de todos os edifícios com a **forma e as cores
  inferidas**, numa camada three.js dentro do MapLibre (`/api/scene`):
  - malhas geradas no backend a partir dos mesmos modelos da vista de detalhe;
  - plantas em L com esqueleto, partes com as suas cores;
  - Vilar e Mosteiró: ~300 mil triângulos, 7 MB, ~3 s na primeira vez, depois em cache;
  - cada edifício é atualizado quando a sua revisão muda.
- **Partes (`building:part`).**
  - **Sugestão:** quando o pipeline encontra um volume secundário, guarda uma divisão
    sugerida (volume principal + resto), já com o telhado de cada parte ajustado
    (`Building.suggested_layout`). Na revisão: "Dividir em partes (sugerido)" ou
    "Dividir manualmente".
  - **Editor 2D (vista "Planta", estilo iD)**, sobre os pontos LiDAR coloridos por
    altura (o degrau entre volumes vê-se pela cor):

    | Ação | Como |
    | --- | --- |
    | Mover vértice | arrastar |
    | Criar vértice | `+` a meio de um lado |
    | Remover vértice | `Delete` |
    | Esquadrar a parte selecionada | `Q` (lados a < 13° de 90°) |
    | Dividir com dois cliques | `S` |
    | Juntar à vizinha | botão |
    | Desfazer | `Ctrl+Z` |
    | Zoom / mover vista | roda / arrastar o fundo |

  - **Topologia** (`src/osm3d/parts.py`):
    - as partes partilham vértices e não há junções em T;
    - os nós do contorno são fixos (o contorno edita-se no JOSM);
    - os vértices sobre um lado do contorno só deslizam ao longo dele.
  - **Cada parte** tem forma, `height`, `min_height`, `roof:height`, `roof:direction`
    e `roof:colour` próprios, ajustados ao LiDAR e corrigíveis. A vista 3D mostra as
    partes com as suas cores.
  - **Exportação:**
    - cada parte é uma way nova `building:part=yes`;
    - os vértices sobre o contorno reutilizam os nós existentes, ou são **inseridos
      no lado do contorno** (o contorno fica `action=modify`);
    - os vértices interiores são nós novos partilhados entre as partes;
    - o contorno recebe `height` = parte mais alta;
    - só para contornos simples (way), e só se o edifício ainda não tiver
      `building:part` no OSM.

- **Separador "Dados"** (`src/osm3d/app/data.py`, `downloads.py`, `static/data.js`):
  - grelha oficial `LiDAR2024_2025_Seccionamento.gpkg` (`sources.dgt_tile_index`):
    lê só `NAME`, `LOTE` e `DISPONIVEL`; a geometria sai do identificador. Sem o
    ficheiro, usa a malha de 1 km calculada. `DISPONIVEL` é só informativo (está
    desatualizado);
  - mapa com o estado local de cada quadrícula (LAZ + MDT, incompleta, nada) e a área
    do `config.yaml`;
  - seleção: clique, `Shift`+arrastar (acrescenta um retângulo), `Alt`+arrastar
    (retira), "Área do projeto", "Com ficheiros";
  - **download em falta**: fila em segundo plano (uma thread, pedidos em série com
    pausa, uma sessão DGT), com progresso por ficheiro e cancelamento. Só descarrega
    os produtos que ainda não existem localmente. Por omissão, LAZ + MDT;
  - **ortofotos noutra grelha:** a DGT distribui-as em folhas de ~8 × 5 km
    (`ORTOS-2025-cog-25cm-097-3`, COG de 25 cm, ~480 MB), sem identificador de
    quadrícula. A pesquisa aceita-as por interseção, o inventário lê os limites do
    GeoTIFF para saber que quadrículas cada folha cobre, e uma folha só é descarregada
    uma vez. Apagá-la retira-a a todas as quadrículas que cobre;
  - **eliminar ficheiros locais** das quadrículas selecionadas, por produto, incluindo
    `.part` e versões antigas e, opcionalmente, as caches de pontos `.npz` da revisão.
    É recusado enquanto houver downloads dessas quadrículas;
  - a aplicação arranca mesmo sem resultados do pipeline: abre diretamente no separador
    "Dados" (área nova).
- **Áreas de estudo** (`src/osm3d/areas.py`, `app/context.py`):
  - **uma área é só um conjunto de quadrículas de 1 km.** As mesmas quadrículas definem
    o LAZ/MDT a descarregar, os ficheiros que o pipeline lê e os edifícios OSM
    processados: os que têm o centróide dentro delas (`pipeline.in_area`). Cada edifício
    pertence a uma só quadrícula, por isso áreas vizinhas não partilham edifícios. Já
    não há bbox nem relação OSM na configuração. Uma `bbox` antiga é convertida nas
    quadrículas que interseta;
  - registo em `data/areas.json`, com a área ativa. A área do `config.yaml` é a inicial,
    pode ser ajustada mas não removida;
  - edição no mapa: clique, `Shift`+arrastar (acrescenta) ou `Alt`+arrastar (retira).
    "Juntar limite" acrescenta as quadrículas que cobrem uma relação OSM (polígono pelo
    Nominatim). Exemplos: Vilar e Mosteiró ocupa 16 quadrículas, Vila do Conde 187. As
    alterações ficam por guardar até "Guardar área";
  - se as quadrículas mudarem depois do processamento, a área fica marcada como
    desatualizada até ser processada de novo;
  - o identificador é neutro e gerado (`area-1`, `area-2`, …), dá a pasta
    `data/processed/<id>` e nunca muda. O nome a mostrar é opcional e editável (✎), porque
    as quadrículas também podem mudar. Os registos antigos, com ids derivados de nomes de
    lugares, migram uma vez para `area-<n>`: a pasta muda de nome e um nome herdado da
    área inicial é descartado. Remover uma área só a tira do registo: os dados ficam;
  - o OSM em cache (`00_fetch.osm`, `osm_context.json`) regista para que quadrículas
    foi obtido (`*.meta.json`). Se as quadrículas mudarem, é obtido de novo;
  - a revisão é recarregada ao voltar ao separador "Revisão" sempre que o servidor a
    recarregou (outra área ou novos resultados: `review_generation` em `/api/areas`);
  - ativar uma área recarrega a página: a revisão passa a ser a dessa área, ou fica
    indisponível até ser processada;
  - a CLI usa a mesma área ativa (`osm3d area` para listar, ativar ou criar).
- **Processamento** (`app/processing.py`): "Processar área" corre `fetch → qa` (e,
  opcionalmente, `candidates`) da área ativa numa thread, com o mesmo `run_stage` da
  CLI, e mostra as fases e o registo. Opção para voltar a obter o OSM. Só há uma corrida
  de cada vez, e não se muda de área durante uma corrida. Cancelar só tem efeito entre
  fases. No fim, a revisão é recarregada com os novos resultados.

### Por fazer

- Ortofoto como camada de fundo opcional (hoje: OSM raster).
- Virtualizar a lista para dezenas de milhares de edifícios (hoje mostra até 2 000).
- Servir o MapLibre e o three.js localmente (hoje dependem de CDN e de internet).

## 7. Questões em aberto

- Que nível de exatidão justifica avançar para a Fase A, por exemplo em `roof:shape`
  e erro de altura?
- A ortofoto é necessária na revisão, ou chegam os pontos LiDAR coloridos?
- É preciso suportar vários revisores (autenticação, registo de quem aprovou o quê)?
  Com um só utilizador, não.
