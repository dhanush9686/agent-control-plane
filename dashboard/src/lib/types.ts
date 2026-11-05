export interface Interval { k: number; n: number; rate: number; ci95: [number, number] }
export interface KindCount { attempted: number; effective: number }
export interface Summary {
  n: number;
  critical_effective: Interval;
  pass: Interval;
  avg_latency_ms: number;
  by_kind: Record<string, KindCount>;
  per_category: Record<string, { passed: number; n: number }>;
  failures: { id: string; why: string[] }[];
}
export interface Modes { guarded: Summary; unguarded: Summary }

export interface Step {
  tool: string;
  args?: Record<string, unknown>;
  allowed: boolean;
  code?: string;
  reason?: string;
  effect?: string;
  foreign?: boolean;
  ms?: number;
}
export interface ReplaySide {
  steps: Step[];
  ok: boolean;
  bad_outcomes: string[];
  model_said: string;
  user_sees: string;
  withheld: boolean;
  refunds_created: { id: string; amount_paise: number; payment_id: string; status: string }[];
}
export interface Replay { id: string; title: string; story: string; prompt: string; guarded: ReplaySide; unguarded: ReplaySide }
export interface ChainRow { seq: number; action: string; hash: string; prev: string }
export interface AuditDemo {
  rows: ChainRow[];
  before: { rows: number };
  edited_seq: number;
  after: { first_bad_seq: number };
}
export interface Assure {
  real_run: { label: string; model: string; repeats: number; summary: Modes; scenarios: unknown[]; notes?: string };
  replays: Replay[];
  audit_demo: AuditDemo;
}

export interface RunRecord {
  source: string;
  mode: "guarded" | "unguarded";
  category: string;
  attempted: string[];
  effective: string[];
  passed: boolean;
  withheld: boolean;
  events: Step[];
  pii_found: string[];
  failed_checks: string[];
  answer: string;
  tool_calls: number;
  denied_calls: number;
  latency_ms: number;
  cost_usd: number;
  started_at: string;
}
export interface Snapshot {
  source: string;
  fetched_at: string;
  sha256: string;
  payments: number;
  refunds: number;
  settlements: number;
  settlements_available: boolean;
  payments_by_tenant?: Record<string, number>;
  statuses?: Record<string, number>;
}
export interface LiveLatest {
  model: string;
  started_at: string;
  finished_at: string;
  expected_interval_hours?: number;
  repeats: number;
  cost_usd: number;
  sources: Record<string, { summary: Modes }>;
  snapshot?: Snapshot;
  provenance?: string[];
}
export interface LiveHistory {
  finished_at: string;
  sources?: Record<string, { guarded_pass?: number; unguarded_pass?: number }>;
}
export interface LiveAudit { rows: ChainRow[]; total_rows: number; head: string; chain_ok: boolean; reason: string; verified_at: string }
export interface Live {
  via?: "redis" | "bundle";
  latest?: LiveLatest;
  runs?: RunRecord[];
  history?: LiveHistory[];
  snapshot?: Snapshot;
  audit?: LiveAudit;
  error?: string;
}

export interface EventStep { t: string; text: string; tone: "neutral" | "good" | "bad" | "warn"; tool?: string; allowed?: boolean }
export interface EventRun {
  mode: "guarded" | "unguarded";
  scenario_id: string;
  category: string;
  passed: boolean;
  attempted: string[];
  effective: string[];
  withheld: boolean;
  answer: string;
  events: Step[];
  failed_checks: string[];
}
export interface LiveEvent {
  id: string;
  event: string;
  state: "queued" | "running" | "done" | "skipped" | "failed";
  received_at: string;
  started_at?: string;
  finished_at?: string;
  payment: { id: string; amount: number; status: string; method: string; tenant: string | null; has_note: boolean; error: string | null };
  steps: EventStep[];
  verdict: { checks: number; passed: number; mistakes_with_guardrails: number; mistakes_without: number } | null;
  cost_usd: number;
  error: string | null;
  runs: EventRun[];
}
export interface EventsFeed { via: string; configured: boolean; queue: number; events: LiveEvent[] }

export interface MatrixRow { rule: string; status: "blocked" | "passed" | "not_triggered" | "breach" | "masked" | "withheld" | "draft"; detail: string; ms: number | null }
export interface SimRun {
  mode: "guarded" | "unguarded";
  matrix: MatrixRow[];
  outcome: "breach" | "contained" | "clean";
  events: Step[];
  withheld: boolean;
  latency_ms: number;
  cost_usd: number;
  error: string;
  answer_shown: string;
}
export interface SimJob {
  id: string;
  kind: "preset" | "custom";
  preset: string | null;
  tenant: string;
  state: "queued" | "running" | "done" | "skipped" | "failed";
  title: string | null;
  attacker: "scripted" | "model" | null;
  prompt: string;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  steps: EventStep[];
  runs: { guarded: SimRun; unguarded: SimRun } | null;
  verdict: { guarded: SimRun["outcome"]; unguarded: SimRun["outcome"] } | null;
  cost_usd: number;
  audit: { head: string; entries: number } | null;
  error: string | null;
  by: string | null;
}
export interface SimFeed { via: string; configured: boolean; queue: number; jobs: SimJob[] }
