"""Adaptadores isolam a CLI. Simulação nunca abre conexão com equipamentos."""
import hashlib
import re
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass

import paramiko
from django.conf import settings
from django.core.exceptions import ValidationError


class AdapterError(Exception):
    """Falha conhecida antes de alterar o equipamento."""


class UncertainResult(AdapterError):
    """Uma alteração pode ter ocorrido. Exige conferência presencial na CLI."""


@dataclass(frozen=True)
class OpticalReading:
    tx: float
    rx: float


class SimulatedAdapter:
    def plan(self, onu):
        return [f"SIMULAÇÃO: cadastrar {onu.serial} em {onu.pon_address}, ONT {onu.ont_id}",
                f"SIMULAÇÃO: VLAN {onu.vlan.number}; descrição: {onu.description}"]

    def provision(self, onu):
        return "Provisionamento simulado concluído. Nenhuma conexão com a OLT foi aberta."

    def optics(self, onu):
        seed = int(hashlib.sha256(onu.serial.encode()).hexdigest()[:8], 16)
        return OpticalReading(tx=round(1.5 + (seed % 150) / 100, 2), rx=round(-17 - (seed % 900) / 100, 2))


class HuaweiAdapter:
    """CLI GPON Huawei MA56xx/MA58xx. Homologação por firmware é obrigatória.

    Perfis e VLANs devem existir na OLT. Não salva configuração global, reinicia,
    remove ONUs nem faz rollback automático de alterações remotas.
    """

    def plan(self, onu):
        if not re.fullmatch(r"(?:[A-Z]{4}[0-9A-F]{8}|[0-9A-F]{16})", onu.serial):
            raise AdapterError("Para Huawei, use serial com 4 letras + 8 hexadecimais ou 16 hexadecimais.")
        text = unicodedata.normalize("NFKD", onu.description).encode("ascii", "ignore").decode()
        desc = re.sub(r"[^A-Za-z0-9 ._-]", "", text)
        if not desc or len(desc) > 64:
            raise AdapterError("A descrição Huawei deve ter no máximo 64 caracteres ASCII após normalização.")
        return ["enable", "config", f"interface gpon {onu.frame}/{onu.slot}",
                f'ont add {onu.pon} {onu.ont_id} sn-auth "{onu.serial}" omci '
                f'ont-lineprofile-id {onu.olt.line_profile} ont-srvprofile-id {onu.olt.service_profile} desc "{desc}"',
                "quit", f"service-port {onu.service_port} vlan {onu.vlan.number} gpon {onu.pon_address} "
                f"ont {onu.ont_id} gemport {onu.olt.gem_port} multi-service user-vlan {onu.vlan.number} tag-transform translate"]

    def preflight(self, olt):
        if not settings.ENABLE_LIVE_OLT:
            raise AdapterError("SSH real desabilitado. Homologue modelo/firmware e configure ENABLE_LIVE_OLT=1 para habilitar.")
        if not olt.model or not olt.host or not olt.username:
            raise AdapterError("Preencha modelo/firmware, endereço e usuário da OLT.")
        target = olt.model.upper().replace("-", "").replace(" ", "")
        expected = {"MA5800X7": "MA5800V100R022C11", "MA5683T": "MA5600V800R017C10"}
        if target not in expected or olt.firmware != expected[target]:
            raise AdapterError("O alvo SSH deve ser MA5800-X7 / MA5800V100R022C11 ou MA5683T / MA5600V800R017C10.")

    @contextmanager
    def session(self, olt):
        self.preflight(olt)
        client = paramiko.SSHClient()
        try:
            client.load_host_keys(settings.OLT_KNOWN_HOSTS)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(olt.host, port=olt.ssh_port, username=olt.username, password=olt.get_password(),
                           look_for_keys=False, allow_agent=False, timeout=10, auth_timeout=10, banner_timeout=10)
            channel = client.invoke_shell(width=240, height=100)
            self.read_prompt(channel)
            version = self.command(channel, "display version")
            if olt.firmware not in version:
                raise AdapterError("O firmware informado não corresponde ao retornado pela OLT.")
        except AdapterError:
            client.close()
            raise
        except (OSError, paramiko.SSHException, ValidationError):
            client.close()
            raise AdapterError("Conexão SSH não estabelecida. Confira rede, credenciais e a chave verificada em known_hosts.") from None
        try:
            yield channel
        finally:
            client.close()

    def read_prompt(self, channel):
        deadline = time.monotonic() + 15
        output = ""
        completed_at = -1
        while time.monotonic() < deadline:
            if channel.recv_ready():
                chunk = channel.recv(65536).decode("utf-8", "replace")
                if not chunk:
                    raise AdapterError("A OLT encerrou a conexão.")
                output += chunk
                if len(output) > 1_000_000:
                    raise AdapterError("Resposta SSH acima do limite.")
                if re.search(r"----\s*More", chunk, re.I):
                    channel.send(" ")
                # Huawei pede Enter para finalizar argumentos opcionais da CLI.
                if len(output) != completed_at and re.search(r"\{\s*<cr>[^\n]*\}\s*:\s*$", output, re.I):
                    channel.sendall("\n")
                    completed_at = len(output)
                if re.search(r"(?:^|\n)[^\n]{1,100}[>#]\s*$", output):
                    return output
            if channel.closed or channel.exit_status_ready():
                raise AdapterError("A OLT encerrou a conexão antes da confirmação.")
            time.sleep(0.05)
        raise AdapterError("A OLT não retornou um prompt no tempo esperado.")

    def command(self, channel, command):
        channel.sendall(command + "\n")
        output = self.read_prompt(channel)
        if re.search(r"(?:Failure|Error:|Unknown command|Unrecognized command|does not exist|already exists)", output, re.I):
            raise AdapterError("A OLT rejeitou um comando. Confira o estado e o plano na CLI; a resposta bruta não é salva no ERP.")
        return output

    def provision(self, onu):
        commands = self.plan(onu)
        with self.session(onu.olt) as channel:
            changing = False
            try:
                for command in commands:
                    if command.startswith(("ont add", "service-port")):
                        changing = True
                    self.command(channel, command)
            except (AdapterError, OSError, paramiko.SSHException):
                if changing:
                    raise UncertainResult("Resultado remoto incerto. Confira ONT e service-port na OLT antes de qualquer nova tentativa.") from None
                raise AdapterError("A sessão falhou antes do envio de comandos de alteração.") from None
        return "CLI confirmou o cadastro da ONT e do service-port. Configuração persistente da OLT deve seguir a política da sua rede."

    @staticmethod
    def parse_optics(output):
        values = {}
        for label in ("Tx", "Rx"):
            match = re.search(rf"^\s*(?:ONT\s+)?{label}\s+optical\s+power\s*\(\s*dBm\s*\)\s*:\s*(-?\d+(?:\.\d+)?)", output, re.I | re.M)
            if not match:
                raise AdapterError("A OLT não retornou TX/RX numéricos no formato suportado. Confira a ONU e o firmware.")
            value = float(match[1])
            if not -60 <= value <= 15:
                raise AdapterError("Leitura óptica fora da faixa suportada; nenhuma potência foi atualizada.")
            values[label] = value
        return OpticalReading(tx=values["Tx"], rx=values["Rx"])

    def optics(self, onu):
        with self.session(onu.olt) as channel:
            for command in ["enable", "config", f"interface gpon {onu.frame}/{onu.slot}"]:
                self.command(channel, command)
            return self.parse_optics(self.command(channel, f"display ont optical-info {onu.pon} {onu.ont_id}"))


def get_adapter(olt):
    if olt.mode == "simulation":
        return SimulatedAdapter()
    if olt.mode == "ssh" and olt.vendor == "huawei":
        return HuaweiAdapter()
    raise AdapterError("Não há adaptador homologado para esta combinação. Use simulação até integrar o fabricante.")
