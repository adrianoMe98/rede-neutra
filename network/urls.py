from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/", views.health, name="health"),
    path("olts/<int:pk>/comunicacao/", views.olt_communication, name="olt_communication"),
    path("cadastros/<str:kind>/", views.catalog, name="catalog"),
    path("cadastros/<str:kind>/novo/", views.catalog_edit, name="catalog_create"),
    path("cadastros/vlans/faixa/", views.vlan_range_create, name="vlan_range_create"),
    path("cadastros/vlans/remover-faixa/", views.vlan_range_remove, name="vlan_range_remove"),
    path("cadastros/<str:kind>/<int:pk>/", views.catalog_edit, name="catalog_edit"),
    path("onus/", views.onu_list, name="onu_list"),
    path("onus/descobrir/", views.onu_discovery, name="onu_discovery"),
    path("onus/nova/", views.onu_edit, name="onu_create"),
    path("onus/<int:pk>/", views.onu_detail, name="onu_detail"),
    path("onus/<int:pk>/editar/", views.onu_edit, name="onu_edit"),
    path("onus/<int:pk>/<str:action>/", views.onu_action, name="onu_action"),
    path("relatorios/", views.onu_list, {"report": True}, name="reports"),
    path("relatorios/exportar/", views.export_csv, name="export_csv"),
    path("relatorios/onts-por-vlan/", views.olt_inventory, name="olt_inventory"),
    path("relatorios/onts-por-vlan/coletar/", views.inventory_collect, name="inventory_collect"),
    path("relatorios/onts-por-vlan/coletas/<int:pk>/xlsx/", views.inventory_history_download, name="inventory_history_download"),
]
