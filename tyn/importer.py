"""Repeatable, idempotent importer: dataset.json -> Jira Cloud project.

Usage:
  python -m tyn.importer import --dataset inputs/dataset.json [--project TYN] [--dry-run]
  python -m tyn.importer verify --dataset inputs/dataset.json [--project TYN]

--project defaults to JIRA_PROJECT.

Pass 1 creates one issue per record (bulk, 25 per call), pass 2 posts comments
verbatim in recorded-time order, pass 3 creates "Relates" links. Every pass first
reads what already exists in Jira, so re-running creates nothing new.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .adf import adf_to_text, text_to_adf
from .config import JiraConfig, default_project
from .jira_client import JiraError, JiraReader, JiraWriter
from .text import parse_comment, sort_key

REQUIREMENT_LABEL = "rr-requirement"


# ------------------------------------------------------------------ pure helpers
def load_dataset(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("dataset_label", "target", "records"):
        if key not in data:
            raise ValueError(f"dataset is missing '{key}'")
    ids = [r["source_id"] for r in data["records"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source_id values in dataset")
    known = set(ids)
    for r in data["records"]:
        for link in r.get("links", []):
            if link not in known:
                raise ValueError(f"{r['source_id']} links to unknown record {link}")
    return data


def slug(value: str) -> str:
    return value.strip().replace(" ", "-")


def src_label(source_id: str) -> str:
    return f"src-{source_id}"


def record_labels(record: dict[str, Any], dataset_label: str) -> list[str]:
    labels = [
        dataset_label,
        src_label(record["source_id"]),
        f"kind-{slug(record['kind'])}",
        f"release-{slug(record['release'])}",
        f"recorded-status-{slug(record['recorded_status'])}",
    ]
    if record["kind"] == "requirement":
        labels.append(REQUIREMENT_LABEL)
    return labels


def description_text(record: dict[str, Any]) -> str:
    header = [
        f"Source ID: {record['source_id']}",
        f"Kind: {record['kind']}",
        f"Release: {record['release']}",
        f"Recorded status: {record['recorded_status']}",
        f"Links to: {', '.join(record.get('links', [])) or '(none)'}",
        "Imported record. Native Jira author/timestamps/status reflect the importer, not the scenario.",
        "---",
    ]
    return "\n".join(header) + "\n" + record["description"]


def issue_summary(record: dict[str, Any]) -> str:
    return f"[{record['source_id']}] {record['summary']}"[:255]


def issue_fields(record: dict[str, Any], project: str, issue_type: str, dataset_label: str) -> dict[str, Any]:
    return {
        "project": {"key": project},
        "issuetype": {"name": issue_type},
        "summary": issue_summary(record),
        "description": text_to_adf(description_text(record)),
        "labels": record_labels(record, dataset_label),
    }


def ordered_comments(record: dict[str, Any]) -> list[str]:
    """Comments verbatim, ordered by their recorded event time (stable for ties)."""
    return sorted(record.get("comments", []), key=lambda c: sort_key(parse_comment(c)["recorded_at"]))


def expected_link_pairs(records: list[dict[str, Any]]) -> set[frozenset[str]]:
    return {frozenset((r["source_id"], l)) for r in records for l in r.get("links", [])}


def source_id_from_labels(labels: list[str]) -> str | None:
    for lab in labels:
        if lab.startswith("src-"):
            return lab[4:]
    return None


def scope_jql(project: str, dataset_label: str) -> str:
    return f'project = "{project}" AND labels = "{dataset_label}"'


BATCH_SIZE = 25  # issues per bulk-create call (smaller batches finish well inside the timeout)


class DuplicateIssuesError(RuntimeError):
    pass


# ------------------------------------------------------------------ Jira state
def key_number(key: str) -> int:
    return int(key.rsplit("-", 1)[1])


def scan_imported(jira: JiraReader, project: str, dataset_label: str) -> dict[str, list[dict[str, Any]]]:
    """source_id -> list of {key, links:set(keys)} (normally one per source id)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for issue in jira.search(scope_jql(project, dataset_label) + " ORDER BY key ASC",
                             ["labels", "issuelinks"]):
        sid = source_id_from_labels(issue["fields"].get("labels", []))
        if not sid:
            continue
        linked = set()
        for link in issue["fields"].get("issuelinks", []) or []:
            other = link.get("outwardIssue") or link.get("inwardIssue")
            if other:
                linked.add(other["key"])
        out.setdefault(sid, []).append({"key": issue["key"], "links": linked})
    return out


def existing_issues(jira: JiraReader, project: str, dataset_label: str) -> dict[str, dict[str, Any]]:
    """source_id -> {key, links:set(keys)} for issues already imported. Refuses duplicates."""
    scanned = scan_imported(jira, project, dataset_label)
    dups = {sid: [i["key"] for i in v] for sid, v in scanned.items() if len(v) > 1}
    if dups:
        sample = ", ".join(f"{s}: {k}" for s, k in list(dups.items())[:3])
        raise DuplicateIssuesError(
            f"{len(dups)} source ids have more than one Jira issue (e.g. {sample}). Run "
            f"'python -m tyn.importer dedupe --dataset <file> --project {project}' to review, then add --apply.")
    return {sid: v[0] for sid, v in scanned.items()}


def plan_dedupe(jira: JiraReader, project: str, dataset_label: str) -> list[dict[str, Any]]:
    """For each duplicated source id, keep the copy with the most comments+links (lowest key on ties)."""
    plan = []
    for sid, copies in scan_imported(jira, project, dataset_label).items():
        if len(copies) < 2:
            continue
        scored = []
        for c in copies:
            n_comments = len(jira.get_comments(c["key"]))
            scored.append((-(n_comments + len(c["links"])), key_number(c["key"]), c["key"], n_comments, len(c["links"])))
        scored.sort()
        keep = scored[0][2]
        plan.append({"source_id": sid, "keep": keep,
                     "delete": [s[2] for s in scored[1:]],
                     "detail": {s[2]: {"comments": s[3], "links": s[4]} for s in scored}})
    return sorted(plan, key=lambda p: key_number(p["keep"]))


# ------------------------------------------------------------------ import
def run_import(data: dict[str, Any], project: str, dry_run: bool, workers: int = 4) -> int:
    records = data["records"]
    label = data["dataset_label"]
    n_comments = sum(len(r.get("comments", [])) for r in records)
    pairs = expected_link_pairs(records)
    print(f"Dataset: {len(records)} records, {n_comments} comments, {len(pairs)} links, label '{label}'")
    if dry_run:
        sample = issue_fields(records[0], project, "Task", label)
        print("Dry run - no network calls. Sample issue payload:")
        print(json.dumps({**sample, "description": description_text(records[0])[:400] + "..."}, indent=2))
        print(f"Would create up to {len(records)} issues, {n_comments} comments, {len(pairs)} links.")
        return 0

    cfg = JiraConfig.from_env()
    jira = JiraWriter(cfg, on_retry=lambda p, a, w: print(f"  retry {a} for {p} in {w:.1f}s"))
    me = jira.myself()
    proj = jira.project(project)
    print(f"Connected as account {me.get('accountId', '?')[:8]}… to project {proj['key']} ({proj.get('name')})")
    t0 = time.time()
    created = sync(jira, data, project, cfg.issue_type, cfg.link_type, workers)
    print(f"Import finished in {time.time() - t0:.1f}s. Created {created['issues']} issues, "
          f"{created['comments']} comments, {created['links']} links. Run 'verify' next.")
    return 0


def sync(jira: Any, data: dict[str, Any], project: str, issue_type: str = "Task", link_type: str = "Relates",
         workers: int = 4) -> dict[str, int]:
    """Bring Jira in line with the dataset, creating only what is missing."""
    records = data["records"]
    label = data["dataset_label"]
    pairs = expected_link_pairs(records)
    # ---- pass 1: issues
    have = existing_issues(jira, project, label)
    missing = [r for r in records if r["source_id"] not in have]
    print(f"Pass 1: {len(have)} issues already exist, creating {len(missing)}")
    new_ids: set[str] = set()
    for i in range(0, len(missing), BATCH_SIZE):
        batch = missing[i:i + BATCH_SIZE]
        created = jira.create_issues_bulk([issue_fields(r, project, issue_type, label) for r in batch])
        if len(created) != len(batch):
            raise RuntimeError(f"bulk create returned {len(created)} issues for {len(batch)} requested")
        for rec, iss in zip(batch, created):
            have[rec["source_id"]] = {"key": iss["key"], "links": set()}
            new_ids.add(rec["source_id"])
        print(f"  created {min(i + BATCH_SIZE, len(missing))}/{len(missing)}")
    key_of = {sid: v["key"] for sid, v in have.items()}

    # ---- pass 2: comments
    def sync_comments(rec: dict[str, Any]) -> int:
        key = key_of[rec["source_id"]]
        wanted = ordered_comments(rec)
        if not wanted:
            return 0
        present: list[str] = []
        if rec["source_id"] not in new_ids:
            present = [adf_to_text(c.get("body")).strip() for c in jira.get_comments(key)]
        posted = 0
        remaining = list(present)
        for body in wanted:
            if body.strip() in remaining:
                remaining.remove(body.strip())  # multiset match: duplicates are respected
                continue
            jira.add_comment(key, text_to_adf(body))
            posted += 1
        return posted

    print("Pass 2: syncing comments")
    posted_total = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(sync_comments, r): r["source_id"] for r in records}
        for n, fut in enumerate(as_completed(futures), 1):
            posted_total += fut.result()
            if n % 50 == 0:
                print(f"  {n}/{len(records)} issues checked, {posted_total} comments posted")
    print(f"  posted {posted_total} comments")

    # ---- pass 3: links
    existing_pairs = set()
    for sid, v in have.items():
        for other_key in v["links"]:
            existing_pairs.add(frozenset((v["key"], other_key)))
    todo = []
    for r in records:
        for target in r.get("links", []):
            pair = frozenset((key_of[r["source_id"]], key_of[target]))
            if pair not in existing_pairs:
                existing_pairs.add(pair)
                todo.append((key_of[r["source_id"]], key_of[target]))
    print(f"Pass 3: creating {len(todo)} links ({len(pairs) - len(todo)} already present)")
    for a, b in todo:
        jira.create_link(link_type, a, b)
    return {"issues": len(missing), "comments": posted_total, "links": len(todo)}


# ------------------------------------------------------------------ verify
def run_verify(data: dict[str, Any], project: str, wait_s: int = 120, workers: int = 6) -> int:
    records = data["records"]
    label = data["dataset_label"]
    jira = JiraReader(JiraConfig.from_env())
    ok = True

    # Search index can lag right after import: wait until all issues are searchable.
    deadline = time.time() + wait_s
    while True:
        have = existing_issues(jira, project, label)
        if len(have) >= len(records) or time.time() > deadline:
            break
        print(f"  search shows {len(have)}/{len(records)} issues, waiting for index…")
        time.sleep(10)
    print(f"Issues: {len(have)} / expected {len(records)}")
    missing = [r["source_id"] for r in records if r["source_id"] not in have]
    if missing:
        ok = False
        print(f"  MISSING: {missing[:10]}")

    key_of = {sid: v["key"] for sid, v in have.items()}

    def count(rec: dict[str, Any]) -> tuple[str, int, int]:
        key = key_of.get(rec["source_id"])
        got = len(jira.get_comments(key)) if key else 0
        return rec["source_id"], got, len(rec.get("comments", []))

    total_got = total_want = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for sid, got, want in pool.map(count, records):
            total_got += got
            total_want += want
            if got != want:
                ok = False
                print(f"  comment count mismatch on {sid}: {got} vs {want}")
    print(f"Comments: {total_got} / expected {total_want}")

    want_pairs = {frozenset(key_of[s] for s in p) for p in expected_link_pairs(records) if all(s in key_of for s in p)}
    got_pairs = {frozenset((v["key"], o)) for v in have.values() for o in v["links"]}
    print(f"Links: {len(got_pairs & want_pairs)} / expected {len(expected_link_pairs(records))}"
          f" (unexpected extra: {len(got_pairs - want_pairs)})")
    if want_pairs - got_pairs or len(want_pairs) != len(expected_link_pairs(records)):
        ok = False

    # Text search sanity check: the agent relies on comment text being indexed. The probe is
    # chosen from the dataset itself (a token unique to one record's comments), not hard-coded.
    probe = pick_search_probe(records)
    if probe is None:
        print("Text-search probe: skipped (no comment token unique to a single record)")
    else:
        token, expected_sid = probe
        found: list[str | None] = []
        for attempt in range(4):  # comment text can be indexed a little after the issue itself
            hits = jira.search(scope_jql(project, label) + f' AND text ~ "\\"{token}\\""', ["labels"], 10)
            found = [source_id_from_labels(i["fields"]["labels"]) for i in hits]
            if expected_sid in found or attempt == 3:
                break
            time.sleep(10)
        print(f"Text-search probe for '{token}': {found} (expected {expected_sid})")
        if expected_sid not in found:
            ok = False
            print("  comment text is not searchable yet; wait a minute and run verify again")
    print("VERIFY OK" if ok else "VERIFY FAILED")
    return 0 if ok else 1


_PROBE_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")  # run/defect/ticket-style identifiers


def pick_search_probe(records: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Pick an identifier-like token that occurs in exactly one record (anywhere in its text)
    and in that record's comments, so a phrase search must return exactly that record."""
    owners: dict[str, set[str]] = {}
    in_comments: dict[str, str] = {}
    for r in records:
        texts = [r.get("summary", ""), r.get("description", "")] + list(r.get("comments", []))
        for t in texts:
            for tok in _PROBE_TOKEN.findall(t):
                owners.setdefault(tok, set()).add(r["source_id"])
        for c in r.get("comments", []):
            for tok in _PROBE_TOKEN.findall(c.partition("\n")[2]):  # comment body, not the header line
                in_comments.setdefault(tok, r["source_id"])
    ids = {r["source_id"] for r in records}
    candidates = sorted(tok for tok, sid in in_comments.items()
                        if owners.get(tok) == {sid} and tok not in ids)
    return (candidates[0], in_comments[candidates[0]]) if candidates else None


def run_dedupe(data: dict[str, Any], project: str, apply: bool) -> int:
    """List (and with --apply, permanently delete) duplicate copies of imported issues.

    Only issues carrying the dataset label AND a src-* label are considered; for each
    source id the copy with the most comments/links (then lowest key) is kept."""
    label = data["dataset_label"]
    jira = JiraWriter(JiraConfig.from_env())
    plan = plan_dedupe(jira, project, label)
    n_delete = sum(len(p["delete"]) for p in plan)
    if not plan:
        print("No duplicate imported issues found.")
        return 0
    print(f"{len(plan)} source ids are duplicated; {n_delete} extra issues would be deleted:")
    for p in plan:
        print(f"  {p['source_id']:10} keep {p['keep']:9} delete {', '.join(p['delete'])}")
    if not apply:
        print("\nNothing deleted. Re-run with --apply to permanently delete the extra copies listed above.")
        return 0
    print(f"\nDeleting {n_delete} issues (permanent)…")
    for p in plan:
        for key in p["delete"]:
            jira.delete_issue(key)
    print(f"Deleted {n_delete} duplicate issues.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tyn.importer", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["import", "verify", "dedupe"])
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--project", default=None, help="Jira project key, e.g. TYN (default: JIRA_PROJECT)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true", help="dedupe: actually delete the listed duplicates")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    args.project = args.project or default_project()
    if not args.project:
        ap.error("--project is required (or set JIRA_PROJECT)")
    data = load_dataset(args.dataset)
    try:
        if args.command == "import":
            return run_import(data, args.project, args.dry_run, args.workers)
        if args.command == "dedupe":
            return run_dedupe(data, args.project, args.apply)
        return run_verify(data, args.project)
    except JiraError as exc:
        print(f"Jira error: {exc}", file=sys.stderr)
        return 2
    except DuplicateIssuesError as exc:
        print(f"Import stopped: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
