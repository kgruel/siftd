"""Private, fixture-only Pi identity exposure diagnostic (never an apply token)."""

import hashlib
import io
import json
import math
import os
import re
import stat
from collections import Counter
from pathlib import Path

from siftd.adapters import pi_agent
from siftd.dateparse import to_utc
from siftd.storage._pi_identity_preflight import read_snapshot

_LIMITATIONS = [
    "exposure_only", "mapping_unproved", "selected_scope_only", "projection_not_full_graph",
    "logical_read_only", "not_atomic_with_sources", "no_corpus_orphan_census",
    "unrelated_dangling_assignments_unattributed", "historical_fidelity_unproved",
]
_KINDS = ("prompt", "response", "tool_call")
_TARGETS = (*_KINDS, "exchange")
_BUCKETS = ("key_present_in_all_parses", "key_absent_from_all_parses", "mixed", "unknown")
_CANDIDATE = re.compile(r"[A-Za-z0-9_-]{1,256}\Z", re.ASCII)
_HASH = re.compile(r"[a-fA-F0-9]{64}\Z", re.ASCII)


def _path_reason(path: Path, *, database=False):
    value = str(path)
    if (not path.is_absolute() or ".." in path.parts or any(c in value for c in "~$*?[]{}")
            or (database and (value.startswith("//") or any(c in value for c in "?#%")))):
        return "invalid_path"
    try:
        for part in (*reversed(path.parents), path):
            if part.is_symlink():
                return "symlink_refused"
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            return "non_regular"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    return None


def _metadata(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _capture(path):
    reason = _path_reason(path)
    if reason:
        return None, reason
    try:
        before = _metadata(path.stat())
        # O_NOFOLLOW covers replacement of the final component between check/open.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                return None, "source_changed_during_read"
            data = stream.read()
            after = _metadata(os.fstat(stream.fileno()))
        if before != _metadata(opened) or before != after or before != _metadata(path.stat()):
            return None, "source_changed_during_read"
        return (data, before, hashlib.sha256(data).hexdigest()), None
    except OSError:
        return None, "unreadable"


def _finite(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(v) for v in value.values())
    if isinstance(value, list):
        return all(_finite(v) for v in value)
    return True


def _optional(obj, name, types, *, nullable=False):
    return name not in obj or (nullable and obj[name] is None) or type(obj[name]) in types


def _valid_record(record):
    """Closed consumed-field admissibility, independent of parser execution."""
    opt = _optional
    if not all(opt(record, name, (str,), nullable=True) for name in ("type", "timestamp")):
        return False
    kind = record.get("type")
    if kind == "session":
        return all(opt(record, name, (str,), nullable=True) for name in ("id", "cwd"))
    if kind == "model_change":
        return opt(record, "modelId", (str,), nullable=True)
    if kind != "message":
        return True
    if not opt(record, "message", (dict,)):
        return False
    msg = record.get("message", {})
    if not opt(msg, "role", (str,), nullable=True):
        return False
    role = msg.get("role")
    if role not in ("user", "assistant", "toolResult"):
        return True
    if not opt(msg, "content", (list,)):
        return False
    if role == "toolResult":
        if not (opt(msg, "toolCallId", (str,), nullable=True) and opt(msg, "toolName", (str,))
                and opt(msg, "isError", (bool,))):
            return False
        return all(not isinstance(b, dict) or b.get("type") != "text" or opt(b, "text", (str,))
                   for b in msg.get("content", []))
    for block in msg.get("content", []):
        if role == "user" and isinstance(block, str):
            continue
        if not isinstance(block, dict) or not opt(block, "type", (str,)):
            return False
        kind = block.get("type")
        if kind == "text" and not opt(block, "text", (str,)):
            return False
        if role == "assistant":
            if kind == "thinking" and not opt(block, "thinking", (str,)):
                return False
            if kind == "toolCall" and not (opt(block, "id", (str,), nullable=True) and opt(block, "name", (str,))):
                return False
    if role == "assistant":
        if not (opt(msg, "model", (str,), nullable=True) and opt(msg, "usage", (dict,), nullable=True)):
            return False
        usage = msg.get("usage") or {}
        if not all(opt(usage, name, (int,), nullable=True) for name in ("input", "output", "cacheRead", "cacheWrite")):
            return False
        if not opt(usage, "cost", (dict,), nullable=True):
            return False
        if not opt(usage.get("cost") or {}, "total", (int, float), nullable=True):
            return False
    return True


def _decode(data):
    records = []
    bad = 0
    try:
        with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", newline=None) as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.strip())
                    if not isinstance(obj, dict) or not _finite(obj):
                        bad += 1
                    else:
                        records.append(obj)
                except (ValueError, RecursionError):
                    bad += 1
    except UnicodeDecodeError:
        # Buffered decoding can lose an unknown number of physical records.
        # Do not invent a one-record denominator for an undecodable remainder.
        bad = None
    return records, bad


def _candidates(records, bad):
    groups = {kind: Counter() for kind in ("user", "assistant", "discarded_assistant", "tool_call")}
    counts = {kind: {"missing": 0, "candidate": 0, "unclassified": 0} for kind in groups}
    tools = Counter()
    seen_prompt = False
    uninspectable = 0
    reason_records = {"missing_id": set(), "unclassified_id": set(), "duplicate_id": set()}
    for record_index, record in enumerate(records):
        if record.get("type") != "message":
            continue
        msg = record.get("message", {})
        if not isinstance(msg, dict):
            uninspectable += 1
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        if role == "user":
            seen_prompt = True
        kind = "discarded_assistant" if role == "assistant" and not seen_prompt else role
        values = [(kind, record.get("id"))]
        if role == "assistant":
            content = msg.get("content", [])
            if not isinstance(content, list):
                uninspectable += 1
            else:
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        key = block.get("id")
                        values.append(("tool_call", key))
                        if isinstance(key, str) and key:
                            tools[key] += 1
        for kind, value in values:
            state = "missing" if value is None else "candidate" if isinstance(value, str) and _CANDIDATE.fullmatch(value) else "unclassified"
            counts[kind][state] += 1
            if state == "candidate":
                if groups[kind][value]:
                    reason_records["duplicate_id"].add(record_index)
                groups[kind][value] += 1
            else:
                reason_records["missing_id" if state == "missing" else "unclassified_id"].add(record_index)
    for kind, group in groups.items():
        counts[kind]["duplicate_groups"] = sum(n > 1 for n in group.values())
        counts[kind]["duplicate_excess"] = sum(n - 1 for n in group.values() if n > 1)
    emitted = [set(groups[k]) for k in ("user", "assistant", "tool_call")]
    return {
        "kinds": counts, "inspected_records": len(records) - uninspectable,
        "uninspectable_records": None if bad is None else bad + uninspectable,
        "partial": bad is None or bool(bad + uninspectable),
        "cross_kind_reuse": sum(sum(key in group for group in emitted) > 1 for key in set().union(*emitted)),
        "repeated_tool_groups": sum(n > 1 for n in tools.values()),
        "reason_records": {reason: len(indices) for reason, indices in reason_records.items()},
    }


def _parse_capture(data, path):
    records, bad = _decode(data)
    candidates = _candidates(records, bad)
    reasons = set()
    if bad is None or bad or any(not _valid_record(r) for r in records):
        reasons.add("malformed")
    if not records and bad == 0:
        reasons.add("empty")
    headers = [r for r in records if r.get("type") == "session"]
    # Missing cwd and explicit null agree: both mean no workspace to the parser.
    if not headers or any(not isinstance(r.get("id"), str) or not r["id"] for r in headers):
        reasons.add("invalid_session")
    elif any((r.get("id"), r.get("cwd")) != (headers[0].get("id"), headers[0].get("cwd")) for r in headers):
        reasons.add("invalid_session")
    for record in records:
        ts = record.get("timestamp")
        if record.get("type") in ("session", "message") and not ts:
            reasons.add("missing_timestamp")
        if isinstance(ts, str) and ts:
            try:
                # to_utc owns timestamp parsing. Its lowercase-z extension is not
                # admitted here; retain the original string, never its result.
                if ts.endswith("z"):
                    raise ValueError
                to_utc(ts)
            except ValueError:
                reasons.add("invalid_timestamp")
            except OverflowError:
                # Parsing succeeded, but converting an extreme year/offset to
                # UTC overflowed. This gate validates spelling, not conversion.
                pass
    if candidates["repeated_tool_groups"]:
        reasons.add("repeated_tool_id")
    parsed = None
    if not reasons:
        # Deliberately outside every decode/validation exception boundary.
        parsed = list(pi_agent._parse_records(records, path))[0]
        if not parsed.prompts:
            reasons.add("no_events")
    return parsed, candidates, reasons


def _filtered(value):
    if isinstance(value, str):
        return value in ("[binary content filtered]", "[base64 content filtered]")
    if isinstance(value, dict):
        source = value.get("source")
        return (value.get("filtered_reason") in ("binary_content", "base64_content")
                or isinstance(source, dict) and source.get("type") == "filtered"
                or any(_filtered(v) for v in value.values()))
    return isinstance(value, list) and any(_filtered(v) for v in value)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _projection(events, blocks, tools):
    """Rooted unordered siblings, ordered node blocks; None means not comparable."""
    by_id = {r["id"]: r for r in events}
    by_tool = {r["event_id"]: r for r in tools}
    if len(by_id) != len(events) or len(by_tool) != len(tools):
        return None, 0
    children = {key: [] for key in by_id}
    contents = {key: [] for key in by_id}
    roots = []
    for row in events:
        kind, parent = row["kind"], row["parent_id"]
        if kind not in _KINDS:
            return None, 0
        if kind == "prompt":
            if parent is not None:
                return None, 0
            roots.append(row["id"])
        else:
            expected = "prompt" if kind == "response" else "response"
            if parent not in by_id or by_id[parent]["kind"] != expected:
                return None, 0
            children[parent].append(row["id"])
        if (kind == "tool_call") != (row["id"] in by_tool):
            return None, 0
    try:
        indices = set()
        for block in sorted(blocks, key=lambda r: r["block_index"]):
            eid = block["event_id"]
            index = block["block_index"]
            if (eid not in by_id or by_id[eid]["kind"] == "tool_call" or type(index) is not int
                    or index < 0 or (eid, index) in indices or not isinstance(block["block_type"], str)):
                return None, 0
            indices.add((eid, index))
            value = json.loads(block["content"])
            if not _finite(value) or _filtered(value):
                return None, 0
            contents[eid].append([block["block_type"], value])
        payloads = {}
        for eid, tool in by_tool.items():
            if eid not in by_id or tool["status"] not in ("success", "error", "pending"):
                return None, 0
            if tool["result_hash"] is not None and tool["result"] is None:
                return None, 0
            value = json.loads(tool["input"]) if tool["input"] is not None else None
            result = json.loads(tool["result"]) if tool["result"] is not None else None
            if not _finite(value) or not _finite(result) or _filtered(result):
                return None, 0
            payloads[eid] = [value, result, tool["status"]]
        signatures = Counter()

        def node(eid):
            result = _canonical([by_id[eid]["kind"], contents[eid], payloads.get(eid), sorted(node(c) for c in children[eid])])
            signatures[result] += 1
            return result

        result = sorted(node(root) for root in roots)
        return result, sum(n > 1 for n in signatures.values())
    except (ValueError, TypeError, RecursionError):
        # Stored JSON/shape failures only; the shared parser is never called here.
        return None, 0


def _domain_rows(conv):
    events, blocks, tools = [], [], []

    def add(kind, obj, parent):
        eid = len(events)
        events.append({"id": eid, "kind": kind, "parent_id": parent, "external_id": obj.external_id})
        for index, block in enumerate(getattr(obj, "content", [])):
            blocks.append({"id": len(blocks), "event_id": eid, "block_index": index,
                           "block_type": block.block_type, "content": json.dumps(block.content)})
        if kind == "tool_call":
            tools.append({"event_id": eid, "input": json.dumps(obj.input), "result_hash": "present" if obj.result is not None else None,
                          "result": json.dumps(obj.result) if obj.result is not None else None, "status": obj.status})
        return eid

    for prompt in conv.prompts:
        pid = add("prompt", prompt, None)
        for response in prompt.responses:
            rid = add("response", response, pid)
            for tool in response.tool_calls:
                add("tool_call", tool, rid)
    return events, blocks, tools


def _inventory(graph):
    events = {kind: {"null_key": 0, "nonnull_key": 0, "invalid_key": 0} for kind in (*_KINDS, "other")}
    assignments = {kind: {"null_key": 0, "nonnull_key": 0} for kind in _TARGETS}
    result: dict = {"events": events, "assignments": assignments, "block_assignments": 0, "conversation_assignments": 0,
              "unknown_assignments": 0, "assignment_rows": 0, "owners": 0,
              "keyed_exposure": {kind: dict.fromkeys(_BUCKETS, 0) for kind in _TARGETS}}
    keyed = []
    for cid, data in graph.items():
        by_id = {row["id"]: row for row in data["events"]}
        block_ids = {row["id"] for row in data["blocks"]}
        for row in data["events"]:
            counts = events[row["kind"] if row["kind"] in _KINDS else "other"]
            key = row["external_id"]
            counts["null_key" if key is None else "nonnull_key"] += 1
            counts["invalid_key"] += key is not None and (not isinstance(key, str) or not key)
        result["owners"] += data["owners"]
        for row in data["assignments"]:
            result["assignment_rows"] += 1
            kind, target = row["target_kind"], row["target_id"]
            if kind == "conversation" and target == cid:
                result["conversation_assignments"] += 1
            elif kind == "block" and target in block_ids:
                result["block_assignments"] += 1
            elif kind in _TARGETS and target in by_id and by_id[target]["kind"] == ("prompt" if kind == "exchange" else kind):
                key = by_id[target]["external_id"]
                assignments[kind]["null_key" if key is None else "nonnull_key"] += 1
                if key is not None:
                    keyed.append((cid, kind, key))
            else:
                result["unknown_assignments"] += 1
    return result, keyed


def inspect_pi_identity(*, db_path: Path, source_paths: tuple[Path, ...], orphan_conversation_ids: tuple[str, ...]) -> dict[str, object]:
    """Measure only explicit disposable fixtures; no default scope, key assignment or apply."""
    paths = sorted(set(str(p) for p in source_paths))
    orphans = sorted(set(orphan_conversation_ids))
    findings = {}

    def finding(code, unit, refs=(), count=None):
        key = code, unit
        item = findings.setdefault(key, {"code": code, "unit": unit, "total": 0, "references": set()})
        item["total"] += len(refs) if count is None else count
        item["references"].update(refs)

    report: dict = {
        "report_version": 1, "status": "measured", "migration_safe": False, "limitations": _LIMITATIONS.copy(),
        "scope": {"source_input_occurrences": len(source_paths), "orphan_input_occurrences": len(orphan_conversation_ids),
                  "selected_paths": len(paths), "matched_rows": None, "valid_links": None,
                  "conversations": None, "explicit_orphans": len(orphans), "outside_references": None},
        "inventory": None, "pending": None, "sources": [], "conversations": [], "findings": [],
        "unassessed": {"sources": len(paths), "conversations": None, "keyed_assignments": None},
    }

    def finish():
        report["findings"] = [{**item, "references": sorted(item["references"])[:5]} for _, item in sorted(findings.items())]
        return report

    def refuse(code):
        report["status"] = "refused"
        # A whole-request refusal is one finding, independent of source scope.
        finding(code, "request", count=1)
        return finish()

    reason = _path_reason(db_path, database=True)
    if reason:
        return refuse("database_" + reason)
    for value in paths:
        reason = _path_reason(Path(value))
        if reason in ("invalid_path", "symlink_refused"):
            return refuse("source_" + reason)
    snapshot, reason = read_snapshot(db_path, paths, orphans)
    if reason:
        return refuse(reason)
    selected, graph = snapshot["selected"], snapshot["graph"]
    scope = report["scope"]
    scope.update(matched_rows=len(selected), valid_links=sum(r["state"] == "linked" for r in selected.values()),
                 conversations=len(graph), outside_references=sum(r["path"] not in paths for g in graph.values() for r in g["references"]))
    inventory, keyed = _inventory(graph)
    report["inventory"] = inventory
    observations = {}
    for ref, value in enumerate(paths):
        row = selected.get(value)
        state = row["state"] if row else "untracked"
        obs = {"reference": ref, "link": state, "capture": "unknown", "hash": "unknown", "graph": "unknown",
               "candidates": None, "bookkeeping_error": bool(row and row["has_error"]), "unassessed": 1}
        report["sources"].append(obs)
        reasons = set() if state == "linked" else {state}
        if obs["bookkeeping_error"]:
            reasons.add("bookkeeping_error")
        capture, parsed, candidate = None, None, None
        if state != "non_pi_source":
            capture, error = _capture(Path(value))
            if error:
                reasons.add(error)
            else:
                obs["capture"] = "captured"
                parsed, candidate, parse_reasons = _parse_capture(capture[0], Path(value))
                reasons.update(parse_reasons)
                obs["candidates"] = candidate
        observations[value] = {"obs": obs, "capture": capture, "parsed": parsed, "reasons": reasons, "rows": None, "projection": None}
        if parsed is not None:
            rows = _domain_rows(parsed)
            projection, repeated = _projection(*rows)
            observations[value].update(rows=rows, projection=projection, repeated=repeated)
    for value, item in observations.items():
        capture = item["capture"]
        obs, reasons = item["obs"], item["reasons"]
        if capture is not None:
            row = selected.get(value)
            evidence = row["file_hash"] if row else None
            if not isinstance(evidence, str) or not _HASH.fullmatch(evidence):
                obs["hash"] = "unverified"
                reasons.add("hash_unverified")
            else:
                obs["hash"] = "equal" if evidence.lower() == capture[2] else "different"
                if obs["hash"] == "different":
                    reasons.add("hash_different")
    comparable_keys = {}
    for ref, (cid, data) in enumerate(sorted(graph.items())):
        references = data["references"]
        all_selected = bool(references) and all(r["path"] in observations for r in references)
        items = [observations[r["path"]] for r in references if r["path"] in observations]
        stable = all_selected and all(i["capture"] is not None for i in items)
        multiple = "not_multiple" if len(references) < 2 else "unknown"
        if len(references) > 1 and stable:
            multiple = "identical" if len({i["capture"][0] for i in items}) == 1 else "divergent"
        stored, repeated = _projection(data["events"], data["blocks"], data["tools"])
        keys = [r["external_id"] for r in data["events"]]
        key_state = ("empty" if not keys else "invalid" if any(k is not None and (not isinstance(k, str) or not k) for k in keys)
                     else "all_null" if all(k is None for k in keys) else "all_nonnull" if all(k is not None for k in keys) else "partial")
        if key_state in ("partial", "invalid"):
            finding("partial_keys" if key_state == "partial" else "invalid_keys", "conversation", [ref])
        if not references:
            finding("orphan", "conversation", [ref])
        if repeated:
            finding("repeated_projection", "conversation", [ref])
        usable = (stable and stored is not None and not repeated and key_state != "invalid"
                  and all(i["parsed"] is not None and i["parsed"].external_id == data["external_id"]
                          and i["projection"] is not None and not i["repeated"] for i in items))
        if usable:
            comparable_keys[cid] = [{(r["kind"], r["external_id"]) for r in i["rows"][0] if r["external_id"] is not None} for i in items]
        for item in items:
            obs = item["obs"]
            if obs["link"] != "linked":
                continue
            if stored is not None and item["projection"] is not None:
                obs["graph"] = "equal" if stored == item["projection"] else "different"
        # Neither equal bytes nor equal projections establish historical authority.
        if data["events"]:
            finding("mapping_unknown", "conversation", [ref])
        if references:
            finding("authority_unknown", "conversation", [ref])
        report["conversations"].append({"reference": ref, "references": len(references),
                                         "outside_references": sum(r["path"] not in paths for r in references),
                                         "source_bytes": multiple, "event_key_state": key_state,
                                         "repeated_projection_groups": repeated, "unassessed": int(bool(data["events"]) or not usable)})
    exposures = []
    for cid, kind, key in keyed:
        parses = comparable_keys.get(cid)
        bucket = "unknown"
        if parses is not None:
            present = [("prompt" if kind == "exchange" else kind, key) in keys for keys in parses]
            bucket = "key_present_in_all_parses" if all(present) else "key_absent_from_all_parses" if not any(present) else "mixed"
        exposures.append((cid, kind, bucket))

    # Every source-derived comparison is complete. Reopen each capture once;
    # changes invalidate observations, not trigger a retry or a fresh parse.
    changed_conversations = set()
    for value, item in observations.items():
        capture = item["capture"]
        if capture is None:
            continue
        checked, error = _capture(Path(value))
        if error or checked != capture:
            item["reasons"].difference_update(("hash_different", "hash_unverified"))
            item["reasons"].add("source_changed_during_read")
            item["obs"].update(capture="changed", candidates=None, hash="unknown", graph="unknown")
            row = selected.get(value)
            if row and row["state"] == "linked":
                changed_conversations.add(row["conversation_id"])
        else:
            item["obs"]["capture"] = "stable"
    for cid, kind, bucket in exposures:
        inventory["keyed_exposure"][kind]["unknown" if cid in changed_conversations else bucket] += 1
    for ref, (cid, data) in enumerate(sorted(graph.items())):
        observation = report["conversations"][ref]
        if cid in changed_conversations:
            observation["unassessed"] = 1
            if len(data["references"]) > 1:
                observation["source_bytes"] = "unknown"
        if observation["source_bytes"] != "not_multiple":
            finding("multiple_sources_" + observation["source_bytes"], "conversation", [ref])
        comparisons = [observations[r["path"]]["obs"]["graph"] for r in data["references"] if r["path"] in observations]
        if "different" in comparisons:
            finding("graph_different", "conversation", [ref])
        if not comparisons or "unknown" in comparisons:
            finding("graph_unavailable", "conversation", [ref])
    pending: dict = {"unique_selected": 0, "ambiguous_touching_scope": 0, "unmatched_pi_attributed": 0,
               "unattributed_or_out_of_scope": 0, "groups": []}
    groups = Counter()
    for state, kind, marker in snapshot["pending"]:
        pending[state] += 1
        groups[(state, kind if kind in (*_TARGETS, "conversation") else "other",
                marker if marker in ("last_prompt", "last_response", "last_exchange", "last_tool_call") else "none" if marker is None else "other")] += 1
    pending["groups"] = [{"scope": s, "entity_type": k, "last_marker": m, "rows": n} for (s, k, m), n in sorted(groups.items())]
    report["pending"] = pending
    for state in ("ambiguous_touching_scope", "unmatched_pi_attributed", "unattributed_or_out_of_scope"):
        if pending[state]:
            finding("pending_" + state, "pending_row", count=pending[state])
    if inventory["unknown_assignments"]:
        finding("invalid_assignment_target", "assignment", count=inventory["unknown_assignments"])
    for item in observations.values():
        obs = item["obs"]
        for reason in sorted(item["reasons"]):
            finding(reason, "source", [obs["reference"]])
        candidate = obs["candidates"]
        if candidate is not None:
            for reason, count in candidate["reason_records"].items():
                if count:
                    finding(reason, "record", [obs["reference"]], count=count)
        obs["unassessed"] = int(obs["graph"] == "unknown" or obs["hash"] in ("unknown", "unverified") or candidate is None)
    report["unassessed"] = {"sources": sum(s["unassessed"] for s in report["sources"]),
                             "conversations": sum(c["unassessed"] for c in report["conversations"]),
                             "keyed_assignments": sum(c["unknown"] for c in inventory["keyed_exposure"].values())}
    if (any(report["unassessed"].values()) or inventory["unknown_assignments"] or findings
            or pending["ambiguous_touching_scope"] or pending["unmatched_pi_attributed"] or pending["unattributed_or_out_of_scope"]):
        report["status"] = "partial"
    return finish()
