// server.ts — local development entry point only. In production Vercel runs the app from app.ts.
// Without DATABASE_URL the API serves the bundle in ../db/bundle (or BUNDLE_DIR) through PGlite.

import { app } from "./app.ts";

const port = Number(process.env.PORT ?? 3001);

app.listen(port, () => {
  const source = process.env.DATABASE_URL ? "Postgres (DATABASE_URL)" : "local bundle via PGlite";
  console.log(`API listening on http://localhost:${port} using ${source}`);
});
