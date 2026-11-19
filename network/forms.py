from collections import Counter

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
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


class VlanIntervalForm(forms.Form):
    first = forms.IntegerField(label="VLAN inicial", min_value=1, max_value=4094)
    last = forms.IntegerField(label="VLAN final", min_value=1, max_value=4094)
    provider = forms.ModelChoiceField(queryset=Provider.objects.all(), label="Provedor")

    def clean(self):
        data = super().clean()
        first, last = data.get("first"), data.get("last")
        if first is not None and last is not None and first > last:
            self.add_error("last", "A VLAN final deve ser igual ou maior que a inicial.")
        return data

    def vlan_range(self):
        return Vlan.objects.filter(number__range=(self.cleaned_data["first"], self.cleaned_data["last"]))


class VlanRangeForm(VlanIntervalForm):
    name = forms.CharField(
        label="Descrição", max_length=120, required=False,
        help_text="A mesma descrição é usada para as VLANs novas.",
    )

    def save(self):
        first, last = self.cleaned_data["first"], self.cleaned_data["last"]
        provider = self.cleaned_data["provider"]
        name = self.cleaned_data["name"]
        with transaction.atomic():
            vlan_range = self.vlan_range().select_for_update()
            existing = dict(vlan_range.values_list("number", "provider_id"))
            conflicts = [str(number) for number, provider_id in existing.items() if provider_id != provider.pk]
            if conflicts:
                numbers = ", ".join(conflicts[:10])
                if len(conflicts) > 10:
                    numbers += f" e mais {len(conflicts) - 10}"
                raise ValidationError(f"Há VLANs vinculadas a outro provedor na faixa ({numbers}). Ajuste os limites.")
            new_vlans = [Vlan(number=number, provider=provider, name=name)
                         for number in range(first, last + 1) if number not in existing]
            Vlan.objects.bulk_create(new_vlans)
        return len(new_vlans), len(existing)


class CTOForm(forms.ModelForm):
    class Meta:
        model = CTO
        fields = ["name", "location", "total_ports"]


class OLTForm(forms.ModelForm):
    telnet_port = forms.IntegerField(label="Porta Telnet", min_value=1, max_value=65535, initial=23, required=False,
                                     help_text="Porta usada no modo Telnet. O padrão é 23.")
    password = forms.CharField(label="Senha de gerenciamento", required=False, widget=forms.PasswordInput(render_value=False),
                               help_text="Armazenada criptografada. Em branco, mantém a credencial atual.")

    class Meta:
        model = OLT
        fields = ["name", "vendor", "model", "firmware", "mode", "host", "ssh_port", "telnet_port",
                  "username", "password", "line_profile", "service_profile", "gem_port"]

    def clean_telnet_port(self):
        return self.cleaned_data["telnet_port"] or self.instance.telnet_port

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            try:
                self.instance.set_password(password)
            except (ValidationError, ValueError) as exc:
                self.add_error("password", str(exc))
        if cleaned.get("mode") in {"ssh", "telnet"} and not self.instance.encrypted_password:
            self.add_error("password", "Informe a senha de gerenciamento da OLT.")
        return cleaned


class ProvisioningProfileForm(forms.ModelForm):
    vlan = forms.ChoiceField(
        label="VLAN de serviço",
        help_text="Ao selecionar um grupo, o número da VLAN é escolhido no cadastro da ONU.",
    )

    class Meta:
        model = ProvisioningProfile
        fields = ["name", "provider", "olt", "vlan", "line_profile", "service_profile", "gem_port", "user_vlan", "description", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        vlans = list(Vlan.objects.select_related("provider"))
        group_sizes = Counter((vlan.provider_id, vlan.name) for vlan in vlans if vlan.name)
        current_group = (self.instance.provider_id, self.instance.vlan_description)

        choices = {"": "Selecione uma VLAN ou descrição"}
        self.vlan_choice_providers = {}
        self.vlan_groups = {}
        for vlan in vlans:
            group = (vlan.provider_id, vlan.name)
            size = group_sizes[group]
            grouped = size > 1 or (size and group == current_group)
            if grouped:
                value = f"grupo:{vlan.provider_id}:{vlan.name}"
                unit = "VLAN" if size == 1 else "VLANs"
                label = f"{vlan.name} · {size} {unit}"
                choices[value] = label
                self.vlan_choice_providers[value] = vlan.provider_id
                self.vlan_groups[value] = group
            if not grouped or vlan.pk == self.instance.vlan_id:
                value = str(vlan.pk)
                choices[value] = f"{vlan.number} · {vlan.name or vlan.provider.name}"
                self.vlan_choice_providers[value] = vlan.provider_id

        self.fields["vlan"].choices = list(choices.items())
        if self.instance.vlan_description:
            self.initial["vlan"] = f"grupo:{self.instance.provider_id}:{self.instance.vlan_description}"

    def clean_vlan(self):
        selected = self.cleaned_data["vlan"]
        group = self.vlan_groups.get(selected)
        if group:
            provider = self.cleaned_data.get("provider")
            provider_id, description = group
            if provider is None or provider.pk != provider_id:
                raise ValidationError("O grupo de VLANs deve pertencer ao provedor do perfil.")
            self.instance.vlan_description = description
            return None
        self.instance.vlan_description = ""
        vlan = Vlan.objects.filter(pk=selected).first()
        if vlan is None:
            raise ValidationError("A VLAN selecionada não está mais cadastrada.")
        return vlan


class DiscoveryForm(forms.Form):
    olt = forms.ModelChoiceField(queryset=OLT.objects.all(), label="OLT para consulta", empty_label="Selecione uma OLT")


class InventoryForm(forms.Form):
    provider = forms.ModelChoiceField(queryset=Provider.objects.all(), label="Provedor", empty_label="Selecione um provedor")


class ONUForm(forms.ModelForm):
    discovery_token = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, discovery=None, **kwargs):
        self.discovery = discovery
        super().__init__(*args, **kwargs)
        profiles = ProvisioningProfile.objects.filter(active=True)
        if self.instance.profile_id:
            profiles = ProvisioningProfile.objects.filter(Q(active=True) | Q(pk=self.instance.profile_id))
        self.fields["profile"].queryset = profiles.select_related("provider", "vlan", "olt")
        self.fields["profile"].help_text = "O perfil preenche provedor e OLT. Se ele usa um grupo, escolha a VLAN dentro desse grupo."
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
