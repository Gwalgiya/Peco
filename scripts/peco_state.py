#!/usr/bin/env python3
"""Peco v3 local state. SQLite is authoritative; snapshots are disposable.

No network or notification delivery is performed. A claimed/unknown/failed key
is never automatically retried: this is a delivery ledger, not exactly-once IO.
"""
import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc
SLOTS = ("morning", "afternoon")
ACTIVE = ("unstarted", "in_progress", "waiting", "paused")
TASK_DEFAULTS = {
    "title": None, "bucket": None, "importance": "unknown", "urgency": "unknown",
    "deadline": None, "preferred_window": None, "estimate_minutes": None,
    "progress": "unstarted", "completion_criterion": None, "first_action": None,
    "first_action_stop": None, "breakpoint": None, "next_action": None,
    "next_action_stop": None,
    "dependencies": [], "parent_id": None, "required_for_parent": True,
    "series_id": None, "occurrence_date": None, "result": None, "evidence": None,
    "sources": [], "assignee": None,
}
PLAN_DEFAULTS = {"outcome": None, "status": "planned"}
PARKING_DEFAULTS = {"status": "open", "task_id": None, "date": None}


class StateError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def require(condition, message, code="validation_error", **details):
    if not condition:
        raise StateError(code, message, **details)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def meaningful(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def date_value(value):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "Expected YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise StateError("validation_error", "Invalid calendar date", value=value)


def instant(value):
    require(isinstance(value, str), "Expected aware ISO timestamp")
    try:
        result = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise StateError("validation_error", "Invalid ISO timestamp", value=value)
    require(result.tzinfo is not None and result.utcoffset() is not None,
            "Timestamp must include a UTC offset", value=value)
    return result.astimezone(UTC)


def wall_time(value):
    require(isinstance(value, str) and re.fullmatch(r"\d{2}:\d{2}", value), "Expected HH:MM")
    try:
        return dt.time.fromisoformat(value)
    except ValueError:
        raise StateError("validation_error", "Invalid wall-clock time", value=value)


def timezone(value):
    require(nonempty(value), "timezone must be an IANA name")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        raise StateError("validation_error", "Unknown timezone", timezone=value)


def interval(record):
    require(isinstance(record, dict), "Time interval must be an object")
    start, end = instant(record.get("start")), instant(record.get("end"))
    require(end > start, "Time interval end must be after start")
    return start, end


def overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def load_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key", key=key)
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, TypeError) as exc:
        raise StateError("invalid_json", str(exc))


def default_state(owner, zone):
    return {
        "schema_version": 3, "owner": owner,
        "settings": {
            "timezone": zone, "availability": [], "fixed_events": [],
            "support_tone": None, "mode": None,
            "reminders": {"enabled": False,
                          "times": {"morning": "08:00", "afternoon": "13:00"},
                          "route": None, "job_ids": {}, "quiet_hours": [],
                          "snooze_until": None, "max_lateness_minutes": 30},
        },
        "tasks": [], "plans": [], "parking": [], "reviews": [],
    }


def validate(state):
    require(state.get("schema_version") == 3 and nonempty(state.get("owner")), "Invalid state identity")
    settings = state["settings"]
    require(isinstance(settings, dict), "settings must be an object")
    timezone(settings.get("timezone"))
    for key in ("support_tone", "mode"):
        require(settings.get(key) is None or nonempty(settings[key]), key + " must be a string or null")
    for key in ("availability", "fixed_events"):
        require(isinstance(settings.get(key), list), key + " must be an interval list")
        for item in settings[key]:
            interval(item)
    fixed_ids = [x.get("id") for x in settings["fixed_events"]]
    require(all(nonempty(x) for x in fixed_ids) and len(set(fixed_ids)) == len(fixed_ids),
            "fixed_events require unique nonempty ids")
    for event in settings["fixed_events"]:
        require(event.get("kind") in (None, "rest", "busy"), "fixed event kind must be rest, busy, or unspecified")
    reminders = settings.get("reminders")
    require(isinstance(reminders, dict), "reminders must be an object")
    require(isinstance(reminders.get("enabled"), bool), "reminders.enabled must be boolean")
    require(isinstance(reminders.get("times"), dict) and set(reminders["times"]) == set(SLOTS),
            "reminders.times requires morning and afternoon")
    for value in reminders["times"].values():
        wall_time(value)
    require(reminders.get("route") is None or isinstance(reminders["route"], dict), "route must be an object or null")
    if reminders["route"] is not None:
        route = reminders["route"]
        require(isinstance(route.get("confirmed", False), bool), "route.confirmed must be boolean")
        for key in ("channel", "recipient", "account_id", "thread_id"):
            require(route.get(key) is None or nonempty(route[key]), "route field must be a nonempty string", field=key)
    jobs = reminders.get("job_ids")
    require(isinstance(jobs, dict) and all(nonempty(k) and nonempty(v) for k, v in jobs.items()),
            "job_ids must map names to actual nonempty ids")
    require(isinstance(reminders.get("quiet_hours"), list), "quiet_hours must be a list")
    for window in reminders["quiet_hours"]:
        require(isinstance(window, dict), "quiet_hours entry must be an object")
        require(wall_time(window.get("start")) != wall_time(window.get("end")), "quiet_hours start and end must differ")
    if reminders.get("snooze_until") is not None:
        instant(reminders["snooze_until"])
    lateness = reminders.get("max_lateness_minutes")
    require(type(lateness) is int and lateness >= 0, "max_lateness_minutes must be a nonnegative integer")
    maps = {}
    for collection in ("tasks", "plans", "parking", "reviews"):
        require(isinstance(state.get(collection), list), collection + " must be a list")
        records = state[collection]
        maps[collection] = {}
        for record in records:
            require(isinstance(record, dict), "Record must be an object", collection=collection, record_id=None, field="record")
            record_id = record.get("id")
            require(nonempty(record_id), collection + " require ids", collection=collection, record_id=record_id, field="id")
            require(record_id not in maps[collection], "Duplicate record id", collection=collection, record_id=record_id, field="id")
            maps[collection][record_id] = record
    tasks = maps["tasks"]
    edges = {task_id: [] for task_id in tasks}
    series = set()
    for task in tasks.values():
        require(set(task) <= set(TASK_DEFAULTS) | {"id"}, "Unknown task fields", task_id=task["id"])
        for key in ("title", "preferred_window", "completion_criterion", "first_action", "first_action_stop", "breakpoint", "next_action", "next_action_stop", "assignee"):
            require(task.get(key) is None or nonempty(task[key]), "Task field must be a string or null", field=key)
        require(task["bucket"] in (None, "core", "maintenance", "fuel"), "Invalid bucket")
        for key in ("importance", "urgency"):
            require(task[key] in ("high", "medium", "low", "unknown"), "Invalid priority", field=key)
        require(task["progress"] in ACTIVE + ("completed", "cancelled"), "Invalid progress")
        estimate = task["estimate_minutes"]
        require(estimate is None or (type(estimate) is int and estimate > 0), "estimate_minutes must be positive integer or null")
        if task["deadline"] is not None:
            require(isinstance(task["deadline"], str), "deadline must be a date, aware timestamp, or null")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", task["deadline"]):
                date_value(task["deadline"])
            else:
                instant(task["deadline"])
        require(isinstance(task["required_for_parent"], bool), "required_for_parent must be boolean")
        parent = task["parent_id"]
        require(parent is None or parent in tasks, "Unknown parent reference", task_id=task["id"])
        if parent is not None and task["required_for_parent"]:
            edges[parent].append(task["id"])
        require(isinstance(task["dependencies"], list), "dependencies must be a list")
        for dep in task["dependencies"]:
            require(isinstance(dep, dict), "Dependency must be an object")
            require(set(dep) <= {"task_id", "waiting_for", "ready", "resume_signal"}, "Unknown dependency field")
            require((nonempty(dep.get("task_id"))) != (nonempty(dep.get("waiting_for"))), "Dependency requires task_id or waiting_for")
            require(isinstance(dep.get("ready"), bool), "Dependency requires an explicit ready boolean")
            require(dep.get("resume_signal") is None or nonempty(dep["resume_signal"]), "resume_signal must be a string or null")
            if dep.get("task_id"):
                require(dep["task_id"] in tasks, "Unknown dependency reference")
                edges[task["id"]].append(dep["task_id"])
        require(isinstance(task["sources"], list) and all(nonempty(x) for x in task["sources"]), "sources must be locator strings")
        pair = (task["series_id"], task["occurrence_date"])
        require((pair[0] is None) == (pair[1] is None), "series_id and occurrence_date must be supplied together")
        if pair[0] is not None:
            require(nonempty(pair[0]), "series_id must be nonempty")
            date_value(pair[1])
            require(pair not in series, "Duplicate series occurrence")
            series.add(pair)
        if task["progress"] == "completed":
            require(nonempty(task["completion_criterion"]) and (meaningful(task["result"]) or meaningful(task["evidence"])),
                    "Completion requires a criterion and result or evidence", task_id=task["id"])
            required = [x for x in tasks.values() if x["parent_id"] == task["id"] and x["required_for_parent"]]
            require(all(x["progress"] == "completed" for x in required), "Necessary child tasks are incomplete", task_id=task["id"])
    # Parent ancestry is checked even for optional children. Necessary children
    # also participate in the dependency graph to detect parent/child deadlocks.
    for task_id in tasks:
        seen, cursor = set(), task_id
        while cursor is not None:
            require(cursor not in seen, "Parent cycle detected")
            seen.add(cursor)
            cursor = tasks[cursor]["parent_id"]
    # Iterative three-color DFS handles arbitrarily long valid chains without
    # depending on Python's recursion limit. Gray nodes are the current path.
    colors = {task_id: 0 for task_id in tasks}
    for task_id in tasks:
        if colors[task_id] != 0:
            continue
        colors[task_id] = 1
        stack = [(task_id, iter(edges[task_id]))]
        while stack:
            node, children = stack[-1]
            try:
                child = next(children)
            except StopIteration:
                colors[node] = 2
                stack.pop()
                continue
            require(colors[child] != 1, "Dependency cycle detected", task_id=child)
            if colors[child] == 0:
                colors[child] = 1
                stack.append((child, iter(edges[child])))
    work = []
    fixed = [interval(x) for x in settings["fixed_events"]]
    available = [interval(x) for x in settings["availability"]]
    for plan in state["plans"]:
        require(set(plan) <= {"id", "task_id", "start", "end", "outcome", "status"}, "Unknown plan field")
        require(plan.get("task_id") in tasks, "Unknown plan task reference")
        span = interval(plan)
        require(plan["status"] in ("planned", "done", "skipped", "cancelled"), "Invalid plan status")
        require(plan["outcome"] is None or nonempty(plan["outcome"]), "Plan outcome must be a string or null")
        if plan["status"] == "planned":
            require(not any(overlaps(span, x) for x in fixed), "Plan conflicts with fixed event", plan_id=plan["id"])
            require(not any(overlaps(span, x[1]) for x in work), "Work segments overlap", plan_id=plan["id"])
            # Availability only constrains local dates it explicitly covers.
            # Historical done segments stay valid when today's settings change.
            zone = timezone(settings["timezone"])
            cursor = span[0].astimezone(zone).date()
            last = (span[1] - dt.timedelta(microseconds=1)).astimezone(zone).date()
            while available and cursor <= last:
                day = day_bounds(cursor, zone)
                declared = sorted(x for x in available if overlaps(x, day))
                if declared:
                    beginning, ending = max(span[0], day[0]), min(span[1], day[1])
                    covered_until = beginning
                    for a, b in declared:
                        if a <= covered_until:
                            covered_until = max(covered_until, b)
                    require(covered_until >= ending, "Plan is outside declared availability", plan_id=plan["id"])
                cursor += dt.timedelta(days=1)
            work.append((plan["id"], span))
    for collection in ("parking", "reviews"):
        for record in state[collection]:
            context = {"collection": collection, "record_id": record["id"]}
            reference = record.get("task_id")
            require(reference is None or (nonempty(reference) and reference in tasks), "Unknown task reference", field="task_id", **context)
            if record.get("date") is not None:
                try:
                    date_value(record["date"])
                except StateError as exc:
                    raise StateError(exc.code, str(exc), **dict(exc.details, **context, field="date"))
            if collection == "parking":
                require(nonempty(record.get("text")), "Parking text must be a nonempty string", field="text", **context)
                require(record.get("status", "open") in ("open", "processed", "dismissed"), "Invalid parking status", field="status", **context)
            if collection == "reviews":
                require(isinstance(record.get("confirmed", False), bool), "review.confirmed must be boolean", field="confirmed", **context)


def connect(root, create=False):
    require(root.is_absolute(), "--root must be an absolute owner data directory", "invalid_root")
    path = root / "state.sqlite3"
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    else:
        require(path.is_file(), "State database does not exist; run init explicitly", "not_initialized")
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


def read_state(connection):
    require(connection.execute("PRAGMA user_version").fetchone()[0] == 3, "Unsupported database schema", "schema_mismatch")
    row = connection.execute("SELECT revision, data FROM state WHERE singleton=1").fetchone()
    require(row is not None, "Database has no initialized state", "not_initialized")
    state = load_json(row["data"])
    require(state.get("schema_version") == 3, "Unsupported state schema", "schema_mismatch")
    # Missing status in a valid older parking record means open. Never invent
    # its text, and do not write a read-time compatibility default to the DB.
    for record in state["parking"]:
        record.setdefault("status", "open")
    return state, row["revision"]


def initialize(root, owner, zone):
    require(nonempty(owner), "--owner must be a trusted nonempty key")
    timezone(zone)
    connection = connect(root, create=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        require(version in (0, 3), "Unsupported database schema", "schema_mismatch")
        connection.execute("CREATE TABLE IF NOT EXISTS state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL, data TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS notifications (key TEXT PRIMARY KEY, token TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL, route TEXT NOT NULL, status TEXT NOT NULL, claimed_at TEXT NOT NULL, finished_at TEXT, receipt TEXT)")
        connection.execute("PRAGMA user_version=3")
        row = connection.execute("SELECT data, revision FROM state WHERE singleton=1").fetchone()
        if row:
            state, revision = read_state(connection)
            require(state["owner"] == owner, "Owner key differs from existing state", "owner_mismatch")
            require(state["settings"]["timezone"] == zone, "Timezone differs; change through apply", "timezone_mismatch")
            created = False
        else:
            state = default_state(owner, zone)
            validate(state)
            connection.execute("INSERT INTO state VALUES (1, 0, ?)", (canonical(state),))
            revision, created = 0, True
        connection.commit()
        os.chmod(root / "state.sqlite3", 0o600)
        return {"created": created, "revision": revision, "state": state}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def merge_settings(target, patch, path="settings"):
    require(isinstance(patch, dict), path + " patch must be an object")
    for key, value in patch.items():
        require(key in target, "Unknown settings field", field=path + "." + key)
        if isinstance(target[key], dict) and isinstance(value, dict) and key not in ("job_ids", "route"):
            merge_settings(target[key], value, path + "." + key)
        else:
            target[key] = copy.deepcopy(value)


def apply_patch(connection, expected, event_id, patch):
    require(nonempty(event_id), "event-id must be a trusted message id")
    require(isinstance(patch, dict) and set(patch) <= {"tasks_upsert", "plans_upsert", "parking_upsert", "reviews_upsert", "settings_patch"}, "Unknown patch field")
    payload_hash = hashlib.sha256(canonical(patch).encode("utf-8")).hexdigest()
    connection.execute("BEGIN IMMEDIATE")
    try:
        state, revision = read_state(connection)
        prior = connection.execute("SELECT payload_hash, revision FROM events WHERE event_id=?", (event_id,)).fetchone()
        if prior:
            require(prior["payload_hash"] == payload_hash, "Same event id has different payload", "event_id_conflict")
            connection.commit()
            return {"revision": revision, "event_revision": prior["revision"], "idempotent": True}
        require(revision == expected, "State revision changed; read latest before applying", "revision_conflict", expected=expected, actual=revision)
        updated = copy.deepcopy(state)
        for collection in ("tasks", "plans", "parking", "reviews"):
            changes = patch.get(collection + "_upsert", [])
            require(isinstance(changes, list), collection + "_upsert must be a list")
            lookup = {item["id"]: item for item in updated[collection]}
            changed_ids = set()
            for change in changes:
                record_id = change.get("id") if isinstance(change, dict) else None
                require(isinstance(change, dict) and nonempty(record_id), "Upsert requires id", collection=collection, record_id=record_id, field="id")
                require(record_id not in changed_ids, "Duplicate upsert id", collection=collection, record_id=record_id, field="id")
                changed_ids.add(change["id"])
                if change["id"] not in lookup:
                    defaults = TASK_DEFAULTS if collection == "tasks" else PLAN_DEFAULTS if collection == "plans" else PARKING_DEFAULTS if collection == "parking" else {"confirmed": False}
                    record = copy.deepcopy(defaults)
                    record["id"] = change["id"]
                    updated[collection].append(record)
                    lookup[change["id"]] = record
                lookup[change["id"]].update(copy.deepcopy(change))
        if "settings_patch" in patch:
            merge_settings(updated["settings"], patch["settings_patch"])
        validate(updated)
        new_revision = revision + 1
        connection.execute("UPDATE state SET revision=?, data=? WHERE singleton=1", (new_revision, canonical(updated)))
        connection.execute("INSERT INTO events VALUES (?,?,?,?)", (event_id, payload_hash, new_revision, dt.datetime.now(UTC).isoformat()))
        connection.commit()
        return {"revision": new_revision, "event_revision": new_revision, "idempotent": False}
    except Exception:
        connection.rollback()
        raise


def task_readiness(task, task_map):
    reasons = []
    if task["progress"] not in ("unstarted", "in_progress"):
        reasons.append("progress_" + task["progress"])
    for dep in task["dependencies"]:
        if not dep["ready"] or (dep.get("task_id") and task_map[dep["task_id"]]["progress"] != "completed"):
            reasons.append("dependency_not_ready")
    using_next = nonempty(task.get("next_action"))
    action = task["next_action"] if using_next else task["first_action"]
    if not nonempty(action):
        reasons.append("no_executable_action")
    return {"task_id": task["id"], "title": task["title"], "ready": not reasons,
            "reasons": sorted(set(reasons)), "action": action,
            "stop": task.get("next_action_stop") if using_next else task["first_action_stop"],
            "breakpoint": task["breakpoint"]}


def day_bounds(date, zone):
    lower = dt.datetime.combine(date, dt.time(), zone).astimezone(UTC)
    upper = dt.datetime.combine(date + dt.timedelta(days=1), dt.time(), zone).astimezone(UTC)
    return lower, upper


def today_plans(state, date):
    bounds = day_bounds(date, timezone(state["settings"]["timezone"]))
    return sorted([x for x in state["plans"] if overlaps(interval(x), bounds)], key=lambda x: instant(x["start"]))


def candidates(state, date, now=None):
    task_map = {x["id"]: x for x in state["tasks"]}
    result = []
    for plan in today_plans(state, date):
        if plan["status"] != "planned" or (now is not None and instant(plan["end"]) <= now):
            continue
        item = task_readiness(task_map[plan["task_id"]], task_map)
        item.update({"plan_id": plan["id"], "start": plan["start"], "end": plan["end"],
                     "outcome": plan["outcome"]})
        result.append(item)
    return result


def notification_view(row):
    if row is None:
        return None
    return {k: row[k] for k in ("key", "task_id", "revision", "status", "claimed_at", "finished_at", "receipt")}


def reminder(connection, state, revision, slot, now):
    local = now.astimezone(timezone(state["settings"]["timezone"]))
    key = local.date().isoformat() + ":" + slot
    config = state["settings"]["reminders"]
    route = config["route"]
    reasons = []
    if not config["enabled"]:
        reasons.append("disabled")
    if not (isinstance(route, dict) and route.get("confirmed") is True and nonempty(route.get("channel")) and nonempty(route.get("recipient"))):
        reasons.append("no_confirmed_route")
    if config["snooze_until"] and now < instant(config["snooze_until"]):
        reasons.append("snoozed")
    if any(event.get("kind") == "rest" and interval(event)[0] <= now < interval(event)[1]
           for event in state["settings"]["fixed_events"]):
        reasons.append("resting")
    clock = local.time().replace(tzinfo=None)
    for window in config["quiet_hours"]:
        start, end = wall_time(window["start"]), wall_time(window["end"])
        if (start < end and start <= clock < end) or (start > end and (clock >= start or clock < end)):
            reasons.append("quiet_hours")
            break
    scheduled_local = dt.datetime.combine(local.date(), wall_time(config["times"][slot]), local.tzinfo)
    scheduled = scheduled_local.astimezone(UTC)
    if scheduled.astimezone(local.tzinfo).replace(tzinfo=None) != scheduled_local.replace(tzinfo=None):
        reasons.append("nonexistent_local_slot")
    if now < scheduled:
        reasons.append("too_early")
    elif now > scheduled + dt.timedelta(minutes=config["max_lateness_minutes"]):
        reasons.append("slot_expired")
    prior = connection.execute("SELECT * FROM notifications WHERE key=?", (key,)).fetchone()
    if prior:
        reasons.append("already_" + prior["status"])
    details = candidates(state, local.date(), now)
    ready = [x for x in details if x["ready"]]
    if not ready:
        reasons.append("no_ready_current_or_future_plan")
    return {"eligible": not reasons, "key": key, "revision": revision,
            "route": route, "ready_candidates": ready, "candidates": details,
            "reasons": reasons, "notification": notification_view(prior),
            "delivery_performed": False, "now": local.isoformat()}


def claim(connection, key, expected, task_id):
    require(isinstance(key, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}:(morning|afternoon)", key), "Invalid notification key")
    connection.execute("BEGIN IMMEDIATE")
    try:
        state, revision = read_state(connection)
        require(revision == expected, "State revision changed; read latest before claiming", "revision_conflict", expected=expected, actual=revision)
        now = dt.datetime.now(UTC)
        checked = reminder(connection, state, revision, key.split(":")[1], now)
        require(checked["key"] == key and checked["eligible"], "Notification is not currently eligible", "not_eligible", reasons=checked["reasons"], current_key=checked["key"])
        selected = next((x for x in checked["ready_candidates"] if x["task_id"] == task_id), None)
        require(selected is not None, "Task has no executable current or future plan", "task_not_ready")
        require(nonempty(selected["action"]) and nonempty(selected["stop"]),
                "Claim requires the current action and its explicit stop", "action_stop_required")
        token = secrets.token_urlsafe(32)
        connection.execute("INSERT INTO notifications (key,token,task_id,revision,route,status,claimed_at) VALUES (?,?,?,?,?,'claimed',?)",
                           (key, token, task_id, revision, canonical(checked["route"]), now.isoformat()))
        connection.commit()
        return {"claimed": True, "key": key, "token": token, "task_id": task_id, "revision": revision,
                "route": checked["route"], "delivery_performed": False}
    except Exception:
        connection.rollback()
        raise


def finish(connection, key, token, outcome, receipt):
    require(receipt is None or nonempty(receipt), "receipt must be an actual nonempty id")
    require(outcome != "delivered" or nonempty(receipt), "delivered requires an actual receipt", "receipt_required")
    connection.execute("BEGIN IMMEDIATE")
    try:
        _, revision = read_state(connection)
        row = connection.execute("SELECT * FROM notifications WHERE key=?", (key,)).fetchone()
        require(row is not None, "No notification claim exists", "claim_missing")
        require(secrets.compare_digest(row["token"], token), "Incorrect claim token", "token_mismatch")
        if row["status"] != "claimed":
            require(row["status"] == outcome and row["receipt"] == receipt, "Notification already has a different terminal outcome", "invalid_transition")
            connection.commit()
            return {"revision": revision, "idempotent": True, "notification": notification_view(row)}
        connection.execute("UPDATE notifications SET status=?, receipt=?, finished_at=? WHERE key=?",
                           (outcome, receipt, dt.datetime.now(UTC).isoformat(), key))
        row = connection.execute("SELECT * FROM notifications WHERE key=?", (key,)).fetchone()
        connection.commit()
        return {"revision": revision, "idempotent": False, "notification": notification_view(row)}
    except Exception:
        connection.rollback()
        raise


def view(state, revision, date, mode, task_id=None, now=None):
    task_map = {x["id"]: x for x in state["tasks"]}
    require(task_id is None or task_id in task_map, "Unknown requested task", "task_missing")
    base = {"revision": revision, "date": date.isoformat(), "mode": mode,
            "owner": state["owner"], "settings": state["settings"]}
    if mode == "full":
        return dict(base, state=state)
    plans = today_plans(state, date)
    now = now or dt.datetime.now(UTC)
    current_date = now.astimezone(timezone(state["settings"]["timezone"])).date()
    planned_candidates = candidates(state, date, now if date == current_date else None)
    ready = [x for x in planned_candidates if x["ready"]]
    if mode == "overview":
        active = [x for x in state["tasks"] if x["progress"] in ACTIVE]
        fields = ("id", "title", "bucket", "importance", "urgency", "progress", "deadline", "preferred_window", "estimate_minutes", "parent_id")
        return dict(base, active_tasks=[{k: x[k] for k in fields} for x in active[:50]],
                    active_task_count=len(active), truncated=len(active) > 50,
                    today_plans=plans, parking=state["parking"][-50:],
                    parking_count=len(state["parking"]), parking_truncated=len(state["parking"]) > 50,
                    candidates=planned_candidates)
    if mode == "start":
        source = "planned"
        readiness_by_task = {}
        if not ready:
            source = "backlog"
            for task in state["tasks"]:
                candidate = task_readiness(task, task_map)
                readiness_by_task[task["id"]] = candidate
                if candidate["ready"]:
                    ready.append(candidate)
        current = task_map[task_id] if task_id else task_map[ready[0]["task_id"]] if ready else None
        current_readiness = readiness_by_task.get(current["id"]) if current else None
        if current and current_readiness is None:
            current_readiness = task_readiness(current, task_map)
        related_ids = {d["task_id"] for d in current["dependencies"] if d.get("task_id")} if current else set()
        if current and current["parent_id"]:
            related_ids.add(current["parent_id"])
        return dict(base, current_task=current, current_readiness=current_readiness,
                    related_tasks=[task_map[x] for x in sorted(related_ids)], candidates=ready[:5],
                    candidate_source=source, candidate_count=len(ready), candidates_truncated=len(ready) > 5)
    reviews = [x for x in state["reviews"] if x.get("date") == date.isoformat() or not x.get("confirmed", False)]
    shown = reviews[-50:]
    ids = {x["task_id"] for x in plans} | {x["task_id"] for x in shown if x.get("task_id")}
    return dict(base, today_plans=plans, reviews=shown, review_count=len(reviews),
                reviews_truncated=len(reviews) > 50, related_tasks=[task_map[x] for x in sorted(ids)])


def snapshot(root, state, revision, date):
    context = view(state, revision, date, "overview")
    active = [x for x in state["tasks"] if x["progress"] in ACTIVE]
    context["active_task_details"] = active[:50]
    context["active_task_details_truncated"] = len(active) > 50
    content = ("# Peco Project Context\n\n"
               "此文件是按需生成的可再生快照；权威状态在 `state.sqlite3`。\n"
               "不要直接编辑此快照来改变状态，使用 `view` 读取最新状态。\n\n"
               "- schema_version: 3\n- revision: " + str(revision) + "\n- date: " + date.isoformat() +
               "\n\n```json\n" + json.dumps(context, ensure_ascii=False, indent=2, allow_nan=False) + "\n```\n")
    descriptor, path = tempfile.mkstemp(prefix=".Project_Context-", dir=str(root), text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(path, root / "Project_Context.md")
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return {"revision": revision, "path": str(root / "Project_Context.md"), "authoritative": False}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise StateError("argument_error", message)


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--owner-key", help="Trusted owner key; required for every non-init command")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    command = commands.add_parser("init")
    command.add_argument("--owner", required=True)
    command.add_argument("--timezone", default="Asia/Shanghai")
    command = commands.add_parser("view")
    command.add_argument("--date", required=True)
    command.add_argument("--mode", choices=("overview", "start", "review", "full"), required=True)
    command.add_argument("--task-id")
    command.add_argument("--now", help="Aware timestamp for reproducible current-date candidate filtering")
    command = commands.add_parser("apply")
    command.add_argument("--expected-revision", type=int, required=True)
    command.add_argument("--event-id", required=True)
    command.add_argument("--file", required=True)
    command = commands.add_parser("snapshot")
    command.add_argument("--date", required=True)
    command = commands.add_parser("reminder")
    command.add_argument("--slot", choices=SLOTS, required=True)
    command.add_argument("--now")
    command = commands.add_parser("claim")
    command.add_argument("--key", required=True)
    command.add_argument("--expected-revision", type=int, required=True)
    command.add_argument("--task-id", required=True)
    command = commands.add_parser("finish")
    command.add_argument("--key", required=True)
    command.add_argument("--token", required=True)
    command.add_argument("--outcome", choices=("delivered", "failed", "unknown", "skipped"), required=True)
    command.add_argument("--receipt")
    args = parser.parse_args()
    if args.command == "init":
        require(args.owner_key is None or args.owner_key == args.owner, "Owner keys differ", "owner_mismatch")
        return initialize(args.root, args.owner, args.timezone)
    require(nonempty(args.owner_key), "--owner-key is required for non-init commands", "argument_error")
    connection = connect(args.root)
    try:
        private_state, _ = read_state(connection)
        require(private_state["owner"] == args.owner_key, "Owner key differs from existing state", "owner_mismatch")
        if args.command == "apply":
            text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
            return apply_patch(connection, args.expected_revision, args.event_id, load_json(text))
        if args.command == "claim":
            return claim(connection, args.key, args.expected_revision, args.task_id)
        if args.command == "finish":
            return finish(connection, args.key, args.token, args.outcome, args.receipt)
        connection.execute("BEGIN")
        state, revision = read_state(connection)
        if args.command == "reminder":
            result = reminder(connection, state, revision, args.slot, instant(args.now) if args.now else dt.datetime.now(UTC))
        else:
            date = date_value(args.date)
            result = view(state, revision, date, args.mode, args.task_id, instant(args.now) if args.now else None) if args.command == "view" else snapshot(args.root, state, revision, date)
        connection.commit()
        return result
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        print(json.dumps(dict(ok=True, **main()), ensure_ascii=False, allow_nan=False))
    except StateError as exc:
        print(json.dumps({"ok": False, "error": dict(code=exc.code, message=str(exc), **exc.details)}, ensure_ascii=False))
        sys.exit(2)
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": {"code": "interrupted", "message": "Operation interrupted"}}))
        sys.exit(130)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": {"code": "operation_failed", "message": str(exc)}}, ensure_ascii=False))
        sys.exit(2)
