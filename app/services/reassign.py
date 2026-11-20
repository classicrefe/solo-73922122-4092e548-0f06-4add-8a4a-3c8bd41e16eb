"""Transactional reassignment (转挂) of a misfiled DiagnosticReport.

A data clerk submits the report id, the version seen while verifying the
mistake, and the correct patient. The whole correction commits in one
transaction:

* observations referenced only by this report keep their ids and move with
  the report to the target patient;
* observations also referenced by other reports are copied (current content)
  for the target patient, this report is re-pointed at the copy, and the
  other reports keep referencing the original;
* the report body, its observation links and the new resource versions are
  written together, and the append-only history stays queryable;
* a stale report version rejects the request (412) and any failure rolls
  back every change, so no copies, versions or link edits survive.

Concurrency: the report's observations are locked (FOR UPDATE, ascending id
order) before the report row itself — the same order the bundle pipeline
uses — so a concurrent POST /fhir correcting these observations or adding
references to them serializes against this transaction, and the committed
result equals some sequential execution. If PostgreSQL aborts the
reassignment (deadlock / lock timeout / serialization failure) the caller
gets 409 and may simply retry.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import (
    FhirError,
    conflict,
    not_found,
    precondition_failed,
    unprocessable,
)
from app.fhir_utils import etag
from app.models import (
    DiagnosticReport,
    Observation,
    Patient,
    ReportLink,
    ResourceVersion,
)
from app.services.bundle import _locked

# deadlock_detected | lock_not_available | serialization_failure
_LOCK_FAILURE_PGCODES = {"40P01", "55P03", "40001"}


def reassign_report(
    session: Session,
    report_id: str,
    expected_version: int,
    target_patient_id: str,
) -> dict:
    """Move DiagnosticReport/<report_id> and its observations to the patient.

    Returns the receipt listing every observation's before/after ids.
    """
    try:
        return _reassign(session, report_id, expected_version, target_patient_id)
    except FhirError:
        # Roll back inside the service (like process_bundle does) so row
        # locks are released before the error response is sent, not only
        # when the request's session dependency cleans up afterwards.
        session.rollback()
        raise
    except OperationalError as exc:
        session.rollback()
        if _pgcode(exc) in _LOCK_FAILURE_PGCODES:
            raise conflict(
                "Reassignment raced a concurrent change and was aborted by the "
                "database; re-read the report and retry."
            )
        raise
    except IntegrityError:
        session.rollback()
        raise conflict(
            "Reassignment conflicted with a concurrent change; re-read the "
            "report and retry."
        )


def _pgcode(exc: OperationalError) -> str | None:
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def _reassign(
    session: Session,
    report_id: str,
    expected_version: int,
    target_patient_id: str,
) -> dict:
    # Patients are insert-only in this store (no update/delete paths), so a
    # plain read of the target row is stable and needs no lock.
    patient = session.get(Patient, target_patient_id)
    if patient is None:
        raise not_found(f"Patient/{target_patient_id} does not exist.")

    link_ids = _linked_observation_ids(session, report_id)
    _enforce_observation_limit(report_id, len(link_ids))

    # Lock observations first (ascending id order), then the report — the
    # same order the bundle pipeline takes them, so the two serialize
    # instead of deadlocking in the common interleavings.
    observations: dict[str, Observation] = {}
    for oid in sorted(link_ids):
        obs = _locked(session, Observation, oid)
        if obs is None:  # pragma: no cover - FK guarantees existence
            raise conflict(f"Observation/{oid} vanished concurrently; retry.")
        observations[oid] = obs

    report = _locked(session, DiagnosticReport, report_id)
    if report is None:
        raise not_found(f"DiagnosticReport/{report_id} does not exist.")

    if report.version != expected_version:
        raise precondition_failed(
            f"Report version {expected_version} is stale; current version is "
            f"{report.version}. Re-read DiagnosticReport/{report_id} and retry."
        )
    if report.patient_id == target_patient_id:
        raise unprocessable(
            f"DiagnosticReport/{report_id} already belongs to "
            f"Patient/{target_patient_id}."
        )

    # The report row is locked now, so its link set can no longer change
    # under us; re-read it to catch any change committed before the lock.
    current_ids = _linked_observation_ids(session, report_id)
    if set(current_ids) != set(link_ids):
        raise conflict(
            "The report's observation set changed concurrently; re-read the "
            "report and retry."
        )
    _enforce_observation_limit(report_id, len(current_ids))

    # Observations referenced by other reports must be copied, not moved.
    # Concurrent bundles cannot add new references to these observations
    # while we hold their row locks, so this classification is stable.
    shared_ids = set(
        session.execute(
            select(ReportLink.observation_id).where(
                ReportLink.report_id != report_id,
                ReportLink.observation_id.in_(current_ids),
            )
        ).scalars().all()
    )

    now = datetime.now(timezone.utc).isoformat()
    receipt_items: list[dict] = []
    new_result_ids: list[str] = []
    for oid in _result_order(report, current_ids):
        obs = observations[oid]
        if oid in shared_ids:
            clone = _copy_observation(session, obs, target_patient_id, now)
            new_result_ids.append(clone.id)
            receipt_items.append(
                {
                    "action": "copied",
                    "beforeId": oid,
                    "afterId": clone.id,
                    "version": 1,
                }
            )
        else:
            new_version = _move_observation(session, obs, target_patient_id, now)
            new_result_ids.append(oid)
            receipt_items.append(
                {
                    "action": "moved",
                    "beforeId": oid,
                    "afterId": oid,
                    "version": new_version,
                }
            )

    from_patient_id = report.patient_id
    new_report_version = report.version + 1
    resource = copy.deepcopy(report.resource) if isinstance(report.resource, dict) else {}
    resource["id"] = report.id
    resource["subject"] = {"reference": f"Patient/{target_patient_id}"}
    resource["result"] = [
        {"reference": f"Observation/{oid}"} for oid in new_result_ids
    ]
    _stamp(resource, new_report_version, now)
    report.resource = resource
    report.patient_id = target_patient_id
    report.version = new_report_version
    report.status = "updated"

    # Replace the report's links with the corrected reference set.
    old_links = session.execute(
        select(ReportLink).where(ReportLink.report_id == report_id)
    ).scalars().all()
    for link in old_links:
        session.delete(link)
    session.flush()
    for oid in new_result_ids:
        session.add(ReportLink(report_id=report_id, observation_id=oid))

    session.add(
        ResourceVersion(
            resource_type="DiagnosticReport",
            resource_id=report_id,
            version=new_report_version,
            status="updated",
            resource=resource,
        )
    )

    receipt = {
        "receipt": "diagnostic-report-reassign",
        "reportId": report_id,
        "reportVersion": new_report_version,
        "reportEtag": etag(new_report_version),
        "fromPatientId": from_patient_id,
        "toPatientId": target_patient_id,
        "observations": receipt_items,
    }
    session.commit()
    return receipt


def _linked_observation_ids(session: Session, report_id: str) -> list[str]:
    return list(
        session.execute(
            select(ReportLink.observation_id).where(ReportLink.report_id == report_id)
        )
        .scalars()
        .all()
    )


def _enforce_observation_limit(report_id: str, count: int) -> None:
    if count > settings.MAX_REASSIGN_OBSERVATIONS:
        raise unprocessable(
            f"DiagnosticReport/{report_id} references {count} observations; "
            f"reassignment accepts at most {settings.MAX_REASSIGN_OBSERVATIONS}."
        )


def _result_order(report: DiagnosticReport, obs_ids: list[str]) -> list[str]:
    """Observation ids in the report's stored result order (stable receipt)."""
    remaining = set(obs_ids)
    ordered: list[str] = []
    result = report.resource.get("result") if isinstance(report.resource, dict) else None
    if isinstance(result, list):
        for ref in result:
            if isinstance(ref, dict):
                reference = ref.get("reference")
                if isinstance(reference, str) and reference.startswith("Observation/"):
                    oid = reference.split("/", 1)[1]
                    if oid in remaining:
                        ordered.append(oid)
                        remaining.discard(oid)
    ordered.extend(sorted(remaining))
    return ordered


def _stamp(resource: dict, version: int, now: str) -> None:
    meta = dict(resource.get("meta") or {})
    meta["versionId"] = str(version)
    meta["lastUpdated"] = now
    resource["meta"] = meta


def _copy_observation(
    session: Session, obs: Observation, target_patient_id: str, now: str
) -> Observation:
    """Clone the observation's current content for the target patient."""
    clone = Observation(
        patient_id=target_patient_id, status="created", version=1, resource={}
    )
    session.add(clone)
    session.flush()  # assign the new id
    resource = copy.deepcopy(obs.resource)
    resource["id"] = clone.id
    resource["subject"] = {"reference": f"Patient/{target_patient_id}"}
    _stamp(resource, 1, now)
    clone.resource = resource
    session.add(
        ResourceVersion(
            resource_type="Observation",
            resource_id=clone.id,
            version=1,
            status="created",
            resource=resource,
        )
    )
    return clone


def _move_observation(
    session: Session, obs: Observation, target_patient_id: str, now: str
) -> int:
    """Reassign an observation in place, keeping its id; bump its version."""
    new_version = obs.version + 1
    resource = copy.deepcopy(obs.resource)
    resource["id"] = obs.id
    resource["subject"] = {"reference": f"Patient/{target_patient_id}"}
    _stamp(resource, new_version, now)
    obs.resource = resource
    obs.patient_id = target_patient_id
    obs.version = new_version
    obs.status = "updated"
    session.add(
        ResourceVersion(
            resource_type="Observation",
            resource_id=obs.id,
            version=new_version,
            status="updated",
            resource=resource,
        )
    )
    return new_version
