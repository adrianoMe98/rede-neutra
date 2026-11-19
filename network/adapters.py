"""Adaptadores isolam a CLI. Simulação nunca abre conexão com equipamentos."""
import hashlib
import re
import socket
import time
import unicodedata
from codecs import getincrementaldecoder
from contextlib import contextmanager
from dataclasses import dataclass

import paramiko
from django.conf import settings
from django.core.exceptions import ValidationError
from telnetlib3.sync import TelnetConnection
from .serials import normalize_serial


class AdapterError(Exception):
    """Falha conhecida antes de alterar o equipamento."""

    def __init__(self, message, *, code="cli"):
        super().__init__(message)
        self.code = code


class UncertainResult(AdapterError):
    """Uma alteração pode ter ocorrido. Exige conferência presencial na CLI."""


class PromptTimeout(AdapterError):
    """O equipamento não retornou o prompt esperado a tempo."""


class TelnetLoginTimeout(AdapterError):
    """O login não avançou para o prompt esperado."""


def cli_prompt_pattern(hostname=r"[\w.-]+"):
    return re.compile(
        rf"(?:^|\n)[ \t]*(?:<({hostname})>|({hostname})(?:\([^\n]*\))?[>#])[ \t]*\n?\Z"
    )


CLI_PROMPT = cli_prompt_pattern()
LOGIN_PROMPT = re.compile(r"(?:^|\n)[ \t]*(?:>>[ \t]*)?(?:user[ \t]*name|login)[ \t]*:[ \t]*\n?\Z", re.I)
PASSWORD_PROMPT = re.compile(r"(?:^|\n)[ \t]*(?:>>[ \t]*)?(?:user[ \t]+)?password[ \t]*:[ \t]*\n?\Z", re.I)
LOGIN_REJECTED = re.compile(
    r"(?:^|\n)[ \t]*(?:(?:error|failure)[ \t]*:[ \t]*)?"
    r"(?:invalid password|incorrect password|authentication failed|login failed|access denied)[.!]?[ \t]*\n?\Z",
    re.I,
)
PAGINATION_MARKER = re.compile(r"^[ \t]*(?:----[ \t]*More[^\n]*?----|--More--)", re.I | re.M)


class TelnetTerminal:
    def __init__(self, connection):
        self.connection = connection

    def read(self, timeout):
        try:
            return self.connection.read_some(timeout=timeout)
        except TimeoutError:
            raise PromptTimeout("A OLT não retornou um prompt no tempo esperado.") from None
        except (EOFError, OSError):
            raise AdapterError("A conexão Telnet foi interrompida.") from None

    def sendall(self, text):
        try:
            self.connection.write(text.replace("\n", "\r\n").encode("utf-8"))
            self.connection.flush()
        except (EOFError, OSError):
            raise AdapterError("A conexão Telnet foi interrompida durante o envio.") from None


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
    def test_connection(self, olt):
        return "Teste simulado concluído. Nenhuma conexão com uma OLT física foi aberta."

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

    protocol = "SSH"

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
    def clean_output(output):
        output = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", output)
        output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
        while "\b" in output:
            output = re.sub(r"[^\n\b]\x08|^\x08", "", output, flags=re.M)
        return output.replace("\r\x00", "").replace("\r", "")

    @staticmethod
    def empty_discovery(output):
        return bool(re.search(
            r"(?:automatically found ONTs?|ONT autofind information)[^\r\n]*(?:do(?:es)? not exist|not exist)"
            r"|(?:number of (?:GPON )?(?:autofind|auto-find) ONTs?)[^\r\n]*(?::|is)\s*0\b"
            r"|no ONTs? (?:was|were) auto-found", output, re.I))

    @classmethod
    def parse_discovery(cls, output):
        output = cls.clean_output(output)
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
            serial = re.match(r"\s*ONT\s*SN(?:\s*\((?:HEX|ASCII)\))?\s*:\s*([A-Za-z0-9-]+)(?:\s|\(|$)", line, re.I)
            if serial:
                normalized = normalize_serial(serial[1])
                if not re.fullmatch(r"(?:[A-Z]{4}[0-9A-F]{8}|[0-9A-F]{16})", normalized):
                    raise AdapterError("A resposta de autofind contém um serial fora do formato suportado.")
                current["serial"] = normalized
                continue
            for label, key in [(r"Vendor\s*ID", "vendor"), (r"Equipment\s*ID", "equipment")]:
                value = re.match(rf"\s*ONT\s*{label}\s*:\s*(.*?)\s*$", line, re.I)
                if value:
                    current[key] = "".join(char for char in value[1] if char.isprintable())[:120]
        flush()
        if not results and not cls.empty_discovery(output):
            raise AdapterError("Não foi possível interpretar a resposta de display ont autofind all. Confira o formato e as permissões da CLI.")
        unique = {}
        for item in results:
            previous = unique.get(item.serial)
            if previous:
                if previous.pon_address != item.pon_address:
                    raise AdapterError("A busca retornou o mesmo serial em posições diferentes. Confira a OLT.")
                item = DiscoveredONT(item.serial, item.frame, item.slot, item.pon,
                                     previous.vendor or item.vendor, previous.equipment or item.equipment)
            unique[item.serial] = item
        return list(unique.values())

    def discover(self, olt):
        with self.session(olt, read_only=True) as channel:
            self.command(channel, "enable")
            self.command(channel, "config")
            return self.parse_discovery(self.command(channel, "display ont autofind all"))

    def test_connection(self, olt):
        with self.session(olt, read_only=True):
            return f"Comunicação {self.protocol} confirmada: autenticação, prompt e firmware {olt.firmware} verificados."

    def preflight(self, olt, *, read_only=False):
        if not read_only and not settings.ENABLE_LIVE_OLT:
            raise AdapterError(f"Provisionamento {self.protocol} desabilitado. Homologue modelo/firmware e configure ENABLE_LIVE_OLT=1 para habilitar o envio.", code="configuration")
        if not olt.model or not olt.host or not olt.username:
            raise AdapterError("Preencha modelo/firmware, endereço e usuário da OLT.", code="configuration")
        target = olt.model.upper().replace("-", "").replace(" ", "")
        expected = {"MA5800X7": "MA5800V100R022C11", "MA5683T": "MA5600V800R017C10"}
        if target not in expected or olt.firmware != expected[target]:
            raise AdapterError(f"O alvo {self.protocol} deve ser MA5800-X7 / MA5800V100R022C11 ou MA5683T / MA5600V800R017C10.", code="configuration")

    def read_password(self, olt):
        try:
            return olt.get_password()
        except ValidationError:
            raise AdapterError(f"Não foi possível recuperar a senha {self.protocol}. Confira OLT_CREDENTIAL_KEY e recadastre a senha da OLT.", code="authentication") from None

    def check_firmware(self, channel, olt):
        version = self.command(channel, "display version")
        if olt.firmware not in version:
            raise AdapterError(f"A OLT respondeu por {self.protocol}, mas o firmware informado não corresponde ao retornado pela OLT.", code="firmware")

    @contextmanager
    def session(self, olt, *, read_only=False):
        self.preflight(olt, read_only=read_only)
        client = paramiko.SSHClient()
        try:
            try:
                client.load_host_keys(settings.OLT_KNOWN_HOSTS)
            except (OSError, paramiko.SSHException, paramiko.hostkeys.InvalidHostKey):
                raise AdapterError("Arquivo known_hosts ausente ou inválido. Cadastre a chave SSH verificada da OLT no arquivo configurado.", code="host_key") from None
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            host_key_name = olt.host if olt.ssh_port == 22 else f"[{olt.host}]:{olt.ssh_port}"
            if not client.get_host_keys().lookup(host_key_name):
                raise AdapterError("A chave SSH desta OLT/porta não está cadastrada em known_hosts. Confira a impressão digital com o responsável pela rede e cadastre a chave.", code="host_key")
            password = self.read_password(olt)
            client.connect(olt.host, port=olt.ssh_port, username=olt.username, password=password,
                           look_for_keys=False, allow_agent=False, timeout=10, auth_timeout=10, banner_timeout=10)
            channel = client.invoke_shell(width=240, height=100)
            self.read_prompt(channel)
            self.check_firmware(channel, olt)
        except AdapterError:
            client.close()
            raise
        except paramiko.AuthenticationException:
            client.close()
            raise AdapterError("A OLT recusou a autenticação SSH. Confira usuário, senha e permissões da conta.", code="authentication") from None
        except paramiko.BadHostKeyException:
            client.close()
            raise AdapterError("A chave SSH recebida difere da chave cadastrada. Confira a impressão digital da OLT antes de atualizar known_hosts.", code="host_key") from None
        except socket.gaierror:
            client.close()
            raise AdapterError("Não foi possível resolver o hostname da OLT. Confira o endereço e o DNS do servidor.", code="network") from None
        except (TimeoutError, socket.timeout):
            client.close()
            raise AdapterError("Tempo de conexão SSH esgotado. Confira IP, porta, rota, VPN e regras de acesso até a OLT.", code="network") from None
        except (OSError, paramiko.ssh_exception.NoValidConnectionsError):
            client.close()
            raise AdapterError("Não foi possível abrir a conexão SSH. Confira IP, porta, serviço SSH e regras de acesso da OLT.", code="network") from None
        except paramiko.SSHException:
            client.close()
            raise AdapterError("Falha na negociação SSH. Confira o serviço SSH e a compatibilidade dos algoritmos do equipamento.", code="ssh") from None
        try:
            yield channel
        finally:
            client.close()

    def read_prompt(self, channel, *, prompts=None):
        if prompts is None:
            cli_prompt = getattr(channel, "cli_prompt", None)
            prompts = (cli_prompt if isinstance(cli_prompt, re.Pattern) else CLI_PROMPT,)
        deadline = time.monotonic() + 15
        output = ""
        decoder = getincrementaldecoder("utf-8")("replace")
        completed_at = -1
        paged_at = 0
        while (remaining := deadline - time.monotonic()) > 0:
            if isinstance(channel, TelnetTerminal):
                chunk = channel.read(remaining)
            elif channel.recv_ready():
                chunk = channel.recv(65536)
            elif channel.closed or channel.exit_status_ready():
                raise AdapterError("A OLT encerrou a conexão antes da confirmação.")
            else:
                time.sleep(0.05)
                continue
            if not chunk:
                raise AdapterError("A OLT encerrou a conexão.")
            output += decoder.decode(chunk)
            if len(output) > 1_000_000:
                raise AdapterError(f"Resposta {self.protocol} acima do limite.")
            cleaned = self.clean_output(output)
            # O marcador de paginação pode chegar dividido em vários pacotes.
            for marker in PAGINATION_MARKER.finditer(cleaned):
                if marker.end() > paged_at:
                    channel.sendall(" ")
                    paged_at = marker.end()
            # A OLT pode escrever o primeiro registro da página na linha do marcador.
            cleaned = PAGINATION_MARKER.sub("", cleaned)
            # Huawei pede Enter para finalizar argumentos opcionais da CLI.
            if len(output) != completed_at and re.search(r"\{\s*<cr>[^\n]*\}\s*:\s*$", cleaned, re.I):
                channel.sendall("\n")
                completed_at = len(output)
            for prompt in prompts:
                if match := prompt.search(cleaned):
                    if prompt is CLI_PROMPT:
                        # Seções como <bbs-config> também chegam sozinhas em um pacote.
                        hostname = match[1] or match[2]
                        channel.cli_prompt = cli_prompt_pattern(re.escape(hostname))
                    return cleaned
        raise PromptTimeout("A OLT não retornou um prompt no tempo esperado.")

    def command(self, channel, command, *, empty_response=None):
        channel.sendall(command + "\n")
        output = self.read_prompt(channel)
        error_pattern = r"Failure|Error\s*:|Unknown command|Unrecognized command|does not exist|do not exist|already exists|(?:permission|access) denied|insufficient privilege"
        if command.startswith("display current-configuration ont "):
            # A descrição do assinante pode conter palavras da lista de erros.
            error_pattern = rf"^\s*(?:%\s*)?(?:{error_pattern})"
        errors = re.compile(error_pattern, re.I)
        for line in output.splitlines():
            allowed_empty = command == "display ont autofind all" and self.empty_discovery(line)
            allowed_empty = allowed_empty or (empty_response is not None and empty_response(line))
            if errors.search(line) and not allowed_empty:
                raise AdapterError("A OLT rejeitou um comando da CLI. Confira as permissões da conta SSH e o formato suportado pelo firmware; a resposta bruta não é salva no ERP.")
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
        with self.session(onu.olt, read_only=True) as channel:
            for command in ["enable", "config", f"interface gpon {onu.frame}/{onu.slot}"]:
                self.command(channel, command)
            return self.parse_optics(self.command(channel, f"display ont optical-info {onu.pon} {onu.ont_id}"))


class HuaweiTelnetAdapter(HuaweiAdapter):
    protocol = "Telnet"

    def login(self, channel, username, password):
        expected = "o prompt inicial"
        try:
            initial = self.read_prompt(channel, prompts=(LOGIN_PROMPT, PASSWORD_PROMPT))
            if LOGIN_PROMPT.search(initial):
                channel.sendall(username + "\n")
                expected = "o prompt de senha"
                challenge = self.read_prompt(channel, prompts=(LOGIN_PROMPT, PASSWORD_PROMPT, LOGIN_REJECTED))
                if not PASSWORD_PROMPT.search(challenge):
                    raise AdapterError("A OLT recusou o usuário cadastrado para o login Telnet.", code="authentication")
            channel.sendall(password + "\n")
            expected = "o prompt da CLI após o envio da senha"
            result = self.read_prompt(channel, prompts=(CLI_PROMPT, LOGIN_PROMPT, PASSWORD_PROMPT, LOGIN_REJECTED))
            if not CLI_PROMPT.search(result):
                raise AdapterError("A OLT recusou o login Telnet. Confira o usuário e a senha cadastrados.", code="authentication")
        except PromptTimeout:
            raise TelnetLoginTimeout(f"Tempo esgotado aguardando {expected} no login Telnet.", code="authentication") from None
        except AdapterError as error:
            if error.code == "authentication":
                raise
            raise AdapterError("A sessão Telnet foi interrompida durante o login.", code="authentication") from None

    @contextmanager
    def session(self, olt, *, read_only=False):
        self.preflight(olt, read_only=read_only)
        password = self.read_password(olt)
        if not password or any(char in value for value in (olt.username, password) for char in "\r\n\x00"):
            raise AdapterError("Informe credenciais Telnet válidas para o login da OLT.", code="authentication")
        attempts = 2 if read_only else 1
        for attempt in range(attempts):
            connection = TelnetConnection(olt.host, port=olt.telnet_port, timeout=10, connect_timeout=10,
                                          encoding=False, cols=240, rows=100)
            try:
                try:
                    connection.connect()
                except (OSError, EOFError):
                    raise AdapterError("Não foi possível abrir a conexão Telnet. Confira IP, porta, rota e acesso à rede de gerenciamento.", code="network") from None
                channel = TelnetTerminal(connection)
                try:
                    self.login(channel, olt.username, password)
                except TelnetLoginTimeout:
                    if attempt + 1 == attempts:
                        raise
                    continue
                self.check_firmware(channel, olt)
                yield channel
                return
            finally:
                connection.close()


def get_adapter(olt):
    if olt.mode == "simulation":
        return SimulatedAdapter()
    if olt.mode == "ssh" and olt.vendor == "huawei":
        return HuaweiAdapter()
    if olt.mode == "telnet" and olt.vendor == "huawei":
        return HuaweiTelnetAdapter()
    raise AdapterError("Não há adaptador homologado para esta combinação. Use simulação até integrar o fabricante.")
