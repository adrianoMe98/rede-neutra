from django.db import transaction
from django.utils import timezone
from .adapters import AdapterError, UncertainResult, get_adapter
from .models import ONU, Operation


def provision(onu, actor):
    adapter = get_adapter(onu.olt)
    onu.full_clean()
    adapter.plan(onu)
    if onu.olt.mode == "ssh":
        adapter.preflight(onu.olt)
    # Compare-and-swap: o mesmo cadastro não pode ser enviado duas vezes.
    with transaction.atomic():
        if not ONU.objects.filter(pk=onu.pk, status=ONU.Status.DRAFT).update(
            status=ONU.Status.RUNNING, profile_snapshot=onu.profile_snapshot, discovered_at=onu.discovered_at
        ):
            raise AdapterError("Esta ONU já foi provisionada, está em andamento ou exige conferência. Não será reenviada.")
        operation = Operation.objects.create(onu=onu, actor=actor, kind="provision", outcome="running",
                                             simulation=onu.olt.mode == "simulation", detail="Envio iniciado.")
    try:
        detail = adapter.provision(onu)
    except AdapterError as exc:
        uncertain = isinstance(exc, UncertainResult)
        with transaction.atomic():
            ONU.objects.filter(pk=onu.pk).update(status=ONU.Status.REVIEW if uncertain else ONU.Status.DRAFT)
            operation.outcome = "review" if uncertain else "failed"
            operation.detail = str(exc)
            operation.save(update_fields=["outcome", "detail"])
        raise
    except Exception:
        # Uma interrupção inesperada nunca libera reenvio potencialmente destrutivo.
        with transaction.atomic():
            ONU.objects.filter(pk=onu.pk).update(status=ONU.Status.REVIEW)
            operation.outcome = "review"
            operation.detail = "Falha inesperada. Confira a ONT e o service-port na OLT antes de reconciliar o cadastro."
            operation.save(update_fields=["outcome", "detail"])
        raise AdapterError(operation.detail) from None
    with transaction.atomic():
        ONU.objects.filter(pk=onu.pk).update(status=ONU.Status.ACTIVE)
        operation.outcome = "success"
        operation.detail = detail
        operation.save(update_fields=["outcome", "detail"])
    return detail


def refresh_optics(onu, actor):
    if onu.status != ONU.Status.ACTIVE:
        raise AdapterError("Provisione a ONU antes de consultar a potência óptica.")
    try:
        reading = get_adapter(onu.olt).optics(onu)
    except AdapterError as exc:
        Operation.objects.create(onu=onu, actor=actor, kind="optics", outcome="failed",
                                 simulation=onu.olt.mode == "simulation", detail=str(exc))
        raise
    except Exception:
        Operation.objects.create(onu=onu, actor=actor, kind="optics", outcome="failed",
                                 simulation=onu.olt.mode == "simulation", detail="Não foi possível concluir a leitura óptica.")
        raise AdapterError("Não foi possível concluir a leitura óptica. As medições anteriores foram preservadas.") from None
    with transaction.atomic():
        ONU.objects.filter(pk=onu.pk).update(tx_dbm=reading.tx, rx_dbm=reading.rx, measured_at=timezone.now())
        Operation.objects.create(onu=onu, actor=actor, kind="optics", outcome="success",
                                 simulation=onu.olt.mode == "simulation", detail=f"TX {reading.tx:.2f} dBm / RX {reading.rx:.2f} dBm (na ONU).")
    return reading
