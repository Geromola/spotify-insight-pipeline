// api.test.ts — end-to-end tests of every endpoint against PGlite loaded with a real run's bundle.
//
// By default the s2 pilot run (runs/pilot-100-s2) is turned into a fresh bundle in a temp directory with
// dashboard/db/build_bundle.py, so the test covers the whole chain: SQLite run -> CSV bundle -> Postgres.
// Override with RUN_DIR (another finalized run) or BUNDLE_DIR (an existing bundle; skips the build).

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, before, describe, test } from "node:test";
import type { Server } from "node:http";

const here = import.meta.dirname;
const repo = path.resolve(here, "..", "..", "..");
const runDir = path.resolve(process.env.RUN_DIR ?? path.join(repo, "runs", "pilot-100-s2"));

let base = "";
let server: Server;
let tempBundle: string | undefined;

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
const LENS_NAMES: Record<string, string> = {
  baseline: "all complaints (baseline)",
  no_free_limits: "without free-tier limits",
  paying: "paying reviewers only",
};

function readCsv(file: string): Record<string, string>[] {
  // ranking.csv, ranking_segments.csv and segments_area.csv have no quoted fields, so a plain split works.
  const [header, ...lines] = readFileSync(file, "utf8").trim().split("\n");
  const cols = header.split(",");
  return lines.map((line) => Object.fromEntries(line.split(",").map((v, i) => [cols[i], v])));
}

/** RFC 4180 CSV with quoted fields (the diagnosis files quote symptom names that contain commas). */
function readQuotedCsv(file: string): Record<string, string>[] {
  const text = readFileSync(file, "utf8");
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        field += '"';
        i++;
      } else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += c;
  }
  if (field || row.length) rows.push([...row, field]);
  const [header, ...body] = rows.filter((r) => r.some((v) => v !== ""));
  return body.map((r) => Object.fromEntries(header.map((h, i) => [h, r[i] ?? ""])));
}

async function get(pathname: string) {
  const res = await fetch(base + pathname);
  return { status: res.status, cache: res.headers.get("cache-control"), body: (await res.json()) as any };
}

before(async () => {
  delete process.env.DATABASE_URL; // tests always run against the local bundle
  if (!process.env.BUNDLE_DIR) {
    tempBundle = mkdtempSync(path.join(tmpdir(), "dashboard-bundle-"));
    execFileSync(
      process.env.PYTHON ?? "python3.12",
      [path.join(repo, "dashboard", "db", "build_bundle.py"), "--run", runDir, "--out", tempBundle],
      { stdio: ["ignore", "ignore", "inherit"] },
    );
    process.env.BUNDLE_DIR = tempBundle;
  }
  const { app } = await import("./app.ts");
  await new Promise<void>((resolve) => {
    server = app.listen(0, () => resolve());
  });
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

after(() => {
  server?.close();
  if (tempBundle) rmSync(tempBundle, { recursive: true, force: true });
});

describe("overview", () => {
  test("counts, period, cost and agreement", async () => {
    const { status, cache, body } = await get("/api/overview");
    assert.equal(status, 200);
    assert.equal(cache, "public, s-maxage=3600");
    const summary = JSON.parse(readFileSync(path.join(runDir, "run_summary.json"), "utf8"));
    assert.equal(body.run_id, summary.run_id);
    assert.equal(body.reviews, summary.source_rows);
    assert.equal(body.classified, summary.completed);
    assert.equal(body.complaints, summary.complaints);
    assert.equal(body.quarantined, summary.quarantined);
    assert.match(body.period.first, /^\d{4}-\d{2}-\d{2}$/);
    assert.match(body.period.last, /^\d{4}-\d{2}-\d{2}$/);
    // The calls table is the ledger. run_summary.json is written when the run is finalized, so it can omit
    // calls made by later stages (the diagnose stage runs after finalization); it never counts more.
    const db = new DatabaseSync(path.join(runDir, "pipeline.sqlite"), { readOnly: true });
    const ledger = db.prepare("SELECT count(*) AS calls, coalesce(sum(cost_usd), 0) AS cost FROM calls").get() as {
      calls: number;
      cost: number;
    };
    db.close();
    assert.ok(Math.abs(body.cost.total_usd - ledger.cost) < 1e-9);
    assert.equal(body.cost.calls, ledger.calls);
    assert.ok(summary.usage.calls <= ledger.calls && summary.usage.cost_usd <= ledger.cost + 1e-9);
    assert.equal(typeof body.needs_review_share, "number");
    for (const k of ["topic", "intent", "severity"]) {
      // Golden-50 results are optional: they are regenerated per taxonomy and may be absent.
      if (body.golden !== null) assert.equal(typeof body.golden[k], "number");
      assert.equal(typeof body.verifier[k], "number");
    }
  });
});

describe("flow", () => {
  test("nine stages with real numbers", async () => {
    const { status, body } = await get("/api/flow");
    assert.equal(status, 200);
    assert.deepEqual(
      body.nodes.map((n: any) => n.id),
      ["prepare", "enrich", "fallback", "verify", "rank", "group", "diagnose", "memo", "critic"],
    );
    for (const n of body.nodes) {
      assert.ok(n.kind === "code" || n.kind === "model");
      assert.equal(typeof n.metric.value, "number", `${n.id} metric`);
      assert.equal(typeof n.cost_usd, "number");
      assert.equal(typeof n.reads, "string");
      if (n.kind === "model") assert.ok(n.calls > 0, `${n.id} has calls`);
    }
    const sum = body.nodes.reduce((a: number, n: any) => a + n.calls, 0);
    assert.equal(sum, body.totals.calls);
  });

  test("Diagnose holds the group·diagnose calls and Jev's diagnosis check; Verify and Group do not", async () => {
    const db = new DatabaseSync(path.join(runDir, "pipeline.sqlite"), { readOnly: true });
    const count = (role: string, step: string) =>
      (db.prepare("SELECT count(*) AS n FROM calls WHERE role = ? AND step = ?").get(role, step) as { n: number }).n;
    const haiku = count("group", "diagnose");
    const jev = count("verify", "diagnose");
    const verifyAll = (db.prepare("SELECT count(*) AS n FROM calls WHERE role = 'verify'").get() as { n: number }).n;
    db.close();
    const { body } = await get("/api/flow");
    const node = (id: string) => body.nodes.find((n: any) => n.id === id);
    assert.equal(node("diagnose").calls, haiku + jev);
    assert.equal(node("verify").calls, verifyAll - jev);
    assert.ok(!node("group").steps.includes("diagnose"));
    const coverage = JSON.parse(readFileSync(path.join(runDir, "diagnosis.json"), "utf8")).coverage;
    assert.equal(node("diagnose").metric.value, coverage.completed);
    if (jev > 0) assert.match(node("diagnose").note, /Jev/);
  });
});

describe("recommendation", () => {
  test("memo and claims whose numbers equal ranking.csv", async () => {
    const { status, body } = await get("/api/recommendation");
    assert.equal(status, 200);
    assert.equal(typeof body.memo.headline, "string");
    assert.equal(typeof body.memo.memo_markdown, "string");
    assert.ok(Array.isArray(body.memo.final_problems));
    const ranking = new Map(readCsv(path.join(runDir, "ranking.csv")).map((r) => [r.issue_id, r]));
    assert.ok(body.claims.length > 0);
    for (const c of body.claims) {
      const row = ranking.get(c.issue_id);
      assert.ok(row, `${c.claim_id} issue ${c.issue_id} is ranked`);
      assert.equal(Number(c.value), Number(row[c.metric]), `${c.claim_id} ${c.metric}`);
      assert.equal(c.matches_ranking, true);
      assert.equal(typeof c.issue_title, "string");
    }
  });

  test("quotes instead of review ids, and no review id anywhere in the payload", async () => {
    const { body } = await get("/api/recommendation");
    assert.ok(Array.isArray(body.quotes) && body.quotes.length > 0);
    for (const q of body.quotes) {
      assert.deepEqual(Object.keys(q).sort(), ["issue_id", "issue_title", "quote", "symptom"]);
      assert.equal(typeof q.quote, "string");
      // An issue quote has its title; a diagnosis quote (memo_v3) names its symptom instead.
      assert.ok(typeof q.issue_title === "string" || q.symptom !== null, `${q.issue_id} resolves`);
    }
    const raw = JSON.stringify(body);
    assert.ok(!raw.includes("cited_review_ids"), "no cited_review_ids key");
    assert.ok(!("cited_review_ids" in body.memo));
    assert.doesNotMatch(raw, UUID, "no UUID-looking string");
  });

  test("a quote whose issue_id is a symptom id resolves to that symptom and its area", async () => {
    // The pilot memo predates memo_v3, so a diagnosis quote is added to the loaded memo for this test only.
    const { query } = await import("./db.ts");
    const symptoms = readQuotedCsv(path.join(runDir, "diagnosis_symptoms.csv"));
    const memo = JSON.parse(readFileSync(path.join(runDir, "memo.json"), "utf8"));
    const target =
      symptoms.find((r) => r.area === memo.final.priority_area && r.symptom_id !== "other" && Number(r.complaints) > 0) ??
      symptoms.find((r) => r.symptom_id !== "other")!;
    const [{ quotes: original }] = await query<{ quotes: unknown }>("SELECT quotes FROM memo WHERE id = 1");
    const added = { issue_id: target.symptom_id, quote: "Symptom quote for the test" };
    await query("UPDATE memo SET quotes = $1::jsonb WHERE id = 1", [JSON.stringify([...(original as any[]), added])]);
    try {
      const { body } = await get("/api/recommendation");
      const q = body.quotes.find((x: any) => x.issue_id === target.symptom_id);
      assert.ok(q, "symptom quote is returned");
      assert.equal(q.issue_title, null);
      assert.deepEqual(q.symptom, { area: target.area, symptom_id: target.symptom_id, name: target.symptom_name });
      // Every other quote is either an issue quote or (memo_v3) a diagnosis quote, never both.
      for (const other of body.quotes.filter((x: any) => x !== q)) {
        assert.ok((other.symptom === null) !== (other.issue_title === null), other.issue_id);
      }
      assert.doesNotMatch(JSON.stringify(body), UUID);
    } finally {
      await query("UPDATE memo SET quotes = $1::jsonb WHERE id = 1", [JSON.stringify(original)]);
    }
  });
});

describe("areas and issues", () => {
  test("areas", async () => {
    const { status, body } = await get("/api/areas");
    assert.equal(status, 200);
    assert.ok(body.areas.length > 0);
    for (const a of body.areas) {
      assert.equal(typeof a.complaints, "number");
      assert.equal(typeof a.severity_sum, "number");
      assert.equal(typeof a.in_question, "boolean");
    }
  });

  test("area segments sum to the area totals and match segments_area.csv", async () => {
    const { body } = await get("/api/areas");
    const rows = readCsv(path.join(runDir, "segments_area.csv"));
    for (const a of body.areas) {
      assert.equal(a.free_tier_limit_complaints + a.other_complaints, a.complaints, `${a.area} split`);
      assert.equal(a.free_tier_limit_severity_sum + a.other_severity_sum, a.severity_sum, `${a.area} severity split`);
      assert.ok(a.paying_complaints <= a.complaints, `${a.area} paying is a subset`);
      const mine = rows.filter((r) => r.area === a.area);
      const paid = mine.filter((r) => r.plan === "paid");
      assert.equal(a.paying_complaints, paid.reduce((s, r) => s + Number(r.complaints), 0));
      assert.equal(a.paying_severity_sum, paid.reduce((s, r) => s + Number(r.severity_sum), 0));
    }
    const plans = body.stated_plan;
    assert.deepEqual(Object.keys(plans).sort(), ["free", "paid", "unstated"]);
    const overview = (await get("/api/overview")).body;
    assert.equal(plans.paid + plans.free + plans.unstated, overview.complaints, "plans cover every complaint");
  });

  test("issues follow ranking.csv order", async () => {
    const { status, body } = await get("/api/issues");
    assert.equal(status, 200);
    const ranking = readCsv(path.join(runDir, "ranking.csv"));
    assert.deepEqual(
      body.issues.map((i: any) => i.issue_id),
      ranking.map((r) => r.issue_id),
    );
    assert.equal(body.issues[0].priority_score, Number(ranking[0].priority_score));
  });

  test("each lens matches ranking_segments.csv with the same issue shape", async () => {
    const segments = readCsv(path.join(runDir, "ranking_segments.csv"));
    const baseline = (await get("/api/issues")).body;
    for (const key of Object.keys(LENS_NAMES)) {
      const { status, cache, body } = await get(`/api/issues?lens=${key}`);
      assert.equal(status, 200, key);
      assert.equal(cache, "public, s-maxage=3600");
      assert.equal(body.lens, key);
      const expected = segments.filter((r) => r.lens === LENS_NAMES[key]);
      assert.deepEqual(body.issues.map((i: any) => i.issue_id), expected.map((r) => r.issue_id), key);
      body.issues.forEach((issue: any, idx: number) => {
        assert.deepEqual(Object.keys(issue).sort(), Object.keys(baseline.issues[0]).sort(), `${key} shape`);
        assert.equal(issue.rank, Number(expected[idx].rank));
        assert.equal(issue.complaint_count, Number(expected[idx].complaint_count));
        assert.equal(issue.priority_score, Number(expected[idx].priority_score));
      });
    }
    // The baseline lens in the segment file is the plain ranking.
    const segBaseline = segments.filter((r) => r.lens === LENS_NAMES.baseline).map((r) => r.issue_id);
    assert.deepEqual(baseline.issues.map((i: any) => i.issue_id), segBaseline);
  });

  test("unknown or repeated lens is 400", async () => {
    for (const qs of ["lens=premium", "lens=BASELINE", "lens=paying;drop", "lens=paying&lens=baseline"]) {
      const { status, cache } = await get(`/api/issues?${qs}`);
      assert.equal(status, 400, qs);
      assert.equal(cache, "no-store");
    }
  });

  test("issue detail: trend over full months and six ordered evidence reviews", async () => {
    const { body: list } = await get("/api/issues");
    const id = list.issues[0].issue_id;
    const { status, body } = await get(`/api/issues/${id}`);
    assert.equal(status, 200);
    assert.equal(body.issue.issue_id, id);
    assert.ok(Array.isArray(body.example_review_ids));
    assert.ok(body.trend.length > 0);
    for (const t of body.trend) {
      assert.match(t.month, /^\d{4}-\d{2}$/);
      assert.equal(typeof t.share, "number");
    }
    const months = body.trend.map((t: any) => t.month);
    assert.deepEqual([...months].sort(), months);
    assert.ok(body.evidence.length > 0 && body.evidence.length <= 6);
    for (const r of body.evidence) {
      assert.ok("free_tier_limit" in r && "plan" in r, "evidence carries segment fields");
      assert.ok(r.plan === null || ["paid", "free", "unstated"].includes(r.plan));
    }
    for (let i = 1; i < body.evidence.length; i++) {
      const [a, b] = [body.evidence[i - 1], body.evidence[i]];
      const order = (b.likes ?? -1) - (a.likes ?? -1) || (b.severity ?? -1) - (a.severity ?? -1);
      assert.ok(order < 0 || (order === 0 && a.review_id < b.review_id), "likes, severity, review_id order");
    }
  });

  test("malformed issue ids are rejected, unknown ones are 404", async () => {
    for (const bad of ["nodot", "UPPER.case", "a.b;drop", "a.b%20c", ".x", "x.", "a.b.c", "x".repeat(200) + ".y"]) {
      const { status, cache } = await get(`/api/issues/${bad}`);
      assert.equal(status, 400, bad);
      assert.equal(cache, "no-store");
    }
    assert.equal((await get("/api/issues/access.does-not-exist")).status, 404);
  });
});

describe("reviews", () => {
  test("pages are capped at 50 and filtered by issue", async () => {
    const { status, body } = await get("/api/reviews?limit=500");
    assert.equal(status, 200);
    assert.equal(body.limit, 50);
    assert.ok(body.items.length <= 50);
    assert.equal(body.total, 100);

    const { body: list } = await get("/api/issues");
    const issue = list.issues[0];
    const page = await get(`/api/reviews?issue=${issue.issue_id}&limit=3&offset=1`);
    assert.equal(page.body.total, issue.complaint_count);
    assert.ok(page.body.items.length <= 3);
    for (const r of page.body.items) assert.equal(r.issue_id, issue.issue_id);
  });

  test("q is a case-insensitive literal substring search", async () => {
    const lower = await get("/api/reviews?q=worst");
    const upper = await get("/api/reviews?q=WORST");
    assert.ok(lower.body.total > 0);
    assert.equal(lower.body.total, upper.body.total);
    for (const r of lower.body.items) assert.match(r.review_text, /worst/i);
    assert.equal((await get("/api/reviews?q=%25%25%25")).body.total, 0, "% is matched literally");
  });

  test("bad parameters are 400", async () => {
    for (const qs of ["limit=-1", "limit=abc", "limit=0", "offset=-5", "offset=1.5", "issue=bad", `q=${"a".repeat(101)}`]) {
      assert.equal((await get(`/api/reviews?${qs}`)).status, 400, qs);
    }
  });
});

describe("diagnosis", () => {
  const AREAS = ["access", "usability", "playback", "billing/support"];
  const symptomsCsv = () => readQuotedCsv(path.join(runDir, "diagnosis_symptoms.csv"));
  const num = (v: string) => (v === "" ? null : Number(v));

  test("default area is the memo's priority area", async () => {
    const { status, cache, body } = await get("/api/diagnosis");
    assert.equal(status, 200);
    assert.equal(cache, "public, s-maxage=3600");
    const memo = JSON.parse(readFileSync(path.join(runDir, "memo.json"), "utf8"));
    assert.equal(body.area, memo.final.priority_area);
    assert.equal(body.recommended, true);
    assert.deepEqual(body.areas.map((a: any) => a.area), AREAS);
    assert.deepEqual(body.areas.filter((a: any) => a.recommended).map((a: any) => a.area), [body.area]);
    assert.equal(typeof body.coverage.completed, "number");
    assert.equal(typeof body.verification.symptom, "number");
    assert.equal(typeof body.verification.n, "number");
  });

  test("each area: symptoms ranked by severity, counts and other match diagnosis_symptoms.csv", async () => {
    const csv = symptomsCsv();
    const customers = readQuotedCsv(path.join(runDir, "diagnosis_customers.csv"));
    const memo = JSON.parse(readFileSync(path.join(runDir, "memo.json"), "utf8"));
    for (const area of AREAS) {
      const { status, cache, body } = await get(`/api/diagnosis?area=${encodeURIComponent(area)}`);
      assert.equal(status, 200, area);
      assert.equal(cache, "public, s-maxage=3600");
      assert.equal(body.area, area);
      assert.equal(body.recommended, area === memo.final.priority_area);
      const mine = csv.filter((r) => r.area === area);
      const total = mine.reduce((s, r) => s + Number(r.complaints), 0);
      const listed = [...body.symptoms, ...body.plan_gated_symptoms];
      const summed = listed.reduce((s: number, x: any) => s + x.complaints, 0) + body.other.complaints;
      assert.equal(summed, total, `${area} symptoms + plan-gated + other = diagnosed total`);
      assert.equal(body.areas.find((a: any) => a.area === area).complaints, total);
      const other = mine.find((r) => r.symptom_id === "other");
      assert.equal(body.other.complaints, Number(other?.complaints ?? 0));
      assert.ok(!listed.some((s: any) => s.symptom_id === "other"), "other is separate");
      assert.deepEqual(
        listed.map((s: any) => s.symptom_id).sort(),
        mine.filter((r) => r.symptom_id !== "other").map((r) => r.symptom_id).sort(),
      );
      for (let i = 1; i < body.symptoms.length; i++) {
        assert.ok(body.symptoms[i - 1].severity_sum >= body.symptoms[i].severity_sum, `${area} severity order`);
      }
      for (const s of listed) {
        const row = mine.find((r) => r.symptom_id === s.symptom_id)!;
        assert.equal(s.complaints, Number(row.complaints));
        assert.equal(s.severity_sum, Number(row.severity_sum));
        assert.equal(s.first6_complaints, Number(row.first6_complaints));
        assert.equal(s.last6_complaints, Number(row.last6_complaints));
        assert.equal(s.paying_complaints, Number(row.paying_complaints));
        assert.equal(s.name, row.symptom_name);
        assert.equal(typeof s.definition, "string");
      }
      const cust = customers.find((r) => r.area === area);
      if (cust) assert.equal(body.customers.long_time_users, Number(cust.long_time_users));
    }
  });

  test("version callout only when the version has 2,000+ reviews and at least twice the overall share", async () => {
    const csv = symptomsCsv();
    for (const area of AREAS) {
      const { body } = await get(`/api/diagnosis?area=${encodeURIComponent(area)}`);
      for (const s of [...body.symptoms, ...body.plan_gated_symptoms]) {
        const row = csv.find((r) => r.area === area && r.symptom_id === s.symptom_id)!;
        const share = num(row.top_version_share_pct);
        const overall = num(row.overall_share_pct);
        const holds = Boolean(row.top_version) && share !== null && overall !== null && overall > 0 &&
          (num(row.top_version_reviews) ?? 0) >= 2000 && share >= 2 * overall;
        assert.equal("version" in s, holds, `${area} ${s.symptom_id}`);
        if (holds) assert.equal(s.version.app_version, row.top_version);
      }
    }
    // The pilot run is too small to trigger the callout, so the gate itself is checked directly too.
    const { versionCallout } = await import("./routes.ts");
    const base = { top_version: "8.7.90.544", overall_share_pct: 2, top_version_reviews: 2000 };
    assert.deepEqual(versionCallout({ ...base, top_version_share_pct: 9.6 }), {
      app_version: "8.7.90.544", share_pct: 9.6, overall_share_pct: 2, ratio: 4.8,
    });
    assert.ok(versionCallout({ ...base, top_version_share_pct: 4 }), "exactly twice qualifies");
    assert.equal(versionCallout({ ...base, top_version_share_pct: 3.9 }), null);
    assert.equal(versionCallout({ ...base, top_version_share_pct: 9.6, top_version_reviews: 1999 }), null);
    assert.equal(versionCallout({ ...base, top_version_share_pct: 9.6, top_version_reviews: null }), null);
    assert.equal(versionCallout({ ...base, top_version_share_pct: 9.6, overall_share_pct: 0 }), null);
    assert.equal(versionCallout({ ...base, top_version: null, top_version_share_pct: 9.6 }), null);
  });

  // Plan-gated ids come from the run's plan_gated column when it has one, else from the approved catalog.
  function expectedGated(): Set<string> {
    const csv = symptomsCsv();
    if (csv.length && "plan_gated" in csv[0]) {
      return new Set(csv.filter((r) => ["1", "true", "t"].includes(r.plan_gated.toLowerCase())).map((r) => r.symptom_id));
    }
    const catalog = JSON.parse(readFileSync(path.join(repo, "prompts", "symptoms.json"), "utf8"));
    return new Set(catalog.plan_gated ?? []);
  }

  test("plan-gated flags reach the API and plan-gated symptoms are kept out of the ranked list", async () => {
    const gated = expectedGated();
    assert.ok(gated.size > 0, "the run or the catalog names plan-gated symptoms");
    const csv = symptomsCsv();
    let seen = 0;
    for (const area of AREAS) {
      const { body } = await get(`/api/diagnosis?area=${encodeURIComponent(area)}`);
      for (const s of body.symptoms) {
        assert.equal(s.plan_gated, false, `${area} ${s.symptom_id}`);
        assert.ok(!gated.has(s.symptom_id), `${s.symptom_id} is plan-gated but ranked as a defect`);
      }
      for (const s of body.plan_gated_symptoms) {
        assert.equal(s.plan_gated, true);
        assert.ok(gated.has(s.symptom_id), `${s.symptom_id} is not plan-gated`);
        seen += 1;
      }
      const expected = csv.filter((r) => r.area === area && gated.has(r.symptom_id)).map((r) => r.symptom_id).sort();
      assert.deepEqual(body.plan_gated_symptoms.map((s: any) => s.symptom_id).sort(), expected, area);
    }
    assert.equal(seen, csv.filter((r) => gated.has(r.symptom_id)).length);
  });

  test("concrete_by_area matches diagnosis.json's concrete block (omitted when the run has none)", async () => {
    const diagnosis = JSON.parse(readFileSync(path.join(runDir, "diagnosis.json"), "utf8"));
    const memo = JSON.parse(readFileSync(path.join(runDir, "memo.json"), "utf8"));
    const { body } = await get("/api/diagnosis");
    if (!diagnosis.concrete) {
      assert.ok(!("concrete" in body) && !("concrete_by_area" in body), "omitted for runs without the block");
      return;
    }
    assert.deepEqual(body.concrete_by_area.map((a: any) => a.area), AREAS);
    const gated = expectedGated();
    const csv = symptomsCsv();
    for (const a of body.concrete_by_area) {
      const c = diagnosis.concrete[a.area] ?? {};
      for (const k of ["complaints", "severity_sum", "last6_complaints", "last6_severity_sum"]) {
        assert.equal(a[k], c[k] ?? 0, `${a.area} ${k}`);
      }
      const share = c.complaints > 0 ? Math.round((c.paying_complaints / c.complaints) * 1000) / 10 : null;
      assert.equal(a.paying_share_pct, share, `${a.area} paying share`);
      assert.equal(a.recommended, a.area === memo.final.priority_area, `${a.area} recommended`);
      // Concrete means a named symptom that is not free-tier policy: the CSV rows must add up to it.
      const rows = csv.filter((r) => r.area === a.area && r.symptom_id !== "other" && !gated.has(r.symptom_id));
      assert.equal(a.complaints, rows.reduce((s, r) => s + Number(r.complaints), 0), `${a.area} concrete = CSV`);
    }
    assert.equal(body.concrete.complaints, diagnosis.concrete[body.area].complaints);
    assert.equal(body.concrete.plan_gated_complaints, diagnosis.concrete[body.area].plan_gated_complaints);
  });

  test("unknown or repeated area is 400", async () => {
    for (const qs of ["area=billing", "area=ACCESS", "area=outside%20the%20question", "area=access;drop",
                      "area=access&area=playback"]) {
      const { status, cache, body } = await get(`/api/diagnosis?${qs}`);
      assert.equal(status, 400, qs);
      assert.equal(cache, "no-store");
      assert.equal(typeof body.error.message, "string");
    }
  });

  test("no review id or UUID anywhere in the payload", async () => {
    for (const qs of ["", ...AREAS.map((a) => `?area=${encodeURIComponent(a)}`)]) {
      const raw = JSON.stringify((await get(`/api/diagnosis${qs}`)).body);
      assert.doesNotMatch(raw, UUID, qs);
      assert.ok(!raw.includes("review_id"), qs);
    }
  });
});

describe("quality and safety", () => {
  test("quality has golden, verifier, planted, injection and quarantine", async () => {
    const { status, body } = await get("/api/quality");
    assert.equal(status, 200);
    if (body.golden !== null) {
      for (const set of ["contract_corrected", "original_labels"]) {
        for (const model of ["haiku", "jev"]) {
          assert.equal(typeof body.golden[set][model].agreement.topic, "number", `${set} ${model}`);
        }
      }
    }
    assert.equal(typeof body.verifier.n, "number");
    assert.ok(body.verifier.disagreements.length <= 20);
    assert.equal(typeof body.planted.detected, "number");
    assert.ok(Array.isArray(body.injection.cases));
    assert.ok(Array.isArray(body.quarantine));
  });

  test("no write endpoints and no configuration leaks", async () => {
    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      const res = await fetch(`${base}/api/issues`, { method });
      assert.equal(res.status, 404, method);
    }
    const health = await get("/api/health");
    assert.deepEqual(health.body, { ok: true });
    assert.equal((await get("/api/nope")).status, 404);
  });
});
