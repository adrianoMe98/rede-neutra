from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from .models import CTO, OLT, ONU, Provider, ProvisioningProfile, Vlan
from .serials import normalize_serial, serial_aliases


class ProviderForm(forms.ModelForm):
    class Meta:
        model = Provider
        fields = ["name", "document", "contact", "email", "active"]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if self.instance.pk and self.instance.onus.exclude(status=ONU.Status.DRAFT).exists() and name != self.instance.name:
            raise ValidationError("Há ONUs provisionadas. A alteração do nome exige sincronizar as descrições na OLT primeiro.")
        return name


class VlanForm(forms.ModelForm):
    class Meta:
        model = Vlan
        fields = ["number", "provider", "name"]


class CTOForm(forms.ModelForm):
    class Meta:
        model = CTO
        fields = ["name", "location", "total_ports"]


class OLTForm(forms.ModelForm):
    password = forms.CharField(label="Senha SSH", required=False, widget=forms.PasswordInput(render_value=False),
                               help_text="Armazenada criptografada. Em branco, mantém a credencial atual.")

    class Meta:
        model = OLT
        fields = ["name", "vendor", "model", "firmware", "mode", "host", "ssh_port", "username", "password", "line_profile", "service_profile", "gem_port"]

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            try:
                self.instance.set_password(password)
            except (ValidationError, ValueError) as exc:
                self.add_error("password", str(exc))
        if cleaned.get("mode") == "ssh" and not self.instance.encrypted_password:
            self.add_error("password", "Informe a credencial para a conexão SSH.")
        return cleaned


class ProvisioningProfileForm(forms.ModelForm):
    class Meta:
        model = ProvisioningProfile
        fields = ["name", "provider", "olt", "vlan", "line_profile", "service_profile", "gem_port", "user_vlan", "description", "active"]


class DiscoveryForm(forms.Form):
    olt = forms.ModelChoiceField(queryset=OLT.objects.all(), label="OLT para consulta", empty_label="Selecione uma OLT")


class ONUForm(forms.ModelForm):
    discovery_token = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, discovery=None, **kwargs):
        self.discovery = discovery
        super().__init__(*args, **kwargs)
        profiles = ProvisioningProfile.objects.filter(active=True)
        if self.instance.profile_id:
            profiles = ProvisioningProfile.objects.filter(Q(active=True) | Q(pk=self.instance.profile_id))
        self.fields["profile"].queryset = profiles.select_related("provider", "vlan", "olt")
        self.fields["profile"].help_text = "Selecione um perfil para preencher provedor, OLT e VLAN. Sem perfil, usa os parâmetros padrão da OLT."
    class Meta:
        model = ONU
        fields = ["profile", "provider", "subscriber", "serial", "vlan", "cto", "port", "olt", "frame", "slot", "pon", "ont_id", "service_port"]
        help_texts = {"subscriber": "A descrição será: provedor - nome do cliente.",
                      "service_port": "ID exclusivo nesta OLT; reserve um ID livre antes de provisionar.",
                      "ont_id": "ID exclusivo dentro da porta PON."}

    def clean_serial(self):
        serial = normalize_serial(self.cleaned_data["serial"])
        if ONU.objects.filter(serial__in=serial_aliases(serial)).exclude(pk=self.instance.pk).exists():
            raise ValidationError("Esta ONT já está cadastrada, inclusive em outra representação do serial.")
        return serial

    def clean(self):
        cleaned = super().clean()
        if self.discovery:
            expected = self.discovery
            for field in ("serial", "frame", "slot", "pon"):
                if field in cleaned and cleaned[field] != expected[field]:
                    self.add_error(field, "O dado deve corresponder à ONT descoberta. Faça uma nova busca para selecionar outra ONT.")
            if cleaned.get("olt") and cleaned["olt"].pk != expected["olt"]:
                self.add_error("olt", "A OLT deve corresponder à busca realizada.")
        return cleaned
