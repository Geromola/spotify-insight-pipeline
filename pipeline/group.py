"""Stage 4 · the grouper role. Code assigns every complaint to an issue (topic.subtopic); the model
only names issues from a bounded evidence pack (phase B) or proposes subtopics from a sample (phase A).
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from .config import AREA_CODES, DIAGNOSIS_AREAS, PROMPTS
from .roles import Writer, cached, digest, remember

QUOTE_CHARS = 240
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")

NAME_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["issues"],
    "properties": {"issues": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["issue_id", "title", "description", "example_review_ids"],
        "properties": {"issue_id": {"type": "string"}, "title": {"type": "string"},
                       "description": {"type": "string"},
                       "example_review_ids": {"type": "array", "items": {"type": "string"}}}}}}}

DISCOVER_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["topics"],
    "properties": {"topics": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["topic", "subtopics"],
        "properties": {"topic": {"type": "string"}, "subtopics": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "name", "definition", "example_review_ids"],
            "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "definition": {"type": "string"},
                           "example_review_ids": {"type": "array", "items": {"type": "string"}}}}}}}}}}


SYMPTOMS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["symptoms"],
    "properties": {"symptoms": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "name", "definition", "example_review_ids"],
        "properties": {"id": {"type": "string"}, "name": {"type": "string"}, "definition": {"type": "string"},
                       "example_review_ids": {"type": "array", "items": {"type": "string"}}}}}}}
SYMPTOM_RANGE = (4, 9)
DIAGNOSABLE = "c.intent IN ('complaint','cancellation') AND c.free_tier_limit=0"


def examples(conn, where: str, args: tuple, k: int, order: str = "likes", with_issue: bool = False) -> list[dict]:
    """Deterministic evidence: original reviews only (no cache copies), quote from the saved record."""
    rows = conn.execute(
        "SELECT r.review_id, r.review_likes, json_extract(t.result,'$.severity') AS severity, "
        "c.topic || '.' || c.subtopic AS issue_id, "
        "json_extract(t.result,'$.evidence_quote') AS quote FROM records c JOIN reviews r USING(review_id) "
        f"JOIN texts t ON t.text_sha=r.text_sha WHERE c.status='completed' AND c.cache_source_id IS NULL AND {where}",
        args).fetchall()
    if order == "likes":  # most-endorsed first, then most severe
        rows = sorted(rows, key=lambda r: (-int(r["review_likes"] or 0), -r["severity"], r["review_id"]))
    else:  # a spread-out sample for discovery
        rows = sorted(rows, key=lambda r: hashlib.sha256(f"discover:{r['review_id']}".encode()).hexdigest())
    if with_issue:
        return [{"review_id": r["review_id"], "issue_id": r["issue_id"], "quote": r["quote"][:QUOTE_CHARS]}
                for r in rows[:k]]
    return [{"review_id": r["review_id"], "quote": r["quote"][:QUOTE_CHARS]} for r in rows[:k]]


def write_issues(run_dir: Path, tax: dict) -> None:
    """issues.csv: every taxonomy issue, plus the grouper's title and description when it has run."""
    names = json.loads((run_dir / "issue_names.json").read_text(encoding="utf-8")) \
        if (run_dir / "issue_names.json").exists() else {}
    with (run_dir / "issues.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["issue_id", "topic", "subtopic", "name", "definition", "title", "description",
                         "example_review_ids"])
        for topic, subs in tax["topics"].items():
            for sub in subs:
                issue = f"{topic}.{sub['id']}"
                named = names.get("issues", {}).get(issue, {})
                writer.writerow([issue, topic, sub["id"], sub["name"], sub["definition"], named.get("title", ""),
                                 named.get("description", ""), " ".join(named.get("example_review_ids", []))])


def name_issues(conn, run_dir: Path, writer: Writer, tax: dict, k: int) -> dict:
    """One call names every ranked issue. Unknown issue ids and unsupported example ids are dropped."""
    with (run_dir / "ranking.csv").open(encoding="utf-8", newline="") as f:
        ranking = list(csv.DictReader(f))
    packs = []
    for row in ranking:
        topic, subtopic = row["issue_id"].split(".", 1)
        packs.append({"issue_id": row["issue_id"], "complaint_count": int(row["complaint_count"]),
                      "mean_severity": row["mean_severity"],
                      "examples": examples(conn, "c.intent IN ('complaint','cancellation') AND c.topic=? "
                                                 "AND c.subtopic=?", (topic, subtopic), k)})
    pack = {"issues": packs}
    key = digest(pack, writer.label_config("group_name_v1.md"))
    output = cached(conn, "group_name", key)
    from_cache = output is not None
    if output is None:
        evidence = [e["review_id"] for p in packs for e in p["examples"]]
        output = writer.ask(role="group", step="name", prompt_name="group_name_v1.md", pack=pack,
                            schema=NAME_SCHEMA, review_ids=evidence)
        remember(conn, "group_name", key, output)
    allowed = {p["issue_id"]: {e["review_id"] for e in p["examples"]} for p in packs}
    issues, problems = {}, []
    for item in output["issues"]:
        if item["issue_id"] not in allowed:
            problems.append(f"unknown issue id dropped: {item['issue_id']}")
            continue
        cited = [r for r in item["example_review_ids"] if r in allowed[item["issue_id"]]]
        if len(cited) != len(item["example_review_ids"]):
            problems.append(f"{item['issue_id']}: dropped example ids not in its evidence pack")
        issues[item["issue_id"]] = {"title": item["title"].strip(), "description": item["description"].strip(),
                                    "example_review_ids": cited}
    for issue in allowed.keys() - issues.keys():
        problems.append(f"{issue}: not named by the model; title falls back to the id")
        issues[issue] = {"title": issue, "description": "", "example_review_ids": []}
    result = {"issues": issues, "problems": problems, "from_cache": from_cache}
    (run_dir / "issue_names.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_issues(run_dir, tax)
    return result


def discover_subtopics(conn, run_dir: Path, writer: Writer, tax: dict, k: int) -> dict:
    """Phase A: propose subtopics per topic from a bounded sample. A person approves before use."""
    topics = []
    for topic in tax["topics"]:
        if topic == "other":
            continue
        complaints = conn.execute("SELECT COUNT(*) FROM records WHERE status='completed' AND topic=? "
                                  "AND intent IN ('complaint','cancellation')", (topic,)).fetchone()[0]
        terms = Counter()
        for (result,) in conn.execute(
                "SELECT t.result FROM records c JOIN reviews r USING(review_id) JOIN texts t ON t.text_sha=r.text_sha "
                "WHERE c.status='completed' AND c.topic=? AND c.intent IN ('complaint','cancellation')", (topic,)):
            terms.update(json.loads(result)["entities"])
        topics.append({"topic": topic, "complaints": complaints, "top_entities": terms.most_common(15),
                       "examples": examples(conn, "c.intent IN ('complaint','cancellation') AND c.topic=?",
                                            (topic,), k, order="spread")})
    pack = {"topics": topics}
    evidence = {e["review_id"] for t in topics for e in t["examples"]}
    output = writer.ask(role="group", step="discover", prompt_name="group_discover_v1.md", pack=pack,
                        schema=DISCOVER_SCHEMA, review_ids=sorted(evidence))
    proposal, problems = {"version": "t1", "topics": {}}, []
    for item in output["topics"]:
        if item["topic"] not in tax["topics"] or item["topic"] == "other":
            problems.append(f"unknown topic dropped: {item['topic']}")
            continue
        subs, seen = [], set()
        for sub in item["subtopics"]:
            if not SLUG.match(sub["id"]) or sub["id"] in seen:
                problems.append(f"{item['topic']}: invalid or repeated subtopic id {sub['id']!r} dropped")
                continue
            seen.add(sub["id"])
            subs.append({"id": sub["id"], "name": sub["name"].strip(), "definition": sub["definition"].strip(),
                         "example_review_ids": [r for r in sub["example_review_ids"] if r in evidence]})
        if not subs or subs[0]["id"] != "general":  # index 0 is the catch-all the enricher falls back to
            subs = [s for s in subs if s["id"] != "general"]
            subs.insert(0, {"id": "general", "name": f"Other {item['topic']} problem",
                            "definition": f"A {item['topic']} problem that fits no other {item['topic']} subtopic",
                            "example_review_ids": []})
        proposal["topics"][item["topic"]] = subs[:7]
    for topic, subs in tax["topics"].items():
        proposal["topics"].setdefault(topic, [dict(s, example_review_ids=[]) for s in subs[:1]])
    proposal["topics"] = {t: proposal["topics"][t] for t in tax["topics"]}
    result = {"proposal": proposal, "problems": problems, "sample_reviews": len(evidence)}
    (run_dir / "taxonomy_proposal.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                                                    encoding="utf-8")
    return result


def approve_taxonomy(run_dir: Path) -> Path:
    """The human gate: copy an approved proposal into prompts/taxonomy.json (a new label_config)."""
    proposal = json.loads((run_dir / "taxonomy_proposal.json").read_text(encoding="utf-8"))["proposal"]
    taxonomy = {"version": proposal["version"], "note": f"Approved by Gero from {run_dir.name}",
                "topics": {t: [{k: s[k] for k in ("id", "name", "definition")} for s in subs]
                           for t, subs in proposal["topics"].items()}}
    path = PROMPTS / "taxonomy.json"
    path.write_text(json.dumps(taxonomy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def discover_symptoms(conn, run_dir: Path, writer: Writer, tax: dict, k: int) -> dict:
    """Calibration: propose 4-9 concrete symptoms per area, one call per area. A person approves before use."""
    code_of = {area: code for code, area in AREA_CODES.items()}
    proposal, problems, seen, sample = {"version": "y1", "areas": {}}, [], set(), 0
    for area, topics in DIAGNOSIS_AREAS.items():
        marks = ",".join("?" * len(topics))
        where = f"{DIAGNOSABLE} AND c.topic IN ({marks})"
        counts = dict(conn.execute(f"SELECT c.topic || '.' || c.subtopic, COUNT(*) FROM records c WHERE "
                                   f"c.status='completed' AND {where} GROUP BY 1", topics).fetchall())
        liked = examples(conn, where, topics, k, with_issue=True)
        known = {e["review_id"] for e in liked}
        spread = [e for e in examples(conn, where, topics, 2 * k, order="discover", with_issue=True)
                  if e["review_id"] not in known][:k]
        pack = {"area": area, "subtopics": [
            {"issue_id": f"{t}.{sub['id']}", "name": sub["name"], "definition": sub["definition"],
             "complaints": counts.get(f"{t}.{sub['id']}", 0)} for t in topics for sub in tax["topics"][t]],
            "examples": liked + spread}
        evidence = [e["review_id"] for e in pack["examples"]]
        sample += len(evidence)
        output = writer.ask(role="group", step="symptoms", prompt_name="symptoms_discover_v1.md", pack=pack,
                            schema=SYMPTOMS_SCHEMA, review_ids=sorted(set(evidence)))
        symptoms, mine = [], set()
        for item in output["symptoms"]:
            sid, name = item["id"].strip(), item["name"].strip()
            if not SLUG.match(sid) or sid == "other" or sid in mine or not name:
                problems.append(f"{area}: invalid, reserved or repeated symptom id {sid!r} dropped")
                continue
            mine.add(sid)
            if sid in seen:  # ids stay unique across areas, so a record's symptom id names one symptom
                problems.append(f"{area}: id {sid!r} is used in another area; renamed {code_of[area]}-{sid}")
                sid = f"{code_of[area]}-{sid}"[:31]
            seen.add(sid)
            cited = [r for r in item["example_review_ids"] if r in evidence]
            if len(cited) != len(item["example_review_ids"]):
                problems.append(f"{area}.{sid}: dropped example ids not in its evidence pack")
            symptoms.append({"id": sid, "name": name, "definition": item["definition"].strip(),
                             "example_review_ids": cited})
        low, high = SYMPTOM_RANGE
        if len(symptoms) > high:
            problems.append(f"{area}: {len(symptoms)} symptoms; kept the first {high}")
            symptoms = symptoms[:high]
        elif len(symptoms) < low:
            problems.append(f"{area}: only {len(symptoms)} symptoms (asked for {low}-{high}); edit before approving")
        proposal["areas"][area] = symptoms
    result = {"proposal": proposal, "problems": problems, "sample_reviews": sample}
    (run_dir / "symptoms_proposal.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                                                    encoding="utf-8")
    return result


def approve_symptoms(run_dir: Path, out: Path | None = None) -> Path:
    """The human gate: copy an approved symptom proposal into prompts/symptoms.json (or `out`)."""
    proposal = json.loads((run_dir / "symptoms_proposal.json").read_text(encoding="utf-8"))["proposal"]
    catalog = {"version": proposal["version"], "note": f"Approved from {run_dir.name}",
               "areas": {a: [{k: s[k] for k in ("id", "name", "definition")} for s in symptoms]
                         for a, symptoms in proposal["areas"].items()}}
    path = Path(out) if out else PROMPTS / "symptoms.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
