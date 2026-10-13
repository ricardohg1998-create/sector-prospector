from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sistema_prospeccion.campaigns import campaign_export, campaign_prepare, campaign_run, campaign_status, register_recipe
from sistema_prospeccion.campaigns import campaign_brief_save
from sistema_prospeccion.crm_bridge import crm_apply, crm_requests, load_state, read_json, save_state
from sistema_prospeccion.engine import Store, atomic_json, BRIEF_HEADINGS

SECTOR = "Talleres ficticios de ejemplo"
PROJECT_ID = "fake-project-ricardo"


def mcp_result(value):
    """Shape returned by tools/call in the local CRM MCP contract."""
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


class FakeCRM:
    """Persistent deterministic fake implementing local bulk-crm.ts JSON envelopes."""

    def __init__(self):
        self.batches: dict[str, dict] = {}
        self.members: dict[str, dict] = {}
        self.identities: dict[str, str] = {}
        self.email_contacts: dict[str, str] = {}
        self.next_batch = 0
        self.next_member = 0
        self.next_contact = 0
        self.write_receipts: dict[str, tuple[str, dict]] = {}
        self.executed_writes: dict[str, int] = {}
        self.transition_actions: list[str] = []
        self.activities: list[dict] = []

    def execute(self, request: dict) -> dict:
        tool = request["tool"]
        args = request["arguments"]
        write_tool = tool in {"crm_batches_create", "crm_bulk_import", "crm_bulk_transition"}
        request_id = args.get("requestId") if write_tool else None
        signature = json.dumps([tool, args], sort_keys=True, ensure_ascii=False)
        if request_id and request_id in self.write_receipts:
            old_signature, old_result = self.write_receipts[request_id]
            if signature != old_signature:
                raise AssertionError("El mismo requestId se reutilizó con otro contenido")
            return mcp_result(old_result)

        if tool == "crm_bulk_check":
            result = self._check(args)
        elif tool == "crm_batches_create":
            payload = args["input"]
            self.next_batch += 1
            batch_id = f"fake-batch-{self.next_batch:03d}"
            self.batches[batch_id] = {"id": batch_id, **payload}
            result = {"id": batch_id, "name": payload["name"]}
        elif tool == "crm_bulk_import":
            result = self._import(args["input"])
        elif tool == "crm_bulk_transition":
            result = self._transition(args["input"])
        elif tool == "crm_bulk_members":
            result = self._members(args)
        else:
            raise AssertionError(f"Herramienta fuera del contrato esperado: {tool}")

        if request_id:
            self.write_receipts[request_id] = (signature, result)
            self.executed_writes[request_id] = self.executed_writes.get(request_id, 0) + 1
        return mcp_result(result)

    def _check(self, args: dict) -> dict:
        rows = []
        for entry in args["entries"]:
            contact_id = self.identities.get(entry["businessKey"])
            reasons = []
            if contact_id:
                prior = [m for m in self.members.values() if m.get("contactId") == contact_id]
                if any(m["status"] in {"reserved", "exported", "sent"} for m in prior):
                    reasons.append("prior_commercial_use")
            email_owner = self.email_contacts.get(entry["email"])
            if email_owner and email_owner != contact_id:
                reasons.append("shared_recipient_review")
            rows.append({
                "key": entry["key"], "contactId": contact_id,
                "identityId": "identity-" + entry["businessKey"],
                "emailContactId": email_owner, "domainHintIds": [],
                "reasons": reasons, "eligible": not reasons,
                "reservableForEmail": not reasons and bool(entry.get("email")),
            })
        return {"records": rows, "checkedAt": "2026-10-11T10:00:00Z", "domainMatchesAreIdentity": False}

    def _import(self, args: dict) -> dict:
        batch_id = args["batchId"]
        if batch_id not in self.batches:
            raise AssertionError("Import sobre tanda inexistente")
        records = []
        for entry in args["entries"]:
            key = entry["key"]
            existing = next((m for m in self.members.values()
                             if m["batchId"] == batch_id and m["entryKey"] == key), None)
            if existing:
                records.append({"key": key, "memberId": existing["memberId"],
                                "contactId": existing.get("contactId"), "status": existing["status"],
                                "version": existing["version"], "reused": True})
                continue
            reason = ""
            owner = self.email_contacts.get(entry["email"])
            if owner and self.identities.get(entry["businessKey"]) != owner:
                reason = "shared_recipient_review"
            eligible = not reason
            contact_id = self.identities.get(entry["businessKey"])
            if eligible and not contact_id:
                self.next_contact += 1
                contact_id = f"fake-contact-{self.next_contact:05d}"
                self.identities[entry["businessKey"]] = contact_id
                self.email_contacts[entry["email"]] = contact_id
            self.next_member += 1
            member_id = f"fake-member-{self.next_member:05d}"
            member = {
                "memberId": member_id, "id": member_id, "batchId": batch_id,
                "entryKey": key, "businessKey": entry["businessKey"],
                "contactId": contact_id, "email": entry["email"],
                "status": "available" if eligible else "excluded",
                "reason": reason, "version": 1, "reservationToken": "",
                "reservedUntil": "", "exportedAt": "", "sentAt": "", "sentRef": "",
            }
            self.members[member_id] = member
            records.append({"key": key, "memberId": member_id, "contactId": contact_id,
                            "status": member["status"], "reason": reason,
                            "version": member["version"]})
        return {"batchId": batch_id, "records": records}

    def _transition(self, args: dict) -> dict:
        batch_id = args["batchId"]
        action = args["action"]
        self.transition_actions.append(action)
        records = []
        for request_entry in args["entries"]:
            member = self.members[request_entry["memberId"]]
            if member["batchId"] != batch_id or member["version"] != request_entry["expectedVersion"]:
                raise AssertionError("Versión o tanda CRM no vigente")
            if action == "reserve":
                if member["status"] == "reserved" and member["reservedUntil"] > datetime.now(timezone.utc).isoformat():
                    raise AssertionError("No renovar implícitamente una reserva vigente")
                if member["status"] not in {"available", "reserved"}:
                    raise AssertionError("Solo se reservan miembros disponibles o caducados")
                member["status"] = "reserved"
                member["reservationToken"] = "token-" + member["memberId"] + f"-v{member['version'] + 1}"
                member["reservedUntil"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
                member["version"] += 1
            elif action == "export":
                if member["status"] != "reserved" or request_entry.get("reservationToken") != member["reservationToken"]:
                    raise AssertionError("Export requiere reserva vigente y token")
                member["status"] = "exported"
                member["exportedAt"] = "2026-10-11T10:30:00Z"
                member["version"] += 1
            elif action == "renew":
                if member["status"] != "reserved":
                    raise AssertionError("Renew requiere reserva")
                member["version"] += 1
            elif action == "release":
                member["status"] = "available"
                member["reservationToken"] = ""
                member["reservedUntil"] = ""
                member["version"] += 1
            elif action == "confirm_sent":
                # The e2e flow must never reach this branch.
                self.activities.append({"kind": "email", "memberId": member["memberId"]})
                member["status"] = "sent"
                member["sentAt"] = request_entry.get("sentAt", "2026-10-11T10:00:00Z")
                member["sentRef"] = request_entry.get("sentRef", "")
                member["version"] += 1
            else:
                raise AssertionError(f"Acción inesperada: {action}")
            records.append({k: member.get(k) for k in (
                "memberId", "contactId", "email", "status", "version",
                "reservationToken", "reservedUntil", "exportedAt", "sentAt", "sentRef")})
        return {"batchId": batch_id, "action": action, "records": records}

    def _members(self, args: dict) -> dict:
        rows = sorted((m for m in self.members.values() if m["batchId"] == args["batchId"]), key=lambda m: m["memberId"])
        cursor = args.get("cursor", "")
        rows = [m for m in rows if m["memberId"] > cursor]
        limit = args.get("limit", 100)
        page = rows[:limit]
        next_cursor = page[-1]["memberId"] if len(rows) > limit else None
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        records = []
        for member in page:
            expired = member["status"] == "reserved" and member["reservedUntil"] <= now
            records.append({**member, "effectiveStatus": "expired" if expired else member["status"]})
        counts = {}
        for member in self.members.values():
            if member["batchId"] == args["batchId"]:
                status = "expired" if member["status"] == "reserved" and member["reservedUntil"] <= now else member["status"]
                counts[status] = counts.get(status, 0) + 1
        return {"records": records, "nextCursor": next_cursor,
                "counts": [{"status": k, "count": v} for k, v in sorted(counts.items())]}


def fixture_records(count: int) -> list[dict]:
    rows = []
    for index in range(count):
        rows.append({
            "name": f"Negocio ficticio {index:04d}",
            "city": f"Municipio {index % 40:02d}",
            "province": "Provincia de Prueba",
            "address": f"Calle de la Prueba {index:04d}, 1",
            "country": "ES",
            "email": f"contacto{index:04d}@example.org",
            "website": f"https://negocio{index:04d}.example.org",
            "email_source": f"https://example.org/directorio/{index:04d}/contacto",
        })
    return rows


def prepare_fixture(root: Path, *, count: int, target: int, run_name: str, sector: str = SECTOR) -> Path:
    registry = root / "registry"
    source_file = root / "published-example.org.json"
    recipe_file = root / "recipe.json"
    run_path = root / run_name
    source = {
        "id": "public-example-directory", "kind": "json", "path": str(source_file),
        "scope": {"verified": True, "sector": sector, "country": "ES", "basis": "Fixture sintético de prueba"},
        "publication_verified": True, "publication_basis": "Email publicado ficticio en example.org",
        "source_group": "Directorio ficticio", "role": "discovery",
    }
    atomic_json(source_file, fixture_records(count))
    atomic_json(recipe_file, {"sector": sector, "sources": [source]})
    register_recipe(recipe_file, registry)
    prepared = campaign_prepare(sector, str(run_path), fixture=True, registry=registry, target=target)
    assert prepared["fixture_mode"] is True
    return Path(prepared["run"])


def _write_fake_response(run: Path, request_file: str | Path, crm: FakeCRM, *, requests=None, incomplete=False) -> tuple[Path, list[dict]]:
    request_data = read_json(request_file)
    selected = list(requests if requests is not None else request_data["requests"])
    envelopes = []
    for request in selected:
        result = crm.execute(request)
        if incomplete and request["tool"] in {"crm_bulk_import", "crm_bulk_transition"}:
            payload = json.loads(result["content"][0]["text"])
            rows = payload.get("records", [])
            if rows:
                payload["records"] = rows[:-1]
            result = mcp_result(payload)
        envelopes.append({"request_id": request["request_id"], "result": result})
    response_path = Path(request_file).with_name(Path(request_file).stem + "-fake-response.json")
    atomic_json(response_path, {"batch": request_data["batch"], "fixture_mode": True, "responses": envelopes})
    return response_path, selected


def _apply_operation(run: Path, crm: FakeCRM, operation: str, project_id: str | None = None) -> tuple[list[dict], int]:
    pending = crm_requests(run, operation, project_id)
    request_file = Path(pending["file"])
    request_data = read_json(request_file)
    requests = request_data["requests"]
    if requests:
        response_file, _ = _write_fake_response(run, request_file, crm)
        result = crm_apply(run, response_file)
        return requests, result["applied"]
    return [], 0


def run_acceptance(temp_root: str | Path | None = None) -> dict:
    """Run two full campaign rounds without network or a real CRM."""
    cleanup = None
    if temp_root is None:
        cleanup = tempfile.TemporaryDirectory(prefix="campaign-acceptance-")
        root = Path(cleanup.name)
    else:
        root = Path(temp_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
    try:
        crm = FakeCRM()
        run1 = prepare_fixture(root / "first", count=1005, target=1000, run_name="run-one")
        first_source = campaign_run(run1, network=False, budget=1005)
        if first_source["network_requests"] != 0:
            raise AssertionError("La campaña fixture realizó peticiones de red")
        with Store(run1) as store:
            first_stock = store.selected_items()
            first_main, first_reserve = len(first_stock["main"]), len(first_stock["reserve"])
        if (first_main, first_reserve) != (1000, 5):
            raise AssertionError(f"Stock local inesperado: main={first_main}, reserve={first_reserve}")

        check_requests, _ = _apply_operation(run1, crm, "check")
        if not check_requests or any(len(r["arguments"]["entries"]) > 200 for r in check_requests):
            raise AssertionError("Check no respetó tramos de máximo 200")
        create_requests, _ = _apply_operation(run1, crm, "reserve", PROJECT_ID)
        if len(create_requests) != 1 or create_requests[0]["tool"] != "crm_batches_create":
            raise AssertionError("No se creó la tanda CRM esperada")

        import_request_file = Path(crm_requests(run1, "reserve")["file"])
        import_requests = read_json(import_request_file)["requests"]
        import_sizes = [len(r["arguments"]["input"]["entries"]) for r in import_requests]
        if len(import_requests) != 10 or any(size > 100 for size in import_sizes):
            raise AssertionError(f"Importación no respetó tramos de 100: {import_sizes}")
        first_import_req = import_requests[0]
        state_file = run1 / "campaign.json"
        before_bad_receipt = state_file.read_bytes()
        incomplete_file, _ = _write_fake_response(run1, import_request_file, crm,
                                                  requests=[first_import_req], incomplete=True)
        try:
            crm_apply(run1, incomplete_file)
        except ValueError as exc:
            if "incompleta" not in str(exc).lower():
                raise
        else:
            raise AssertionError("crm_apply aceptó una respuesta incompleta")
        if state_file.read_bytes() != before_bad_receipt:
            raise AssertionError("Respuesta incompleta alteró el estado local")
        pending_again = read_json(crm_requests(run1, "reserve")["file"])["requests"]
        if not pending_again or pending_again[0]["request_id"] != first_import_req["request_id"]:
            raise AssertionError("El reintento cambió el requestId")
        retry_file, _ = _write_fake_response(run1, import_request_file, crm, requests=[first_import_req])
        crm_apply(run1, retry_file)
        if crm.executed_writes.get(first_import_req["arguments"]["requestId"]) != 1:
            raise AssertionError("El reintento import duplicó el efecto CRM")
        remaining_import_file = Path(crm_requests(run1, "reserve")["file"])
        remaining_imports = read_json(remaining_import_file)["requests"]
        remaining_file, _ = _write_fake_response(run1, remaining_import_file, crm, requests=remaining_imports)
        if remaining_imports:
            crm_apply(run1, remaining_file)
        imported_total = sum(import_sizes)
        if imported_total != 1000:
            raise AssertionError(f"Entradas importadas inesperadas: {imported_total}")

        reserve_requests, _ = _apply_operation(run1, crm, "reserve")
        reserve_sizes = [len(r["arguments"]["input"]["entries"]) for r in reserve_requests]
        if len(reserve_requests) != 10 or any(size > 100 for size in reserve_sizes):
            raise AssertionError(f"Reserva no respetó tramos de 100: {reserve_sizes}")
        export_requests, _ = _apply_operation(run1, crm, "export")
        export_sizes = [len(r["arguments"]["input"]["entries"]) for r in export_requests]
        if len(export_requests) != 10 or any(size > 100 for size in export_sizes):
            raise AssertionError(f"Exportación CRM no respetó tramos de 100: {export_sizes}")
        first_export = campaign_export(run1, excel=False)
        first_csv = Path(first_export["output"]) / "empresas.csv"
        with first_csv.open(encoding="utf-8-sig", newline="") as handle:
            first_rows = list(csv.DictReader(handle, delimiter=";"))
        if len(first_rows) != 1000 or first_export["status"] != "complete" or first_export["sent"] is not False:
            raise AssertionError("La salida CSV o el estado completo no coinciden")
        first_emails = {row["email"].lstrip("'").casefold() for row in first_rows}
        first_tirada = read_json(Path(first_export["output"]) / "tirada.json")
        if first_tirada["rows"] != 1000 or first_tirada["sent"] is not False:
            raise AssertionError("El manifiesto no confirma 1.000 filas sin envío")
        if any(m["status"] == "sent" for m in crm.members.values()) or crm.activities:
            raise AssertionError("Se registró actividad de envío sin evidencia")

        run2 = prepare_fixture(root / "second", count=1005, target=1000, run_name="run-two")
        second_source = campaign_run(run2, network=False, budget=1005)
        if second_source["network_requests"] != 0:
            raise AssertionError("La segunda campaña fixture realizó peticiones de red")
        check2, _ = _apply_operation(run2, crm, "check")
        if len(check2) != 6 or any(len(r["arguments"]["entries"]) > 200 for r in check2):
            raise AssertionError("La segunda comprobación no respetó lotes de 200")
        second_status = campaign_status(run2)
        if second_status["crm_eligible_checked"] != 5:
            raise AssertionError(f"El CRM no dejó exactamente 5 destinatarios: {second_status['crm_eligible_checked']}")
        _apply_operation(run2, crm, "reserve", PROJECT_ID)
        import2, _ = _apply_operation(run2, crm, "reserve")
        if sum(len(r["arguments"]["input"]["entries"]) for r in import2) != 5:
            raise AssertionError("La segunda tanda no limitó importación a los 5 restantes")
        reserve2, _ = _apply_operation(run2, crm, "reserve")
        export2, _ = _apply_operation(run2, crm, "export")
        second_export = campaign_export(run2, excel=False)
        second_csv = Path(second_export["output"]) / "empresas.csv"
        with second_csv.open(encoding="utf-8-sig", newline="") as handle:
            second_rows = list(csv.DictReader(handle, delimiter=";"))
        second_emails = {row["email"].lstrip("'").casefold() for row in second_rows}
        with (Path(second_export['output']) / 'reserva.csv').open(encoding='utf-8-sig', newline='') as handle:
            if list(csv.DictReader(handle, delimiter=';')):
                raise AssertionError('La reserva de la segunda tanda contiene destinatarios ya usados')
        if len(second_rows) != 5 or second_export["status"] != "partial" or second_export["sent"] is not False:
            raise AssertionError("La segunda salida debe ser parcial de 5 destinatarios")
        if first_emails & second_emails:
            raise AssertionError("La segunda tanda repitió destinatarios de la primera")
        all_statuses = {}
        for member in crm.members.values():
            all_statuses[member["status"]] = all_statuses.get(member["status"], 0) + 1
        if all_statuses != {"exported": 1005}:
            raise AssertionError(f"Estados CRM inesperados: {all_statuses}")
        if crm.activities or any(action == "confirm_sent" for action in crm.transition_actions):
            raise AssertionError("La aceptación creó una actividad o transición de envío")
        return {
            "fixture_records_per_round": 1005,
            "network_requests": first_source["network_requests"] + second_source["network_requests"],
            "first_round": {"checked_chunks": len(check_requests), "batch_created": len(create_requests),
                            "import_rows": imported_total, "import_chunks": len(import_requests),
                            "reserve_rows": sum(reserve_sizes), "reserve_chunks": len(reserve_requests),
                            "export_rows": len(first_rows), "export_chunks": len(export_requests),
                            "main_stock": first_main, "reserve_stock": first_reserve,
                            "status": first_export["status"]},
            "second_round": {"checked_chunks": len(check2), "eligible_remaining": 5,
                             "import_rows": 5, "reserve_rows": sum(len(r["arguments"]["input"]["entries"]) for r in reserve2),
                             "export_rows": len(second_rows), "export_chunks": len(export2),
                             "repeated_recipients": len(first_emails & second_emails),
                             "status": second_export["status"]},
            "incomplete_receipt_rejected_without_local_mutation": True,
            "write_retry_idempotent": True,
            "crm_statuses": all_statuses,
            "sent_activities": len(crm.activities),
            "sent_transitions": crm.transition_actions.count("confirm_sent"),
            "project_id": PROJECT_ID,
        }
    finally:
        if cleanup is not None:
            cleanup.cleanup()


class CampaignCRMAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="campaign-unit-")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def new_run(self, name="run", count=1, target=1):
        return prepare_fixture(self.root / name, count=count, target=target, run_name="execution")

    def apply(self, run: Path, crm: FakeCRM, operation: str, project_id: str | None = None):
        return _apply_operation(run, crm, operation, project_id)

    def test_full_volume_retries_crm_batches_and_next_run_uses_only_remaining_recipients(self):
        report = run_acceptance(self.root / "acceptance")
        self.assertEqual(report["first_round"]["main_stock"], 1000)
        self.assertEqual(report["first_round"]["reserve_stock"], 5)
        self.assertEqual(report["first_round"]["import_rows"], 1000)
        self.assertEqual(report["first_round"]["export_rows"], 1000)
        self.assertEqual(report["second_round"]["eligible_remaining"], 5)
        self.assertEqual(report["second_round"]["export_rows"], 5)
        self.assertEqual(report["second_round"]["status"], "partial")
        self.assertEqual(report["second_round"]["repeated_recipients"], 0)
        self.assertTrue(report["write_retry_idempotent"])
        self.assertEqual(report["sent_activities"], 0)

    def test_expired_lease_requires_members_refresh_before_reserve(self):
        run = self.new_run(count=1)
        campaign_run(run, network=False, budget=1)
        crm = FakeCRM()
        self.apply(run, crm, "check")
        self.apply(run, crm, "reserve", PROJECT_ID)
        self.apply(run, crm, "reserve")
        reserve_requests, _ = self.apply(run, crm, "reserve")
        self.assertEqual(reserve_requests[0]["arguments"]["input"]["action"], "reserve")
        state = load_state(run)
        member = next(iter(state["members"].values()))
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        member["reservedUntil"] = past
        save_state(run, state)
        crm.members[member["memberId"]]["reservedUntil"] = past
        with self.assertRaisesRegex(ValueError, "caducada; consultar members"):
            crm_requests(run, "export")
        member_requests, _ = self.apply(run, crm, "members")
        self.assertEqual(len(member_requests), 1)
        refreshed = load_state(run)["members"][member["key"]]
        self.assertEqual(refreshed["status"], "available")
        renewed_requests, _ = self.apply(run, crm, "reserve")
        self.assertEqual(renewed_requests[0]["arguments"]["input"]["action"], "reserve")

    def test_csv_export_requires_export_receipt_and_does_not_imply_sent(self):
        run = self.new_run(count=1)
        campaign_run(run, network=False, budget=1)
        crm = FakeCRM()
        self.apply(run, crm, "check")
        self.apply(run, crm, "reserve", PROJECT_ID)
        self.apply(run, crm, "reserve")
        self.apply(run, crm, "reserve")
        with self.assertRaisesRegex(ValueError, "Sin destinatarios confirmados como exportados"):
            campaign_export(run, excel=False)
        self.apply(run, crm, "export")
        exported = campaign_export(run, excel=False)
        self.assertEqual(exported["rows"], 1)
        self.assertFalse(exported["sent"])
        self.assertFalse(crm.activities)

    def test_unknown_crm_operation_and_fixture_network_are_rejected(self):
        run = self.new_run(count=1)
        with self.assertRaisesRegex(ValueError, "Operación CRM no admitida"):
            crm_requests(run, "invented-mode")
        with self.assertRaisesRegex(ValueError, "no permite red real"):
            campaign_run(run, network=True, budget=1)

    def test_same_catalogue_new_round_preserves_stock_and_excludes_prior_use(self):
        run = self.new_run(count=2, target=1)
        campaign_run(run, budget=2)
        crm = FakeCRM()
        self.apply(run, crm, 'check')
        self.apply(run, crm, 'reserve', PROJECT_ID)
        self.apply(run, crm, 'reserve')
        self.apply(run, crm, 'reserve')
        self.apply(run, crm, 'export')
        first = campaign_export(run, excel=False)
        old_batch = load_state(run)['id']
        registry = run.parent / 'registry'
        campaign_prepare(SECTOR, run=run, fixture=True, registry=registry, target=1)
        self.assertNotEqual(load_state(run)['id'], old_batch)
        self.assertEqual(load_state(run)['project_id'], PROJECT_ID)
        self.apply(run, crm, 'check')
        self.assertEqual(campaign_status(run)['crm_excluded'], 1)
        self.apply(run, crm, 'reserve')
        self.apply(run, crm, 'reserve')
        self.apply(run, crm, 'reserve')
        self.apply(run, crm, 'export')
        second = campaign_export(run, excel=False)
        def addresses(delivery):
            with (Path(delivery['output']) / 'empresas.csv').open(encoding='utf-8-sig', newline='') as handle:
                return {row['email'] for row in csv.DictReader(handle, delimiter=';')}
        self.assertFalse(addresses(first) & addresses(second))
        with (Path(second['output']) / 'reserva.csv').open(encoding='utf-8-sig', newline='') as handle:
            self.assertEqual(list(csv.DictReader(handle, delimiter=';')), [])

    def test_research_is_reused_without_row_dependency_and_detects_tampering(self):
        run = self.new_run(count=1)
        campaign_run(run, budget=1)
        with Store(run) as store:
            store.brief_template()
            brief = '# Investigación ficticia\n\n' + '\n\n'.join('## ' + heading + '\n\nTexto sintético suficiente para probar conservación de evidencia; no es investigación real.' for heading in BRIEF_HEADINGS)
            (run / 'entregables/brief-sector.md').write_text(brief, encoding='utf-8')
            atomic_json(run / 'investigacion/evidencias.json', [{'url':'https://example.org/fixture', 'title':'Fixture', 'checked_at':'2026-10-11', 'claim':'Datos sintéticos, no investigación real', 'scope':'fixture', 'inspected':True}])
            store.review_brief('test automatizado')
            store.configure({'scope_notes':'Nuevo lote técnico sin cambiar contexto sectorial'})
            self.assertTrue(store.brief_status()['reviewed'])
        cache = campaign_brief_save(run, registry=run.parent / 'registry')
        self.assertEqual(campaign_status(run)['sector_research']['status'], 'reviewed')
        crm = FakeCRM()
        for operation in ('check', 'reserve', 'reserve', 'reserve', 'export'):
            self.apply(run, crm, operation, PROJECT_ID if operation == 'check' else None)
        exported = campaign_export(run, excel=False)
        self.assertEqual(exported['delivery_status'], 'complete')
        self.assertTrue((Path(exported['output']) / 'evidencias-sector.json').is_file())
        evidence = Path(cache['research']) / 'evidencias.json'
        evidence.write_text('[]', encoding='utf-8')
        self.assertEqual(campaign_status(run)['sector_research']['status'], 'invalid')
        self.assertEqual(campaign_export(run, excel=False)['delivery_status'], 'partial')
        with self.assertRaisesRegex(ValueError, 'investigación guardada cambió'):
            campaign_prepare(SECTOR, run=run, fixture=True, registry=run.parent / 'registry', target=1)

    def test_foreign_crm_batch_receipt_does_not_change_local_state(self):
        run = self.new_run(count=1)
        campaign_run(run, budget=1)
        crm = FakeCRM()
        self.apply(run, crm, 'check')
        self.apply(run, crm, 'reserve', PROJECT_ID)
        request_file = Path(crm_requests(run, 'reserve')['file'])
        response_file, _ = _write_fake_response(run, request_file, crm)
        response = read_json(response_file)
        result = json.loads(response['responses'][0]['result']['content'][0]['text'])
        result['batchId'] = 'otra-tanda'
        response['responses'][0]['result'] = mcp_result(result)
        atomic_json(response_file, response)
        before = (run / 'campaign.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'otra tanda'):
            crm_apply(run, response_file)
        self.assertEqual((run / 'campaign.json').read_bytes(), before)

    def test_interrupted_import_recovers_route_outbox_before_more_extraction(self):
        from sistema_prospeccion.source_routes import _persist_source_batch, pending_source_batches
        run = self.new_run(count=1)
        state = load_state(run)
        with Store(run) as store:
            batch = _persist_source_batch(store, state['recipe']['sources'][0], {'records':fixture_records(1)}, ['fixture-capture-hash'])
        with patch.object(Store, 'ingest_file', side_effect=RuntimeError('Fallo simulado antes de commit')):
            with self.assertRaisesRegex(RuntimeError, 'simulado'):
                campaign_run(run, budget=1)
        self.assertEqual(len(pending_source_batches(run)), 1)
        with patch('sistema_prospeccion.source_routes.run_source', side_effect=AssertionError('No debe descargar con stock suficiente')):
            recovered = campaign_run(run, budget=1)
        self.assertEqual(recovered['stock_unique_recipients'], 1)
        self.assertEqual(recovered['network_requests'], 0)
        self.assertEqual(pending_source_batches(run), [])
        self.assertTrue((run / 'rutas/outbox' / (batch['batch_id'] + '.ack')).is_file())

    def test_enough_checked_stock_does_not_check_the_entire_unused_catalogue(self):
        run = self.new_run(count=3, target=1)
        campaign_run(run, budget=3)
        state = load_state(run)
        with Store(run) as store:
            cid = store.selected_items()['main'][0]['company_id']
        state['checks'][cid] = {'eligible':True, 'reasons':[]}
        save_state(run, state)
        status = campaign_status(run)
        self.assertEqual(status['crm_unchecked'], 2)
        self.assertIn('operation reserve', status['next_action'])
        plan = crm_requests(run, 'auto', PROJECT_ID)
        requests = read_json(plan['file'])['requests']
        self.assertEqual([request['tool'] for request in requests], ['crm_batches_create'])

    def test_partial_export_does_not_abandon_reserved_recipients(self):
        run = self.new_run(count=2, target=2)
        campaign_run(run, budget=2)
        crm = FakeCRM()
        for op in ('check', 'reserve', 'reserve', 'reserve'):
            self.apply(run, crm, op, PROJECT_ID if op == 'check' else None)
        state = load_state(run)
        next(iter(state['members'].values()))['status'] = 'exported'
        save_state(run, state)
        with self.assertRaisesRegex(ValueError, 'Quedan reservas pendientes'):
            campaign_export(run, excel=False)
        self.assertFalse(load_state(run)['closed'])


if __name__ == "__main__":
    unittest.main()
