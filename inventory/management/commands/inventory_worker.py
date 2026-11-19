import logging
import time
from contextlib import contextmanager

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections
from django.utils import timezone

from inventory.models import InventoryRun
from inventory.router import INVENTORY_DB
from inventory.services import enqueue_inventory, collect_inventory


logger = logging.getLogger(__name__)
WORKER_LOCK = 419530519801


@contextmanager
def worker_lock(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [WORKER_LOCK])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            # Encerrar a sessão libera o lock mesmo após falha de uma transação.
            connection.close()


class Command(BaseCommand):
    help = "Processa coletas manuais e agenda uma coleta diária às 03h no horário do ERP."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Executa uma rodada da fila e encerra.")

    def handle(self, *args, **options):
        connection = connections[INVENTORY_DB]
        if connection.vendor != "postgresql":
            raise CommandError("Configure o PostgreSQL de inventário para executar o coletor.")
        while True:
            try:
                with worker_lock(connection) as acquired:
                    if acquired:
                        self.process_queue()
            except DatabaseError:
                connection.close()
                if options["once"]:
                    raise
                logger.warning("Banco indisponível ao processar a fila de inventário.")
            if options["once"]:
                return
            connections["default"].close()
            time.sleep(10)

    def process_queue(self):
        now = timezone.localtime()
        InventoryRun.objects.filter(status=InventoryRun.Status.RUNNING).update(
            status=InventoryRun.Status.FAILED, finished_at=now,
            detail="Coleta interrompida antes de gravar o inventário.",
        )
        if now.hour >= 3 and not InventoryRun.objects.filter(scheduled_day=now.date()).exists():
            enqueue_inventory(scheduled_day=now.date())
        run = InventoryRun.objects.filter(status=InventoryRun.Status.QUEUED).first()
        if run is None:
            return
        run.status = InventoryRun.Status.RUNNING
        run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at"])
        self.stdout.write(f"Iniciando coleta {run.pk}.")
        try:
            collect_inventory(run)
        except DatabaseError:
            raise
        except Exception:
            logger.exception("A coleta %s foi interrompida.", run.pk)
            run.status = InventoryRun.Status.FAILED
            run.finished_at = timezone.now()
            run.detail = "Erro inesperado ao coletar o inventário."
            run.save(update_fields=["status", "finished_at", "detail"])
        self.stdout.write(f"Coleta {run.pk} finalizada com resultado {run.get_status_display()}.")
