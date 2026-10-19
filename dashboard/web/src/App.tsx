import { useCallback, useState } from "react";

import { useApi } from "./api.ts";
import { AgentFlow } from "./components/AgentFlow.tsx";
import { AreasSection } from "./components/AreasSection.tsx";
import { DiagnosisSection, type SymptomTarget } from "./components/DiagnosisSection.tsx";
import { IssuesSection } from "./components/IssuesSection.tsx";
import { Kpis } from "./components/Kpis.tsx";
import { QualitySection } from "./components/QualitySection.tsx";
import { RecommendationCard } from "./components/RecommendationCard.tsx";
import { Status } from "./components/Status.tsx";
import { n } from "./format.ts";
import type { Areas, DiagnosisArea, Flow, Overview, Quality, Recommendation } from "./types.ts";

const REPO_URL = "https://github.com/Geromola/spotify-insight-pipeline";

export function App() {
  const overview = useApi<Overview>("/api/overview");
  const flow = useApi<Flow>("/api/flow");
  const recommendation = useApi<Recommendation>("/api/recommendation");
  const areas = useApi<Areas>("/api/areas");
  const quality = useApi<Quality>("/api/quality");

  const [selectedIssue, setSelectedIssue] = useState<string | null>(null);

  // Claim chips in the memo select an issue and bring the ranking into view.
  const showIssue = useCallback((issueId: string) => {
    setSelectedIssue(issueId);
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    document.getElementById("issues")?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  }, []);

  // Diagnosis quote chips in the memo open that symptom's area in "What to fix" and highlight its row.
  const [symptomTarget, setSymptomTarget] = useState<SymptomTarget | null>(null);
  const showSymptom = useCallback((area: DiagnosisArea, symptomId: string) => {
    setSymptomTarget((prev) => ({ area, symptom_id: symptomId, seq: (prev?.seq ?? 0) + 1 }));
  }, []);

  const o = overview.status === "ready" ? overview.data : null;

  return (
    <>
      <header className="page-header">
        <div className="container">
          <div className="title-row">
            <h1>Spotify review insights</h1>
            <span className="pill">Saved outputs, no live model calls</span>
          </div>
          <p className="subtitle">
            {o ? (
              <>
                {n(o.reviews)} reviews · {o.period.first ?? "–"} to {o.period.last ?? "–"} · run{" "}
                <code>{o.run_id ?? "unknown"}</code>
              </>
            ) : (
              "Loading run details…"
            )}
          </p>
        </div>
      </header>

      <main className="container">
        <section aria-label="Key figures">
          <Status state={overview} what="key figures">
            {(data) => <Kpis overview={data} />}
          </Status>
        </section>

        <section className="card" aria-labelledby="flow-title">
          <h2 id="flow-title">Agent flow</h2>
          <p className="section-note">
            Code stages in gray, model stages in purple. Select a stage for its calls, tokens and cost.
          </p>
          <Status state={flow} what="the agent flow">
            {(data) => <AgentFlow flow={data} />}
          </Status>
        </section>

        <section className="card" aria-labelledby="rec-title">
          <h2 id="rec-title">AI recommendation</h2>
          <Status state={recommendation} what="the recommendation">
            {(data) => <RecommendationCard data={data} onSelectIssue={showIssue} onSelectSymptom={showSymptom} />}
          </Status>
        </section>

        <section className="card" id="diagnosis" aria-labelledby="diagnosis-title">
          <h2 id="diagnosis-title">What to fix</h2>
          <DiagnosisSection target={symptomTarget} />
        </section>

        <section className="card" aria-labelledby="areas-title">
          <h2 id="areas-title">Product areas</h2>
          <Status state={areas} what="product areas">
            {(data) => <AreasSection data={data} />}
          </Status>
        </section>

        <section className="card" id="issues" aria-labelledby="issues-title">
          <h2 id="issues-title">Issue ranking</h2>
          <IssuesSection selected={selectedIssue} onSelect={setSelectedIssue} />
        </section>

        <section className="card" aria-labelledby="quality-title">
          <h2 id="quality-title">Quality checks</h2>
          <Status state={quality} what="quality checks">
            {(data) => <QualitySection data={data} />}
          </Status>
        </section>
      </main>

      <footer className="page-footer">
        <div className="container footer-row">
          <p>
            Methods: code prepares, deduplicates, ranks and checks every number; models only read language (Haiku
            enriches, names issues, diagnoses symptoms and writes the memo, Jev verifies blind samples).
          </p>
          <nav aria-label="Project links">
            <a href={REPO_URL}>Repository</a>
            <a href={`${REPO_URL}#readme`}>Method notes</a>
          </nav>
        </div>
      </footer>
    </>
  );
}
