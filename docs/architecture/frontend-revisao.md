# Frontend: aplicação de revisão

**Estado:** proposta, adiada até haver resultados reais aceitáveis (ver §6).

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
| **Seleção de área** | ⏸️ Fase posterior | Útil com várias áreas ou para o concelho inteiro. Hoje é uma entrada no `config.yaml`. |
| **Gestão de downloads/processamento** | ⏸️ Fase posterior | Para um só utilizador, a CLI chega. Ganha valor com ~170 quadrículas (concelho), como página de estado: descarregada → processada → revista. |

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

A exportação `.osm` passa a incluir **apenas edifícios `accepted`**, com as
correções aplicadas.

## 6. Faseamento

| Fase | Entrega | Condição para avançar |
| --- | --- | --- |
| **Agora** | Pipeline com resultados reais: alturas, telhados, avaliação contra os `roof:shape` existentes. Inspeção via QGIS ou um relatório HTML estático por quadrícula (mapa + tabela + 3D, sem servidor). Estado de revisão no modelo (§5). | — |
| **A. Aplicação de revisão** | Fila, mapa, 3D modelo vs. pontos, edição de tags, exportação dos aceites para `.osm` JOSM. | Ajuste de telhados com exatidão aceitável nos dados reais (a definir após a primeira avaliação). |
| **B. Gestão de áreas e tarefas** | Seleção de área no mapa, estado por quadrícula (descarregada/processada/revista), lançamento de downloads e processamento em segundo plano. | Avançar para o concelho de Vila do Conde (~28 000 edifícios, ~170 quadrículas). |

## 7. Questões em aberto

- Que nível de exatidão justifica avançar para a Fase A, por exemplo em `roof:shape`
  e erro de altura?
- A ortofoto é necessária na revisão, ou chegam os pontos LiDAR coloridos?
- É preciso suportar vários revisores (autenticação, registo de quem aprovou o quê)?
  Com um só utilizador, não.
