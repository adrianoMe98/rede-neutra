from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models


def bounded(low, high):
    return [MinValueValidator(low), MaxValueValidator(high)]


class Provider(models.Model):
    name = models.CharField("nome do provedor", max_length=80, unique=True)
    document = models.CharField("CNPJ / documento", max_length=30, blank=True)
    contact = models.CharField("responsável", max_length=120, blank=True)
    email = models.EmailField("e-mail", blank=True)
    active = models.BooleanField("ativo", default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "provedor"

    def __str__(self):
        return self.name


class Vlan(models.Model):
    number = models.PositiveIntegerField("VLAN", unique=True, validators=bounded(1, 4094))
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, verbose_name="provedor", related_name="vlans")
    name = models.CharField("descrição", max_length=120, blank=True)

    class Meta:
        ordering = ["number"]
        verbose_name = "VLAN"

    def clean(self):
        if self.pk and self.onus.exclude(provider_id=self.provider_id).exists():
            raise ValidationError("Não é possível trocar o provedor de uma VLAN em uso por outro provedor.")
        if self.pk and self.provisioning_profiles.exclude(provider_id=self.provider_id).exists():
            raise ValidationError("Esta VLAN está vinculada a perfis de outro provedor.")

    def __str__(self):
        return f"{self.number} · {self.provider.name}"


class CTO(models.Model):
    name = models.CharField("número / identificação da CTO", max_length=60, unique=True)
    location = models.CharField("endereço / localização", max_length=200, blank=True)
    total_ports = models.PositiveIntegerField("quantidade de portas", default=16, validators=bounded(1, 64))

    class Meta:
        ordering = ["name"]
        verbose_name = "CTO"

    def clean(self):
        if self.pk and self.onus.filter(port__gt=self.total_ports).exists():
            raise ValidationError("Há ONUs utilizando portas acima da nova capacidade.")

    def __str__(self):
        return self.name


class OLT(models.Model):
    VENDORS = [("huawei", "Huawei"), ("zte", "ZTE"), ("fiberhome", "FiberHome"), ("other", "Outro fabricante")]
    MODES = [("simulation", "Simulação"), ("ssh", "SSH • Huawei (experimental)")]
    name = models.CharField("nome da OLT", max_length=80, unique=True)
    vendor = models.CharField("fabricante", max_length=20, choices=VENDORS, default="huawei")
    model = models.CharField("modelo / firmware", max_length=120, blank=True)
    firmware = models.CharField("versão do firmware", max_length=60, blank=True,
                               help_text="Huawei: MA5800V100R022C11 ou MA5600V800R017C10.")
    mode = models.CharField("integração", max_length=20, choices=MODES, default="simulation")
    host = models.CharField("IP / hostname de gerenciamento", max_length=253, blank=True,
                            validators=[RegexValidator(r"^[a-zA-Z0-9_.:-]+$", "Informe um IP ou hostname válido.")])
    ssh_port = models.PositiveIntegerField("porta SSH", default=22, validators=bounded(1, 65535))
    username = models.CharField("usuário SSH", max_length=80, blank=True)
    encrypted_password = models.TextField(blank=True, editable=False)
    line_profile = models.PositiveIntegerField("ID do line profile", default=10, validators=bounded(1, 4095))
    service_profile = models.PositiveIntegerField("ID do service profile", default=10, validators=bounded(1, 4095))
    gem_port = models.PositiveIntegerField("GEM port", default=1, validators=bounded(0, 4095))

    class Meta:
        ordering = ["name"]
        verbose_name = "OLT"

    def clean(self):
        if self.mode == "ssh" and (self.vendor != "huawei" or not self.host or not self.username):
            raise ValidationError("O adaptador SSH disponível exige Huawei, endereço e usuário. Outros fabricantes usam simulação.")
        if self.pk and self.onus.exclude(status=ONU.Status.DRAFT).exists():
            original = type(self).objects.get(pk=self.pk)
            if any(getattr(original, f) != getattr(self, f) for f in ["vendor", "mode", "host", "ssh_port"]):
                raise ValidationError("A OLT tem ONUs provisionadas ou pendentes de conferência. Sua identidade e integração não podem ser alteradas.")

    def set_password(self, raw):
        if not settings.OLT_CREDENTIAL_KEY:
            raise ValidationError("Configure OLT_CREDENTIAL_KEY antes de salvar credenciais.")
        self.encrypted_password = Fernet(settings.OLT_CREDENTIAL_KEY.encode()).encrypt(raw.encode()).decode()

    def get_password(self):
        try:
            return Fernet(settings.OLT_CREDENTIAL_KEY.encode()).decrypt(self.encrypted_password.encode()).decode()
        except (ValueError, InvalidToken):
            raise ValidationError("Credencial indisponível: verifique a chave de criptografia e recadastre a senha.")

    def __str__(self):
        return f"{self.name} · {self.get_vendor_display()}"


class ONU(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Cadastrada"
        RUNNING = "running", "Em provisionamento"
        ACTIVE = "active", "Provisionada"
        REVIEW = "review", "Conferir na OLT"

    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, verbose_name="provedor", related_name="onus")
    profile = models.ForeignKey("ProvisioningProfile", null=True, blank=True, on_delete=models.PROTECT,
                               verbose_name="perfil de provisionamento", related_name="onus")
    profile_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    discovered_at = models.DateTimeField(null=True, blank=True, editable=False)
    subscriber = models.CharField("nome do cliente / identificação", max_length=80)
    serial = models.CharField("serial da ONT / ONU", max_length=16, unique=True,
                              validators=[RegexValidator(r"^[A-Z0-9]{8,16}$", "Use de 8 a 16 letras maiúsculas ou números.")])
    vlan = models.ForeignKey(Vlan, on_delete=models.PROTECT, verbose_name="VLAN", related_name="onus")
    cto = models.ForeignKey(CTO, on_delete=models.PROTECT, verbose_name="CTO", related_name="onus")
    port = models.PositiveIntegerField("porta na CTO", validators=bounded(1, 64))
    olt = models.ForeignKey(OLT, on_delete=models.PROTECT, verbose_name="OLT", related_name="onus")
    frame = models.PositiveIntegerField("frame", default=0, validators=bounded(0, 15))
    slot = models.PositiveIntegerField("slot", default=1, validators=bounded(0, 31))
    pon = models.PositiveIntegerField("porta PON", default=0, validators=bounded(0, 31))
    ont_id = models.PositiveIntegerField("ONT ID", default=0, validators=bounded(0, 255))
    service_port = models.PositiveIntegerField("service-port ID", validators=bounded(0, 65535))
    status = models.CharField("situação", max_length=20, choices=Status, default=Status.DRAFT, editable=False)
    rx_dbm = models.FloatField("RX da ONU (dBm)", null=True, editable=False)
    tx_dbm = models.FloatField("TX da ONU (dBm)", null=True, editable=False)
    measured_at = models.DateTimeField("última leitura", null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "ONT / ONU"
        constraints = [
            models.UniqueConstraint(fields=["cto", "port"], name="unique_cto_port"),
            models.UniqueConstraint(fields=["olt", "frame", "slot", "pon", "ont_id"], name="unique_olt_ont"),
            models.UniqueConstraint(fields=["olt", "service_port"], name="unique_olt_service_port"),
        ]

    @property
    def description(self):
        return f"{self.provider.name} - {self.subscriber}"

    @property
    def pon_address(self):
        return f"{self.frame}/{self.slot}/{self.pon}"

    @property
    def provisioning_parameters(self):
        if self.profile_snapshot:
            return self.profile_snapshot
        return {"line_profile": self.olt.line_profile, "service_profile": self.olt.service_profile,
                "gem_port": self.olt.gem_port, "user_vlan": self.vlan.number}

    def clean(self):
        errors = {}
        if self.provider_id and not self.provider.active:
            errors["provider"] = "O provedor está inativo."
        if self.vlan_id and self.provider_id and self.vlan.provider_id != self.provider_id:
            errors["vlan"] = "A VLAN precisa pertencer ao provedor selecionado."
        if self.cto_id and self.port and self.port > self.cto.total_ports:
            errors["port"] = f"Esta CTO possui apenas {self.cto.total_ports} portas."
        if self.profile_id:
            profile = self.profile
            if not profile.active:
                errors["profile"] = "O perfil selecionado está inativo."
            if (self.provider_id, self.vlan_id, self.olt_id) != (profile.provider_id, profile.vlan_id, profile.olt_id):
                errors["profile"] = "Provedor, VLAN e OLT devem corresponder ao perfil selecionado."
            if not self.profile_snapshot or self.profile_snapshot.get("profile_id") != profile.pk:
                self.profile_snapshot = profile.parameters()
        else:
            self.profile_snapshot = {}
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.description


class ProvisioningProfile(models.Model):
    name = models.CharField("nome do perfil", max_length=100, unique=True)
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, verbose_name="provedor", related_name="provisioning_profiles")
    olt = models.ForeignKey(OLT, on_delete=models.PROTECT, verbose_name="OLT", related_name="provisioning_profiles")
    vlan = models.ForeignKey(Vlan, on_delete=models.PROTECT, verbose_name="VLAN de serviço", related_name="provisioning_profiles")
    line_profile = models.PositiveIntegerField("ID do line profile", validators=bounded(1, 4095))
    service_profile = models.PositiveIntegerField("ID do service profile", validators=bounded(1, 4095))
    gem_port = models.PositiveIntegerField("GEM port", default=1, validators=bounded(0, 4095))
    user_vlan = models.PositiveIntegerField("VLAN do cliente", null=True, blank=True, validators=bounded(1, 4094),
                                           help_text="Em branco, utiliza o mesmo número da VLAN de serviço.")
    description = models.CharField("observações", max_length=240, blank=True)
    active = models.BooleanField("ativo", default=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "perfil de provisionamento"

    def clean(self):
        if self.vlan_id and self.provider_id and self.vlan.provider_id != self.provider_id:
            raise ValidationError({"vlan": "A VLAN de serviço precisa pertencer ao provedor do perfil."})
        if self.pk and self.onus.exists():
            original = type(self).objects.get(pk=self.pk)
            if any(getattr(self, name) != getattr(original, name) for name in ("provider_id", "vlan_id", "olt_id")):
                raise ValidationError("Um perfil em uso não pode trocar de provedor, VLAN ou OLT. Crie outro perfil.")

    def parameters(self):
        return {"profile_id": self.pk, "name": self.name, "line_profile": self.line_profile,
                "service_profile": self.service_profile, "gem_port": self.gem_port,
                "user_vlan": self.user_vlan if self.user_vlan is not None else self.vlan.number}

    def __str__(self):
        return self.name


class DiscoveryScan(models.Model):
    olt = models.ForeignKey(OLT, on_delete=models.PROTECT, related_name="discovery_scans")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    simulation = models.BooleanField()
    outcome = models.CharField(max_length=10, choices=[("success", "Concluída"), ("failed", "Falhou")])
    count = models.PositiveIntegerField(default=0)
    detail = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Operation(models.Model):
    onu = models.ForeignKey(ONU, on_delete=models.PROTECT, related_name="operations")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=20, choices=[("provision", "Provisionamento"), ("optics", "Leitura óptica")])
    outcome = models.CharField(max_length=20, choices=[("running", "Em andamento"), ("success", "Concluída"), ("failed", "Falhou"), ("review", "Conferir na OLT")])
    simulation = models.BooleanField()
    detail = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
