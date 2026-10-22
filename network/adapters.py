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
from .serials import normalize_serial


class AdapterError(Exception):
    """Falha conhecida antes de alterar o equipamento."""


class UncertainResult(AdapterError):
    """Uma alteração pode ter ocorrido. Exige conferência presencial na CLI."""


@dataclass(frozen=True)
class OpticalReading:
    tx: float
    rx: float


@dataclass(frozen=True)
class DiscoveredONT:
    serial: str
    frame: int
    slot: int
    pon: int
    vendor: str = ""
    equipment: str = ""

    @property
    def pon_address(self):
        return f"{self.frame}/{self.slot}/{self.pon}"


class SimulatedAdapter:
    def discover(self, olt):
        prefix = hashlib.sha256(f"olt:{olt.pk}".encode()).hexdigest()[:6].upper()
        return [DiscoveredONT(f"HWTC{prefix}{index:02X}", 0, 1, index - 1, "HWTC", "ONT de demonstração")
                for index in (1, 2, 3)]

    def plan(self, onu):
        return [f"SIMULAÇÃO: cadastrar {onu.serial} em {onu.pon_address}, ONT {onu.ont_id}",
                f"SIMULAÇÃO: VLAN {onu.vlan.number}; descrição: {onu.description}",
                f"SIMULAÇÃO: line profile {onu.provisioning_parameters['line_profile']}; "
                f"service profile {onu.provisioning_parameters['service_profile']}; GEM {onu.provisioning_parameters['gem_port']}"]

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
        parameters = onu.provisioning_parameters
        checked = {}
        for name, low, high in [("line_profile", 1, 4095), ("service_profile", 1, 4095), ("gem_port", 0, 4095), ("user_vlan", 1, 4094)]:
            try:
                value = parameters[name]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError
                checked[name] = value
            except (ValueError, KeyError):
                raise AdapterError("O perfil de provisionamento contém parâmetros inválidos. Revise o cadastro.") from None
        return ["enable", "config", f"interface gpon {onu.frame}/{onu.slot}",
                f'ont add {onu.pon} {onu.ont_id} sn-auth "{onu.serial}" omci '
                f'ont-lineprofile-id {checked["line_profile"]} ont-srvprofile-id {checked["service_profile"]} desc "{desc}"',
                "quit", f"service-port {onu.service_port} vlan {onu.vlan.number} gpon {onu.pon_address} "
                f"ont {onu.ont_id} gemport {checked['gem_port']} multi-service user-vlan {checked['user_vlan']} tag-transform translate"]

    @staticmethod
    def empty_discovery(output):
        return bool(re.search(
            r"(?:automatically found ONTs?|ONT autofind information)[^\r\n]*(?:do(?:es)? not exist|not exist)"
            r"|(?:number of (?:GPON )?(?:autofind|auto-find) ONTs?)[^\r\n]*(?::|is)\s*0\b"
            r"|no ONTs? (?:was|were) auto-found", output, re.I))

    @classmethod
    def parse_discovery(cls, output):
        results = []
        current = {}

        def flush():
            if not current:
                return
            if "serial" not in current or "position" not in current:
                raise AdapterError("A resposta de autofind contém um registro incompleto. Confira o formato da CLI deste firmware.")
            frame, slot, pon = current["position"]
            if not (0 <= frame <= 15 and 0 <= slot <= 31 and 0 <= pon <= 31):
                raise AdapterError("A OLT retornou uma posição PON fora da faixa suportada.")
            results.append(DiscoveredONT(current["serial"], frame, slot, pon,
                                         current.get("vendor", ""), current.get("equipment", "")))
            current.clear()

        for line in output.splitlines():
            if re.match(r"\s*Number\s*:\s*\d+\s*$", line, re.I):
                flush()
                continue
            position = re.match(r"\s*F\s*/\s*S\s*/\s*P\s*:\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*$", line, re.I)
            if position:
                if "position" in current:
                    flush()
                current["position"] = tuple(map(int, position.groups()))
                continue
            serial = re.match(r"\s*ONT\s*SN\s*:\s*([A-Za-z0-9-]+)(?:\s|\(|$)", line, re.I)
            if serial:
                normalized = normalize_serial(serial[1])
                if not re.fullmatch(r"(?:[A-Z]{4}[0-9A-F]{8}|[0-9A-F]{16})", normalized):
                    raise AdapterError("A resposta de autofind contém um serial fora do formato suportado.")
                current["serial"] = normalized
                continue
            for label, key in [("VendorID", "vendor"), ("EquipmentID", "equipment")]:
                value = re.match(rf"\s*ONT\s*{label}\s*:\s*(.*?)\s*$", line, re.I)
                if value:
                    current[key] = "".join(char for char in value[1] if char.isprintable())[:120]
        flush()
        if not results and not cls.empty_discovery(output):
            raise AdapterError("Não foi possível interpretar a resposta de display ont autofind all. Confira o formato e as permissões da CLI.")
        unique = {}
        for item in results:
            if item.serial in unique and unique[item.serial] != item:
                raise AdapterError("A busca retornou o mesmo serial em posições ou registros diferentes. Confira a OLT.")
            unique[item.serial] = item
        return list(unique.values())

    def discover(self, olt):
        with self.session(olt) as channel:
            self.command(channel, "enable")
            self.command(channel, "config")
            return self.parse_discovery(self.command(channel, "display ont autofind all"))

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
        empty_search = command == "display ont autofind all" and self.empty_discovery(output)
        if not empty_search and re.search(r"(?:Failure|Error:|Unknown command|Unrecognized command|does not exist|already exists)", output, re.I):
            raise AdapterError("A OLT rejeitou um comando. Confira o estado e o plano na CLI; a resposta bruta não é salva no ERP.")
        return output

    def provision(self, onu):
        commands = self.plan(onu)
        with self.session(onu.olt) as channel:
            changing = False
            try:
                for command in commands:
                    if onu.discovered_at and command.startswith("interface gpon"):
                        found = self.parse_discovery(self.command(channel, "display ont autofind all"))
                        if not any(item.serial == normalize_serial(onu.serial) and
                                   (item.frame, item.slot, item.pon) == (onu.frame, onu.slot, onu.pon) for item in found):
                            raise AdapterError("A ONT não está mais no autofind desta PON. Faça uma nova busca e confira o cadastro antes de provisionar.")
                    if command.startswith(("ont add", "service-port")):
                        changing = True
                    self.command(channel, command)
            except (AdapterError, OSError, paramiko.SSHException) as exc:
                if changing:
                    raise UncertainResult("Resultado remoto incerto. Confira ONT e service-port na OLT antes de qualquer nova tentativa.") from None
                if isinstance(exc, AdapterError):
                    raise
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
