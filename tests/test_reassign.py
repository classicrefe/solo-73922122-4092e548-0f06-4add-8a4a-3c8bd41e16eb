"""Reassignment (转挂) API tests.

Covers: move vs copy semantics, version precondition, the 8-observation limit,
atomicity on failure, history preservation, and serializability against
concurrent POST /fhir corrections / new references (PostgreSQL only).
"""
from __future__ import annotations

import concurrent.futures
import threading

import pytest

from tests.conftest import (
    make_bundle,
    observation_entry,
    patient_entry,
    report_entry,
    urn,
)


def _submit(client, key, bundle, expect=200):
    resp = client.post("/fhir", json=bundle, headers={"Idempotency-Key": key})
    assert resp.status_code == expect, resp.text
    return resp.json()


def _make_world(client, key, obs_count=2):
    """Create patient A (wrong), patient B (correct) and a report on A.

    Returns (patient_a, patient_b, report_id, [observation_ids]) where the
    report's result order matches the returned observation id order.
    """
    pa, _ = patient_entry(urn(), value=f"{key}-A")
    pb, _ = patient_entry(urn(), value=f"{key}-B")
    pid_a = _submit(client, f"{key}-PA", make_bundle([pa]))["resourceMap"][0]["id"]
    pid_b = _submit(client, f"{key}-PB", make_bundle([pb]))["resourceMap"][0]["id"]

    obs_urns = [urn() for _ in range(obs_count)]
    entries = [
        observation_entry(u, f"Patient/{pid_a}", code=f"C{i}", value=float(i + 1))
        for i, u in enumerate(obs_urns)
    ]
    r_urn = urn()
    entries.append(report_entry(r_urn, f"Patient/{pid_a}", obs_urns))
    receipt = _submit(client, f"{key}-R", make_bundle(entries))
    ids = {m["fullUrl"]: m["id"] for m in receipt["resourceMap"]}
    return pid_a, pid_b, ids[r_urn], [ids[u] for u in obs_urns]


def _reassign(client, report_id, version, patient_id):
    return client.post(
        f"/diagnostic-reports/{report_id}/reassign",
        json={"expectedVersion": version, "targetPatientId": patient_id},
    )


def _obs_patient(client, obs_id):
    return client.get(f"/observations/{obs_id}").json()["resource"]["subject"]["reference"]


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #

def test_reassign_moves_unshared_observations_keeping_ids(client):
    pid_a, pid_b, rid, obs_ids = _make_world(client, "MV", obs_count=2)

    resp = _reassign(client, rid, 1, pid_b)
    assert resp.status_code == 200, resp.text
    receipt = resp.json()
    assert receipt["receipt"] == "diagnostic-report-reassign"
    assert receipt["reportId"] == rid
    assert receipt["reportVersion"] == 2
    assert receipt["reportEtag"] == 'W/"2"'
    assert receipt["fromPatientId"] == pid_a
    assert receipt["toPatientId"] == pid_b
    assert receipt["observations"] == [
        {"action": "moved", "beforeId": oid, "afterId": oid, "version": 2}
        for oid in obs_ids
    ]

    # Report now belongs to patient B, still referencing the same observations.
    report = client.get(f"/diagnostic-reports/{rid}").json()
    assert report["version"] == 2
    assert report["resource"]["subject"] == {"reference": f"Patient/{pid_b}"}
    assert report["resource"]["result"] == [
        {"reference": f"Observation/{oid}"} for oid in obs_ids
    ]
    assert sorted(report["observationIds"]) == sorted(obs_ids)

    # Observations kept their ids and moved to patient B with bumped versions.
    for oid in obs_ids:
        view = client.get(f"/observations/{oid}").json()
        assert view["version"] == 2
        assert view["resource"]["subject"] == {"reference": f"Patient/{pid_b}"}

    assert client.get(f"/patients/{pid_a}/observations").json()["total"] == 0
    assert client.get(f"/patients/{pid_b}/observations").json()["total"] == 2
    assert client.get(f"/patients/{pid_a}/diagnostic-reports").json()["total"] == 0
    assert client.get(f"/patients/{pid_b}/diagnostic-reports").json()["total"] == 1


def test_reassign_copies_shared_observation(client):
    pid_a, pid_b, rid, obs_ids = _make_world(client, "SH", obs_count=2)
    shared_oid, own_oid = obs_ids

    # A second report on patient A also references the first observation.
    r2_urn = urn()
    other = _submit(
        client,
        "SH-R2",
        make_bundle([report_entry(r2_urn, f"Patient/{pid_a}", [f"Observation/{shared_oid}"])]),
    )
    rid2 = other["resourceMap"][0]["id"]

    resp = _reassign(client, rid, 1, pid_b)
    assert resp.status_code == 200, resp.text
    receipt = resp.json()
    by_before = {item["beforeId"]: item for item in receipt["observations"]}

    copy_item = by_before[shared_oid]
    assert copy_item["action"] == "copied"
    copy_id = copy_item["afterId"]
    assert copy_id != shared_oid
    assert copy_item["version"] == 1
    assert by_before[own_oid] == {
        "action": "moved",
        "beforeId": own_oid,
        "afterId": own_oid,
        "version": 2,
    }

    # This report now references the copy; the other report keeps the original.
    moved_report = client.get(f"/diagnostic-reports/{rid}").json()
    assert moved_report["resource"]["result"] == [
        {"reference": f"Observation/{copy_id}"},
        {"reference": f"Observation/{own_oid}"},
    ]
    assert sorted(moved_report["observationIds"]) == sorted([copy_id, own_oid])
    assert client.get(f"/diagnostic-reports/{rid2}").json()["observationIds"] == [
        shared_oid
    ]

    # The original observation is untouched; the copy holds the same content
    # under a new id for the target patient.
    original = client.get(f"/observations/{shared_oid}").json()
    assert original["version"] == 1
    assert original["resource"]["subject"] == {"reference": f"Patient/{pid_a}"}
    clone = client.get(f"/observations/{copy_id}").json()
    assert clone["version"] == 1
    assert clone["resource"]["subject"] == {"reference": f"Patient/{pid_b}"}
    for field in ("status", "code", "valueQuantity"):
        assert clone["resource"][field] == original["resource"][field]

    assert client.get(f"/patients/{pid_a}/observations").json()["total"] == 1
    assert client.get(f"/patients/{pid_b}/observations").json()["total"] == 2


def test_reassign_accepts_etag_string_version(client):
    _, pid_b, rid, _ = _make_world(client, "ETAG", obs_count=1)
    resp = _reassign(client, rid, 'W/"1"', pid_b)
    assert resp.status_code == 200, resp.text


def test_reassign_history_remains_queryable(client):
    pid_a, pid_b, rid, obs_ids = _make_world(client, "HIST", obs_count=1)
    assert _reassign(client, rid, 1, pid_b).status_code == 200

    report_history = client.get(f"/diagnostic-reports/{rid}/_history").json()
    assert [e["version"] for e in report_history["entry"]] == [1, 2]
    # Version 1 still shows the wrong patient; version 2 the corrected one.
    assert report_history["entry"][0]["resource"]["subject"] == {
        "reference": f"Patient/{pid_a}"
    }
    assert report_history["entry"][1]["resource"]["subject"] == {
        "reference": f"Patient/{pid_b}"
    }

    obs_history = client.get(f"/observations/{obs_ids[0]}/_history").json()
    assert [e["version"] for e in obs_history["entry"]] == [1, 2]
    assert obs_history["entry"][0]["resource"]["subject"] == {
        "reference": f"Patient/{pid_a}"
    }
    assert obs_history["entry"][1]["resource"]["subject"] == {
        "reference": f"Patient/{pid_b}"
    }


# --------------------------------------------------------------------------- #
# Rejections and atomicity
# --------------------------------------------------------------------------- #

def _assert_world_unchanged(client, pid_a, pid_b, rid, obs_ids, report_version=1):
    report = client.get(f"/diagnostic-reports/{rid}").json()
    assert report["version"] == report_version
    assert sorted(report["observationIds"]) == sorted(obs_ids)
    assert report["resource"]["subject"] == {"reference": f"Patient/{pid_a}"}
    assert client.get(f"/patients/{pid_a}/observations").json()["total"] == len(obs_ids)
    assert client.get(f"/patients/{pid_b}/observations").json()["total"] == 0
    assert client.get(f"/patients/{pid_b}/diagnostic-reports").json()["total"] == 0
    for oid in obs_ids:
        assert client.get(f"/observations/{oid}").json()["version"] == 1
        assert client.get(f"/observations/{oid}/_history").json()["total"] == 1
    assert client.get(f"/diagnostic-reports/{rid}/_history").json()["total"] == report_version


def test_reassign_stale_version_rejected_without_side_effects(client):
    pid_a, pid_b, rid, obs_ids = _make_world(client, "STALE", obs_count=2)
    # Share one observation so a buggy implementation would leave a copy behind.
    _submit(
        client,
        "STALE-R2",
        make_bundle(
            [report_entry(urn(), f"Patient/{pid_a}", [f"Observation/{obs_ids[0]}"])]
        ),
    )

    resp = _reassign(client, rid, 99, pid_b)
    assert resp.status_code == 412, resp.text
    assert resp.json()["resourceType"] == "OperationOutcome"

    # No copies, no new versions, no link changes.
    _assert_world_unchanged(client, pid_a, pid_b, rid, obs_ids)


def test_reassign_retry_after_success_is_stale(client):
    _, pid_b, rid, _ = _make_world(client, "TWICE", obs_count=1)
    assert _reassign(client, rid, 1, pid_b).status_code == 200
    resp = _reassign(client, rid, 1, pid_b)
    assert resp.status_code == 412


def test_reassign_observation_limit(client):
    # 9 observations -> rejected; 8 -> accepted.
    pid_a, pid_b, rid9, obs9 = _make_world(client, "LIM9", obs_count=9)
    resp = _reassign(client, rid9, 1, pid_b)
    assert resp.status_code == 422, resp.text
    _assert_world_unchanged(client, pid_a, pid_b, rid9, obs9)

    _, pid_b2, rid8, obs8 = _make_world(client, "LIM8", obs_count=8)
    resp = _reassign(client, rid8, 1, pid_b2)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["observations"]) == 8


def test_reassign_not_found_and_same_patient(client):
    pid_a, pid_b, rid, _ = _make_world(client, "NF", obs_count=1)

    resp = _reassign(client, "no-such-report", 1, pid_b)
    assert resp.status_code == 404

    resp = _reassign(client, rid, 1, "no-such-patient")
    assert resp.status_code == 404

    resp = _reassign(client, rid, 1, pid_a)
    assert resp.status_code == 422


def test_reassign_body_validation(client):
    _, pid_b, rid, _ = _make_world(client, "BODY", obs_count=1)

    resp = client.post(
        f"/diagnostic-reports/{rid}/reassign", json={"targetPatientId": pid_b}
    )
    assert resp.status_code == 422
    assert resp.json()["resourceType"] == "OperationOutcome"

    resp = client.post(
        f"/diagnostic-reports/{rid}/reassign", json={"expectedVersion": 1}
    )
    assert resp.status_code == 422

    resp = _reassign(client, rid, "not-a-version", pid_b)
    assert resp.status_code == 400

    resp = _reassign(client, rid, 0, pid_b)
    assert resp.status_code == 400


def test_reassign_endpoint_in_openapi(client):
    spec = client.get("/openapi.json").json()
    assert "/diagnostic-reports/{report_id}/reassign" in spec["paths"]


# --------------------------------------------------------------------------- #
# Concurrency (PostgreSQL only): results must equal some sequential execution
# --------------------------------------------------------------------------- #

def _require_postgres():
    from app import database

    if database.engine.dialect.name == "sqlite":
        pytest.skip("concurrency test requires PostgreSQL")


def test_concurrent_reassign_same_report_only_one_wins(client):
    _require_postgres()
    _, pid_b, rid, _ = _make_world(client, "CC3", obs_count=2)
    barrier = threading.Barrier(4)

    def do_reassign(_):
        barrier.wait()
        return client.post(
            f"/diagnostic-reports/{rid}/reassign",
            json={"expectedVersion": 1, "targetPatientId": pid_b},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(do_reassign, range(4)))

    codes = sorted(r.status_code for r in responses)
    assert codes[0] == 200, [r.text for r in responses]
    # Losers see the version bump (412) or a lock conflict (409); no 5xx.
    assert all(c in (409, 412) for c in codes[1:]), codes
    assert client.get(f"/diagnostic-reports/{rid}").json()["version"] == 2


def test_concurrent_reassign_and_observation_correction(client):
    _require_postgres()
    pid_a, pid_b, rid, obs_ids = _make_world(client, "CC1", obs_count=1)
    oid = obs_ids[0]
    barrier = threading.Barrier(2)

    def do_reassign():
        barrier.wait()
        return client.post(
            f"/diagnostic-reports/{rid}/reassign",
            json={"expectedVersion": 1, "targetPatientId": pid_b},
        )

    def do_correction():
        barrier.wait()
        body = {
            "resource": {
                "resourceType": "Observation",
                "id": oid,
                "status": "final",
                "code": {"coding": [{"code": "C0"}]},
                "subject": {"reference": f"Patient/{pid_a}"},
                "valueQuantity": {"value": 7.7, "unit": "mmol/L"},
            },
            "request": {"method": "PUT", "url": f"Observation/{oid}", "ifMatch": 'W/"1"'},
        }
        return client.post(
            "/fhir", json=make_bundle([body]), headers={"Idempotency-Key": "CC1-FIX"}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        reassign_resp, fix_resp = pool.map(lambda f: f(), [do_reassign, do_correction])

    assert reassign_resp.status_code in (200, 409), reassign_resp.text
    assert fix_resp.status_code in (200, 412), fix_resp.text
    corrected_first = fix_resp.status_code == 200

    if reassign_resp.status_code == 409:
        # Deadlock victim: retry must succeed against the committed state.
        retry = _reassign(client, rid, 1, pid_b)
        assert retry.status_code == 200, retry.text
    else:
        receipt = reassign_resp.json()
        assert receipt["observations"][0]["action"] == "moved"

    # Final state equals one of the two sequential executions:
    # fix-then-reassign (value 7.7 travels to patient B) or
    # reassign-then-fix (fix rejected stale, original value moved).
    obs = client.get(f"/observations/{oid}").json()
    assert obs["resource"]["subject"] == {"reference": f"Patient/{pid_b}"}
    assert client.get(f"/diagnostic-reports/{rid}").json()["version"] == 2
    if corrected_first:
        assert obs["resource"]["valueQuantity"]["value"] == 7.7
        assert obs["version"] == 3  # create + correction + reassign move
    else:
        assert obs["resource"]["valueQuantity"]["value"] == 1.0
        assert obs["version"] == 2  # create + reassign move


def test_concurrent_reassign_and_new_reference(client):
    _require_postgres()
    pid_a, pid_b, rid, obs_ids = _make_world(client, "CC2", obs_count=1)
    oid = obs_ids[0]
    barrier = threading.Barrier(2)

    def do_reassign():
        barrier.wait()
        return client.post(
            f"/diagnostic-reports/{rid}/reassign",
            json={"expectedVersion": 1, "targetPatientId": pid_b},
        )

    def add_reference():
        barrier.wait()
        r_urn = urn()
        entry = report_entry(r_urn, f"Patient/{pid_a}", [f"Observation/{oid}"])
        return client.post(
            "/fhir", json=make_bundle([entry]), headers={"Idempotency-Key": "CC2-REF"}
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        reassign_resp, ref_resp = pool.map(lambda f: f(), [do_reassign, add_reference])

    assert reassign_resp.status_code in (200, 409), reassign_resp.text
    assert ref_resp.status_code in (200, 422), ref_resp.text

    if reassign_resp.status_code == 409:
        retry = _reassign(client, rid, 1, pid_b)
        assert retry.status_code == 200, retry.text
        reassign_resp = retry

    action = reassign_resp.json()["observations"][0]["action"]
    if ref_resp.status_code == 200:
        # The new report committed first, so the observation was shared and
        # had to be copied; the original stays with patient A.
        assert action == "copied"
        assert _obs_patient(client, oid) == f"Patient/{pid_a}"
    else:
        # Reassign moved the observation first; the new reference then broke
        # patient alignment and was rejected.
        assert action == "moved"
        assert _obs_patient(client, oid) == f"Patient/{pid_b}"

    # Invariant of a serial execution: every report shares a patient with all
    # of its observations.
    for pid in (pid_a, pid_b):
        for entry in client.get(f"/patients/{pid}/diagnostic-reports").json()["entry"]:
            for obs_id in entry["observationIds"]:
                assert _obs_patient(client, obs_id) == f"Patient/{pid}"
