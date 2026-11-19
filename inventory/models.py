from django.db import models


class InventoryRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Na fila"
        RUNNING = "running", "Em andamento"
        COMPLETE = "complete", "Concluída"
        PARTIAL = "partial", "Parcial"
        FAILED = "failed", "Falhou"

    status = models.CharField(max_length=12, choices=Status, default=Status.QUEUED)
    scheduled_day = models.DateField(null=True, unique=True)
    requested_by = models.CharField(max_length=150, blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    detail = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ["-pk"]
        constraints = [
            models.UniqueConstraint(
                models.Value(1), condition=models.Q(status__in=["queued", "running"]),
                name="one_active_inventory_run",
            ),
        ]


class ProviderSnapshot(models.Model):
    run = models.ForeignKey(InventoryRun, on_delete=models.CASCADE, related_name="providers")
    provider_id = models.PositiveBigIntegerField()
    provider_name = models.CharField(max_length=80)
    ont_count = models.PositiveIntegerField()
    vlan_count = models.PositiveIntegerField()
    failed_vlans = models.PositiveIntegerField()
    partial_vlans = models.PositiveIntegerField()

    class Meta:
        ordering = ["provider_name", "provider_id"]
        constraints = [
            models.UniqueConstraint(fields=["run", "provider_id"], name="inventory_provider_once_per_run"),
        ]
        indexes = [models.Index(fields=["provider_id", "-run"], name="inventory_provider_history")]


class VlanSnapshot(models.Model):
    provider = models.ForeignKey(ProviderSnapshot, on_delete=models.CASCADE, related_name="vlans")
    olt_id = models.PositiveBigIntegerField()
    olt_name = models.CharField(max_length=80)
    vlan = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=16)
    detail = models.TextField(blank=True)
    rows = models.JSONField(default=list)

    class Meta:
        ordering = ["olt_name", "vlan"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "olt_id", "vlan"], name="inventory_vlan_once_per_provider"),
        ]
