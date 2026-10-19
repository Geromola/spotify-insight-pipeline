// db.ts — the only module that talks to a database.
//
// With DATABASE_URL set (production on Vercel) every query goes to Neon over HTTP with
// @neondatabase/serverless. Without it (local development and tests) an in-process PGlite database is
// created once from the bundle written by dashboard/db/build_bundle.py: schema.sql plus one CSV per table.
// BUNDLE_DIR points at that bundle; it defaults to ../db/bundle next to this service.
//
// Callers pass SQL text with $1, $2 placeholders and a separate parameter array. Nothing in this API ever
// interpolates request input into SQL text.

import { readFile } from "node:fs/promises";
import path from "node:path";

import { neon } from "@neondatabase/serverless";

export type Row = Record<string, unknown>;
type Runner = (sql: string, params: unknown[]) => Promise<Row[]>;

const INDEX_MARKER = "-- @indexes";
let runner: Promise<Runner> | undefined;

export function bundleDir(): string {
  return path.resolve(process.env.BUNDLE_DIR ?? path.join(import.meta.dirname, "..", "..", "db", "bundle"));
}

async function createRunner(): Promise<Runner> {
  const url = process.env.DATABASE_URL?.trim();
  if (url) {
    const sql = neon(url);
    return async (text, params) => (await sql.query(text, params)) as Row[];
  }
  return createPglite(bundleDir());
}

async function createPglite(dir: string): Promise<Runner> {
  // Imported lazily so production never loads the WebAssembly Postgres.
  const { PGlite } = await import("@electric-sql/pglite");
  const db = await PGlite.create();
  const schema = await readFile(path.join(dir, "schema.sql"), "utf8");
  const [tablesSql, indexSql = ""] = schema.split(INDEX_MARKER);
  await db.exec(tablesSql);
  for (const [, table] of tablesSql.matchAll(/^CREATE TABLE (\w+)/gm)) {
    const csv = await readFile(path.join(dir, `${table}.csv`));
    const header = csv.subarray(0, csv.indexOf(10)).toString("utf8").trim();
    const columns = header.split(",").map((c) => c.replace(/"/g, "")).join(", ");
    await db.query(`COPY ${table} (${columns}) FROM '/dev/blob' WITH (FORMAT csv, HEADER true)`, [], {
      blob: new Blob([csv]),
    });
  }
  if (indexSql.trim()) await db.exec(indexSql);
  return async (text, params) => (await db.query<Row>(text, params)).rows;
}

/** Run one parameterized, read-only query and return its rows. */
export async function query<T extends Row = Row>(sql: string, params: unknown[] = []): Promise<T[]> {
  runner ??= createRunner().catch((err: unknown) => {
    runner = undefined; // let the next request retry instead of caching the failure forever
    throw err;
  });
  return (await (await runner)(sql, params)) as T[];
}
