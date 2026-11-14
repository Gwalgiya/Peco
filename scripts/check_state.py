#!/usr/bin/env python3
"""Isolated subprocess checks; never uses an actual owner directory or sends IO."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).with_name("peco_state.py")
ZONE = ZoneInfo("Asia/Shanghai")


class StateChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="peco-state-check-")
        self.root = Path(self.temp.name) / "owner"
        self.run_cli("init", "--owner", "trusted-test-owner", "--timezone", "Asia/Shanghai")

    def tearDown(self):
        self.temp.cleanup()

    def command(self, *args):
        return [sys.executable, str(SCRIPT), "--root", str(self.root), "--owner-key", "trusted-test-owner", *args]

    def run_cli(self, *args, patch=None, error=None):
        result = subprocess.run(self.command(*args), input=json.dumps(patch) if patch is not None else None,
                                capture_output=True, text=True)
        self.assertEqual(result.stderr, "")
        data = json.loads(result.stdout)
        if error:
            self.assertNotEqual(result.returncode, 0, data)
            self.assertEqual(data["error"]["code"], error, data)
        else:
            self.assertEqual(result.returncode, 0, data)
            self.assertTrue(data["ok"])
        return data

    def apply(self, revision, event, patch, error=None):
        return self.run_cli("apply", "--expected-revision", str(revision), "--event-id", event,
                            "--file", "-", patch=patch, error=error)

    def state(self):
        return self.run_cli("view", "--date", "2026-10-09", "--mode", "full")

    def task(self, task_id="a", **fields):
        return dict(id=task_id, title="Test " + task_id, first_action="Read one sentence",
                    first_action_stop="One sentence read", **fields)

    def route(self, **reminders):
        return {"reminders": dict(enabled=True,
                                  route={"channel": "test", "recipient": "test-owner", "confirmed": True},
                                  **reminders)}

    def test_no_silent_init_and_json_argument_errors(self):
        absent = Path(self.temp.name) / "absent"
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(absent),
                                 "--owner-key", "trusted-test-owner",
                                 "view", "--date", "2026-10-09", "--mode", "full"], capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "not_initialized")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(absent.exists())
        self.run_cli("view", "--mode", "full", error="argument_error")
        self.run_cli("init", "--owner", "other", error="owner_mismatch")
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root),
                                 "--owner-key", "other", "view", "--date", "2026-10-09", "--mode", "full"], capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "owner_mismatch")
        self.assertNotIn("trusted-test-owner", result.stdout)

    def test_cross_process_cas_events_and_transaction_rollback(self):
        patch = {"tasks_upsert": [self.task()]}
        self.assertEqual(self.apply(0, "m1", patch)["revision"], 1)
        self.assertEqual(self.state()["state"]["tasks"][0]["title"], "Test a")
        self.apply(0, "stale", {"tasks_upsert": [self.task("b")]}, error="revision_conflict")
        self.apply(1, "m2", {"tasks_upsert": [{"id": "a", "breakpoint": "Sentence one"}]})
        retry = self.apply(0, "m1", patch)
        self.assertTrue(retry["idempotent"])
        self.assertEqual((retry["revision"], retry["event_revision"]), (2, 1))
        self.apply(2, "m1", {}, error="event_id_conflict")
        invalid = {"tasks_upsert": [self.task("b"), {"id": "a", "progress": "completed"}]}
        self.apply(2, "bad", invalid, error="validation_error")
        result = self.state()
        self.assertEqual(result["revision"], 2)
        self.assertEqual([x["id"] for x in result["state"]["tasks"]], ["a"])
        self.apply(2, "bad", {"tasks_upsert": [self.task("b")]})

    def test_parent_completion_and_cycles(self):
        records = [self.task("parent"), self.task("required", parent_id="parent"),
                   self.task("optional", parent_id="parent", required_for_parent=False)]
        self.apply(0, "parent", {"tasks_upsert": records})
        done = {"progress": "completed", "completion_criterion": "One checked paragraph", "result": "Checked paragraph"}
        self.apply(1, "early", {"tasks_upsert": [dict(id="parent", **done)]}, error="validation_error")
        self.apply(1, "complete", {"tasks_upsert": [dict(id="required", **done), dict(id="parent", **done)]})
        tasks = {x["id"]: x for x in self.state()["state"]["tasks"]}
        self.assertEqual(tasks["optional"]["progress"], "unstarted")
        cycle = [self.task("x", dependencies=[{"task_id": "y", "ready": True}]),
                 self.task("y", dependencies=[{"task_id": "x", "ready": True}])]
        self.apply(2, "cycle", {"tasks_upsert": cycle}, error="validation_error")
        self.apply(2, "unknown", {"tasks_upsert": [self.task("x", parent_id="missing")]}, error="validation_error")
        self.apply(2, "ancestry", {"tasks_upsert": [self.task("x", parent_id="y", required_for_parent=False),
                                                   self.task("y", parent_id="x", required_for_parent=False)]}, error="validation_error")
        self.apply(2, "deadlock", {"tasks_upsert": [self.task("x"), self.task("y", parent_id="x", dependencies=[{"task_id": "x", "ready": True}])]}, error="validation_error")

    def test_long_valid_chain_and_long_cycle_rollback(self):
        tasks = [{"id": "t%04d" % n, "title": "Task %d" % n,
                  "dependencies": [{"task_id": "t%04d" % (n + 1), "ready": False}] if n < 1099 else []}
                 for n in range(1100)]
        self.assertEqual(self.apply(0, "long-valid", {"tasks_upsert": tasks})["revision"], 1)
        self.assertEqual(len(self.state()["state"]["tasks"]), 1100)
        rejected = self.apply(1, "long-cycle", {"tasks_upsert": [{"id": "t1099", "dependencies": [{"task_id": "t0000", "ready": False}]}],
                                               "parking_upsert": [{"id": "rollback", "text": "Must not be committed"}]}, error="validation_error")
        self.assertIn("Dependency cycle", rejected["error"]["message"])
        after = self.state()
        self.assertEqual(after["revision"], 1)
        self.assertEqual(after["state"]["tasks"][-1]["dependencies"], [])
        self.assertEqual(after["state"]["parking"], [])
        # Reusing the rejected event id works: the failed transaction also did
        # not persist an event or partial parking update.
        self.assertEqual(self.apply(1, "long-cycle", {"tasks_upsert": [{"id": "t1099", "dependencies": []}]})["revision"], 2)

    def test_parking_validation_defaults_partial_updates_and_rollback(self):
        self.apply(0, "parking-valid", {"tasks_upsert": [self.task()],
                                        "parking_upsert": [{"id": "p", "text": "Read this later", "task_id": "a", "date": "2026-10-09"}]})
        self.assertEqual(self.state()["state"]["parking"][0]["status"], "open")
        self.apply(1, "parking-partial", {"parking_upsert": [{"id": "p", "status": "processed"}]})
        self.assertEqual(self.state()["state"]["parking"][0]["text"], "Read this later")
        invalid = [("missing-text", {"id": "bad"}, "text"),
                   ("blank-text", {"id": "bad", "text": " \t\n"}, "text"),
                   ("wrong-text", {"id": "bad", "text": ["Not a string"]}, "text"),
                   ("bad-status", {"id": "bad", "text": "Idea", "status": "done"}, "status"),
                   ("bad-reference", {"id": "bad", "text": "Idea", "task_id": "missing"}, "task_id"),
                   ("bad-reference-type", {"id": "bad", "text": "Idea", "task_id": {}}, "task_id"),
                   ("bad-date", {"id": "bad", "text": "Idea", "date": "2026-02-30"}, "date"),
                   ("bad-id", {"id": "", "text": "Idea"}, "id")]
        for event, record, field in invalid:
            with self.subTest(event=event):
                result = self.apply(2, event, {"tasks_upsert": [{"id": "a", "title": "Must roll back"}],
                                               "parking_upsert": [record]}, error="validation_error")
                self.assertEqual(result["error"]["collection"], "parking")
                self.assertEqual(result["error"]["record_id"], record["id"])
                self.assertEqual(result["error"]["field"], field)
                after = self.state()
                self.assertEqual(after["revision"], 2)
                self.assertEqual(after["state"]["tasks"][0]["title"], "Test a")
                self.assertEqual(len(after["state"]["parking"]), 1)
        self.apply(2, "dismiss", {"parking_upsert": [{"id": "p", "status": "dismissed"}]})
        self.assertEqual(self.state()["state"]["parking"][0]["status"], "dismissed")

    def test_legacy_parking_without_status_is_open(self):
        self.apply(0, "legacy-fixture", {"parking_upsert": [{"id": "p", "text": "Original text"}]})
        with sqlite3.connect(str(self.root / "state.sqlite3")) as connection:
            state = json.loads(connection.execute("SELECT data FROM state WHERE singleton=1").fetchone()[0])
            del state["parking"][0]["status"]
            connection.execute("UPDATE state SET data=? WHERE singleton=1", (json.dumps(state),))
        self.assertEqual(self.state()["state"]["parking"][0]["status"], "open")
        repeated = self.run_cli("init", "--owner", "trusted-test-owner", "--timezone", "Asia/Shanghai")
        self.assertEqual(repeated["state"]["parking"][0]["status"], "open")
        self.assertEqual(repeated["revision"], 1)
        with sqlite3.connect(str(self.root / "state.sqlite3")) as connection:
            raw = json.loads(connection.execute("SELECT data FROM state WHERE singleton=1").fetchone()[0])
            self.assertNotIn("status", raw["parking"][0])
        self.apply(1, "legacy-partial", {"parking_upsert": [{"id": "p", "date": "2026-10-09"}]})
        self.assertEqual(self.state()["state"]["parking"][0]["text"], "Original text")

    def test_scheduling_is_independent_and_waiting_not_recommended(self):
        plans = [{"id": "p1", "task_id": "a", "start": "2026-10-09T09:00:00+08:00", "end": "2026-10-09T10:00:00+08:00"},
                 {"id": "p2", "task_id": "waiting", "start": "2026-10-09T10:00:00+08:00", "end": "2026-10-09T11:00:00+08:00"}]
        self.apply(0, "schedule", {"tasks_upsert": [self.task(deadline="2026-10-11"), self.task("waiting", progress="waiting")],
                                    "plans_upsert": plans, "settings_patch": self.route()})
        tasks = {x["id"]: x for x in self.state()["state"]["tasks"]}
        self.assertEqual(tasks["a"]["progress"], "unstarted")
        self.assertEqual(tasks["waiting"]["progress"], "waiting")
        self.assertEqual(tasks["a"]["deadline"], "2026-10-11")
        check = self.run_cli("reminder", "--slot", "morning", "--now", "2026-10-09T08:05:00+08:00")
        self.assertEqual([x["task_id"] for x in check["ready_candidates"]], ["a"])
        self.assertEqual(check["candidates"][1]["reasons"], ["progress_waiting"])
        self.apply(1, "mark-segment-done", {"plans_upsert": [{"id": "p1", "status": "done", "outcome": "One sentence"}]})
        self.assertEqual(self.state()["state"]["tasks"][0]["progress"], "unstarted")
        self.assertFalse(self.run_cli("reminder", "--slot", "morning", "--now", "2026-10-09T08:05:00+08:00")["eligible"])

    def test_intervals_fixed_events_dependencies_and_naive_times(self):
        self.apply(0, "tasks", {"tasks_upsert": [self.task(), self.task("b", dependencies=[{"waiting_for": "Client data", "ready": False, "resume_signal": "Data received"}])]})
        self.apply(1, "naive", {"plans_upsert": [{"id": "p", "task_id": "a", "start": "2026-10-09T09:00:00", "end": "2026-10-09T10:00:00+08:00"}]}, error="validation_error")
        fixed = {"id": "f", "title": "Meeting", "start": "2026-10-09T09:00:00+08:00", "end": "2026-10-09T10:00:00+08:00"}
        self.apply(1, "fixed", {"settings_patch": {"fixed_events": [fixed]}})
        self.apply(2, "conflict", {"plans_upsert": [{"id": "p", "task_id": "a", "start": "2026-10-09T01:30:00Z", "end": "2026-10-09T02:30:00Z"}]}, error="validation_error")
        plans = [{"id": "p", "task_id": "a", "start": "2026-10-09T11:00:00+08:00", "end": "2026-10-09T12:00:00+08:00"},
                 {"id": "q", "task_id": "b", "start": "2026-10-09T11:30:00+08:00", "end": "2026-10-09T12:30:00+08:00"}]
        self.apply(2, "overlap", {"plans_upsert": plans}, error="validation_error")
        self.apply(2, "blocked", {"plans_upsert": [plans[1]], "settings_patch": self.route()})
        self.assertEqual(self.run_cli("reminder", "--slot", "morning", "--now", "2026-10-09T08:01:00+08:00")["reasons"], ["no_ready_current_or_future_plan"])

    def test_historical_segments_survive_new_availability(self):
        self.apply(0, "history", {"tasks_upsert": [self.task()], "plans_upsert": [
            {"id": "yesterday", "task_id": "a", "start": "2026-10-08T09:00:00+08:00", "end": "2026-10-08T10:00:00+08:00", "status": "done"}]})
        self.apply(1, "today", {"settings_patch": {"availability": [
            {"start": "2026-10-09T14:00:00+08:00", "end": "2026-10-09T16:00:00+08:00"}],
            "fixed_events": [{"id": "retrospective", "start": "2026-10-08T09:00:00+08:00", "end": "2026-10-08T10:00:00+08:00"}]},
            "plans_upsert": [{"id": "today", "task_id": "a", "start": "2026-10-09T14:00:00+08:00", "end": "2026-10-09T15:00:00+08:00"},
                             {"id": "tomorrow", "task_id": "a", "start": "2026-10-10T09:00:00+08:00", "end": "2026-10-10T10:00:00+08:00"}]})
        self.assertEqual(self.state()["state"]["plans"][0]["status"], "done")
        self.apply(2, "bad-deadline", {"tasks_upsert": [{"id": "a", "deadline": 99}]}, error="validation_error")
        self.assertEqual(self.state()["revision"], 2)
        self.apply(2, "outside", {"plans_upsert": [{"id": "today", "start": "2026-10-09T13:00:00+08:00"}]}, error="validation_error")

    def test_start_ignores_expired_slot_and_retains_backlog(self):
        self.apply(0, "old-plan", {"tasks_upsert": [self.task()], "plans_upsert": [
            {"id": "morning", "task_id": "a", "start": "2026-10-09T09:00:00+08:00", "end": "2026-10-09T10:00:00+08:00"}]})
        view = self.run_cli("view", "--date", "2026-10-09", "--mode", "start", "--now", "2026-10-09T13:00:00+08:00")
        self.assertEqual(view["candidate_source"], "backlog")
        self.assertNotIn("plan_id", view["candidates"][0])
        self.assertEqual(view["owner"], "trusted-test-owner")
        self.assertEqual(view["settings"]["timezone"], "Asia/Shanghai")

    def test_timezone_slots_snooze_quiet_and_confirmed_route(self):
        plan = {"id": "p", "task_id": "a", "start": "2026-10-09T14:00:00+08:00", "end": "2026-10-09T15:00:00+08:00"}
        self.apply(0, "plan", {"tasks_upsert": [self.task()], "plans_upsert": [plan]})
        check = self.run_cli("reminder", "--slot", "morning", "--now", "2026-10-09T00:05:00Z")
        self.assertFalse(check["eligible"])
        self.assertIn("disabled", check["reasons"])
        self.apply(1, "bare-route", {"settings_patch": {"reminders": {"enabled": True, "route": {"confirmed": True}}}})
        self.assertIn("no_confirmed_route", self.run_cli("reminder", "--slot", "morning", "--now", "2026-10-09T00:05:00Z")["reasons"])
        self.apply(2, "route", {"settings_patch": self.route()})
        morning = self.run_cli("reminder", "--slot", "morning", "--now", "2026-10-09T00:05:00Z")
        self.assertTrue(morning["eligible"])
        self.assertEqual(morning["key"], "2026-10-09:morning")
        self.assertFalse(morning["delivery_performed"])
        self.assertIn("too_early", self.run_cli("reminder", "--slot", "afternoon", "--now", "2026-10-09T00:05:00Z")["reasons"])
        self.assertTrue(self.run_cli("reminder", "--slot", "afternoon", "--now", "2026-10-09T05:05:00Z")["eligible"])
        self.assertIn("slot_expired", self.run_cli("reminder", "--slot", "morning", "--now", "2026-10-09T05:05:00Z")["reasons"])
        self.apply(3, "snooze", {"settings_patch": {"reminders": {"snooze_until": "2026-10-09T08:10:00+08:00"}}})
        self.assertIn("snoozed", self.run_cli("reminder", "--slot", "morning", "--now", "2026-10-09T08:05:00+08:00")["reasons"])
        self.apply(4, "quiet", {"settings_patch": {"reminders": {"snooze_until": None, "quiet_hours": [{"start": "22:00", "end": "09:00"}]}}})
        self.assertIn("quiet_hours", self.run_cli("reminder", "--slot", "morning", "--now", "2026-10-09T08:05:00+08:00")["reasons"])
        self.assertTrue(self.run_cli("reminder", "--slot", "afternoon", "--now", "2026-10-09T13:05:00+08:00")["eligible"])

    def setup_live_claim(self):
        now = dt.datetime.now(ZONE)
        plan = {"id": "live", "task_id": "a", "start": now.isoformat(), "end": (now + dt.timedelta(hours=1)).isoformat()}
        self.apply(0, "live", {"tasks_upsert": [self.task()], "plans_upsert": [plan],
                               "settings_patch": self.route(times={"morning": now.strftime("%H:%M"), "afternoon": "13:00"})})
        return self.run_cli("reminder", "--slot", "morning")["key"]

    def test_repeated_init_preserves_state_preferences_and_notification(self):
        key = self.setup_live_claim()
        self.apply(1, "persistent", {"tasks_upsert": [{"id": "a", "breakpoint": "Saved position"}],
                                     "parking_upsert": [{"id": "idea", "text": "Return later"}],
                                     "settings_patch": {"support_tone": "direct", "mode": "start"}})
        token = self.run_cli("claim", "--key", key, "--expected-revision", "2", "--task-id", "a")["token"]
        self.run_cli("finish", "--key", key, "--token", token, "--outcome", "unknown")
        before = self.state()
        notice = self.run_cli("reminder", "--slot", "morning")["notification"]
        for _ in range(2):
            repeated = self.run_cli("init", "--owner", "trusted-test-owner", "--timezone", "Asia/Shanghai")
            self.assertFalse(repeated["created"])
            self.assertEqual(repeated["revision"], 2)
            self.assertEqual(repeated["state"], before["state"])
            self.assertEqual(self.state(), before)
            self.assertEqual(self.run_cli("reminder", "--slot", "morning")["notification"], notice)
        self.run_cli("init", "--owner", "trusted-test-owner", "--timezone", "UTC", error="timezone_mismatch")
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), "--owner-key", "other-owner",
                                 "init", "--owner", "other-owner", "--timezone", "Asia/Shanghai"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "owner_mismatch")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.run_cli("reminder", "--slot", "morning")["notification"], notice)

    def test_parallel_claim_one_winner_unknown_no_retry(self):
        key = self.setup_live_claim()
        self.run_cli("claim", "--key", key, "--expected-revision", "0", "--task-id", "a", error="revision_conflict")
        args = ("claim", "--key", key, "--expected-revision", "1", "--task-id", "a")
        processes = [subprocess.Popen(self.command(*args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = []
        for process in processes:
            out, err = process.communicate()
            self.assertEqual(err, "")
            results.append(json.loads(out))
        winners = [x for x in results if x["ok"]]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual([x["error"]["code"] for x in results if not x["ok"]], ["not_eligible"])
        token = winners[0]["token"]
        self.run_cli("finish", "--key", key, "--token", "incorrect", "--outcome", "unknown", error="token_mismatch")
        self.run_cli("finish", "--key", key, "--token", token, "--outcome", "delivered", error="receipt_required")
        self.assertIn("already_claimed", self.run_cli("reminder", "--slot", "morning")["reasons"])
        self.run_cli("finish", "--key", key, "--token", token, "--outcome", "unknown")
        check = self.run_cli("reminder", "--slot", "morning")
        self.assertFalse(check["eligible"])
        self.assertIn("already_unknown", check["reasons"])
        self.assertTrue(self.run_cli("finish", "--key", key, "--token", token, "--outcome", "unknown")["idempotent"])
        self.run_cli("finish", "--key", key, "--token", token, "--outcome", "delivered", "--receipt", "actual-id", error="invalid_transition")
        self.assertEqual(self.state()["revision"], 1)

    def test_task_without_action_cannot_be_claimed(self):
        key = self.setup_live_claim()
        self.apply(1, "no-action", {"tasks_upsert": [{"id": "a", "first_action": None}]})
        checked = self.run_cli("reminder", "--slot", "morning")
        self.assertFalse(checked["eligible"])
        self.assertEqual(checked["candidates"][0]["reasons"], ["no_executable_action"])
        self.run_cli("claim", "--key", key, "--expected-revision", "2", "--task-id", "a", error="not_eligible")

    def test_next_action_stop_and_plan_outcome_are_current(self):
        key = self.setup_live_claim()
        # Even identical action text must not inherit a previous action's stop.
        self.apply(1, "next", {"tasks_upsert": [{"id": "a", "next_action": "Read one sentence"}],
                               "plans_upsert": [{"id": "live", "outcome": "Understand one concept"}]})
        check = self.run_cli("reminder", "--slot", "morning")
        self.assertTrue(check["eligible"])
        self.assertIsNone(check["ready_candidates"][0]["stop"])
        self.assertEqual(check["ready_candidates"][0]["outcome"], "Understand one concept")
        self.run_cli("claim", "--key", key, "--expected-revision", "2", "--task-id", "a", error="action_stop_required")
        self.apply(2, "invalid-stop", {"tasks_upsert": [{"id": "a", "next_action_stop": 12}]}, error="validation_error")
        self.assertEqual(self.state()["revision"], 2)
        self.apply(2, "stop", {"tasks_upsert": [{"id": "a", "next_action_stop": "Stop after saying the concept aloud"}]})
        check = self.run_cli("reminder", "--slot", "morning")
        self.assertEqual(check["ready_candidates"][0]["stop"], "Stop after saying the concept aloud")
        claimed = self.run_cli("claim", "--key", key, "--expected-revision", "3", "--task-id", "a")
        self.run_cli("finish", "--key", key, "--token", claimed["token"], "--outcome", "skipped")

    def test_explicit_rest_blocks_reminder_and_claim(self):
        now = dt.datetime.now(ZONE)
        plan = {"id": "future", "task_id": "a", "start": (now + dt.timedelta(minutes=10)).isoformat(),
                "end": (now + dt.timedelta(hours=1)).isoformat()}
        event = {"id": "rest", "title": "Rest break", "start": (now - dt.timedelta(minutes=5)).isoformat(),
                 "end": (now + dt.timedelta(minutes=5)).isoformat()}
        self.apply(0, "busy-default", {"tasks_upsert": [self.task()], "plans_upsert": [plan],
                                       "settings_patch": dict(self.route(times={"morning": now.strftime("%H:%M"), "afternoon": "13:00"}),
                                                              fixed_events=[event])})
        check = self.run_cli("reminder", "--slot", "morning")
        self.assertTrue(check["eligible"], check)
        self.assertNotIn("resting", check["reasons"])
        key = check["key"]
        self.apply(1, "rest", {"settings_patch": {"fixed_events": [dict(event, kind="rest")]}})
        check = self.run_cli("reminder", "--slot", "morning")
        self.assertFalse(check["eligible"])
        self.assertIn("resting", check["reasons"])
        denied = self.run_cli("claim", "--key", key, "--expected-revision", "2", "--task-id", "a", error="not_eligible")
        self.assertIn("resting", denied["error"]["reasons"])
        self.apply(2, "unknown-kind", {"settings_patch": {"fixed_events": [dict(event, kind="guess")]}} , error="validation_error")
        self.apply(2, "explicit-busy", {"settings_patch": {"fixed_events": [dict(event, kind="busy")]}})
        self.assertTrue(self.run_cli("reminder", "--slot", "morning")["eligible"])

    def test_failed_not_retried(self):
        key = self.setup_live_claim()
        token = self.run_cli("claim", "--key", key, "--expected-revision", "1", "--task-id", "a")["token"]
        self.run_cli("finish", "--key", key, "--token", token, "--outcome", "failed")
        self.assertIn("already_failed", self.run_cli("reminder", "--slot", "morning")["reasons"])

    def test_delivered_records_actual_receipt(self):
        key = self.setup_live_claim()
        token = self.run_cli("claim", "--key", key, "--expected-revision", "1", "--task-id", "a")["token"]
        result = self.run_cli("finish", "--key", key, "--token", token, "--outcome", "delivered", "--receipt", "actual-test-receipt")
        self.assertEqual(result["notification"]["receipt"], "actual-test-receipt")
        self.assertIn("already_delivered", self.run_cli("reminder", "--slot", "morning")["reasons"])

    def test_views_and_regenerable_snapshot(self):
        tasks = [self.task(str(n)) for n in range(55)]
        tasks[0].update(completion_criterion="Verified report", result="Partial draft", evidence="Draft saved",
                        breakpoint="Paragraph one", next_action="Check one source", next_action_stop="One source checked",
                        dependencies=[{"waiting_for": "Client data", "ready": False}], sources=["https://example.test/source"])
        self.apply(0, "many", {"tasks_upsert": tasks,
                               "reviews_upsert": [{"id": "old", "date": "2026-10-08", "confirmed": False},
                                                  {"id": "done", "date": "2026-10-08", "confirmed": True},
                                                  {"id": "today", "date": "2026-10-09", "confirmed": True}]})
        overview = self.run_cli("view", "--date", "2026-10-09", "--mode", "overview")
        self.assertEqual(len(overview["active_tasks"]), 50)
        self.assertEqual(overview["active_task_count"], 55)
        self.assertTrue(overview["truncated"])
        start = self.run_cli("view", "--date", "2026-10-09", "--mode", "start", "--task-id", "54")
        self.assertEqual(start["current_task"]["id"], "54")
        self.assertLessEqual(len(start["candidates"]), 5)
        self.assertTrue(start["candidates_truncated"])
        self.assertEqual(start["candidate_count"], 54)
        review = self.run_cli("view", "--date", "2026-10-09", "--mode", "review")
        self.assertEqual([x["id"] for x in review["reviews"]], ["old", "today"])
        self.assertFalse((self.root / "Project_Context.md").exists())
        snap = self.run_cli("snapshot", "--date", "2026-10-09")
        self.assertFalse(snap["authoritative"])
        self.assertIn("revision: 1", (self.root / "Project_Context.md").read_text())
        snapshot = (self.root / "Project_Context.md").read_text()
        context = json.loads(snapshot.split("```json\n")[1].split("\n```", 1)[0])
        self.assertEqual(len(context["active_task_details"]), 50)
        self.assertEqual(context["active_task_count"], 55)
        self.assertTrue(context["active_task_details_truncated"])
        for field in ("completion_criterion", "result", "evidence", "breakpoint", "next_action", "next_action_stop", "dependencies", "sources"):
            self.assertEqual(context["active_task_details"][0][field], tasks[0][field])
        (self.root / "Project_Context.md").write_text("stale fabricated snapshot")
        self.assertEqual(self.state()["state"]["tasks"][0]["title"], "Test 0")
        self.run_cli("snapshot", "--date", "2026-10-09")
        self.assertIn("revision: 1", (self.root / "Project_Context.md").read_text())

    def test_parking_and_review_views_are_bounded(self):
        self.apply(0, "records", {"parking_upsert": [{"id": str(n), "text": "idea"} for n in range(55)],
                                  "reviews_upsert": [{"id": str(n), "confirmed": False} for n in range(55)]})
        overview = self.run_cli("view", "--date", "2026-10-09", "--mode", "overview")
        self.assertEqual(len(overview["parking"]), 50)
        self.assertEqual(overview["parking_count"], 55)
        self.assertTrue(overview["parking_truncated"])
        review = self.run_cli("view", "--date", "2026-10-09", "--mode", "review")
        self.assertEqual(len(review["reviews"]), 50)
        self.assertEqual(review["review_count"], 55)
        self.assertTrue(review["reviews_truncated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
