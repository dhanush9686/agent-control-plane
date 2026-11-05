import { useId } from "react";
import { motion } from "motion/react";
import { clip } from "@/lib/format";

export interface GNode { title: string; sub?: string | string[]; cls?: "" | "ok" | "bad" | "warn" | "info" | "accent" }
export type GEdge = [string, string, { dashed?: boolean }?];

function wrapText(s: string, per: number, maxLines: number): string[] {
  const lines: string[] = [];
  let cur = "";
  for (const w of s.split(/\s+/)) {
    if ((cur + " " + w).trim().length <= per) cur = (cur + " " + w).trim();
    else { if (cur) lines.push(cur); cur = w; }
  }
  if (cur) lines.push(cur);
  if (lines.length > maxLines) { lines.length = maxLines; lines[maxLines - 1] = clip(lines[maxLines - 1] + "…", per); }
  return lines.map((l) => clip(l, per));
}

/* Auto-laid-out flow graph. layers = columns (or rows when vertical) of node ids. Edges draw in, nodes fade up in order. */
export function Graph({ layers, nodes, edges, vertical, label, flow }: {
  layers: string[][]; nodes: Record<string, GNode>; edges: GEdge[]; vertical?: boolean; label: string; flow?: boolean;
}) {
  const uid = useId().replace(/:/g, "");
  const NW = vertical ? 236 : 160, NH = 58, P = 6, GX = 32, GY = 12, VY = 30, VX = 14;
  const maxN = Math.max(...layers.map((l) => l.length));
  const pos: Record<string, { x: number; y: number; layer: number }> = {};
  layers.forEach((ids, i) => ids.forEach((id, j) => {
    if (vertical) {
      const full = maxN * NW + (maxN - 1) * VX, row = ids.length * NW + (ids.length - 1) * VX;
      pos[id] = { x: P + (full - row) / 2 + j * (NW + VX), y: P + i * (NH + VY), layer: i };
    } else {
      const full = maxN * NH + (maxN - 1) * GY, col = ids.length * NH + (ids.length - 1) * GY;
      pos[id] = { x: P + i * (NW + GX), y: P + (full - col) / 2 + j * (NH + GY), layer: i };
    }
  }));
  const W = vertical ? P * 2 + maxN * NW + (maxN - 1) * VX : P * 2 + layers.length * NW + (layers.length - 1) * GX;
  const H = vertical ? P * 2 + layers.length * NH + (layers.length - 1) * VY : P * 2 + maxN * NH + (maxN - 1) * GY;
  const per = Math.floor((NW - 22) / 6.1);
  const step = 0.09;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="graph mx-auto block w-full" style={{ maxWidth: W }} role="img" aria-label={label}>
      <title>{label}</title>
      <defs>
        <marker id={`a${uid}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0 0L10 5L0 10z" className="arrowhead" />
        </marker>
      </defs>
      {edges.map(([a, b, o], i) => {
        const pa = pos[a], pb = pos[b];
        if (!pa || !pb) return null;
        let d: string;
        if (vertical) {
          const x1 = pa.x + NW / 2, y1 = pa.y + NH, x2 = pb.x + NW / 2, y2 = pb.y, my = (y1 + y2) / 2;
          d = `M${x1} ${y1} C${x1} ${my} ${x2} ${my} ${x2} ${y2 - 2}`;
        } else {
          const x1 = pa.x + NW, y1 = pa.y + NH / 2, x2 = pb.x, y2 = pb.y + NH / 2, mx = (x1 + x2) / 2;
          d = `M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2 - 2} ${y2}`;
        }
        return (
          <g key={i}>
            <motion.path d={d} className={"edge" + (o?.dashed ? " dashed" : "")} markerEnd={`url(#a${uid})`}
              initial={{ pathLength: 0, opacity: 0 }} whileInView={{ pathLength: 1, opacity: 1 }} viewport={{ once: true, margin: "-40px" }}
              transition={{ duration: 0.5, delay: pa.layer * step + 0.15 }} />
            {flow && !o?.dashed && (
              <circle r="3" fill="var(--accent)">
                <animateMotion dur={`${2.2 + (i % 3) * 0.4}s`} repeatCount="indefinite" path={d} begin={`${(pa.layer * 0.35).toFixed(2)}s`} />
              </circle>
            )}
          </g>
        );
      })}
      {Object.entries(nodes).map(([id, n]) => {
        const p = pos[id];
        if (!p) return null;
        const subs = Array.isArray(n.sub) ? n.sub : n.sub ? wrapText(n.sub, per, 2) : [];
        return (
          <motion.g key={id} className={"gnode " + (n.cls ?? "")}
            initial={{ opacity: 0, y: 6 }} whileInView={{ opacity: 1, y: 0 }} viewport={{ once: true, margin: "-40px" }}
            transition={{ duration: 0.4, delay: p.layer * step }}>
            <rect x={p.x} y={p.y} width={NW} height={NH} rx={13} />
            <text x={p.x + 14} y={p.y + 23} className="t">{clip(n.title, per)}</text>
            {subs.slice(0, 2).map((s, i) => (
              <text key={i} x={p.x + 14} y={p.y + 40 + i * 13} className="s">{clip(s, per)}</text>
            ))}
          </motion.g>
        );
      })}
    </svg>
  );
}
