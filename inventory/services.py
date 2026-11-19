from collections import defaultdict
from dataclasses import asdict

from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from network.models import Provider
from network.olt_inventory import InventoryRow, ONTConfiguration, VLANResult, provider_targets, query_olt

from .models import InventoryRun, ProviderSnapshot, VlanSnapshot
from .router import INVENTORY_DB


ACTIVE_STATUSES = (InventoryRun.Status.QUEUED, InventoryRun.Status.RUNNING)


def enqueue_inventory(*, requested_by="", scheduled_day=None):
    try:
        with transaction.atomic(using=INVENTORY_DB):
            return InventoryRun.objects.create(requested_by=requested_by, scheduled_day=scheduled_day)
    except IntegrityError:
        conflict = Q(status__in=ACTIVE_STATUSES)
        if scheduled_day is not None:
            conflict |= Q(scheduled_day=scheduled_day)
        if InventoryRun.objects.filter(conflict).exists():
            return None
        raise


def latest_complete_snapshots(provider_ids):
    latest = ProviderSnapshot.objects.filter(
        provider_id=OuterRef("provider_id"), failed_vlans=0,
    ).order_by("-run_id").values("pk")[:1]
    return ProviderSnapshot.objects.filter(
        provider_id__in=provider_ids, pk=Subquery(latest),
    ).select_related("run")


def collect_inventory(run):
    providers = {}
    olt_targets = {}
    for provider in Provider.objects.all():
        for olt, vlans in provider_targets(provider):
            providers[provider.pk] = provider.name
            _, owners = olt_targets.setdefault(olt.pk, (olt, {}))
            owners.update((vlan, provider.pk) for vlan in vlans)

    results_by_provider = defaultdict(list)
    for olt, owners in olt_targets.values():
        results = {result.vlan: result for result in query_olt(olt, sorted(owners))}
        for vlan, provider_id in owners.items():
            result = results.get(vlan)
            if result is None:
                result = VLANResult(vlan, "Falhou", "A consulta retornou sem resultado para esta VLAN.")
            results_by_provider[provider_id].append((olt, result))

    with transaction.atomic(using=INVENTORY_DB):
        successful_vlans = incomplete_vlans = 0
        for provider_id, results in results_by_provider.items():
            positions = {(olt.pk, row.position) for olt, result in results for row in result.rows}
            failed = sum(result.status == "Falhou" for _, result in results)
            partial = sum(result.status == "Parcial" for _, result in results)
            successful_vlans += len(results) - failed
            incomplete_vlans += failed + partial
            snapshot = ProviderSnapshot.objects.create(
                run=run, provider_id=provider_id, provider_name=providers[provider_id],
                ont_count=len(positions), vlan_count=len(results), failed_vlans=failed, partial_vlans=partial,
            )
            VlanSnapshot.objects.bulk_create([
                VlanSnapshot(
                    provider=snapshot, olt_id=olt.pk, olt_name=olt.name, vlan=result.vlan,
                    status=result.status, detail=result.detail, rows=[asdict(row) for row in result.rows],
                )
                for olt, result in results
            ])

        if not successful_vlans:
            run.status = InventoryRun.Status.FAILED
            run.detail = ("Nenhuma VLAN respondeu à coleta." if olt_targets else
                          "Nenhuma OLT e VLAN vinculadas a um provedor por perfil ou cadastro de ONU.")
        elif incomplete_vlans:
            run.status = InventoryRun.Status.PARTIAL
            run.detail = f"{incomplete_vlans} VLAN(s) com falha ou detalhes indisponíveis."
        else:
            run.status = InventoryRun.Status.COMPLETE
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "detail", "finished_at"])


def stored_results(snapshot):
    for vlan in snapshot.vlans.iterator():
        rows = [
            InventoryRow(
                position=tuple(row["position"]), service_ports=tuple(row["service_ports"]),
                ont=ONTConfiguration(**row["ont"]), learned_macs=tuple(row["learned_macs"]), detail=row["detail"],
            )
            for row in vlan.rows
        ]
        yield vlan.olt_name, VLANResult(vlan.vlan, vlan.status, vlan.detail, rows)
