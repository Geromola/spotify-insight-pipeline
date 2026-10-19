"""Tests for the golden labelling UI (evals/golden_ui.py). Standard library only.

    python3.12 tests/test_golden_ui.py

Every test writes to a temporary directory. The labels below are made-up labels for
made-up reviews; no test labels a golden review or touches evals/golden_50_labeled.csv.
"""
import csv
import hashlib
import http.client
import json
import math
import re
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

import golden_ui as g  # noqa: E402

REAL_INPUT = ROOT / "data" / "golden_50_to_label.csv"
REAL_OUT = ROOT / "evals" / "golden_50_labeled.csv"

# Synthetic reviews with the awkward cases: surrounding spaces, LF and CRLF inside the text,
# quotes, commas, emoji, a combining accent next to a precomposed one, and an empty app_version.
SYNTHETIC = [
    ["r-1", "  Ads every song,\nso annoying. Me voy a otra app \U0001F624  ", "1", "3", "8.7.40.777", "2022-06-21 20:13:46"],
    ["r-2", 'Says "premium" but\r\nshuffle only, café vs café', "2", "0", "", "2023-01-02 03:04:05"],
    ["r-3", "Muy buena, la mejor app \U0001F44D", "5", "12", "8.9.0", "2024-05-06 07:08:09"],
]
TEXT_1 = SYNTHETIC[0][1]


def valid(**overrides):
    payload = {
        "review_id": "r-1", "topic": "usability", "intent": "cancellation", "sentiment": -0.8, "severity": 2,
        "entities": ["ads"], "evidence_quote": "Ads every song", "needs_review": False, "ambiguous": False,
        "alt_topic": "", "alt_intent": "", "alt_severity": "", "notes": "",
    }
    payload.update(overrides)
    return payload


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        return header, [dict(zip(header, row)) for row in reader]


def file_state(path):
    return (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None


REAL_OUT_BEFORE = file_state(REAL_OUT)
REAL_INPUT_SHA = hashlib.sha256(REAL_INPUT.read_bytes()).hexdigest()


def tearDownModule():
    assert file_state(REAL_OUT) == REAL_OUT_BEFORE, "tests must not touch evals/golden_50_labeled.csv"
    assert hashlib.sha256(REAL_INPUT.read_bytes()).hexdigest() == REAL_INPUT_SHA, "tests must not change the input"


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="golden-ui-test-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.input = self.dir / "golden_input.csv"
        self.out = self.dir / "labels" / "golden_labeled.csv"
        with open(self.input, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(g.INPUT_FIELDS)
            for row in SYNTHETIC:
                writer.writerow(row + [""] * len(g.LABEL_FIELDS))

    def leftovers(self):
        return [p.name for p in self.out.parent.iterdir() if p.name != self.out.name] if self.out.parent.exists() else []


class Validation(unittest.TestCase):
    def check(self, field, **overrides):
        fields, errors = g.validate_label(valid(**overrides), TEXT_1)
        self.assertIsNone(fields, overrides)
        self.assertIn(field, errors, overrides)

    def test_valid_label_becomes_csv_strings(self):
        fields, errors = g.validate_label(valid(entities="ads,  shuffle , Ads", needs_review=True), TEXT_1)
        self.assertEqual(errors, {})
        self.assertEqual(fields["topic"], "usability")
        self.assertEqual(fields["intent"], "cancellation")
        self.assertEqual(fields["sentiment"], "-0.8")
        self.assertEqual(fields["severity"], "2")
        self.assertEqual(json.loads(fields["entities"]), ["ads", "shuffle"])
        self.assertEqual(fields["evidence_quote"], "Ads every song")
        self.assertEqual(fields["needs_review"], "true")
        self.assertEqual(fields["ambiguous"], "false")
        self.assertEqual([fields["alt_topic"], fields["alt_intent"], fields["alt_severity"]], ["", "", ""])
        self.assertNotIn("labeled_at", fields)

    def test_bad_topic(self):
        for topic in ["music", "Playback", "", None, ["playback"], "ac"]:
            self.check("topic", topic=topic)

    def test_bad_intent(self):
        for intent in ["angry", "Complaint", "", None, "x"]:
            self.check("intent", intent=intent)

    def test_bad_severity(self):
        for severity in [0, 6, -1, 2.5, "x", "7", True, None, "", [3]]:
            self.check("severity", severity=severity)
        fields, errors = g.validate_label(valid(severity="3"), TEXT_1)
        self.assertEqual((errors, fields["severity"]), ({}, "3"))

    def test_bad_sentiment(self):
        for sentiment in [1.5, -1.01, "abc", True, None, "", math.nan, math.inf]:
            self.check("sentiment", sentiment=sentiment)
        for sentiment, stored in [(-1, "-1.0"), (1, "1.0"), (0, "0.0"), (-0.0, "0.0"), (0.30000000000000004, "0.3"), ("0.5", "0.5")]:
            fields, errors = g.validate_label(valid(sentiment=sentiment), TEXT_1)
            self.assertEqual((errors, fields["sentiment"]), ({}, stored), sentiment)

    def test_quote_not_in_text(self):
        for quote in [
            "ads every song",                # case differs
            "Ads every song, so annoying",   # the source has a line break, not a space
            "Me voy a otra app \U0001F621",  # different emoji
            "", "   ", None, 42,
        ]:
            self.check("evidence_quote", evidence_quote=quote)

    def test_quote_is_kept_exactly(self):
        quote = "  Ads every song,\nso"
        fields, errors = g.validate_label(valid(evidence_quote=quote), TEXT_1)
        self.assertEqual(errors, {})
        self.assertEqual(fields["evidence_quote"], quote)
        fields, errors = g.validate_label(valid(evidence_quote=TEXT_1), TEXT_1)
        self.assertEqual((errors, fields["evidence_quote"]), ({}, TEXT_1))

    def test_entities_rules(self):
        self.check("entities", entities=["ads", 3])
        self.check("entities", entities={"ads": 1})
        self.check("entities", entities=["x" * (g.MAX_ENTITY_CHARS + 1)])
        self.check("entities", entities=[f"term{i}" for i in range(g.MAX_ENTITIES + 1)])
        for empty in [None, "", [], " , "]:
            fields, errors = g.validate_label(valid(entities=empty), TEXT_1)
            self.assertEqual((errors, fields["entities"]), ({}, "[]"))
        fields, _ = g.validate_label(valid(entities=["café mode", "letras"]), TEXT_1)
        self.assertEqual(fields["entities"], '["café mode", "letras"]')

    def test_booleans_and_extras(self):
        self.check("needs_review", needs_review="yes")
        self.check("ambiguous", ambiguous=1)
        self.check("alt_topic", alt_topic="music", ambiguous=True)
        self.check("alt_topic", alt_topic="usability", ambiguous=True)  # same as topic
        self.check("alt_intent", alt_intent="maybe", ambiguous=True)
        self.check("alt_severity", alt_severity=7, ambiguous=True)
        self.check("ambiguous", alt_topic="billing")  # an alternative needs the ambiguous flag
        self.check("notes", notes=["not", "text"])
        self.check("notes", notes="x" * (g.MAX_NOTES_CHARS + 1))
        self.check("_", sevrity=3)
        fields, errors = g.validate_label(
            valid(ambiguous=True, alt_topic="billing", alt_intent="complaint", alt_severity=3, notes="  unsure \n"), TEXT_1)
        self.assertEqual(errors, {})
        self.assertEqual([fields[k] for k in ("ambiguous", "alt_topic", "alt_intent", "alt_severity", "notes")],
                         ["true", "billing", "complaint", "3", "unsure"])

    def test_not_an_object(self):
        self.assertEqual(g.validate_label(["topic"], TEXT_1)[0], None)

    def test_plan_fields(self):
        for bad in ["yes", "True", 1, 0, ["true"], "maybe"]:
            self.check("free_tier_limit", free_tier_limit=bad)
        for bad in ["premium", "Paid", "none", 1, True, ["free"]]:
            self.check("plan", plan=bad)
        # Blank (or absent) is allowed and stays blank: it means "not labelled yet", never false.
        for blank in [None, ""]:
            fields, errors = g.validate_label(valid(free_tier_limit=blank, plan=blank), TEXT_1)
            self.assertEqual((errors, fields["free_tier_limit"], fields["plan"]), ({}, "", ""))
        fields, errors = g.validate_label(valid(), TEXT_1)
        self.assertEqual((errors, fields["free_tier_limit"], fields["plan"]), ({}, "", ""))
        for value, stored in [(True, "true"), (False, "false"), ("true", "true"), ("false", "false")]:
            for plan in g.PLAN_NAMES:
                fields, errors = g.validate_label(valid(free_tier_limit=value, plan=plan), TEXT_1)
                self.assertEqual((errors, fields["free_tier_limit"], fields["plan"]), ({}, stored, plan))
        # stored_to_payload keeps blank blank and turns the strings back into booleans.
        self.assertEqual(g.stored_to_payload({"free_tier_limit": ""})["free_tier_limit"], "")
        self.assertIs(g.stored_to_payload({"free_tier_limit": "false"})["free_tier_limit"], False)


class SaveAndReload(TempDir):
    def test_atomic_save_and_reload_keep_source_fields_identical(self):
        store = g.LabelStore(self.input, self.out)
        self.assertFalse(self.out.exists(), "starting the store must not create the output")
        ok, result = store.save(valid())
        self.assertTrue(ok, result)
        self.assertEqual(result["done"], 1)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o644, "the temp file's 0600 mode must not leak")

        header, rows = read_rows(self.out)
        self.assertEqual(header, g.OUTPUT_FIELDS)
        self.assertEqual(len(rows), len(SYNTHETIC))
        for source, row in zip(SYNTHETIC, rows):
            self.assertEqual([row[f] for f in g.SOURCE_FIELDS], source)
        first = rows[0]
        self.assertEqual([first[f] for f in g.LABEL_FIELDS],
                         ["usability", "cancellation", "-0.8", "2", '["ads"]', "Ads every song", "false"])
        self.assertEqual(first["ambiguous"], "false")
        self.assertIsNotNone(datetime.fromisoformat(first["labeled_at"]).tzinfo)
        for row in rows[1:]:
            self.assertEqual([row[f] for f in g.LABEL_FIELDS + g.EXTRA_FIELDS], [""] * 13)

        # A new process resumes from the file.
        again = g.LabelStore(self.input, self.out)
        self.assertEqual(again.warnings, [])
        self.assertEqual(again.done_count(), 1)
        snap = again.snapshot()
        self.assertEqual([r["status"] for r in snap["reviews"]], ["done", "todo", "todo"])
        self.assertEqual(snap["reviews"][0]["label"]["entities"], ["ads"])
        self.assertIsNone(snap["reviews"][1]["label"])

        ok, _ = again.save(valid(review_id="r-2", topic="billing", intent="complaint", severity=3, sentiment=-0.4,
                                 entities="premium, shuffle", evidence_quote='"premium" but\r\nshuffle only'))
        self.assertTrue(ok)
        _, rows = read_rows(self.out)
        for source, row in zip(SYNTHETIC, rows):
            self.assertEqual([row[f] for f in g.SOURCE_FIELDS], source)
        self.assertEqual(rows[0]["labeled_at"], first["labeled_at"], "saving one review must not touch another")
        self.assertEqual(rows[1]["evidence_quote"], '"premium" but\r\nshuffle only')
        self.assertEqual(self.leftovers(), [])

    def test_invalid_save_writes_nothing(self):
        store = g.LabelStore(self.input, self.out)
        ok, result = store.save(valid(topic="music", severity=9, evidence_quote="not in the text"))
        self.assertFalse(ok)
        self.assertEqual(set(result["errors"]), {"topic", "severity", "evidence_quote"})
        ok, result = store.save(valid(review_id="nope"))
        self.assertFalse(ok)
        self.assertFalse(self.out.exists())

    def test_failed_replace_keeps_previous_file(self):
        store = g.LabelStore(self.input, self.out)
        self.assertTrue(store.save(valid())[0])
        before = self.out.read_bytes()
        with mock.patch.object(g.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.save(valid(review_id="r-3", topic="other", intent="praise", severity=1, sentiment=0.9,
                                 evidence_quote="la mejor app"))
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual(self.leftovers(), [])
        self.assertNotIn("r-3", store.labels)

    def test_clear_writes_blank_row(self):
        store = g.LabelStore(self.input, self.out)
        store.save(valid())
        ok, result = store.clear("r-1")
        self.assertTrue(ok)
        self.assertEqual(result["done"], 0)
        _, rows = read_rows(self.out)
        self.assertEqual([rows[0][f] for f in g.LABEL_FIELDS + g.EXTRA_FIELDS], [""] * 13)
        self.assertEqual([rows[0][f] for f in g.SOURCE_FIELDS], SYNTHETIC[0])

    def test_hand_edited_bad_label_is_kept_and_flagged(self):
        store = g.LabelStore(self.input, self.out)
        store.save(valid())
        header, rows = read_rows(self.out)
        rows[0]["topic"] = "music"
        with open(self.out, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        again = g.LabelStore(self.input, self.out)
        review = again.snapshot()["reviews"][0]
        self.assertEqual(review["status"], "invalid")
        self.assertIn("topic", review["errors"])
        self.assertEqual(again.done_count(), 0)
        again.save(valid(review_id="r-3", topic="other", intent="praise", severity=1, sentiment=1, evidence_quote="Muy buena"))
        _, rows = read_rows(self.out)
        self.assertEqual(rows[0]["topic"], "music", "a bad saved label is preserved for the human to fix")

    def test_refuses_foreign_or_mismatched_output(self):
        self.out.parent.mkdir(parents=True)
        self.out.write_text("id,prediction\n1,playback\n", encoding="utf-8")
        with self.assertRaises(g.SetupError):
            g.LabelStore(self.input, self.out)
        self.out.write_text(",".join(g.OUTPUT_FIELDS) + "\n" + "zzz" + "," * (len(g.OUTPUT_FIELDS) - 1) + "\n", encoding="utf-8")
        with self.assertRaises(g.SetupError):
            g.LabelStore(self.input, self.out)
        with self.assertRaises(g.SetupError):
            g.LabelStore(self.input, self.input)

    def test_real_input_round_trips_unchanged(self):
        source = g.read_source(REAL_INPUT)
        self.assertEqual(len(source), 50)
        out = self.dir / "real_round_trip.csv"
        g.write_output(out, source, {})
        header, rows = read_rows(out)
        self.assertEqual(header, g.OUTPUT_FIELDS)
        _, original = g.read_csv(REAL_INPUT)
        self.assertEqual([[r[f] for f in g.SOURCE_FIELDS] for r in rows], [row[:6] for row in original])
        self.assertTrue(all(r[f] == "" for r in rows for f in g.LABEL_FIELDS + g.EXTRA_FIELDS))


class PlanFields(TempDir):
    ORIGINAL_COLUMNS = g.INPUT_FIELDS + g.EXTRA_FIELDS  # the 19 columns of the file before the plan fields existed

    def test_saving_plan_fields_on_real_labels_keeps_every_other_column(self):
        if not REAL_OUT.exists():
            self.skipTest("no evals/golden_50_labeled.csv")
        out = self.dir / "copy_of_real_labels.csv"
        # The real file may already carry the two plan fields; test on its 19 original columns.
        real_header, real_rows = read_rows(REAL_OUT)
        self.assertEqual(real_header[:len(self.ORIGINAL_COLUMNS)], self.ORIGINAL_COLUMNS)
        with open(out, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.ORIGINAL_COLUMNS, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(real_rows)
        before_header, before = read_rows(out)
        self.assertEqual(before_header, self.ORIGINAL_COLUMNS)
        noted = sum(1 for r in before if r["notes"])

        store = g.LabelStore(REAL_INPUT, out)
        self.assertEqual((store.warnings, store.done_count(), store.plan_done_count()), ([], 50, 0))
        snap = store.snapshot()
        self.assertEqual(snap["plan_done"], 0)
        self.assertTrue(all(r["label"]["free_tier_limit"] == "" and r["label"]["plan"] == "" for r in snap["reviews"]))

        # Save one review the way the page does: the loaded label sent back plus the two new fields.
        target = snap["reviews"][7]
        ok, result = store.save({**target["label"], "review_id": target["review_id"], "free_tier_limit": True, "plan": "paid"})
        self.assertTrue(ok, result)
        self.assertEqual((result["plan_done"], result["review_plan_done"]), (1, True))
        header, after = read_rows(out)
        self.assertEqual(header, self.ORIGINAL_COLUMNS + ["free_tier_limit", "plan"])
        self.assertEqual(len(after), len(before))
        for old, new in zip(before, after):
            changed = [c for c in self.ORIGINAL_COLUMNS if old[c] != new[c]]
            if old["review_id"] == target["review_id"]:
                self.assertEqual(changed, ["labeled_at"])
                self.assertEqual((new["free_tier_limit"], new["plan"]), ("true", "paid"))
            else:
                self.assertEqual(changed, [], old["review_id"])
                self.assertEqual((new["free_tier_limit"], new["plan"]), ("", ""))
        self.assertEqual(sum(1 for r in after if r["notes"]), noted)

        # Every review, saved in turn with the plan fields, still keeps its 19 original columns (labeled_at aside).
        for i, review in enumerate(snap["reviews"]):
            ok, result = store.save({**review["label"], "review_id": review["review_id"],
                                     "free_tier_limit": i % 2 == 0, "plan": g.PLAN_NAMES[i % 3]})
            self.assertTrue(ok, (review["review_id"], result))
        _, after = read_rows(out)
        for old, new in zip(before, after):
            self.assertEqual([c for c in self.ORIGINAL_COLUMNS if old[c] != new[c]], ["labeled_at"], old["review_id"])
        again = g.LabelStore(REAL_INPUT, out)
        self.assertEqual((again.warnings, again.done_count(), again.plan_done_count()), ([], 50, 50))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["copy_of_real_labels.csv", "golden_input.csv"])

    def test_old_format_file_loads_and_upgrades_on_save(self):
        self.out.parent.mkdir(parents=True)
        old_fields = self.ORIGINAL_COLUMNS
        labelled = valid(ambiguous=True, alt_topic="billing", notes="kept as is")
        fields, errors = g.validate_label(labelled, TEXT_1)
        self.assertEqual(errors, {})
        with open(self.out, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(old_fields)
            for i, row in enumerate(SYNTHETIC):
                extra = [fields[c] for c in old_fields[6:-1]] + ["2026-10-01T10:00:00+02:00"] if i == 0 else [""] * 13
                writer.writerow(row + extra)
        before = read_rows(self.out)[1]

        store = g.LabelStore(self.input, self.out)
        self.assertEqual((store.warnings, store.done_count(), store.plan_done_count()), ([], 1, 0))
        review = store.snapshot()["reviews"][0]
        self.assertEqual((review["status"], review["plan_done"]), ("done", False))
        self.assertEqual((review["label"]["free_tier_limit"], review["label"]["plan"]), ("", ""))

        ok, result = store.save(valid(review_id="r-3", topic="other", intent="praise", severity=1, sentiment=1,
                                      evidence_quote="Muy buena", free_tier_limit=False, plan="unstated"))
        self.assertTrue(ok, result)
        header, rows = read_rows(self.out)
        self.assertEqual(header, g.OUTPUT_FIELDS)
        self.assertEqual(header[:19], old_fields)
        self.assertEqual(header[19:], ["free_tier_limit", "plan"])
        self.assertEqual({c: rows[0][c] for c in old_fields}, before[0], "an untouched row must not change")
        self.assertEqual((rows[0]["free_tier_limit"], rows[0]["plan"]), ("", ""))
        self.assertEqual((rows[2]["free_tier_limit"], rows[2]["plan"]), ("false", "unstated"))
        self.assertEqual(g.LabelStore(self.input, self.out).plan_done_count(), 1)

    def test_bad_plan_value_in_file_is_flagged_not_counted(self):
        store = g.LabelStore(self.input, self.out)
        store.save(valid(free_tier_limit=True, plan="free"))
        header, rows = read_rows(self.out)
        rows[0]["plan"] = "premium"
        with open(self.out, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        again = g.LabelStore(self.input, self.out)
        review = again.snapshot()["reviews"][0]
        self.assertEqual((review["status"], review["plan_done"]), ("invalid", False))
        self.assertIn("plan", review["errors"])

    def test_unknown_columns_still_refused(self):
        self.out.parent.mkdir(parents=True)
        self.out.write_text(",".join(g.OUTPUT_FIELDS + ["premium_guess"]) + "\n", encoding="utf-8")
        with self.assertRaises(g.SetupError):
            g.LabelStore(self.input, self.out)


class Server(TempDir):
    def setUp(self):
        super().setUp()
        self.store = g.LabelStore(self.input, self.out)
        self.server = g.make_server(self.store, 0)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            data = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body
            conn.request(method, path, body=data, headers=headers or {})
            res = conn.getresponse()
            return res.status, dict(res.getheaders()), res.read()
        finally:
            conn.close()

    def test_api(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Golden Labeller", body)
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])

        status, _, body = self.request("GET", "/api/reviews")
        data = json.loads(body)
        self.assertEqual((status, data["total"], data["done"]), (200, 3, 0))
        self.assertEqual(data["reviews"][1]["review_text"], SYNTHETIC[1][1])
        self.assertTrue(all(r["label"] is None for r in data["reviews"]))
        self.assertEqual(data["plan_done"], 0)
        self.assertEqual([p["value"] for p in data["schema"]["plans"]], ["paid", "free", "unstated"])
        self.assertIn("skip limit", data["schema"]["free_tier_limit"])

        json_headers = {"Content-Type": "application/json"}
        status, _, body = self.request("POST", "/api/label", valid(topic="music"), json_headers)
        self.assertEqual(status, 400)
        self.assertIn("topic", json.loads(body)["errors"])
        self.assertFalse(self.out.exists())

        status, _, body = self.request("POST", "/api/label", valid(), json_headers)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["done"], 1)
        self.assertEqual(read_rows(self.out)[1][0]["topic"], "usability")

        status, _, body = self.request("POST", "/api/label", valid(plan="premium"), json_headers)
        self.assertEqual(status, 400)
        self.assertIn("plan", json.loads(body)["errors"])
        status, _, body = self.request("POST", "/api/label", valid(free_tier_limit=True, plan="free"), json_headers)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["plan_done"], 1)
        row = read_rows(self.out)[1][0]
        self.assertEqual((row["free_tier_limit"], row["plan"]), ("true", "free"))

        status, _, _ = self.request("POST", "/api/label", valid(), {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, _ = self.request("POST", "/api/label", valid(), {**json_headers, "Origin": "https://example.com"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/reviews", headers={"Host": "attacker.example:80"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/label", b"{not json", json_headers)
        self.assertEqual(status, 400)

    def test_page_loads_nothing_external(self):
        html = g.HTML_PATH.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"https?://|//cdn|@import|<link[^>]+stylesheet", html), [])
        self.assertEqual(sorted(set(re.findall(r"fetch\('([^']+)'", html)) | set(re.findall(r"post\('([^']+)'", html))),
                         ["/api/clear", "/api/label", "/api/reviews"])
        self.assertNotRegex(html.lower(), r"predict|model_|openai|anthropic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
