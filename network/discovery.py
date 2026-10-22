from dataclasses import asdict

from django.core import signing

from .adapters import AdapterError, get_adapter
from .models import DiscoveryScan, ONU
from .serials import serial_aliases

TOKEN_SALT = "network.ont-discovery.v1"
TOKEN_MAX_AGE = 15 * 60


def scan_olt(olt, actor):
    try:
        found = get_adapter(olt).discover(olt)
        if len(found) > 1000:
            raise AdapterError("A busca excedeu 1.000 ONTs. Restrinja a consulta na OLT antes de tentar novamente.")
    except Exception as exc:
        message = str(exc) if isinstance(exc, AdapterError) else "Não foi possível concluir a busca. Confira a conexão e a integração da OLT."
        DiscoveryScan.objects.create(olt=olt, actor=actor, simulation=olt.mode == "simulation", outcome="failed", detail=message[:240])
        raise AdapterError(message) from None
    scan = DiscoveryScan.objects.create(olt=olt, actor=actor, simulation=olt.mode == "simulation", outcome="success", count=len(found))
    aliases = set().union(*(serial_aliases(item.serial) for item in found)) if found else set()
    existing = {}
    for onu in ONU.objects.filter(serial__in=aliases).select_related("provider", "olt"):
        for alias in serial_aliases(onu.serial):
            existing[alias] = onu
    rows = []
    for item in found:
        onu = existing.get(item.serial)
        position_matches = not onu or (onu.olt_id, onu.frame, onu.slot, onu.pon) == (olt.pk, item.frame, item.slot, item.pon)
        if onu and onu.status == ONU.Status.ACTIVE and position_matches:
            continue
        payload = {"scan": scan.pk, "olt": olt.pk, **asdict(item)}
        can_continue = not onu or (onu.status == ONU.Status.DRAFT and position_matches)
        rows.append({"item": item, "existing": onu, "position_matches": position_matches,
                     "token": signing.dumps(payload, salt=TOKEN_SALT, compress=True) if can_continue else ""})
    return scan, rows


def load_discovery(token, actor):
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
        if any(type(data[key]) is not int for key in ("scan", "olt", "frame", "slot", "pon")):
            raise ValueError
        if not isinstance(data["serial"], str) or not 8 <= len(data["serial"]) <= 16:
            raise ValueError
        scan = DiscoveryScan.objects.get(pk=data["scan"], olt_id=data["olt"], actor=actor, outcome="success")
    except (signing.BadSignature, KeyError, TypeError, ValueError, DiscoveryScan.DoesNotExist):
        raise AdapterError("A seleção da busca expirou ou é inválida. Busque as ONTs novamente.") from None
    data["scanned_at"] = scan.created_at
    return data


def validate_selection(onu, data):
    if (data["olt"], data["frame"], data["slot"], data["pon"]) != (onu.olt_id, onu.frame, onu.slot, onu.pon) or onu.serial not in serial_aliases(data["serial"]):
        raise AdapterError("A ONT descoberta não corresponde à posição deste cadastro. Confira OLT, PON e serial antes de provisionar.")
