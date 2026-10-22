from django import forms
from django.core.exceptions import ValidationError
from .models import CTO, OLT, ONU, Provider, Vlan


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


class ONUForm(forms.ModelForm):
    class Meta:
        model = ONU
        fields = ["provider", "subscriber", "serial", "vlan", "cto", "port", "olt", "frame", "slot", "pon", "ont_id", "service_port"]
        help_texts = {"subscriber": "A descrição será: provedor - nome do cliente.",
                      "service_port": "ID exclusivo nesta OLT; reserve um ID livre antes de provisionar.",
                      "ont_id": "ID exclusivo dentro da porta PON."}

    def clean_serial(self):
        return self.cleaned_data["serial"].strip().upper()
