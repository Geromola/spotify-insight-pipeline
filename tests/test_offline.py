"""Offline plumbing tests. The fake provider stands in for a model; no test makes a paid call.

    python3.12 tests/test_offline.py
"""
import gzip
import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.budget import Rates, reserved, spent  # noqa: E402
from pipeline.config import CONTEXT_CODES, LONG_TIME_CODES, TRIGGER_CODES, Settings, load_taxonomy  # noqa: E402
from pipeline.db import backlog_counts, connect, diagnosis_counts, meta_set  # noqa: E402
from pipeline.diagnose import (Case, Diagnoser, bind_catalog, fill_backlog, label_config as diagnose_config,  # noqa: E402
                               load_symptoms, parse_diagnoses, plan_gated_ids, release_stale_diagnoses)
from pipeline.enrich import (Enricher, Item, parse, release_stale, release_unfunded_fallback,  # noqa: E402
                             resolve_fallback_cap)
from pipeline.export import export_grading, finalize, write_checkpoint  # noqa: E402
from pipeline.group import approve_symptoms, discover_symptoms, name_issues  # noqa: E402
from pipeline.symptom_tables import write_diagnosis  # noqa: E402
from pipeline.memo import diagnosis_facts, quote_sources, render_memo, run_memo, validate_memo  # noqa: E402
from pipeline.roles import Writer  # noqa: E402
from pipeline.verify import run_diagnosis_check, run_verify  # noqa: E402
from pipeline.prepare import prepare  # noqa: E402
from pipeline.providers.fake import FakeProvider  # noqa: E402
from pipeline.textproc import entities, segments  # noqa: E402
from tools import check_submission as checker  # noqa: E402

DATA = ROOT / "data"


class Base(unittest.TestCase):
    sample = "cost_100.csv"
    overrides: dict = {}

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pipeline-test-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.conn = connect(self.dir / "pipeline.sqlite")
        self.addCleanup(self.conn.close)
        self.settings = Settings.load(None, backoff_seconds=0, poll_seconds=0, **self.overrides)
        self.tax = load_taxonomy(self.settings)
        self.report = prepare(self.conn, DATA / self.sample, self.dir)
        self.invocation = 0

    def enricher(self, rates=None, **fake) -> Enricher:
        self.invocation += 1
        meta_set(self.conn, "invocation", str(self.invocation))
        provider = FakeProvider(spool=self.dir / "fake_batches", **fake)
        enricher = Enricher(self.conn, provider, self.settings, rates or Rates(), self.tax, self.invocation,
                            sleep=lambda seconds: None)
        meta_set(self.conn, "label_config", enricher.label_config)
        return enricher

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]


class TextWork(unittest.TestCase):
    def test_every_segment_is_an_exact_substring(self):
        texts = [row["review_text"] for row in checker.csv_rows(DATA / "checkpoint_500.csv")]
        texts += ["Line one.\nLine two!!  \n\nLine three is the longest of them all, by far?", "...", "a" * 300,
                  "Version 8.8.76 broke it. Now it will not open at all. Please fix. Thanks."]
        for text in texts:
            for limit in (0, 80):
                segs = segments(text, limit)
                self.assertTrue(segs and all(s and s in text for s in segs), text)
                self.assertLessEqual(len(segs), 10)

    def test_entities_come_from_the_text(self):
        self.assertEqual(entities("Too many ads and the shuffle is broken"), ["ads", "shuffle"])
        self.assertEqual(entities("Great"), [])


class Validation(unittest.TestCase):
    def setUp(self):
        self.tax = load_taxonomy(Settings())
        long = "It used to be perfectly fine for years. Now it stops every few minutes and I must restart the app."
        self.items = [Item(0, "a", "Love it", "id-0", ["Love it"]), Item(1, "b", long, "id-1", segments(long, 20)),
                      Item(2, "c", "Bad", "id-2", ["Bad"])]

    def test_good_lines_become_contract_records(self):
        good, bad = parse("0|ot|0|p|1|9|0|0|n|u\n1|pb|0|c|3|-6|1|0|y|p\n2|ot|0|c|2|-5|0|0|n|f", self.items, self.tax)
        self.assertEqual(bad, {})
        self.assertEqual(good[1]["topic"], "playback")
        self.assertEqual(good[1]["sentiment"], -0.6)
        self.assertIn(good[1]["evidence_quote"], self.items[1].text)
        self.assertFalse(good[1]["needs_review"])
        self.assertEqual((good[1]["free_tier_limit"], good[1]["plan"], good[2]["plan"]), (True, "paid", "free"))

    def test_each_bad_line_fails_alone(self):
        output = "\n".join([
            "Here are the labels:",          # prose is ignored
            "0|zz|0|p|1|9|0|0|n|u",          # unknown topic
            "1|pb|0|c|9|-6|0|0|n|u",         # severity out of range
            "7|ot|0|p|1|9|0|0|n|u",          # an id that was never sent
        ])                                   # and review 2 is missing
        good, bad = parse(output, self.items, self.tax)
        self.assertEqual(good, {})
        self.assertEqual(bad, {0: "unknown_topic", 1: "severity_range", 2: "missing_line"})

    def test_an_unknown_subtopic_falls_back_to_general_and_is_flagged(self):
        good, bad = parse("0|ot|7|p|1|9|0|0|n|u", self.items[:1], self.tax)
        self.assertEqual(bad, {})
        self.assertEqual((good[0]["subtopic"], good[0]["doubt"], good[0]["needs_review"]),
                         ("general", "unknown_subtopic", True))

    def test_a_bad_quote_index_falls_back_to_the_whole_text(self):
        good, bad = parse("1|pb|0|c|3|-6|7|0|n|u", self.items[1:2], self.tax)
        self.assertEqual(bad, {})
        self.assertEqual(good[1]["evidence_quote"], self.items[1].text.strip())

    def test_bad_segmentation_fields_are_rejected(self):
        good, bad = parse("0|ot|0|p|1|9|0|0|2|u\n2|ot|0|c|2|-5|0|0|n|x", self.items[:1] + self.items[2:], self.tax)
        self.assertEqual((good, bad), ({}, {0: "limit_flag", 2: "unknown_plan"}))

    def test_duplicate_and_inconsistent_lines(self):
        good, bad = parse("0|ot|0|p|4|9|0|0|n|u\n2|ot|0|c|2|-5|0|0|n|u\n2|ot|0|c|3|-5|0|0|n|u", self.items[:1] + self.items[2:],
                          self.tax)
        self.assertEqual(bad, {2: "duplicate_line"})
        self.assertTrue(good[0]["needs_review"])
        self.assertEqual(good[0]["doubt"], "severity_without_problem")


class Backlog(Base):
    overrides = {"batch_size": 10}

    def test_requests_are_bounded_and_everything_completes(self):
        self.assertIsNone(self.enricher().run())
        sizes = [len(json.loads(r[0])) for r in self.conn.execute("SELECT review_ids FROM calls")]
        self.assertEqual(max(sizes), 10)
        self.assertEqual(sum(sizes), 100)
        self.assertEqual(self.one("SELECT COUNT(*) FROM texts WHERE status IN ('pending','leased')"), 0)

    def test_invalid_output_is_retried_once_then_quarantined(self):
        self.enricher(bad_marker="premium").run()
        hit = self.one("SELECT COUNT(*) FROM texts WHERE text GLOB '*premium*'")
        self.assertGreater(hit, 0)
        rows = self.conn.execute("SELECT status, attempts, reason FROM texts WHERE text GLOB '*premium*'").fetchall()
        self.assertTrue(all(tuple(r) == ("quarantined", 2, "invalid_output:field_count") for r in rows))
        self.assertEqual(self.one("SELECT COUNT(*) FROM texts WHERE status='quarantined'"), hit)

    def test_one_retry_recovers_a_dropped_line(self):
        self.enricher(bad_once_marker="premium").run()
        self.assertEqual(self.one("SELECT COUNT(*) FROM texts WHERE status='quarantined'"), 0)
        self.assertGreater(self.one("SELECT COUNT(*) FROM texts WHERE attempts=1"), 0)

    def test_a_refusal_keeps_earlier_lines_and_isolates_the_refused_text(self):
        marker = self.conn.execute("SELECT text FROM texts ORDER BY n_chars DESC LIMIT 1").fetchone()[0][:30]
        self.enricher(refuse_marker=marker).run()
        rows = self.conn.execute("SELECT status, attempts, reason FROM texts WHERE status NOT IN ('completed','fallback_pending')").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("quarantined", 2, "refusal:general_harms")])
        # Neighbours cut off by the refusal were retried alone with no attempt counted, and completed;
        # only the text refused while alone, twice, was quarantined.
        refused_alone = self.one("SELECT COUNT(*) FROM calls WHERE stop_reason='refusal' AND n_valid=0 AND n_invalid=1")
        self.assertEqual(refused_alone, 2)
        self.assertEqual(self.one("SELECT COUNT(*) FROM texts WHERE status='quarantined'"), 1)
        # No request of several reviews was refused more than once: neighbours are never regrouped with the trigger.
        self.assertEqual(self.one("SELECT COUNT(*) FROM calls WHERE stop_reason='refusal' AND n_valid+n_invalid>1"), 1)

    def test_a_transport_failure_is_logged_but_is_not_an_attempt(self):
        self.assertIsNone(self.enricher(fail_first=2).run())
        self.assertEqual(self.one("SELECT COUNT(*) FROM calls WHERE outcome='failed'"), 2)
        self.assertEqual(self.one("SELECT COALESCE(SUM(attempts),0) FROM texts"), 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM texts WHERE status IN ('pending','leased')"), 0)

    def test_persistent_transport_failure_stops_with_progress_saved(self):
        stopped = self.enricher(fail_first=99).run()
        self.assertTrue(stopped.startswith("transport"))
        self.assertEqual(backlog_counts(self.conn), {"pending": 100})

    def test_interruption_then_resume_never_pays_twice(self):
        first = self.enricher(interrupt_after=3)
        with self.assertRaises(KeyboardInterrupt):
            first.run()
        self.assertEqual(self.one("SELECT COUNT(*) FROM texts WHERE status='leased'"), 10)
        self.assertEqual(release_stale(self.conn), 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM texts WHERE status='leased'"), 0)
        before = set(json.loads(write_checkpoint(self.conn, self.dir, 1, "interrupted").read_text())["completed_ids"])
        self.assertTrue(before)

        self.assertIsNone(self.enricher().run())
        resumed = {rid for r in self.conn.execute("SELECT review_ids FROM calls WHERE invocation=2")
                   for rid in json.loads(r[0])}
        self.assertFalse(before & resumed)
        paid = [rid for r in self.conn.execute("SELECT review_ids FROM calls WHERE outcome='succeeded'")
                for rid in json.loads(r[0])]
        self.assertEqual(len(paid), len(set(paid)))  # no text was sent twice

    def test_the_spend_cap_stops_admission(self):
        rates_file = self.dir / "rates.csv"
        rates_file.write_text("provider,model,tier,item,unit,price_usd\n" + "".join(
            f"fake,fake-rules-v0,synthetic,{item},per_1M_tokens,1000\n"
            for item in ("input", "cache_write", "cached_input", "output")))
        self.settings = Settings.load(None, batch_size=10, budget_usd=5.0, enrich_cap_usd=5.0)
        stopped = self.enricher(rates=Rates(rates_file)).run()
        self.assertTrue(stopped.startswith("budget"), stopped)
        self.assertGreater(backlog_counts(self.conn).get("pending", 0), 0)
        self.assertGreater(self.one("SELECT COUNT(*) FROM calls"), 0)
        self.assertLessEqual(spent(self.conn) + reserved(self.conn), 5.0)


class BatchLane(Base):
    sample = "checkpoint_500.csv"
    overrides = {"mode": "batch", "batch_size": 50, "batch_file_requests": 3}

    def test_a_submitted_batch_is_polled_after_restart_not_resent(self):
        first = self.enricher(batch_polls=2)
        first._submit_wave("primary")                       # three requests are now with the "provider"
        self.assertEqual(self.one("SELECT COUNT(*) FROM requests WHERE status='submitted'"), 3)
        self.assertEqual(release_stale(self.conn), 0)       # a restart must leave submitted work alone
        self.assertEqual(self.one("SELECT COUNT(*) FROM texts WHERE status='leased'"), 150)

        self.assertIsNone(self.enricher(batch_polls=2).run())
        self.assertEqual(self.one("SELECT COUNT(*) FROM texts WHERE status IN ('pending','leased')"), 0)
        sent = [rid for r in self.conn.execute("SELECT review_ids FROM calls") for rid in json.loads(r[0])]
        self.assertEqual(len(sent), len(set(sent)))
        self.assertEqual(len(sent), self.report["unique_exact_texts"])


class FlakyBatchProvider(FakeProvider):
    """Fails the first two polls and the first submit, as a dropped connection would."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.poll_failures, self.submit_failures = 2, 1

    def poll_batch(self, provider_batch):
        if self.poll_failures:
            self.poll_failures -= 1
            raise ConnectionError("simulated dropped connection")
        return super().poll_batch(provider_batch)

    def submit_batch(self, requests):
        if self.submit_failures:
            self.submit_failures -= 1
            raise ConnectionError("simulated dropped connection")
        return super().submit_batch(requests)


class BatchNetworkErrors(Base):
    sample = "checkpoint_500.csv"
    overrides = {"mode": "batch", "batch_file_requests": 4}

    def test_network_errors_in_the_batch_lane_are_retried_without_losing_work(self):
        self.invocation += 1
        meta_set(self.conn, "invocation", str(self.invocation))
        provider = FlakyBatchProvider(spool=self.dir / "fake_batches")
        enricher = Enricher(self.conn, provider, self.settings, Rates(), self.tax, self.invocation, sleep=lambda s: None)
        self.assertIsNone(enricher.run())
        self.assertEqual(self.one("SELECT COUNT(*) FROM texts WHERE status IN ('pending','leased')"), 0)
        sent = [rid for r in self.conn.execute("SELECT review_ids FROM calls") for rid in json.loads(r[0])]
        self.assertEqual(len(sent), len(set(sent)))  # the failed submit was released, not double-sent


class EndToEnd(Base):
    sample = "checkpoint_500.csv"

    def test_the_grading_export_passes_the_official_checker(self):
        first = self.enricher()
        first.max_requests = 4
        self.assertEqual(first.run(), "max_requests")
        write_checkpoint(self.conn, self.dir, 1, "max_requests")
        self.enricher().run()
        write_checkpoint(self.conn, self.dir, 2, "finished")
        cap = resolve_fallback_cap(self.conn, self.settings)
        self.assertEqual(cap["to_fallback"], min(cap["doubted"], cap["cap"]))
        self.settings = Settings.load(None, mode="sync", backoff_seconds=0)
        self.enricher().run("fallback")
        write_checkpoint(self.conn, self.dir, 3, "finished")
        meta_set(self.conn, "settings", json.dumps(self.settings.as_dict()))

        report = run_verify(self.conn, self.settings, self.dir, 4, sleep=lambda s: None)
        self.assertEqual(report["n"], 96)  # 20% of 479 unique texts, declared before any call
        self.assertEqual(json.loads((self.dir / "verify_planted.json").read_text())["rate"], 1.0)
        summary = finalize(self.conn, self.dir, self.tax)
        self.assertEqual(summary["completed"], 500)
        self.assertEqual(summary["cache_reuse"], 500 - self.report["unique_exact_texts"])
        writer = Writer(self.conn, FakeProvider(spool=self.dir / "fake_batches"), self.settings, Rates(), 5,
                        sleep=lambda s: None)
        self.assertFalse(name_issues(self.conn, self.dir, writer, self.tax, 6)["problems"])
        memo = run_memo(self.conn, self.dir, writer, self.settings)
        self.assertEqual((memo["final_problems"], memo["from_cache"]), ([], False))
        rule = {"without free-tier limits": ("free_tier_limit", 0), "paying reviewers only": ("plan", "paid")}
        pack = json.loads((self.dir / "memo.json").read_text())["facts"]
        checked = 0
        for lens, issues in pack["segments"]["lens_rankings"].items():  # lens quotes stay in their lens
            column, value = rule[lens]
            for quote in (q for issue in issues for q in issue["quotes"]):
                row = self.conn.execute(f"SELECT {column} FROM records WHERE review_id=?", (quote["review_id"],))
                self.assertEqual(row.fetchone()[0], value, (lens, quote))
                checked += 1
        self.assertGreater(checked, 0)
        self.assertTrue(run_memo(self.conn, self.dir, writer, self.settings)["from_cache"])  # warm: no new call
        # The diagnose stage and its blind check add group and verify calls; the checker must still pass.
        discover_symptoms(self.conn, self.dir, writer, self.tax, 30)
        self.settings = replace(self.settings, symptoms=str(approve_symptoms(self.dir, self.dir / "symptoms.json")))
        catalog = load_symptoms(self.settings)
        bind_catalog(self.conn, catalog, diagnose_config(self.settings, catalog))
        fill_backlog(self.conn)
        provider = FakeProvider(spool=self.dir / "fake_batches")
        self.assertIsNone(Diagnoser(self.conn, provider, self.settings, Rates(), catalog, 6, sleep=lambda s: None).run())
        run_diagnosis_check(self.conn, self.settings, self.dir, 6, catalog, sleep=lambda s: None)
        export_grading(self.conn, self.dir, self.dir / "grading", before_invocation=1)
        with gzip.open(self.dir / "grading" / "calls.jsonl.gz", "rt") as f:
            steps = {(c["role"], c["step"]) for c in map(json.loads, f)}
        self.assertTrue({("group", "diagnose"), ("group", "symptoms"), ("verify", "diagnose")} <= steps)

        sample = DATA / self.sample
        audit = checker.audit(self.dir / "grading", checker.reference(sample, sample))
        self.assertEqual(audit["issue_counts"], {})
        self.assertEqual(audit["status"], "pass")
        self.assertEqual(audit["working_coverage_point_candidate"], 1.0)
        self.assertEqual(audit["coverage"]["valid_cache_reuses"], summary["cache_reuse"])
        self.assertTrue(audit["calculated_ranking"])


AREA_OF = {"access": "access", "usability": "usability", "playback": "playback", "billing": "billing/support",
           "support": "billing/support"}
CATALOG = {"version": "y-test", "areas": {
    "access": [{"id": "login-fails", "name": "Login fails", "definition": "d"}],
    "usability": [],
    "playback": [{"id": "stops", "name": "Music stops", "definition": "d"},
                 {"id": "crash", "name": "App crashes", "definition": "d"},
                 {"id": "buffer", "name": "Buffering", "definition": "d"}],
    "billing/support": [{"id": "charged", "name": "Charged twice", "definition": "d"}]}}


class DiagnosisLines(unittest.TestCase):
    def setUp(self):
        long = "Since the update music stops after a few seconds. Even offline it happens! I have used it for years now."
        self.items = [Case(0, "a", "It stops", "id-0", ["It stops"], area="playback"),
                      Case(1, "b", long, "id-1", segments(long, 20), area="playback"),
                      Case(2, "c", "Cannot log in", "id-2", ["Cannot log in"], area="access"),
                      Case(3, "d", "Bad layout", "id-3", ["Bad layout"], area="usability")]

    def test_good_lines_become_diagnoses(self):
        good, bad = parse_diagnoses("0|1|B|u|y|0\n1|3|N|o|-|1\n2|0|N|k|-|0\n3|0|D|x|-|0", self.items, CATALOG)
        self.assertEqual(bad, {})
        self.assertEqual(good[0], {"symptom_id": "stops", "context": "bluetooth", "trigger": "after-update",
                                   "long_time_user": True, "quote": "It stops"})
        self.assertEqual((good[1]["symptom_id"], good[1]["trigger"], good[1]["quote"]),
                         ("buffer", "offline", self.items[1].segs[1]))
        self.assertIn(good[1]["quote"], self.items[1].text)
        self.assertEqual((good[2]["symptom_id"], good[3]["context"]), ("other", "desktop-web"))

    def test_each_bad_line_fails_alone(self):
        cases = {
            "0|1|B|u|y": "field_count",
            "0|one|B|u|y|0": "non_integer_field",
            "0|4|B|u|y|0": "symptom_range",       # playback lists three symptoms
            "0|-1|B|u|y|0": "symptom_range",
            "0|1|b|u|y|0": "unknown_context",     # context is one uppercase letter
            "0|1|B|U|y|0": "unknown_trigger",     # trigger is one lowercase letter
            "0|1|B|u|n|0": "long_time_flag",      # y or -, never n
        }
        for line, reason in cases.items():
            good, bad = parse_diagnoses(line, self.items[:1], CATALOG)
            self.assertEqual((good, bad), ({}, {0: reason}), line)
        good, bad = parse_diagnoses("3|1|N|x|-|0", self.items[3:], CATALOG)  # usability lists no symptom: only 0
        self.assertEqual(bad, {3: "symptom_range"})
        good, bad = parse_diagnoses("Here you go:\n0|1|B|u|y|0\n0|2|B|u|y|0\n9|1|N|x|-|0", self.items[:2], CATALOG)
        self.assertEqual((good, bad), ({}, {0: "duplicate_line", 1: "missing_line"}))

    def test_an_out_of_range_segment_falls_back_to_the_whole_text(self):
        good, bad = parse_diagnoses("1|1|N|x|y|7", self.items[1:2], CATALOG)
        self.assertEqual(bad, {})
        self.assertEqual(good[1]["quote"], self.items[1].text.strip())

    def test_field_alphabets_are_disjoint_so_a_shifted_code_is_caught(self):
        alphabets = [set(CONTEXT_CODES), set(TRIGGER_CODES), set(LONG_TIME_CODES)]
        for i, a in enumerate(alphabets):
            for b in alphabets[i + 1:]:
                self.assertFalse(a & b)
        for line, reason in (("0|1|u|B|y|0", "unknown_context"),  # context and trigger swapped
                             ("0|1|B|u|u|0", "long_time_flag"),   # a trigger code in the long-time slot
                             ("0|1|B|y|-|0", "unknown_trigger")):
            self.assertEqual(parse_diagnoses(line, self.items[:1], CATALOG)[1], {0: reason}, line)


class DiagnoseBase(Base):
    sample = "checkpoint_500.csv"

    def setUp(self):
        super().setUp()
        self.enricher().run()
        release_unfunded_fallback(self.conn)  # finish doubted texts with their primary labels
        finalize(self.conn, self.dir, self.tax)
        writer = Writer(self.conn, FakeProvider(spool=self.dir / "fake_batches"), self.settings, Rates(), 2,
                        sleep=lambda s: None)
        self.proposal = discover_symptoms(self.conn, self.dir, writer, self.tax, 30)
        self.settings = replace(self.settings, symptoms=str(approve_symptoms(self.dir, self.dir / "symptoms.json")))
        self.catalog = load_symptoms(self.settings)
        bind_catalog(self.conn, self.catalog, diagnose_config(self.settings, self.catalog))
        self.added = fill_backlog(self.conn)
        self.texts_before = self.conn.execute("SELECT * FROM texts ORDER BY text_sha").fetchall()

    def diagnoser(self, rates=None, **fake) -> Diagnoser:
        self.invocation += 1
        provider = FakeProvider(spool=self.dir / "fake_batches", **fake)
        return Diagnoser(self.conn, provider, self.settings, rates or Rates(), self.catalog, self.invocation,
                         sleep=lambda s: None)

    def marker(self, offset: int = 0) -> str:
        """The opening words of one diagnosable text, so a fake hook hits that text alone."""
        return self.conn.execute("SELECT t.text FROM diagnoses d JOIN texts t USING(text_sha) "
                                 "ORDER BY d.text_sha LIMIT 1 OFFSET ?", (offset,)).fetchone()[0][:25]

    def add_copy(self) -> str:
        """A second review with a diagnosable review's exact text: a cache copy that inherits its diagnosis."""
        self.conn.execute("INSERT INTO reviews SELECT 'copy-' || r.review_id, r.row_num + 100000, r.source_sha256, "
                          "r.text_sha, r.review_rating, '7', r.app_version, r.review_timestamp FROM reviews r "
                          "JOIN diagnoses d ON d.text_sha=r.text_sha ORDER BY d.text_sha LIMIT 1")
        finalize(self.conn, self.dir, self.tax)
        return self.conn.execute("SELECT review_id FROM reviews WHERE review_id GLOB 'copy-*'").fetchone()[0]

    def assert_records_untouched(self):
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM texts ORDER BY text_sha")],
                         [tuple(r) for r in self.texts_before])


class Diagnosis(DiagnoseBase):
    def test_discovery_proposes_a_valid_catalog_from_the_pack(self):
        areas = self.proposal["proposal"]["areas"]
        self.assertEqual((self.proposal["problems"], list(areas)),
                         ([], ["access", "usability", "playback", "billing/support"]))
        self.assertTrue(all(4 <= len(symptoms) <= 9 for symptoms in areas.values()))
        calls = self.conn.execute("SELECT review_ids FROM calls WHERE role='group' AND step='symptoms'").fetchall()
        self.assertEqual(len(calls), 4)
        for (ids,) in calls:
            ids = json.loads(ids)
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(ids and len(ids) <= 60)

    def test_discovery_validates_what_the_model_proposes(self):
        class Stub:
            def ask(self, *, pack, review_ids, **_):
                own = pack["examples"][0]["review_id"]
                item = lambda sid: {"id": sid, "name": sid.upper(), "definition": "d",
                                    "example_review_ids": [own, "not-in-pack"]}
                return {"symptoms": [item(f"s{i}") for i in range(11)] + [item("Bad Id"), item("other"), item("s1")]}

        result = discover_symptoms(self.conn, self.dir, Stub(), self.tax, 30)
        areas = result["proposal"]["areas"]
        ids = [s["id"] for symptoms in areas.values() for s in symptoms]
        self.assertEqual(len(ids), len(set(ids)))                 # unique across areas (later areas renamed)
        self.assertTrue(all(len(symptoms) == 9 for symptoms in areas.values()))
        self.assertTrue(all(s["example_review_ids"] and "not-in-pack" not in s["example_review_ids"]
                            for symptoms in areas.values() for s in symptoms))
        problems = " | ".join(result["problems"])
        for expected in ("invalid, reserved or repeated symptom id 'Bad Id'", "symptom id 'other'",
                         "symptom id 's1'", "renamed us-s0", "dropped example ids", "kept the first 9"):
            self.assertIn(expected, problems)

    def test_scope_is_the_unique_complaint_texts_outside_free_tier_limits(self):
        expected = {r["text_sha"]: AREA_OF[json.loads(r["result"])["topic"]] for r in self.conn.execute(
            "SELECT text_sha, result FROM texts WHERE status='completed'")
            if (lambda x: x["intent"] in ("complaint", "cancellation") and x["topic"] in AREA_OF
                and not x["free_tier_limit"])(json.loads(r["result"]))}
        self.assertEqual(dict(self.conn.execute("SELECT text_sha, area FROM diagnoses").fetchall()), expected)
        self.assertGreater(self.added, 20)
        self.assertEqual(fill_backlog(self.conn), 0)  # idempotent

    def test_everything_completes_in_bounded_group_calls(self):
        self.assertIsNone(self.diagnoser().run())
        self.assertEqual(diagnosis_counts(self.conn), {"completed": self.added})
        rows = self.conn.execute("SELECT review_ids, label_config, effort FROM calls WHERE step='diagnose'").fetchall()
        sent = [rid for r in rows for rid in json.loads(r["review_ids"])]
        self.assertTrue(all(1 <= len(json.loads(r["review_ids"])) <= 50 for r in rows))
        self.assertEqual(len(sent), len(set(sent)))
        origins = {r[0] for r in self.conn.execute(
            "SELECT t.origin_review_id FROM diagnoses d JOIN texts t USING(text_sha)")}
        self.assertEqual(set(sent), origins)
        self.assertEqual({r["label_config"] for r in rows}, {diagnose_config(self.settings, self.catalog)})
        self.assertRegex(rows[0]["label_config"], r"^fake:fake-rules-v0\|adaptive/low\|prompt=[0-9a-f]{10}\|symptoms=y1$")
        self.assertEqual(self.one("SELECT COUNT(*) FROM calls WHERE step='diagnose' AND role!='group'"), 0)
        quotes = self.conn.execute("SELECT d.quote, t.text FROM diagnoses d JOIN texts t USING(text_sha)").fetchall()
        self.assertTrue(all(q and q in text for q, text in quotes))
        self.assert_records_untouched()

    def test_invalid_output_is_retried_once_then_failed_without_touching_the_record(self):
        marker = self.marker()
        self.assertIsNone(self.diagnoser(bad_marker=marker).run())
        rows = self.conn.execute("SELECT d.status, d.attempts, d.reason FROM diagnoses d JOIN texts t USING(text_sha) "
                                 "WHERE t.text GLOB ?", (f"*{marker}*",)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("failed", 2, "invalid_output:field_count")])
        self.assertEqual(diagnosis_counts(self.conn), {"completed": self.added - 1, "failed": 1})
        origin = self.conn.execute("SELECT origin_review_id FROM texts WHERE text GLOB ?", (f"*{marker}*",)).fetchone()[0]
        sizes = [len(ids) for ids in (json.loads(r[0]) for r in self.conn.execute(
            "SELECT review_ids FROM calls WHERE step='diagnose' ORDER BY started_at")) if origin in ids]
        self.assertEqual(len(sizes), 2)  # the first try, then one retry in the retry lane
        self.assertLessEqual(sizes[1], self.settings.retry_batch_size)
        self.assert_records_untouched()
        summary = finalize(self.conn, self.dir, self.tax)  # the review behind a failed diagnosis stays completed
        self.assertEqual(summary["quarantined"], 0)

    def test_one_retry_recovers_a_dropped_line(self):
        self.assertIsNone(self.diagnoser(bad_once_marker=self.marker()).run())
        self.assertEqual(diagnosis_counts(self.conn), {"completed": self.added})
        self.assertEqual(self.one("SELECT COUNT(*) FROM diagnoses WHERE attempts=1"), 1)

    def test_a_refusal_isolates_the_refused_text_without_counting_its_neighbours(self):
        marker = self.marker()
        self.diagnoser(refuse_marker=marker).run()
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT status, attempts, reason FROM diagnoses WHERE status!='completed'")],
            [("failed", 2, "refusal:general_harms")])
        self.assertEqual(self.one("SELECT COUNT(*) FROM diagnoses WHERE attempts>0"), 1)

    def test_interruption_then_resume_never_pays_twice(self):
        self.settings = replace(self.settings, batch_size=10)
        with self.assertRaises(KeyboardInterrupt):
            self.diagnoser(interrupt_after=2).run()
        self.assertEqual(self.one("SELECT COUNT(*) FROM diagnoses WHERE status='leased'"), 10)
        self.assertEqual(release_stale_diagnoses(self.conn), 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM diagnoses WHERE status='leased'"), 0)
        self.assertEqual(self.one("SELECT COUNT(*) FROM diagnoses WHERE status='completed'"), 20)
        self.assertIsNone(self.diagnoser().run())
        sent = [rid for r in self.conn.execute("SELECT review_ids FROM calls WHERE step='diagnose'")
                for rid in json.loads(r[0])]
        self.assertEqual(len(sent), len(set(sent)))
        self.assertEqual(diagnosis_counts(self.conn), {"completed": self.added})

    def test_a_submitted_batch_is_polled_after_restart_not_resent(self):
        self.settings = replace(self.settings, mode="batch", batch_size=10, batch_file_requests=3)
        first = self.diagnoser(batch_polls=2)
        self.assertEqual(first._submit_wave("diagnose"), 3)
        self.assertEqual(self.one("SELECT COUNT(*) FROM requests WHERE step='diagnose' AND status='submitted'"), 3)
        self.assertEqual(release_stale_diagnoses(self.conn), 0)   # a restart leaves submitted work alone
        self.assertEqual(release_stale(self.conn), 0)             # and so does the enricher's release
        self.assertEqual(self.one("SELECT COUNT(*) FROM diagnoses WHERE status='leased'"), 30)
        enricher = self.enricher()  # an enrich run in the same database never collects a diagnose batch
        enricher._collect("primary")
        self.assertEqual(self.one("SELECT COUNT(*) FROM provider_batches WHERE status='in_progress'"), 1)
        self.assertEqual(self.one("SELECT COUNT(*) FROM calls WHERE role='enrich' AND invocation=?",
                                  enricher.invocation), 0)

        self.assertIsNone(self.diagnoser(batch_polls=2).run())
        self.assertEqual(diagnosis_counts(self.conn), {"completed": self.added})
        sent = [rid for r in self.conn.execute("SELECT review_ids FROM calls WHERE step='diagnose'")
                for rid in json.loads(r[0])]
        self.assertEqual(len(sent), len(set(sent)))
        self.assertEqual(len(sent), self.added)

    def test_the_diagnose_cap_stops_admission(self):
        rates_file = self.dir / "rates.csv"
        rates_file.write_text("provider,model,tier,item,unit,price_usd\n" + "".join(
            f"fake,fake-rules-v0,synthetic,{item},per_1M_tokens,1000\n"
            for item in ("input", "cache_write", "cached_input", "output")))
        self.settings = replace(self.settings, batch_size=5, diagnose_cap_usd=8.0, diagnose_max_output_tokens=300)
        stopped = self.diagnoser(rates=Rates(rates_file)).run()
        self.assertTrue(stopped.startswith("budget: diagnose cap"), stopped)
        self.assertGreater(diagnosis_counts(self.conn).get("pending", 0), 0)
        self.assertGreater(self.one("SELECT COUNT(*) FROM calls WHERE step='diagnose'"), 0)
        self.assertLessEqual(spent(self.conn, "diagnose") + reserved(self.conn, "diagnose"), 8.0)

    def test_aggregates_add_up_to_the_diagnosed_complaints(self):
        self.diagnoser().run()
        self.add_copy()
        result = write_diagnosis(self.conn, self.dir)
        expected = {area: self.one(
            "SELECT COUNT(*) FROM records c JOIN reviews r USING(review_id) JOIN diagnoses d ON d.text_sha=r.text_sha "
            "WHERE d.status='completed' AND d.area=?", area) for area in result["areas"]}
        severity = {area: self.one(
            "SELECT SUM(c.severity) FROM records c JOIN reviews r USING(review_id) JOIN diagnoses d "
            "ON d.text_sha=r.text_sha WHERE d.status='completed' AND d.area=?", area) for area in result["areas"]}
        self.assertGreater(sum(expected.values()), self.added)  # copies count as complaints too
        import csv
        with (self.dir / "diagnosis_symptoms.csv").open() as f:
            table = list(csv.DictReader(f))
        with (self.dir / "diagnosis_customers.csv").open() as f:
            customers = {r["area"]: r for r in csv.DictReader(f)}
        with (self.dir / "diagnosis_context.csv").open() as f:
            context = list(csv.DictReader(f))
        for area, entries in result["areas"].items():
            self.assertEqual(sum(e["complaints"] for e in entries), expected[area], area)
            self.assertEqual(sum(e["severity_sum"] for e in entries), severity[area] or 0, area)
            self.assertEqual(sum(int(r["complaints"]) for r in table if r["area"] == area), expected[area])
            self.assertEqual(sum(int(r["complaints"]) for r in context if r["area"] == area), expected[area])
            self.assertEqual(int(customers[area]["complaints"]), expected[area])
            plans = sum(int(customers[area][k]) for k in ("paid", "free", "unstated"))
            self.assertEqual(plans, expected[area])
            self.assertEqual([e["severity_sum"] for e in entries], sorted((e["severity_sum"] for e in entries), reverse=True))
            self.assertEqual({e["symptom_id"] for e in entries},
                             {s["id"] for s in self.catalog["areas"][area]} | {"other"})
            for entry in entries:
                self.assertLessEqual(len(entry["quotes"]), 3)
                for quote in entry["quotes"]:
                    text = self.conn.execute("SELECT t.text FROM reviews r JOIN texts t USING(text_sha) "
                                             "WHERE r.review_id=?", (quote["review_id"],)).fetchone()[0]
                    self.assertIn(quote["quote"], text)
        self.assertEqual(result["coverage"], {"diagnosable_texts": self.added, "completed": self.added, "failed": 0,
                                              "other_share_pct": result["coverage"]["other_share_pct"]})

    def test_plan_gated_symptoms_are_flagged_and_left_out_of_concrete_defects(self):
        self.diagnoser().run()
        self.add_copy()
        result = write_diagnosis(self.conn, self.dir)  # the default catalog's plan-gated ids are not in this catalog
        self.assertFalse(any(e["plan_gated"] for entries in result["areas"].values() for e in entries))
        gated = {next(e["symptom_id"] for e in result["areas"][area] if e["symptom_id"] != "other" and e["complaints"])
                 for area in result["areas"] if any(e["symptom_id"] != "other" and e["complaints"]
                                                    for e in result["areas"][area])}
        self.assertTrue(gated)
        result = write_diagnosis(self.conn, self.dir, plan_gated=gated)
        import csv
        with (self.dir / "diagnosis_symptoms.csv").open() as f:
            flags = {r["symptom_id"]: r["plan_gated"] for r in csv.DictReader(f)}
        self.assertEqual({sid for sid, flag in flags.items() if flag == "1"}, gated)
        self.assertEqual(set(flags.values()), {"0", "1"})
        saved = json.loads((self.dir / "diagnosis.json").read_text())
        self.assertEqual(saved["concrete"], result["concrete"])
        for area, entries in result["areas"].items():
            c = result["concrete"][area]
            self.assertEqual({e["symptom_id"] for e in entries if e["plan_gated"]}, gated & {e["symptom_id"] for e in entries})
            fixable = [e for e in entries if e["symptom_id"] != "other" and not e["plan_gated"]]
            held = [e for e in entries if e["plan_gated"]]
            other = [e for e in entries if e["symptom_id"] == "other"]
            self.assertEqual(c["complaints"] + c["other_complaints"] + c["plan_gated_complaints"],
                             sum(e["complaints"] for e in entries), area)
            for key in ("complaints", "severity_sum", "last6_complaints", "last6_severity_sum", "paying_complaints"):
                self.assertEqual(c[key], sum(e[key] for e in fixable), (area, key))
            self.assertEqual(c["other_complaints"], sum(e["complaints"] for e in other))
            self.assertEqual(c["other_last6_complaints"], sum(e["last6_complaints"] for e in other))
            self.assertEqual(c["plan_gated_complaints"], sum(e["complaints"] for e in held))
            self.assertEqual(c["plan_gated_paying_complaints"], sum(e["paying_complaints"] for e in held))
            self.assertEqual(c["plan_gated_last6_complaints"], sum(e["last6_complaints"] for e in held))
            self.assertLessEqual(c["first6_complaints"] + c["last6_complaints"], c["complaints"])
            self.assertLessEqual(c["paying_severity_sum"], c["severity_sum"])
        with (self.dir / "diagnosis_symptoms.csv").open() as f:
            first6 = {(r["area"], r["symptom_id"]): int(r["first6_complaints"]) for r in csv.DictReader(f)}
        for area, entries in result["areas"].items():
            self.assertEqual(result["concrete"][area]["first6_complaints"],
                             sum(first6[(area, e["symptom_id"])] for e in entries
                                 if e["symptom_id"] != "other" and not e["plan_gated"]))

    def test_the_plan_gated_list_is_read_apart_from_the_catalog(self):
        self.assertEqual(plan_gated_ids(), {"chosen-song-not-played", "basic-features-locked-premium"})
        catalog = load_symptoms(Settings.load(None))
        self.assertEqual(set(catalog), {"version", "areas"})  # the diagnose label_config does not see the list

    def test_version_concentration_respects_the_review_floor(self):
        self.diagnoser().run()
        # One diagnosed complaint alone in a tiny version (a 100% share there) and a large version holding the rest.
        target = self.conn.execute("SELECT c.review_id, d.symptom_id, d.area FROM records c JOIN reviews r USING(review_id) "
                                   "JOIN diagnoses d ON d.text_sha=r.text_sha WHERE d.status='completed' "
                                   "ORDER BY c.row_num LIMIT 1").fetchone()
        self.conn.execute("UPDATE records SET app_version='9.9.9.big' WHERE status='completed'")
        self.conn.execute("UPDATE records SET app_version='9.9.9.tiny' WHERE review_id=?", (target["review_id"],))
        floor = 50
        result = write_diagnosis(self.conn, self.dir, version_floor=floor)
        entry = next(e for e in result["areas"][target["area"]] if e["symptom_id"] == target["symptom_id"])
        self.assertNotEqual(entry["top_version"], "9.9.9.tiny")
        big = self.one("SELECT COUNT(*) FROM records WHERE status='completed' AND app_version='9.9.9.big'")
        for entries in result["areas"].values():
            for e in entries:
                self.assertIn(e["top_version"], ("", "9.9.9.big"))
        if entry["top_version"]:
            members = entry["complaints"] - 1
            self.assertEqual(entry["top_version_share_pct"], round(100 * members / big, 3))
        total = self.one("SELECT COUNT(*) FROM records WHERE status='completed' AND trim(app_version)!=''")
        self.assertEqual(entry["overall_share_pct"], round(100 * entry["complaints"] / total, 3))
        result = write_diagnosis(self.conn, self.dir, version_floor=1)  # below the floor nothing is excluded
        entry = next(e for e in result["areas"][target["area"]] if e["symptom_id"] == target["symptom_id"])
        self.assertEqual((entry["top_version"], entry["top_version_share_pct"]), ("9.9.9.tiny", 100.0))

    def test_records_export_carries_diagnosis_keys_only_on_diagnosed_records(self):
        self.diagnoser(bad_marker=self.marker(offset=1)).run()  # one failed diagnosis: its records stay without keys
        copy = self.add_copy()
        write_checkpoint(self.conn, self.dir, 1, "finished")
        export_grading(self.conn, self.dir, self.dir / "grading")
        diagnosed = {r[0] for r in self.conn.execute(
            "SELECT r.review_id FROM reviews r JOIN diagnoses d ON d.text_sha=r.text_sha AND d.status='completed'")}
        keys = ("symptom", "context", "trigger", "long_time_user")
        with gzip.open(self.dir / "grading" / "records.jsonl.gz", "rt") as f:
            records = [json.loads(line) for line in f]
        tagged = {r["review_id"] for r in records if any(k in r for k in keys)}
        self.assertEqual(tagged, diagnosed)
        self.assertTrue(all(all(k in r for k in keys) for r in records if r["review_id"] in tagged))
        self.assertIn(copy, tagged)  # the copy inherits its origin's diagnosis by text
        self.assertEqual(self.one("SELECT COUNT(*) FROM diagnoses WHERE status='failed'"), 1)
        self.assertTrue(all(isinstance(r["long_time_user"], bool) for r in records if r["review_id"] in tagged))
        finalize(self.conn, self.dir, self.tax)  # finalize writes the same keys into the run's own records
        with gzip.open(self.dir / "records.jsonl.gz", "rt") as f:
            again = {r["review_id"] for r in map(json.loads, f) if "symptom" in r}
        self.assertEqual(again, diagnosed)

    def test_the_memo_reads_the_diagnosis_and_names_its_symptoms(self):
        self.diagnoser().run()
        run_diagnosis_check(self.conn, self.settings, self.dir, 8, self.catalog, sleep=lambda s: None)
        write_diagnosis(self.conn, self.dir)
        writer = Writer(self.conn, FakeProvider(spool=self.dir / "fake_batches"), self.settings, Rates(), 9,
                        sleep=lambda s: None)
        memo = run_memo(self.conn, self.dir, writer, self.settings)
        self.assertEqual(memo["final_problems"], [])
        saved = json.loads((self.dir / "memo.json").read_text())
        diagnosis = saved["facts"]["diagnosis"]
        self.assertEqual(diagnosis["verification"]["n"], min(300, self.added))
        symptoms = diagnosis["areas"][memo["priority_area"]]["symptoms"]
        self.assertGreaterEqual(len(symptoms), 1)
        self.assertTrue(all(s["symptom_id"] != "other" and len(s["quotes"]) <= 2 for s in symptoms))
        # The writer chose fixes; code rendered one bullet each into the stored and published memo.
        final = saved["final"]
        fixable = [s for s in symptoms if not s.get("plan_gated")]
        self.assertEqual([f["symptom_id"] for f in final["fixes"]], [s["symptom_id"] for s in fixable[:2]])
        self.assertNotIn("- **", final["model_markdown"])
        self.assertEqual(final["memo_markdown"].count("- **"), len(final["fixes"]))
        self.assertIn(final["memo_markdown"].strip(), (self.dir / "memo.md").read_text())
        self.assertTrue(all(q["review_id"] for q in saved["final_quote_sources"]))

    def test_the_blind_check_reports_agreement_and_lists_disagreements(self):
        self.diagnoser().run()
        report = run_diagnosis_check(self.conn, self.settings, self.dir, 9, self.catalog, sleep=lambda s: None)
        self.assertEqual(report["n"], min(300, self.added))
        self.assertEqual(report["agreement"], {"symptom": 1.0, "context": 1.0})
        self.assertEqual(self.one("SELECT COUNT(*) FROM calls WHERE role='verify' AND step='diagnose'"), report["n"])

        class Contrary:
            name, model, tier, diagnosis_version = "fake", "fake-rules-v0", "synthetic", "contrary"

            def check_diagnosis(self, text, symptoms):
                return {"ok": True, "record": {"symptom_id": "other", "context": "unknown"}, "confidence": {},
                        "request_id": f"contrary-{abs(hash(text))}", "input_tokens": 1, "output_tokens": 0,
                        "usage_available": False, "started_at": 0.0, "duration_ms": 0}

        self.conn.execute("DELETE FROM diagnosis_checks")
        report = run_diagnosis_check(self.conn, replace(self.settings, diagnose_verify_n=60), self.dir, 10,
                                     self.catalog, verifier=Contrary(), sleep=lambda s: None)
        self.assertEqual(report["n"], min(60, self.added))
        self.assertEqual(report["agreement"]["context"], 0.0)
        self.assertEqual(report["disagreements_total"], report["n"])
        self.assertEqual(len(report["disagreements"]), min(50, report["n"]))
        saved = json.loads((self.dir / "diagnosis_verify.json").read_text())
        self.assertEqual(saved["disagreements_total"], report["n"])


class MemoChecks(unittest.TestCase):
    pack = {"ranking": [{"rank": "1", "issue_id": "usability.general", "complaint_count": "10", "severity_sum": "25",
                         "mean_severity": "2.500000", "priority_score": "25"}],
            "top_issues": [{"issue_id": "usability.general", "quotes": [{"review_id": "11111111-2222-3333-4444-555555555555",
                                                                        "quote": "An ad after every single song, unbearable"}]}],
            "scope": {"source_rows": 1000, "first_review": "2022-05-17 00:01:07"}}

    headings = "\n## What to fix first\nx\n## Why\nx\n## Free versus premium\nx\n## Alternatives\nx\n## Limitations\nx"

    def memo(self, text, claims, quotes=()):
        return {"priority_area": "usability", "headline": "h", "memo_markdown": "## Recommendation\n" + text + self.headings,
                "claims": claims,
                "quotes": [{"issue_id": "usability.general", "quote": q} for q in quotes]}

    def test_a_supported_memo_passes(self):
        memo = self.memo("`usability.general` has 10 complaints [C1] out of 1,000 reviews since 2022-05; "
                         "mean severity 2.5 [C2]. Top 3 issues.",
                         [{"claim_id": "C1", "issue_id": "usability.general", "metric": "complaint_count", "value": "10"},
                          {"claim_id": "C2", "issue_id": "usability.general", "metric": "mean_severity",
                           "value": "2.500000"}], ["An ad after every single song"])
        self.assertEqual(validate_memo(memo, self.pack), [])

    def test_length_and_headings_are_checked(self):
        memo = self.memo("word " * 460, [])
        memo["memo_markdown"] = memo["memo_markdown"].replace("## Why", "**Why**")
        problems = " | ".join(validate_memo(memo, self.pack))
        self.assertIn("the limit is 300", problems)
        self.assertIn("not on their own line as '## Heading': Why", problems)
        memo = self.memo("word " * 250, [])
        memo["memo_markdown"] = memo["memo_markdown"].replace("## What to fix first\n", "")
        problems = " | ".join(validate_memo(memo, self.pack))
        self.assertNotIn("the limit", problems)
        self.assertIn("not on their own line as '## Heading': What to fix first", problems)

    def test_an_uncited_issue_number_is_caught(self):
        memo = self.memo("`usability.general` drew 10 complaints.", [])
        self.assertIn("usability.general: issue-level number without a claim citation",
                      " | ".join(validate_memo(memo, self.pack)))

    def test_every_kind_of_unsupported_content_is_caught(self):
        memo = self.memo("`playback.crash` has 37 complaints [C1] [C2], see 99999999-2222-3333-4444-555555555555, "
                         "\"I invented this quote entirely\".",
                         [{"claim_id": "C1", "issue_id": "usability.general", "metric": "complaint_count", "value": "11"}],
                         ["a quote that is nowhere in the pack"])
        problems = " | ".join(validate_memo(memo, self.pack))
        for expected in ("C1: usability.general complaint_count is 10, not 11", "C2: cited in the memo but missing",
                         "review ids appear in the memo text", "quote is not verbatim from the evidence pack",
                         "quoted text is not verbatim from the evidence pack",
                         "issue playback.crash is not in the ranking", "number 37 is not supported"):
            self.assertIn(expected, problems)

    # A pack with a diagnosis: the writer picks fixes, code renders the bullets from facts.
    diagnosis = {"catalog_version": "y-test", "coverage": {"other_share_pct": 12.5},
                 "verification": {"n": 64, "symptom_agreement": 0.9, "context_agreement": 0.8},
                 "areas": {"usability": {"diagnosed_complaints": 40, "other_share_pct": "12.5", "customers": None,
                                         "symptoms": [
                     {"symptom_id": "option-removed", "name": "Familiar option or feature is gone", "complaints": 6005,
                      "first6_complaints": 1890, "last6_complaints": 1845, "paying_complaints": 874,
                      "top_trigger": "after-update", "top_trigger_share_pct": 14.7,
                      "top_context": "bluetooth", "top_context_share_pct": 22.2,
                      "version": {"app_version": "8.7.90.544", "pct_of_this_versions_reviews": 6.127,
                                  "pct_of_all_reviews": 0.04},
                      "quotes": [{"review_id": "bbbbbbbb-2222-3333-4444-555555555555",
                                  "quote": "He said \"never\" to the 2021 sort option"},
                                 {"review_id": "aaaaaaaa-2222-3333-4444-555555555555",
                                  "quote": "They removed the sort by recently added option in 2023 on 9.9.9.9"}]},
                     {"symptom_id": "screens-hard", "name": "Screens hard to navigate or scan", "complaints": 12,
                      "first6_complaints": 3, "last6_complaints": 5, "paying_complaints": 1, "quotes": []},
                     {"symptom_id": "ads-often", "name": "Ads play too often", "complaints": 8, "quotes": []},
                     {"symptom_id": "chosen", "name": "Chosen song or position not played", "complaints": 30,
                      "plan_gated": True, "quotes": []}]},
                     "playback": {"diagnosed_complaints": 5, "other_share_pct": "0.0", "customers": None,
                                  "symptoms": [{"symptom_id": "stops", "name": "Music stops", "complaints": 5,
                                                "quotes": []}]}}}
    step = "Restore the removed sort option or add an equivalent."

    def diag_memo(self, fixes, fix_first="", text="Customers lose options they used.", area="usability"):
        """The writer's output (fixes plus its own markdown), rendered as run_memo renders it."""
        memo = self.memo(text, [])
        memo["priority_area"] = area
        memo["memo_markdown"] = memo["memo_markdown"].replace("## What to fix first\nx", "## What to fix first\n" + fix_first)
        memo["fixes"] = [{"symptom_id": sid, "next_step": step} for sid, step in fixes]
        pack = {**self.pack, "diagnosis": self.diagnosis}
        return render_memo(memo, pack), pack

    def test_code_renders_each_fix_from_facts(self):
        memo, pack = self.diag_memo([("option-removed", self.step), ("screens-hard", "Simplify the home screen")],
                                    fix_first="Two symptoms lead.\n- a bullet the writer should not have written")
        section = memo["memo_markdown"].split("## What to fix first\n")[1].split("\n\n## Why")[0]
        self.assertEqual(section.splitlines(), [
            "Two symptoms lead.",
            "- **Familiar option or feature is gone**: 6,005 complaints (1,890 in the first six full months, 1,845 in "
            "the last six), 874 from paying reviewers; 22.2% involve Bluetooth; 14.7% follow an update; 6.1% of reviews "
            "on version 8.7.90.544 report it, against 0.04% across all versions. \"They removed the sort by recently "
            "added option in 2023 on 9.9.9.9\" Next step: Restore the removed sort option or add an equivalent.",
            "- **Screens hard to navigate or scan**: 12 complaints (3 in the first six full months, 5 in the last six), "
            "1 from paying reviewers. Next step: Simplify the home screen."])
        self.assertIn("## Recommendation\nCustomers lose options they used.", memo["model_markdown"])
        self.assertNotIn("**", memo["model_markdown"])
        # Thousands separators, rounded shares, a quote's own year and version: all pass the number checks.
        self.assertEqual(validate_memo(memo, pack), [])
        self.assertEqual(memo["quotes"], [{"issue_id": "option-removed",
                                           "quote": "They removed the sort by recently added option in 2023 on 9.9.9.9"}])
        self.assertEqual(quote_sources(memo, pack)[0]["review_id"], "aaaaaaaa-2222-3333-4444-555555555555")
        self.assertEqual(validate_memo(self.memo("x", []), self.pack), [])  # no diagnosis, no fixes needed
        plain = self.memo("x", [])
        plain["fixes"] = [{"symptom_id": "option-removed", "next_step": self.step}]
        self.assertIn("fixes are listed but facts have no diagnosis; leave fixes empty", validate_memo(plain, self.pack))

    def test_invalid_duplicate_and_plan_gated_fixes_are_rejected(self):
        memo, pack = self.diag_memo([("option-removed", self.step), ("option-removed", self.step), ("chosen", self.step),
                                     ("stops", self.step), ("made-up", self.step)])
        problems = validate_memo(memo, pack)
        for expected in ("fixes must list 2 to 4 symptoms of usability; it lists 5", "fix option-removed: listed twice",
                         "fix chosen: Chosen song or position not played is plan-gated: free-tier friction, not a fix",
                         "fix stops: not a symptom of usability in facts.diagnosis (use a symptom_id listed there)",
                         "fix made-up: not a symptom of usability in facts.diagnosis (use a symptom_id listed there)",
                         "What to fix first names fewer than two symptoms of usability from facts.diagnosis"):
            self.assertIn(expected, problems)
        self.assertEqual(memo["memo_markdown"].count("- **"), 1)  # only valid fixes are rendered, once
        memo, pack = self.diag_memo([("option-removed", self.step)])
        self.assertIn("fixes must list 2 to 4 symptoms of usability; it lists 1", validate_memo(memo, pack))
        memo, pack = self.diag_memo([("stops", "Reproduce the stop and fix it.")], area="playback")
        self.assertEqual(validate_memo(memo, pack), [])  # an area with one fixable symptom needs only that one
        # The writer naming a plan-gated symptom in its own framing sentence is still caught.
        memo, pack = self.diag_memo([("option-removed", self.step), ("screens-hard", self.step)],
                                    fix_first="Chosen song or position not played is bigger.")
        self.assertIn("Chosen song or position not played is plan-gated: free-tier friction, not a fix",
                      validate_memo(memo, pack))
        memo, pack = self.diag_memo([("option-removed", self.step), ("screens-hard", self.step)],
                                    text="Chosen song or position not played is free-tier friction.")
        self.assertEqual(validate_memo(memo, pack), [])  # naming it outside What to fix first is fine

    def test_a_next_step_is_one_short_sentence_naming_only_stated_contexts(self):
        long_step = "Reproduce " + "the problem carefully " * 9
        memo, pack = self.diag_memo([("option-removed", "Reproduce with Bluetooth connected after an update."),
                                     ("screens-hard", long_step)])
        problems = " | ".join(validate_memo(memo, pack))
        self.assertNotIn("not a stated context", problems)  # Bluetooth is this symptom's stated context
        self.assertIn("fix screens-hard: next_step is 28 words; write one sentence of at most 25", problems)
        memo, pack = self.diag_memo([("option-removed", "Test it in the car. Then fix it."),
                                     ("screens-hard", "Test with the screen off on a watch")])
        problems = validate_memo(memo, pack)
        self.assertIn("fix option-removed: next_step must be one sentence", problems)
        self.assertIn("Familiar option or feature is gone: 'car' is not a stated context or trigger of this symptom "
                      "in facts.diagnosis", problems)
        self.assertIn("Screens hard to navigate or scan: 'background' is not a stated context or trigger of this "
                      "symptom in facts.diagnosis", problems)
        self.assertIn("Screens hard to navigate or scan: 'wearable' is not a stated context or trigger of this "
                      "symptom in facts.diagnosis", problems)

    def test_app_versions_must_come_from_the_facts(self):
        memo, pack = self.diag_memo([("option-removed", "Reproduce on 8.7.90.544 and restore it."),
                                     ("screens-hard", "Simplify the home screen.")])
        self.assertEqual(validate_memo(memo, pack), [])
        memo, pack = self.diag_memo([("option-removed", "Reproduce on 8.7.92.21 and restore it."),
                                     ("screens-hard", "Simplify the home screen.")])
        problems = validate_memo(memo, pack)
        self.assertIn("app version 8.7.92.21 does not appear in the facts", problems)
        self.assertFalse([p for p in problems if p.startswith("number ")], problems)  # no "8.7" number noise

    def test_diagnosis_quotes_are_accepted_as_verbatim_and_traced_to_their_review(self):
        memo, pack = self.diag_memo([("option-removed", self.step), ("screens-hard", self.step)],
                                    text='One reviewer: "removed the sort by recently added".')
        memo["quotes"].append({"issue_id": "option-removed", "quote": "removed the sort by recently added"})
        self.assertEqual(validate_memo(memo, pack), [])
        self.assertEqual(quote_sources(memo, pack)[-1]["review_id"], "aaaaaaaa-2222-3333-4444-555555555555")
        memo["memo_markdown"] = memo["memo_markdown"].replace("One reviewer:", "In 2019 one reviewer said \"it was 77 days of silence\";")
        problems = " | ".join(validate_memo(memo, pack))
        self.assertIn("quoted text is not verbatim", problems)
        self.assertIn("number 77 is not supported", problems)  # an invented quote's numbers are still checked
        self.assertIn("quote is not verbatim", " | ".join(validate_memo(memo, self.pack)))  # only via the diagnosis


class DiagnosisFacts(unittest.TestCase):
    """facts["diagnosis"] from a small saved diagnosis: bounded, "none" left out, versions only when they stand out."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pipeline-test-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        long_quote = "The music stops " + "whenever the phone screen turns off " * 8 + "and it never resumes."

        def entry(sid, n, sev, context, trigger, version, share, overall, quotes=()):
            return {"symptom_id": sid, "name": sid.title(), "definition": "d", "complaints": n, "severity_sum": sev,
                    "last6_complaints": n // 2, "last6_severity_sum": sev // 2, "paying_complaints": 1,
                    "top_context": context, "top_trigger": trigger, "top_version": version,
                    "top_version_share_pct": share, "overall_share_pct": overall, "quotes": list(quotes)}
        entries = [entry("stops", 10, 40, "bluetooth", "none", "8.7.90.544", 5.0, 2.0,
                         [{"review_id": "r1", "quote": "Stops all the time", "likes": 1},
                          {"review_id": "r2", "quote": long_quote, "likes": 9},
                          {"review_id": "r3", "quote": "Stops in the car", "likes": 5}]),
                   entry("crash", 8, 30, "none", "after-update", "8.7.90.544", 3.0, 2.0),
                   entry("buffer", 5, 20, "", "", "8.6.1.1", 10.0, 1.0),
                   entry("other", 7, 25, "none", "none", "", None, 0.9),
                   entry("silent", 0, 0, "", "", "", None, None)]
        entries += [entry(f"extra-{i}", 1, 1, "none", "none", "", None, 0.1) for i in range(5)]
        entries.append({**entry("gated-low", 1, 0, "none", "none", "", None, 0.1), "plan_gated": True})
        self.concrete = {"complaints": 27, "severity_sum": 95, "other_complaints": 7, "plan_gated_complaints": 1}
        (self.dir / "diagnosis.json").write_text(json.dumps({
            "catalog_version": "y-test", "coverage": {"diagnosable_texts": 30, "completed": 30, "failed": 0,
                                                      "other_share_pct": 23.33},
            "areas": {"playback": entries}, "concrete": {"playback": self.concrete}}))
        reviews = {"stops": 3000, "crash": 3000, "buffer": 1500}
        rows = ["area,symptom_id,mean_severity,first6_complaints,likes_sum,top_context_share_pct,"
                "top_trigger_share_pct,top_version_reviews"]
        rows += [f"playback,{e['symptom_id']},4.000000,2,15,40.0,60.0,{reviews.get(e['symptom_id'], '')}"
                 for e in entries]
        (self.dir / "diagnosis_symptoms.csv").write_text("\n".join(rows) + "\n")
        (self.dir / "diagnosis_customers.csv").write_text(
            "area,complaints,long_time_users,family_plan_mentions,student_plan_mentions,paid,free,unstated\n"
            "playback,35,4,1,0,3,2,30\n")
        (self.dir / "diagnosis_verify.json").write_text(json.dumps({"n": 30, "agreement": {"symptom": 0.9,
                                                                                         "context": 0.8}}))
        self.long_quote = long_quote

    def test_the_pack_is_built_from_the_saved_tables(self):
        result = diagnosis_facts(self.dir)
        self.assertEqual(result["verification"], {"n": 30, "symptom_agreement": 0.9, "context_agreement": 0.8})
        self.assertEqual(result["coverage"]["other_share_pct"], 23.33)
        area = result["areas"]["playback"]
        self.assertEqual(area["diagnosed_complaints"], 36)
        self.assertEqual(area["other_share_pct"], "19.4")
        self.assertEqual(area["customers"], {"complaints": 35, "long_time_users": 4, "family_plan_mentions": 1,
                                             "student_plan_mentions": 0, "paid": 3, "free": 2, "unstated": 30})
        ids = [s["symptom_id"] for s in area["symptoms"]]
        # six fixable symptoms at most, no "other", and every plan-gated symptom kept (flagged) for the memo
        self.assertEqual(ids, ["stops", "crash", "buffer", "extra-0", "extra-1", "extra-2", "gated-low"])
        self.assertEqual([s.get("plan_gated", False) for s in area["symptoms"]], [False] * 6 + [True])
        self.assertEqual(area["concrete"], self.concrete)
        stops, crash, buffer = area["symptoms"][:3]
        self.assertEqual(stops["version"], {"app_version": "8.7.90.544", "pct_of_this_versions_reviews": 5.0, "pct_of_all_reviews": 2.0})
        self.assertNotIn("version", crash)   # 3.0 is under twice the overall 2.0
        self.assertNotIn("version", buffer)  # 1,500 reviews is under the 2,000 floor
        self.assertEqual((stops["top_context"], stops["top_context_share_pct"]), ("bluetooth", 40.0))
        self.assertNotIn("top_trigger", stops)  # "none": nothing stated
        self.assertNotIn("top_context", crash)
        self.assertEqual((crash["top_trigger"], crash["top_trigger_share_pct"]), ("after-update", 60.0))
        self.assertNotIn("top_context", buffer)
        self.assertEqual((stops["mean_severity"], stops["first6_complaints"], stops["likes_sum"]), ("4.000000", 2, 15))
        self.assertEqual([q["review_id"] for q in stops["quotes"]], ["r2", "r3"])  # the two most liked
        cut = stops["quotes"][0]["quote"]
        self.assertLessEqual(len(cut), 200)
        self.assertTrue(self.long_quote.startswith(cut) and self.long_quote[len(cut)] == " ")  # a word boundary

    def test_no_diagnosis_means_no_pack_entry(self):
        (self.dir / "diagnosis.json").unlink()
        self.assertIsNone(diagnosis_facts(self.dir))


if __name__ == "__main__":
    unittest.main()
