import csv
from functools import wraps
from itertools import batched

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, IntegrityError, OperationalError, transaction
from django.db.models import Count, Exists, OuterRef, ProtectedError, Q, Subquery
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from inventory.models import InventoryRun, ProviderSnapshot
from inventory.services import enqueue_inventory, latest_complete_snapshots

from .adapters import AdapterError, get_adapter
from .communication import check_olt_connection
from .discovery import load_discovery, scan_olt, validate_selection
from .forms import (
    CTOForm, DiscoveryForm, InventoryForm, OLTForm, ONUForm, ProviderForm,
    ProvisioningProfileForm, VlanForm, VlanIntervalForm, VlanRangeForm,
)
from .inventory_xlsx import inventory_workbook
from .models import CTO, OLT, ONU, DiscoveryScan, OLTConnectionTest, Operation, Provider, ProvisioningProfile, Vlan
from .services import provision, refresh_optics
from .serials import serial_aliases


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
    providers = list(Provider.objects.annotate(
        onu_count=Count("onus"), provisioned_count=Count("onus", filter=Q(onus__status=ONU.Status.ACTIVE)),
    ))
    inventory_unavailable = False
    try:
        snapshots = {
            snapshot.provider_id: snapshot
            for snapshot in latest_complete_snapshots([provider.pk for provider in providers])
        }
    except DatabaseError:
        inventory_unavailable = True
        snapshots = {}
    for provider in providers:
        provider.inventory = snapshots.get(provider.pk)
    return render(request, "network/dashboard.html", {
        "total": ONU.objects.count(), "active": ONU.objects.filter(status=ONU.Status.ACTIVE).count(),
        "providers_count": Provider.objects.filter(active=True).count(), "ctos_count": CTO.objects.count(),
        "recent": ONU.objects.select_related("provider", "vlan", "cto", "olt")[:6],
        "operations": Operation.objects.select_related("onu__provider", "actor")[:5],
        "providers": providers, "inventory_unavailable": inventory_unavailable,
        "simulations": OLT.objects.filter(mode="simulation").count(),
        "review_count": ONU.objects.filter(status__in=[ONU.Status.REVIEW, ONU.Status.RUNNING]).count(),
        "page": "dashboard", "title": "Visão geral",
    })


CATALOGS = {
    "provedores": (Provider, ProviderForm, "Provedores", "provedor"),
    "vlans": (Vlan, VlanForm, "VLANs", "VLAN"),
    "ctos": (CTO, CTOForm, "CTOs", "CTO"),
    "olts": (OLT, OLTForm, "OLTs", "OLT"),
    "perfis": (ProvisioningProfile, ProvisioningProfileForm, "Perfis de provisionamento", "perfil"),
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
    elif kind == "perfis":
        items = items.select_related("provider", "vlan", "olt").annotate(onu_count=Count("onus"))
    else:
        items = items.annotate(onu_count=Count("onus"))
    if kind == "olts":
        latest = OLTConnectionTest.objects.filter(olt_id=OuterRef("pk"))
        items = items.annotate(
            last_test_at=Subquery(latest.values("created_at")[:1]),
            last_test_success=Subquery(latest.values("success")[:1]),
            last_test_simulation=Subquery(latest.values("simulation")[:1]),
        )
    items = items.order_by("number" if kind == "vlans" else "name")
    q = request.GET.get("q", "").strip()
    if q:
        items = items.filter(**{"number" if kind == "vlans" else "name__icontains": q}) if kind != "vlans" or q.isdigit() else items.none()
    template = "network/profiles.html" if kind == "perfis" else "network/catalog.html"
    return render(request, template, {"items": Paginator(items, 30).get_page(request.GET.get("page")),
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
    if request.method == "POST":
        try:
            with transaction.atomic():
                saved = form.is_valid()
                if saved:
                    form.save()
        except IntegrityError:
            form.add_error(None, "Este cadastro já existe ou conflita com outro registro.")
        except OperationalError as exc:
            if not getattr(exc.__cause__, "sqlite_errorname", "").startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
                raise
            form.add_error(None, "Há outro cadastro em andamento. Confira os dados e tente salvar novamente.")
        else:
            if saved:
                messages.success(request, f"Cadastro de {singular} salvo.")
                return redirect("catalog", kind=kind)
    return render(request, "network/form.html", {"form": form, "title": f"{'Editar' if pk else 'Cadastrar'} {singular}",
                                                 "page": kind, "kind": kind, "back_url": f"/cadastros/{kind}/",
                                                 "olt_for_test": obj if kind == "olts" else None})


@operator
@require_http_methods(["GET", "POST"])
def vlan_range_create(request):
    form = VlanRangeForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            created, existing = form.save()
        except ValidationError as exc:
            form.add_error(None, exc)
        except IntegrityError:
            form.add_error(None, "Os cadastros de VLAN mudaram durante a operação. Confira a faixa e tente novamente.")
        except OperationalError as exc:
            if not getattr(exc.__cause__, "sqlite_errorname", "").startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
                raise
            form.add_error(None, "Há outro cadastro em andamento. Tente salvar a faixa novamente.")
        else:
            count = f"{created} {'VLAN cadastrada' if created == 1 else 'VLANs cadastradas'}."
            if existing:
                count += f" Já {'existia' if existing == 1 else 'existiam'} {existing} para o provedor."
            messages.success(request, count)
            return redirect("catalog", kind="vlans")
    return render(request, "network/form.html", {
        "form": form, "title": "Cadastrar faixa de VLANs", "page": "vlans", "kind": "vlan_range",
        "back_url": reverse("catalog", kwargs={"kind": "vlans"}),
    })


VLAN_REMOVAL_SALT = "network.vlan-range-removal"


def vlan_removal_selection(user, form, vlans):
    return {
        "user": user.pk,
        "provider": form.cleaned_data["provider"].pk,
        "bounds": [form.cleaned_data["first"], form.cleaned_data["last"]],
        "vlans": [[vlan.pk, vlan.number] for vlan in vlans],
    }


@operator
@require_http_methods(["GET", "POST"])
def vlan_range_remove(request):
    form = VlanIntervalForm(request.POST if request.method == "POST" else None)
    vlans, confirmation = None, ""
    if request.method == "POST" and form.is_valid():
        targets = form.vlan_range().filter(provider=form.cleaned_data["provider"]).annotate(
            has_onu=Exists(ONU.objects.filter(vlan_id=OuterRef("pk"))),
            has_profile=Exists(ProvisioningProfile.objects.filter(
                Q(vlan_id=OuterRef("pk"))
                | Q(vlan__isnull=True, provider_id=OuterRef("provider_id"), vlan_description=OuterRef("name")),
            )),
        )
        if request.POST.get("action") == "remove":
            try:
                reviewed = signing.loads(request.POST.get("confirmation", ""), salt=VLAN_REMOVAL_SALT)
                with transaction.atomic():
                    locked = list(targets.select_for_update())
                    if not locked or reviewed != vlan_removal_selection(request.user, form, locked):
                        raise ValidationError("A seleção mudou. Confira a nova prévia antes de remover.")
                    if any(vlan.has_onu or vlan.has_profile for vlan in locked):
                        raise ValidationError("Há VLANs da faixa vinculadas a ONUs ou perfis de provisionamento.")
                    for ids in batched((vlan.pk for vlan in locked), 500):
                        targets.filter(pk__in=ids).delete()
            except signing.BadSignature:
                form.add_error(None, "A confirmação é inválida. Confira a prévia antes de remover.")
            except ValidationError as exc:
                form.add_error(None, exc)
            except ProtectedError:
                form.add_error(None, "Há VLANs da faixa vinculadas a ONUs ou perfis de provisionamento.")
            except IntegrityError:
                form.add_error(None, "Os vínculos mudaram durante a operação. Confira a prévia.")
            except OperationalError as exc:
                if not getattr(exc.__cause__, "sqlite_errorname", "").startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
                    raise
                form.add_error(None, "Há outra alteração em andamento. Confira a prévia e confirme novamente.")
            else:
                count = len(locked)
                messages.success(request, f"{count} {'VLAN removida' if count == 1 else 'VLANs removidas'} do ERP.")
                return redirect("catalog", kind="vlans")
        vlans = list(targets)
        if vlans and not any(vlan.has_onu or vlan.has_profile for vlan in vlans):
            selection = vlan_removal_selection(request.user, form, vlans)
            confirmation = signing.dumps(selection, salt=VLAN_REMOVAL_SALT, compress=True)
    return render(request, "network/vlan_range_remove.html", {
        "form": form, "title": "Remover faixa de VLANs", "page": "vlans",
        "back_url": reverse("catalog", kwargs={"kind": "vlans"}), "vlans": vlans,
        "confirmation": confirmation,
    })


def filtered_onus(request):
    query = ONU.objects.select_related("provider", "vlan", "cto", "olt")
    q = request.GET.get("q", "").strip()
    if q:
        query = query.filter(Q(subscriber__icontains=q) | Q(serial__icontains=q) | Q(serial__in=serial_aliases(q)) | Q(provider__name__icontains=q) | Q(cto__name__icontains=q))
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
@require_http_methods(["GET", "POST"])
def olt_inventory(request):
    form = InventoryForm(request.POST if request.method == "POST" else None)
    history, unavailable = None, False
    try:
        if request.method == "POST" and form.is_valid():
            provider = form.cleaned_data["provider"]
            snapshot = latest_complete_snapshots([provider.pk]).first()
            if snapshot:
                return inventory_download(snapshot)
            form.add_error("provider", "Ainda não há uma coleta com todas as VLANs lidas para este provedor. Confira o histórico.")
        runs = InventoryRun.objects.prefetch_related("providers")
        history = Paginator(runs, 12).get_page(request.GET.get("page"))
        history.object_list = list(history.object_list)
    except DatabaseError:
        unavailable = True
    return render(request, "network/olt_inventory.html", {
        "form": form, "history": history, "inventory_unavailable": unavailable,
        "title": "Inventário de ONTs por VLAN", "page": "reports",
    }, status=503 if unavailable else 200)


@operator
@require_POST
def inventory_collect(request):
    try:
        run = enqueue_inventory(requested_by=request.user.get_username())
    except DatabaseError:
        messages.error(request, "O banco de inventário está indisponível. Tente novamente quando o serviço voltar.")
    else:
        if run:
            messages.success(request, "Coleta solicitada. Acompanhe o resultado no histórico.")
        else:
            messages.info(request, "Já existe uma coleta na fila ou em andamento.")
    return redirect("olt_inventory")


@operator
@require_GET
def inventory_history_download(request, pk):
    try:
        snapshot = get_object_or_404(ProviderSnapshot.objects.select_related("run"), pk=pk)
        return inventory_download(snapshot)
    except DatabaseError:
        messages.error(request, "O banco de inventário está indisponível. Tente novamente quando o serviço voltar.")
        return redirect("olt_inventory")


def inventory_download(snapshot):
    workbook = inventory_workbook(snapshot)
    provider_name = slugify(snapshot.provider_name) or f"provedor-{snapshot.provider_id}"
    date = timezone.localdate(snapshot.run.finished_at)
    return FileResponse(
        workbook, as_attachment=True, filename=f"relatorio_{provider_name}_{date:%d-%m-%Y}.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Cache-Control": "private, no-store"},
    )


@operator
def onu_edit(request, pk=None):
    onu = get_object_or_404(ONU, pk=pk) if pk else None
    if onu and onu.status != ONU.Status.DRAFT:
        messages.error(request, "Somente cadastros ainda não provisionados podem ser editados.")
        return redirect("onu_detail", pk=pk)
    token = request.POST.get("discovery_token", "") if request.method == "POST" else request.GET.get("discovery", "")
    discovered = None
    initial = {}
    if token and not pk:
        try:
            discovered = load_discovery(token, request.user)
        except AdapterError as exc:
            messages.error(request, str(exc))
            return redirect("onu_discovery")
        initial = {key: discovered[key] for key in ("serial", "olt", "frame", "slot", "pon")}
        initial["discovery_token"] = token
        occupied_ids = set(ONU.objects.filter(olt_id=discovered["olt"], frame=discovered["frame"],
                                             slot=discovered["slot"], pon=discovered["pon"]).values_list("ont_id", flat=True))
        initial["ont_id"] = next((candidate for candidate in range(256) if candidate not in occupied_ids), None)
        # Apenas sugestão do inventário local; o operador ainda confere a OLT.
        highest = ONU.objects.filter(olt_id=discovered["olt"]).order_by("-service_port").values_list("service_port", flat=True).first()
        initial["service_port"] = (highest + 1) if highest is not None and highest < 65535 else None
    form = ONUForm(request.POST if request.method == "POST" else None, instance=onu, initial=initial, discovery=discovered)
    if request.method == "POST" and form.is_valid():
        try:
            if pk:
                values = {key: form.cleaned_data[key] for key in form.Meta.fields}
                values["profile_snapshot"] = form.instance.profile_snapshot
                if not ONU.objects.filter(pk=pk, status=ONU.Status.DRAFT).update(**values):
                    raise AdapterError("O estado da ONU mudou durante a edição. Recarregue o cadastro.")
                saved = ONU.objects.get(pk=pk)
            else:
                if discovered:
                    form.instance.discovered_at = discovered["scanned_at"]
                saved = form.save()
            messages.success(request, "ONU cadastrada. Confira o plano antes de provisionar.")
            return redirect("onu_detail", pk=saved.pk)
        except (IntegrityError, AdapterError):
            form.add_error(None, "O cadastro mudou ou a porta / ONT ID / service-port já está em uso. Confira os dados.")
    return render(request, "network/form.html", {"form": form, "title": "Editar ONU" if pk else "Cadastrar ONU",
        "page": "onus", "kind": "onu", "back_url": "/onus/", "discovered": discovered,
        "vlans_data": list(Vlan.objects.values("id", "provider_id", "number", "name")),
        "profiles_data": list(form.fields["profile"].queryset.values(
            "id", "provider_id", "vlan_id", "vlan_description", "olt_id", "line_profile", "service_profile", "gem_port", "user_vlan",
        ))})


@operator
def onu_discovery(request):
    form = DiscoveryForm(request.POST if request.method == "POST" else None)
    scan, rows, connection_test = None, [], None
    if request.method == "POST" and form.is_valid():
        if request.POST.get("action", "discover") == "test_connection":
            connection_test = check_olt_connection(form.cleaned_data["olt"], request.user)
        else:
            try:
                scan, rows = scan_olt(form.cleaned_data["olt"], request.user)
            except AdapterError as exc:
                form.add_error(None, str(exc))
    return render(request, "network/discovery.html", {"form": form, "scan": scan, "rows": rows,
        "history": DiscoveryScan.objects.filter(actor=request.user).select_related("olt")[:5],
        "title": "Buscar ONTs não provisionadas", "page": "onus", "connection_test": connection_test,
        "available_count": sum(bool(row["token"]) for row in rows),
        "review_count": sum(not bool(row["token"]) for row in rows)})


@operator
def olt_communication(request, pk):
    olt = get_object_or_404(OLT, pk=pk)
    if request.method == "POST":
        check_olt_connection(olt, request.user)
        return redirect("olt_communication", pk=olt.pk)
    history = olt.connection_tests.select_related("actor")[:20]
    return render(request, "network/communication.html", {
        "olt": olt, "history": history, "connection_test": history[0] if history else None,
        "page": "olts", "title": f"Comunicação · {olt.name}",
    })


@login_required
def onu_detail(request, pk):
    onu = get_object_or_404(ONU.objects.select_related("provider", "vlan", "cto", "olt", "profile"), pk=pk)
    token = request.GET.get("discovery", "")
    if token:
        try:
            validate_selection(onu, load_discovery(token, request.user))
        except AdapterError as exc:
            messages.error(request, str(exc))
            return redirect("onu_discovery")
    try:
        plan = get_adapter(onu.olt).plan(onu)
        plan_error = ""
    except AdapterError as exc:
        plan, plan_error = [], str(exc)
    return render(request, "network/onu_detail.html", {"onu": onu, "plan": plan, "plan_error": plan_error,
        "operations": onu.operations.select_related("actor")[:30], "page": "onus", "title": onu.description,
        "discovery_token": token})


@operator
@require_POST
def onu_action(request, pk, action):
    onu = get_object_or_404(ONU.objects.select_related("provider", "vlan", "cto", "olt"), pk=pk)
    try:
        if action == "provision":
            if request.POST.get("confirmed") != "yes":
                raise AdapterError("Confirme a revisão dos dados e do plano de provisionamento.")
            token = request.POST.get("discovery_token", "")
            if token:
                selected = load_discovery(token, request.user)
                validate_selection(onu, selected)
                onu.discovered_at = selected["scanned_at"]
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
            "Simulação" if onu.olt.mode == "simulation" else f"OLT via {onu.olt.transport_label}"]])
    return response
