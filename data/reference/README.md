# Dados de referência

Material para avaliar e calibrar o pipeline. Só se versionam ficheiros pequenos
(GeoJSON, CSV, imagens). Ver `.gitignore`.

## Observações no LiDAR da DGT

Vista oblíqua da nuvem de pontos colorida (RGB) no visualizador LiDAR da DGT, numa
zona rural/periurbana (a imagem não é versionada: os dados são da DGT, CC BY 4.0).

O que se observa:

- **Os pontos têm RGB** com cores próximas das reais. Os telhados cerâmicos
  aparecem em vermelho/terracota, as coberturas metálicas em cinzento e a
  vegetação em verde. Dá para estimar a cor do telhado a partir do próprio LiDAR.
- **As fachadas não têm pontos.** O voo é aéreo (nadiral), por isso só há pontos
  nas coberturas e no chão. Por baixo dos beirais aparecem "sombras" pretas, que
  são zonas sem dados. Isto confirma que a cor das fachadas não pode vir do LiDAR
  nem da ortofoto.
- **Formas de telhado visíveis:**
  - à esquerda, vários volumes de quatro águas (`hipped`) adjacentes, um caso
    típico de `building:part`;
  - à direita, um telhado longo de duas águas (`gabled`) com claraboias ou
    janelas de sótão (manchas brancas/cinzentas). Estes elementos são ruído
    para o ajuste do modelo;
  - coberturas cinzentas planas ou de baixa inclinação (anexos/armazéns).
- **A vegetação encosta e sobrepõe-se aos telhados.** Por isso é preciso
  excluir as classes 3–5 e usar um ajuste robusto.
- **Os beirais saem para fora das paredes.** O contorno do telhado no LiDAR é
  maior do que o footprint OSM (que segue as paredes).
- **Há pontos de cor errada nos bordos de alguns telhados** (cor do chão ou da
  vegetação). É um artefacto típico de colorir a nuvem a partir de uma ortofoto
  que não é *true ortho*.
