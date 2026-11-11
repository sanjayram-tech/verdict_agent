from tyn.adf import adf_to_text, text_to_adf
from tyn.importer import (description_text, expected_link_pairs, issue_fields, ordered_comments, record_labels,
                          sync)
from tyn.text import parse_comment, sort_key

from .conftest import PROJECT, FakeJira


def test_labels_and_fields(dataset):
    req = next(r for r in dataset["records"] if r["source_id"] == "REQ-08")
    labels = record_labels(req, dataset["dataset_label"])
    assert labels == ["tyn-release-assessment-v2", "src-REQ-08", "kind-requirement", "release-R2.4",
                      "recorded-status-Done", "rr-requirement"]
    f = issue_fields(req, PROJECT, "Task", dataset["dataset_label"])
    assert f["summary"].startswith("[REQ-08] ")
    desc = adf_to_text(f["description"])
    assert "Links to: REC-4076" in desc and desc.endswith(req["description"])


def test_adf_round_trip_is_verbatim(dataset):
    for r in dataset["records"]:
        assert adf_to_text(text_to_adf(description_text(r))) == description_text(r)
        for c in r["comments"]:
            assert adf_to_text(text_to_adf(c)) == c


def test_comments_ordered_by_recorded_time(dataset):
    for r in dataset["records"]:
        times = [sort_key(parse_comment(c)["recorded_at"]) for c in ordered_comments(r)]
        assert times == sorted(times)


def test_link_count(dataset):
    assert len(expected_link_pairs(dataset["records"])) == 76


def test_full_import_counts(dataset, jira):
    assert jira.writes == {"issues": 400, "comments": 2059, "links": 76}
    labelled = [k for k, i in jira.issues.items() if "tyn-release-assessment-v2" in i["fields"]["labels"]]
    assert len(labelled) == 400


def test_import_is_idempotent(dataset, jira):
    before = dict(jira.writes)
    created = sync(jira, dataset, PROJECT, workers=1)
    assert created == {"issues": 0, "comments": 0, "links": 0}
    assert jira.writes == before


def test_import_resumes_partial_state(dataset):
    fj = FakeJira()
    first = dataset["records"][:50]
    ids = {r["source_id"] for r in first}
    partial = {**dataset, "records": [{**r, "links": [l for l in r["links"] if l in ids]} for r in first]}
    sync(fj, partial, PROJECT, workers=1)
    # simulate a crash that lost some comments on one issue
    key = next(k for k, i in fj.issues.items() if len(i["comments"]) > 3)
    lost = fj.issues[key]["comments"].pop()
    created = sync(fj, dataset, PROJECT, workers=1)
    assert created["issues"] == 350
    assert fj.writes["issues"] == 400 and fj.writes["comments"] == 2059 + 1  # only the lost one re-posted
    assert adf_to_text(fj.issues[key]["comments"][-1]["body"]) == adf_to_text(lost["body"])
