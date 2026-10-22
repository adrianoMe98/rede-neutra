from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from network.models import ONU, Operation


class Command(BaseCommand):
    help = "Reconcilia resultado incerto após conferência manual na OLT. Não executa comandos remotos."

    def add_arguments(self, parser):
        parser.add_argument("serial")
        parser.add_argument("--state", choices=["active", "draft"], required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--verified-on-olt", action="store_true", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        if len(options["reason"].strip()) < 12:
            raise CommandError("Descreva a conferência realizada (mínimo de 12 caracteres).")
        try:
            onu = ONU.objects.get(serial=options["serial"].upper())
        except ONU.DoesNotExist:
            raise CommandError("Serial não encontrado.")
        if onu.status not in [ONU.Status.RUNNING, ONU.Status.REVIEW]:
            raise CommandError("Apenas ONUs em andamento ou em conferência podem ser reconciliadas.")
        ONU.objects.filter(pk=onu.pk).update(status=options["state"])
        Operation.objects.create(onu=onu, kind="provision", outcome="success" if options["state"] == "active" else "failed",
                                 simulation=onu.olt.mode == "simulation", detail=f"Reconciliação administrativa para {options['state']}: {options['reason']}")
        self.stdout.write(self.style.SUCCESS("Estado reconciliado. Nenhum comando enviado à OLT."))
