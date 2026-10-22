import csv
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .adapters import AdapterError, get_adapter
from .forms import CTOForm, OLTForm, ONUForm, ProviderForm, VlanForm
from .models import CTO, OLT, ONU, Operation, Provider, Vlan
from .services import provision, refresh_optics


def operator(view):
    @login_required
    @wraps(view)
    def guarded(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return guarded


def health(request):
    # Healthcheck sem dados da rede ou identificação de clientes.
    Provider.objects.exists()
    return JsonResponse({"status": "ok"})


@login_required
def dashboard(request):
    return render(request, "network/dashboard.html", {
        "total": ONU.objects.count(), "active": ONU.objects.filter(status=ONU.Status.ACTIVE).count(),
        "providers_count": Provider.objects.filter(active=True).count(), "ctos_count": CTO.objects.count(),
        "recent": ONU.objects.select_related("provider", "vlan", "cto", "olt")[:6],
        "operations": Operation.objects.select_related("onu__provider", "actor")[:5],
        "providers": Provider.objects.annotate(onu_count=Count("onus"))[:6],
        "simulations": OLT.objects.filter(mode="simulation").count(),
        "review_count": ONU.objects.filter(status__in=[ONU.Status.REVIEW, ONU.Status.RUNNING]).count(),
        "page": "dashboard", "title": "Visão geral",
    })


CATALOGS = {
    "provedores": (Provider, ProviderForm, "Provedores", "provedor"),
    "vlans": (Vlan, VlanForm, "VLANs", "VLAN"),
    "ctos": (CTO, CTOForm, "CTOs", "CTO"),
    "olts": (OLT, OLTForm, "OLTs", "OLT"),
}


@login_required
def catalog(request, kind):
    if kind not in CATALOGS:
        from django.http import Http404
        raise Http404
    model, form_class, title, singular = CATALOGS[kind]
    items = model.objects.all()
    if kind == "vlans":
        items = items.select_related("provider").annotate(onu_count=Count("onus"))
    else:
        items = items.annotate(onu_count=Count("onus"))
    items = items.order_by("number" if kind == "vlans" else "name")
    q = request.GET.get("q", "").strip()
    if q:
        items = items.filter(**{"number" if kind == "vlans" else "name__icontains": q}) if kind != "vlans" or q.isdigit() else items.none()
    return render(request, "network/catalog.html", {"items": Paginator(items, 30).get_page(request.GET.get("page")),
                                                    "kind": kind, "page": kind, "title": title, "singular": singular, "q": q,
                                                    "query_string": request.GET.urlencode()})


@operator
def catalog_edit(request, kind, pk=None):
    if kind not in CATALOGS:
        from django.http import Http404
        raise Http404
    model, form_class, title, singular = CATALOGS[kind]
    obj = get_object_or_404(model, pk=pk) if pk else None
    form = form_class(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
            messages.success(request, f"Cadastro de {singular} salvo.")
            return redirect("catalog", kind=kind)
        except IntegrityError:
            form.add_error(None, "Este cadastro já existe ou conflita com outro registro.")
    return render(request, "network/form.html", {"form": form, "title": f"{'Editar' if pk else 'Cadastrar'} {singular}",
                                                 "page": kind, "kind": kind, "back_url": f"/cadastros/{kind}/"})


def filtered_onus(request):
    query = ONU.objects.select_related("provider", "vlan", "cto", "olt")
    q = request.GET.get("q", "").strip()
    if q:
        query = query.filter(Q(subscriber__icontains=q) | Q(serial__icontains=q) | Q(provider__name__icontains=q) | Q(cto__name__icontains=q))
    for field in ("provider", "vlan", "cto", "olt"):
        value = request.GET.get(field, "")
        if value.isdigit():
            query = query.filter(**{f"{field}_id": int(value)})
    status = request.GET.get("status", "")
    if status in ONU.Status.values:
        query = query.filter(status=status)
    return query


@login_required
def onu_list(request, report=False):
    query = filtered_onus(request)
    return render(request, "network/onu_list.html", {"items": Paginator(query, 25).get_page(request.GET.get("page")),
        "providers": Provider.objects.all(), "vlans": Vlan.objects.all(), "ctos": CTO.objects.all(), "olts": OLT.objects.all(),
        "statuses": ONU.Status.choices, "report": report, "page": "reports" if report else "onus",
        "title": "Relatórios" if report else "ONTs e ONUs", "query_string": request.GET.urlencode()})


@operator
def onu_edit(request, pk=None):
    onu = get_object_or_404(ONU, pk=pk) if pk else None
    if onu and onu.status != ONU.Status.DRAFT:
        messages.error(request, "Somente cadastros ainda não provisionados podem ser editados.")
        return redirect("onu_detail", pk=pk)
    form = ONUForm(request.POST or None, instance=onu)
    if request.method == "POST" and form.is_valid():
        try:
            if pk:
                values = {key: form.cleaned_data[key] for key in form.Meta.fields}
                if not ONU.objects.filter(pk=pk, status=ONU.Status.DRAFT).update(**values):
                    raise AdapterError("O estado da ONU mudou durante a edição. Recarregue o cadastro.")
                saved = ONU.objects.get(pk=pk)
            else:
                saved = form.save()
            messages.success(request, "ONU cadastrada. Confira o plano antes de provisionar.")
            return redirect("onu_detail", pk=saved.pk)
        except (IntegrityError, AdapterError):
            form.add_error(None, "O cadastro mudou ou a porta / ONT ID / service-port já está em uso. Confira os dados.")
    return render(request, "network/form.html", {"form": form, "title": "Editar ONU" if pk else "Cadastrar ONU",
        "page": "onus", "kind": "onu", "back_url": "/onus/", "vlans_data": list(Vlan.objects.values("id", "provider_id"))})


@login_required
def onu_detail(request, pk):
    onu = get_object_or_404(ONU.objects.select_related("provider", "vlan", "cto", "olt"), pk=pk)
    try:
        plan = get_adapter(onu.olt).plan(onu)
        plan_error = ""
    except AdapterError as exc:
        plan, plan_error = [], str(exc)
    return render(request, "network/onu_detail.html", {"onu": onu, "plan": plan, "plan_error": plan_error,
        "operations": onu.operations.select_related("actor")[:30], "page": "onus", "title": onu.description})


@operator
@require_POST
def onu_action(request, pk, action):
    onu = get_object_or_404(ONU.objects.select_related("provider", "vlan", "cto", "olt"), pk=pk)
    try:
        if action == "provision":
            if request.POST.get("confirmed") != "yes":
                raise AdapterError("Confirme a revisão dos dados e do plano de provisionamento.")
            messages.success(request, provision(onu, request.user))
        elif action == "optics":
            reading = refresh_optics(onu, request.user)
            messages.success(request, f"Leitura atualizada: TX {reading.tx:.2f} dBm / RX {reading.rx:.2f} dBm.")
        else:
            raise AdapterError("Operação desconhecida.")
    except (AdapterError, ValidationError) as exc:
        messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
    return redirect("onu_detail", pk=pk)


def csv_safe(value):
    if isinstance(value, (int, float)):
        return value
    text = str(value) if value is not None else ""
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


@login_required
def export_csv(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="rede-neutra-onus.csv"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow(["Descrição", "Provedor", "Serial", "OLT", "PON", "ONT ID", "CTO", "Porta CTO", "VLAN", "TX ONU (dBm)", "RX ONU (dBm)", "Última leitura", "Situação", "Origem das potências"])
    for onu in filtered_onus(request).iterator():
        writer.writerow([csv_safe(v) for v in [onu.description, onu.provider.name, onu.serial, onu.olt.name,
            onu.pon_address, onu.ont_id, onu.cto.name, onu.port, onu.vlan.number,
            onu.tx_dbm, onu.rx_dbm, onu.measured_at.isoformat() if onu.measured_at else "", onu.get_status_display(),
            "Simulação" if onu.olt.mode == "simulation" else "OLT via SSH"]])
    return response
