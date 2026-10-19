// app.ts — the Express application, with no listener attached.
//
// Vercel's Express framework uses this default export as the serverless function; server.ts wraps the
// same app in a long-lived process for local development. Every route is a read-only GET.

import express from "express";

import { router } from "./routes.ts";

export const app = express();

app.disable("x-powered-by");
app.set("query parser", "simple");

app.use((_req, res, next) => {
  res.set("X-Content-Type-Options", "nosniff");
  next();
});

/** Liveness check. Deliberately reveals no configuration values. */
app.get("/api/health", (_req, res) => {
  res.set("Cache-Control", "no-store").json({ ok: true });
});

app.use("/api", router);

app.use((_req, res) => {
  res.set("Cache-Control", "no-store").status(404).json({ error: { message: "Not found." } });
});

// Final safety net: an unexpected throw becomes a generic 500. Internal messages and stack traces stay in
// the logs, never in the HTTP response.
app.use((err: Error, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
  console.error("Unhandled error:", err);
  if (res.headersSent) return;
  res.set("Cache-Control", "no-store").status(500).json({ error: { message: "Something went wrong." } });
});

export default app;
