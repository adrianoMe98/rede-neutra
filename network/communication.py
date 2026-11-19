import time

from .adapters import AdapterError, get_adapter
from .models import OLTConnectionTest


def check_olt_connection(olt, actor):
    started = time.perf_counter()
    try:
        detail = get_adapter(olt).test_connection(olt)
        success, stage = True, "ready"
    except AdapterError as exc:
        success, detail = False, str(exc)
        stage = exc.code if exc.code in dict(OLTConnectionTest.STAGES) else "cli"
    except Exception:
        success, stage = False, "unexpected"
        detail = "Não foi possível concluir o teste. Confira a configuração da OLT e a conectividade do servidor."
    return OLTConnectionTest.objects.create(
        olt=olt, actor=actor, success=success, simulation=olt.mode == "simulation", stage=stage,
        target="Simulação" if olt.mode == "simulation" else f"{olt.host}:{olt.connection_port}",
        detail=detail, duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
    )
