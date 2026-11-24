// Separador "Resultados": indicadores, avaliação contra o OSM, distribuição de formas.
import { CHANGE_COLORS, CHANGE_LABELS, ORIGIN_COLORS, ORIGIN_LABELS, SHAPE_COLORS, SHAPE_LABELS } from "./colors.js";

const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)} %`);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function bars(counts, colors = {}, labels = {}) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, v]) => v));
  return entries
    .map(([k, v]) => `<div class="bar"><span>${esc(labels[k] ?? k)}</span>
      <div class="track"><div class="fill" style="width:${(100 * v) / max}%;background:${colors[k] ?? "var(--accent)"}"></div></div>
      <span>${v}</span></div>`)
    .join("");
}

export function renderResults(el, s) {
  const ev = s.evaluation;
  const reviewed = (s.status.accepted ?? 0) + (s.status.rejected ?? 0);
  const matrix = ev.labels.length
    ? `<table class="matrix"><tr><th>OSM \\ previsto</th>${ev.labels.map((l) => `<th>${esc(l)}</th>`).join("")}</tr>
        ${ev.labels.map((r, i) => `<tr><th>${esc(r)}</th>${ev.confusion[i]
          .map((v, j) => `<td class="${v ? (i === j ? "diag" : "off") : ""}">${v || ""}</td>`).join("")}</tr>`).join("")}
      </table>`
    : "<p class='muted'>Sem edifícios com roof:shape no OSM.</p>";
  const h = ev.heights;
  el.innerHTML = `
    <div class="cards">
      <div class="card"><div class="v">${s.n_buildings}</div><div class="l">edifícios reconstruídos</div></div>
      <div class="card"><div class="v">${reviewed}</div><div class="l">revistos (${s.status.accepted ?? 0} aceites, ${s.status.rejected ?? 0} rejeitados)</div></div>
      <div class="card"><div class="v">${s.needs_review}</div><div class="l">com sinalizações</div></div>
      <div class="card"><div class="v">${s.osm3d ?? "—"}</div><div class="l">já com 3D no OSM (${s.changes_osm3d?.diverge ?? 0} divergem)</div></div>
      <div class="card"><div class="v">${s.export.selected}</div><div class="l">prontos a exportar (${s.export.conflicts} com conflitos)</div></div>
      <div class="card"><div class="v">${pct(ev.accuracy)}</div><div class="l">exatidão roof:shape vs OSM (${ev.n_correct}/${ev.n_predicted})</div></div>
      <div class="card"><div class="v">${pct(ev.accuracy_compatible)}</div><div class="l">compatível com OSM (três águas ≈ hipped)</div></div>
      <div class="card"><div class="v">${pct(ev.coverage)}</div><div class="l">cobertura (${ev.n_predicted}/${ev.n_reference} com referência)</div></div>
    </div>
    <div class="grid2">
      <div class="box"><h3>roof:shape — previsto vs. mapeado no OSM</h3>${matrix}
        <p class="small muted">Inclui as correções da revisão. A referência OSM também tem erros.</p></div>
      <div class="box"><h3>Relação com o OSM</h3>${bars(s.changes ?? {}, CHANGE_COLORS, CHANGE_LABELS)}
        <p class="small muted">"diverge": a análise propõe valores diferentes dos já mapeados (mantêm-se os do OSM na exportação).</p></div>
      <div class="box"><h3>Formas previstas</h3>${bars(s.shapes, SHAPE_COLORS, SHAPE_LABELS)}</div>
      <div class="box"><h3>Cor do telhado (roof:colour final)</h3>${bars(s.roof_colours ?? {}, Object.fromEntries(Object.keys(s.roof_colours ?? {}).map((k) => [k, k])), Object.fromEntries([...(s.palette ?? []).map((c) => [c.hex, c.label]), [s.default_colours?.roof, "sem roof:colour"]]))}
        <p class="small muted">Cor da ortofoto confirmada pelo LiDAR; abaixo de ${pct(s.min_colour_confidence)} de confiança não é exportada.</p></div>
      <div class="box"><h3>Edifícios da cartografia municipal em falta no OSM</h3>${Object.keys(s.candidate_sources ?? {}).length ? bars(s.candidate_sources, ORIGIN_COLORS, ORIGIN_LABELS) : "<p class='muted'>Sem candidatos.</p>"}
        <p class="small muted">Contorno da cartografia municipal, confirmado pelo LiDAR (que dá a forma e a altura do telhado).</p></div>
      <div class="box"><h3>Sinalizações</h3>${bars(s.flags) || "<p class='muted'>Nenhuma.</p>"}</div>
      <div class="box"><h3>Exportação</h3>${bars(s.export.reasons)}
        <h3 style="margin-top:14px">Altura por piso implícita (vs building:levels)</h3>
        ${h.mediana ? `<p>mediana <b>${h.mediana} m</b>, P10 ${h.p10} m, P90 ${h.p90} m; entre 2,4 e 4,0 m: ${pct(h["entre 2.4 e 4.0 m"])}</p>` : "<p class='muted'>Sem dados.</p>"}
      </div>
    </div>
    <p class="small muted" style="margin-top:16px">Fonte: ${esc(s.source)}</p>`;
}
