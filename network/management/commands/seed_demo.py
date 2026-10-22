from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from network.models import CTO, OLT, ONU, Provider, Vlan


class Command(BaseCommand):
    help = "Cria dados de demonstração sem contatar equipamentos reais. Não cria usuários."

    @transaction.atomic
    def handle(self, *args, **options):
        if Provider.objects.exists() or OLT.objects.exists() or CTO.objects.exists() or ONU.objects.exists():
            raise CommandError("Use seed_demo apenas em uma base vazia para preservar cadastros existentes.")
        acert = Provider.objects.create(name="acert", contact="Equipe Acert", email="operacao@acert.example")
        veloz = Provider.objects.create(name="Veloz Fibra", contact="Equipe Veloz")
        link = Provider.objects.create(name="Link Regional", contact="Equipe Link")
        vlans = [Vlan.objects.create(number=n, provider=p, name="Acesso residencial") for n, p in [(100, acert), (200, veloz), (300, link)]]
        ctos = [CTO.objects.create(name=f"CTO-{i:03}", location=loc, total_ports=16) for i, loc in [(1, "Centro · Rua das Flores"), (2, "Boa Vista · Av. Principal"), (3, "São José · Praça Central")]]
        olt = OLT.objects.create(name="OLT-Centro (demo)", vendor="huawei", model="MA5800-X7", firmware="MA5800V100R022C11")
        OLT.objects.create(name="OLT-Norte (demo)", vendor="huawei", model="MA5683T", firmware="MA5600V800R017C10")
        OLT.objects.create(name="OLT-ZTE (demo)", vendor="zte", model="Demonstração")
        for i, name in enumerate(["adriano", "Mariana Costa", "João Pedro", "Ana Silva", "Lucas Almeida", "Camila Santos"]):
            vlan = vlans[i % 3]
            ONU.objects.create(provider=vlan.provider, subscriber=name, serial=f"HWTC{i + 1:08X}", vlan=vlan,
                               cto=ctos[i % 3], port=i // 3 + 1, olt=olt, ont_id=i, service_port=1000 + i)
        self.stdout.write(self.style.SUCCESS("Demonstração criada: 3 provedores, 3 VLANs, 3 CTOs, 3 OLTs e 6 ONUs. Nenhum equipamento acessado."))
