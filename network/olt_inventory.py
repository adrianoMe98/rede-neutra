"""Inventário GPON por VLAN de serviço, consultado diretamente nas OLTs."""
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass, field

import paramiko

from .adapters import AdapterError, HuaweiAdapter, get_adapter
from .models import OLT, ONU, ProvisioningProfile, Vlan


TOTAL = re.compile(r"^\s*Total\s*:\s*(\d+)\b", re.I | re.M)
SERVICE_ROW = re.compile(r"^\s*(\d+)\s+(\d+)\s+\S+\s+(\S+)\s+(.*)$")
ONT_POSITION = re.compile(r"^(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s+(\d+)(?=\s|$)")
PON_TYPES = {"gpon", "xgpon", "xgspon", "xg-pon", "xgs-pon", "epon", "10gepon"}
MAC_ADDRESS = r"(?:[0-9a-f]{4}(?:-[0-9a-f]{4}){2}|[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}|[0-9a-f]{12})"
MAC_ROW = re.compile(rf"^\s*(\d+|-)\s+(?:\S+\s+){{1,2}}({MAC_ADDRESS})\s+\S+", re.I)
EMPTY_SERVICE_PORTS = re.compile(r"\b(?:no service[- ](?:virtual )?ports?|service[- ](?:virtual )?ports?\s+(?:does|do) not exist)\b", re.I)
EMPTY_MAC_ADDRESSES = re.compile(r"\b(?:no MAC (?:addresses?|entries)|MAC addresses?\s+(?:does|do) not exist)\b", re.I)
CONFIG_END = re.compile(r"^\s*(?:#|\[|<|interface\s|ont\s|service-port\s|return\b)", re.I)
SERIAL = re.compile(r"(?:[A-Z]{4}-?[0-9A-F]{8}|[0-9A-F]{16})", re.I)


@dataclass(frozen=True)
class ONTConfiguration:
    description: str = ""
    serial: str = ""
    mac: str = ""


@dataclass(frozen=True)
class InventoryRow:
    position: tuple[int, int, int, int]
    service_ports: tuple[int, ...]
    ont: ONTConfiguration
    learned_macs: tuple[str, ...]
    detail: str = ""


@dataclass(frozen=True)
class VLANResult:
    vlan: int
    status: str
    detail: str = ""
    rows: list[InventoryRow] = field(default_factory=list)


def provider_targets(provider):
    vlan_numbers = defaultdict(set)
    for model in (ProvisioningProfile, ONU):
        pairs = model.objects.filter(provider=provider, vlan__provider=provider).order_by()
        for olt_id, vlan in pairs.values_list("olt_id", "vlan__number").distinct():
            vlan_numbers[olt_id].add(vlan)
    grouped_profiles = ProvisioningProfile.objects.filter(provider=provider, vlan__isnull=True).order_by()
    numbers_by_description = defaultdict(set)
    for description, number in Vlan.objects.filter(provider=provider).exclude(name="").values_list("name", "number"):
        numbers_by_description[description].add(number)
    for olt_id, description in grouped_profiles.values_list("olt_id", "vlan_description").distinct():
        if description in numbers_by_description:
            vlan_numbers[olt_id].update(numbers_by_description[description])
    return [(olt, sorted(vlan_numbers[olt.pk])) for olt in OLT.objects.filter(pk__in=vlan_numbers)]


def verify_table(output, count, empty_pattern, label):
    totals = TOTAL.findall(output)
    if totals and int(totals[-1]) == count:
        return
    if not totals and not count and empty_pattern.search(output):
        return
    raise AdapterError(f"A resposta de {label} está incompleta ou em um formato desconhecido.")


def parse_service_ports(output, vlan):
    ports_by_ont = defaultdict(set)
    ports_seen = set()
    for line in output.splitlines():
        row = SERVICE_ROW.match(line)
        if not row:
            continue
        service_port, row_vlan = map(int, row.groups()[:2])
        if row_vlan != vlan or service_port in ports_seen:
            raise AdapterError("A OLT retornou VLAN ou service-port incompatível com a consulta.")
        ports_seen.add(service_port)
        if row[3].lower() not in PON_TYPES:
            continue
        position = ONT_POSITION.match(row[4])
        if not position:
            raise AdapterError("Não foi possível identificar uma posição de ONT na tabela de service-ports.")
        address = tuple(map(int, position.groups()))
        if any(value > maximum for value, maximum in zip(address, (15, 31, 31, 255))):
            raise AdapterError("A tabela de service-ports retornou uma posição de ONT fora da faixa suportada.")
        ports_by_ont[address].add(service_port)
    verify_table(output, len(ports_seen), EMPTY_SERVICE_PORTS, "service-ports")
    return ports_by_ont


def format_mac(value):
    digits = re.sub(r"[:-]", "", value).upper()
    return ":".join(digits[index:index + 2] for index in range(0, 12, 2))


def parse_mac_table(output):
    macs_by_port = defaultdict(set)
    count = 0
    for line in output.splitlines():
        row = MAC_ROW.match(line)
        if row:
            count += 1
            if row[1] != "-":
                macs_by_port[int(row[1])].add(format_mac(row[2]))
    verify_table(output, count, EMPTY_MAC_ADDRESSES, "endereços MAC")
    return macs_by_port


def parse_ont_configuration(output, position):
    _, _, pon, ont_id = position
    start = re.compile(rf"^\s*ont\s+add\s+{pon}\s+{ont_id}\s+(.+)$", re.I)
    arguments = []
    for line in output.splitlines():
        if arguments:
            if CONFIG_END.match(line):
                break
            arguments.append(line.strip())
        elif match := start.match(line):
            arguments.append(match[1])
    try:
        options = shlex.split(" ".join(arguments))
    except ValueError:
        options = []
    if not options:
        raise AdapterError("Não foi possível interpretar a configuração da ONT nesta posição.")
    description = options[-1] if len(options) >= 2 and options[-2].lower() == "desc" else ""
    auth, identifier = options[:2] if len(options) >= 2 else ("", "")
    serial = identifier.upper() if auth.lower() == "sn-auth" and SERIAL.fullmatch(identifier) else ""
    mac = format_mac(identifier) if auth.lower() == "mac-auth" and re.fullmatch(MAC_ADDRESS, identifier, re.I) else ""
    return ONTConfiguration(description, serial, mac)


def query_vlan(adapter, channel, vlan, ont_cache):
    try:
        output = adapter.command(channel, f"display service-port vlan {vlan}", empty_response=EMPTY_SERVICE_PORTS.search)
        positions = parse_service_ports(output, vlan)
    except AdapterError as exc:
        return VLANResult(vlan, "Falhou", str(exc))
    if not positions:
        return VLANResult(vlan, "Sem ONTs")

    warnings = []
    try:
        output = adapter.command(channel, f"display mac-address vlan {vlan}", empty_response=EMPTY_MAC_ADDRESSES.search)
        macs_by_port = parse_mac_table(output)
    except AdapterError as exc:
        macs_by_port = {}
        warnings.append(f"Tabela MAC: {exc}")

    rows = []
    for position, ports in sorted(positions.items()):
        if position not in ont_cache:
            frame, slot, pon, ont_id = position
            try:
                output = adapter.command(channel, f"display current-configuration ont {frame}/{slot}/{pon} {ont_id}")
                ont_cache[position] = (parse_ont_configuration(output, position), "")
            except AdapterError as exc:
                ont_cache[position] = (ONTConfiguration(), str(exc))
        ont, detail = ont_cache[position]
        macs = set().union(*(macs_by_port.get(port, ()) for port in ports))
        rows.append(InventoryRow(position, tuple(sorted(ports)), ont, tuple(sorted(macs)), detail))
    config_errors = sum(bool(row.detail) for row in rows)
    if config_errors:
        warnings.append(f"Configuração indisponível para {config_errors} ONT(s).")
    return VLANResult(vlan, "Parcial" if warnings else "Concluída", " ".join(warnings), rows)


def query_olt(olt, vlans):
    results = []
    try:
        adapter = get_adapter(olt)
        if not isinstance(adapter, HuaweiAdapter):
            raise AdapterError("O inventário da OLT requer integração Huawei por SSH ou Telnet.")
        with adapter.session(olt, read_only=True) as channel:
            adapter.command(channel, "enable")
            adapter.command(channel, "config")
            ont_cache = {}
            for vlan in vlans:
                results.append(query_vlan(adapter, channel, vlan, ont_cache))
        return results
    except (AdapterError, OSError, paramiko.SSHException) as exc:
        detail = str(exc) if isinstance(exc, AdapterError) else "A sessão SSH foi interrompida durante a consulta."
        return results + [VLANResult(vlan, "Falhou", detail) for vlan in vlans[len(results):]]
