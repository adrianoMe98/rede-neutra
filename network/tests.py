import csv
import io
import logging
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .adapters import AdapterError, HuaweiAdapter, SimulatedAdapter, UncertainResult, get_adapter
from .forms import CTOForm, OLTForm, ONUForm, ProviderForm, VlanForm
from .models import CTO, OLT, ONU, Operation, Provider, Vlan
from .services import provision, refresh_optics


@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                             "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class NetworkTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = get_user_model().objects.create_user("operador", password="test-only-Op!42", is_staff=True)
        cls.viewer = get_user_model().objects.create_user("consulta", password="test-only-Vi!42")
        cls.provider = Provider.objects.create(name="acert")
        cls.other = Provider.objects.create(name="Outro")
        cls.vlan = Vlan.objects.create(number=100, provider=cls.provider)
        cls.other_vlan = Vlan.objects.create(number=200, provider=cls.other)
        cls.cto = CTO.objects.create(name="CTO-001", total_ports=8)
        cls.olt = OLT.objects.create(name="Huawei Centro", model="MA5800-X7", firmware="MA5800V100R022C11")
        cls.onu = ONU.objects.create(provider=cls.provider, subscriber="adriano", serial="HWTC00000001", vlan=cls.vlan,
                                     cto=cls.cto, port=1, olt=cls.olt, frame=0, slot=1, pon=0, ont_id=1, service_port=1000)

    def setUp(self):
        self.client.force_login(self.operator)

    def onu_data(self, **changes):
        data = {"provider": self.provider.pk, "subscriber": "Maria", "serial": "HWTC00000002", "vlan": self.vlan.pk,
                "cto": self.cto.pk, "port": 2, "olt": self.olt.pk, "frame": 0, "slot": 1, "pon": 0, "ont_id": 2, "service_port": 1001}
        return data | changes

    def test_description(self):
        self.assertEqual(self.onu.description, "acert - adriano")

    def test_provider_vlan_mismatch_rejected(self):
        form = ONUForm(self.onu_data(vlan=self.other_vlan.pk))
        self.assertFalse(form.is_valid())
        self.assertIn("vlan", form.errors)

    def test_port_capacity_rejected(self):
        form = ONUForm(self.onu_data(port=9))
        self.assertFalse(form.is_valid())
        self.assertIn("port", form.errors)

    def test_cto_port_reserved(self):
        form = ONUForm(self.onu_data(port=1))
        self.assertFalse(form.is_valid())
        self.assertIn("__all__", form.errors)

    def test_database_guards_duplicate_ports(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ONU.objects.create(provider=self.provider, subscriber="Outra", serial="HWTC00000003", vlan=self.vlan,
                               cto=self.cto, port=1, olt=self.olt, ont_id=3, service_port=1003)

    def test_duplicate_ont_and_service_port_rejected(self):
        for changes in [{"ont_id": 1}, {"service_port": 1000}]:
            form = ONUForm(self.onu_data(**changes))
            self.assertFalse(form.is_valid())

    def test_vlan_boundaries_and_duplicate(self):
        for number in (0, 4095, 100):
            self.assertFalse(VlanForm({"number": number, "provider": self.provider.pk}).is_valid())

    def test_capacity_cannot_shrink_under_occupied_port(self):
        self.onu.port = 8
        self.onu.save()
        self.assertFalse(CTOForm({"name": self.cto.name, "total_ports": 4}, instance=self.cto).is_valid())

    def test_vlan_owner_cannot_change_when_in_use(self):
        form = VlanForm({"number": 100, "provider": self.other.pk}, instance=self.vlan)
        self.assertFalse(form.is_valid())

    def test_inactive_provider_rejected(self):
        self.provider.active = False
        self.provider.save()
        self.assertFalse(ONUForm(self.onu_data()).is_valid())

    def test_lowercase_serial_normalized(self):
        form = ONUForm(self.onu_data(serial="hwtc00000002"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["serial"], "HWTC00000002")

    def test_anonymous_pages_redirect(self):
        self.client.logout()
        for url in ["/", "/onus/", "/relatorios/", "/relatorios/exportar/", "/cadastros/olts/"]:
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_viewer_can_read_but_not_modify(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get("/onus/").status_code, 200)
        self.assertEqual(self.client.get("/relatorios/exportar/").status_code, 200)
        self.assertEqual(self.client.post(reverse("onu_action", args=[self.onu.pk, "provision"]), {"confirmed": "yes"}).status_code, 403)
        self.onu.refresh_from_db()
        self.assertEqual(self.onu.status, ONU.Status.DRAFT)
        self.assertEqual(self.client.post("/cadastros/provedores/novo/", {"name": "Blocked"}).status_code, 403)
        self.assertFalse(Provider.objects.filter(name="Blocked").exists())

    def test_all_screens_render(self):
        urls = ["/", "/onus/", "/onus/nova/", f"/onus/{self.onu.pk}/", "/relatorios/", "/relatorios/exportar/"]
        for kind in ["provedores", "vlans", "ctos", "olts"]:
            urls += [f"/cadastros/{kind}/", f"/cadastros/{kind}/novo/"]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_create_onu_web(self):
        response = self.client.post("/onus/nova/", self.onu_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ONU.objects.get(serial="HWTC00000002").description, "acert - Maria")

    def test_confirmation_and_post_required(self):
        url = reverse("onu_action", args=[self.onu.pk, "provision"])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        self.onu.refresh_from_db()
        self.assertEqual(self.onu.status, ONU.Status.DRAFT)

    def test_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        response = client.post(reverse("onu_action", args=[self.onu.pk, "provision"]), {"confirmed": "yes"})
        self.assertEqual(response.status_code, 403)

    @patch("paramiko.SSHClient")
    def test_simulation_end_to_end_no_network(self, ssh):
        self.client.post(reverse("onu_action", args=[self.onu.pk, "provision"]), {"confirmed": "yes"})
        self.onu.refresh_from_db()
        self.assertEqual(self.onu.status, ONU.Status.ACTIVE)
        self.client.post(reverse("onu_action", args=[self.onu.pk, "optics"]))
        self.onu.refresh_from_db()
        self.assertIsNotNone(self.onu.rx_dbm)
        self.assertIsNotNone(self.onu.tx_dbm)
        self.assertIsNotNone(self.onu.measured_at)
        self.assertEqual(Operation.objects.filter(outcome="success", actor=self.operator).count(), 2)
        ssh.assert_not_called()
        csv_rows = list(csv.reader(io.StringIO(self.client.get("/relatorios/exportar/").content.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(csv_rows[1][0], "acert - adriano")
        self.assertEqual(csv_rows[1][6:9], ["CTO-001", "1", "100"])
        self.assertEqual(csv_rows[1][-1], "Simulação")
        self.assertEqual(float(csv_rows[1][10]), self.onu.rx_dbm)

    def test_duplicate_provision_is_blocked(self):
        provision(self.onu, self.operator)
        with self.assertRaises(AdapterError):
            provision(self.onu, self.operator)
        self.assertEqual(Operation.objects.count(), 1)

    def test_optics_require_provisioning(self):
        with self.assertRaises(AdapterError):
            refresh_optics(self.onu, self.operator)

    def test_active_record_cannot_be_edited(self):
        provision(self.onu, self.operator)
        self.client.post(reverse("onu_edit", args=[self.onu.pk]), self.onu_data(serial=self.onu.serial))
        self.onu.refresh_from_db()
        self.assertEqual(self.onu.subscriber, "adriano")

    def test_provider_name_locked_after_provisioning(self):
        provision(self.onu, self.operator)
        form = ProviderForm({"name": "new name", "active": True}, instance=self.provider)
        self.assertFalse(form.is_valid())

    def test_olt_mode_locked_after_provisioning(self):
        provision(self.onu, self.operator)
        self.olt.host = "192.0.2.1"
        with self.assertRaises(ValidationError):
            self.olt.full_clean()

    def test_failed_connection_allows_retry_and_logs_failure(self):
        with patch.object(SimulatedAdapter, "provision", side_effect=AdapterError("Falha antes de alterar")):
            with self.assertRaises(AdapterError):
                provision(self.onu, self.operator)
        self.onu.refresh_from_db()
        self.assertEqual(self.onu.status, ONU.Status.DRAFT)
        self.assertEqual(Operation.objects.get().outcome, "failed")

    def test_partial_failure_blocks_retry(self):
        with patch.object(SimulatedAdapter, "provision", side_effect=UncertainResult("Conferir na OLT")):
            with self.assertRaises(AdapterError):
                provision(self.onu, self.operator)
        self.onu.refresh_from_db()
        self.assertEqual(self.onu.status, ONU.Status.REVIEW)
        with self.assertRaises(AdapterError):
            provision(self.onu, self.operator)
        self.assertEqual(Operation.objects.count(), 1)

    def test_unexpected_failure_is_conservative(self):
        with patch.object(SimulatedAdapter, "provision", side_effect=RuntimeError("secret-detail")):
            with self.assertRaises(AdapterError):
                provision(self.onu, self.operator)
        self.onu.refresh_from_db()
        self.assertEqual(self.onu.status, ONU.Status.REVIEW)
        self.assertNotIn("secret-detail", Operation.objects.get().detail)

    def test_optics_failure_preserves_previous_measurements(self):
        provision(self.onu, self.operator)
        self.onu.refresh_from_db()
        refresh_optics(self.onu, self.operator)
        self.onu.refresh_from_db()
        old_rx = self.onu.rx_dbm
        with patch.object(SimulatedAdapter, "optics", side_effect=AdapterError("Offline")):
            with self.assertRaises(AdapterError):
                refresh_optics(self.onu, self.operator)
        self.onu.refresh_from_db()
        self.assertEqual(self.onu.rx_dbm, old_rx)
        self.assertEqual(Operation.objects.first().outcome, "failed")

    def test_csv_filter_and_formula_injection(self):
        self.provider.name = "=HYPERLINK(123)"
        self.provider.save()
        response = self.client.get("/relatorios/exportar/", {"provider": self.provider.pk, "q": "adriano"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1][0].startswith("'="))
        self.assertEqual(len(self.client.get("/relatorios/exportar/", {"q": "missing"}).content.decode().splitlines()), 1)

    @override_settings(OLT_CREDENTIAL_KEY=Fernet.generate_key().decode())
    def test_password_encryption_and_roundtrip(self):
        self.olt.set_password("test-secret")
        self.assertNotIn("test-secret", self.olt.encrypted_password)
        self.olt.save()
        self.olt.refresh_from_db()
        self.assertEqual(self.olt.get_password(), "test-secret")

    @override_settings(ENABLE_LIVE_OLT=False)
    def test_live_ssh_requires_explicit_activation(self):
        self.olt.mode = "ssh"
        with self.assertRaisesMessage(AdapterError, "desabilitado"):
            provision(self.onu, self.operator)
        self.assertEqual(Operation.objects.count(), 0)

    @override_settings(ENABLE_LIVE_OLT=True)
    def test_firmware_allowlist(self):
        self.olt.host, self.olt.username = "192.0.2.1", "test"
        adapter = HuaweiAdapter()
        adapter.preflight(self.olt)
        self.olt.model, self.olt.firmware = "MA5683T", "MA5600V800R017C10"
        adapter.preflight(self.olt)
        self.olt.firmware = "unknown"
        with self.assertRaises(AdapterError):
            adapter.preflight(self.olt)

    def test_huawei_plan(self):
        plan = HuaweiAdapter().plan(self.onu)
        self.assertIn('desc "acert - adriano"', plan[3])
        self.assertIn("ont-lineprofile-id 10 ont-srvprofile-id 10", plan[3])
        self.assertIn("service-port 1000 vlan 100 gpon 0/1/0 ont 1", plan[-1])
        self.assertNotIn("save", plan)

    def test_huawei_plan_sanitizes_cli_description(self):
        self.onu.subscriber = 'João"\nquit'
        commands = HuaweiAdapter().plan(self.onu)
        self.assertIn('desc "acert - Joaoquit"', commands[3])
        self.assertEqual(len(commands), 6)

    def test_huawei_serial_validation(self):
        self.onu.serial = "NOTASERIAL"
        with self.assertRaises(AdapterError):
            HuaweiAdapter().plan(self.onu)

    def test_optical_parser(self):
        result = HuaweiAdapter.parse_optics("ONT optical information\n  Rx optical power(dBm) : -22.45\n  Tx optical power(dBm) : 2.31\nOLT Rx ONT optical power(dBm) : -24.50")
        self.assertEqual((result.tx, result.rx), (2.31, -22.45))
        for output in ["Rx optical power(dBm): --\nTx optical power(dBm): 2", "Rx optical power(dBm): -99\nTx optical power(dBm): 2"]:
            with self.assertRaises(AdapterError):
                HuaweiAdapter.parse_optics(output)

    def test_other_vendor_simulation(self):
        for vendor in ("zte", "fiberhome", "other"):
            self.olt.vendor = vendor
            self.assertIsInstance(get_adapter(self.olt), SimulatedAdapter)

    def test_reconcile_requires_review_state_and_audits(self):
        self.onu.status = ONU.Status.REVIEW
        self.onu.save()
        call_command("reconcile_onu", self.onu.serial, state="active", reason="ONT e service-port conferidos na CLI", verified_on_olt=True, stdout=io.StringIO())
        self.onu.refresh_from_db()
        self.assertEqual(self.onu.status, ONU.Status.ACTIVE)
        self.assertIn("Reconciliação", Operation.objects.get().detail)
        with self.assertRaises(CommandError):
            call_command("reconcile_onu", self.onu.serial, state="draft", reason="test", verified_on_olt=True)

    def test_demo_preserves_existing_data(self):
        with self.assertRaises(CommandError):
            call_command("seed_demo", stdout=io.StringIO())
        self.assertEqual(ONU.objects.count(), 1)

    def test_health_is_public_without_network_data(self):
        self.client.logout()
        self.assertEqual(self.client.get("/health/").json(), {"status": "ok"})

    @override_settings(DEBUG=False)
    def test_production_500_logs_traceback_without_exposing_it_in_browser(self):
        self.client.raise_request_exception = False
        output = io.StringIO()
        logger = logging.getLogger("django")
        console = next(handler for handler in logger.handlers if isinstance(handler, logging.StreamHandler))
        with patch.object(console, "stream", output), patch("network.views.ONU.objects.count", side_effect=RuntimeError("falha-controlada-de-teste")):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 500)
        self.assertIn("Traceback", output.getvalue())
        self.assertIn("RuntimeError: falha-controlada-de-teste", output.getvalue())
        self.assertNotContains(response, "falha-controlada-de-teste", status_code=500)
        self.assertNotContains(response, "Traceback", status_code=500)

    def test_real_ssh_host_key_is_never_auto_accepted(self):
        with override_settings(ENABLE_LIVE_OLT=True), patch("paramiko.SSHClient") as client:
            self.olt.host, self.olt.username = "192.0.2.1", "test"
            self.olt.get_password = lambda: "not-a-real-password"
            shell = client.return_value.invoke_shell.return_value
            with patch.object(HuaweiAdapter, "read_prompt", return_value="MA5800>"), patch.object(HuaweiAdapter, "command", return_value=self.olt.firmware):
                with HuaweiAdapter().session(self.olt) as channel:
                    self.assertEqual(channel, shell)
            import paramiko
            self.assertIsInstance(client.return_value.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)

    def test_remote_firmware_mismatch_closes_session(self):
        with override_settings(ENABLE_LIVE_OLT=True), patch("paramiko.SSHClient") as client:
            self.olt.host, self.olt.username = "192.0.2.1", "test"
            self.olt.get_password = lambda: "not-a-real-password"
            with patch.object(HuaweiAdapter, "read_prompt", return_value="MA5800>"), patch.object(HuaweiAdapter, "command", return_value="MA5800V100R019C10") as command:
                with self.assertRaisesMessage(AdapterError, "não corresponde"):
                    with HuaweiAdapter().session(self.olt):
                        self.fail("A sessão não deve ser disponibilizada.")
                command.assert_called_once()
                self.assertEqual(command.call_args.args[1], "display version")
                client.return_value.close.assert_called_once()


class HuaweiCliTest(TestCase):
    def test_huawei_optional_enter_prompt(self):
        shell = MagicMock()
        shell.closed = False
        shell.exit_status_ready.return_value = False
        shell.recv_ready.return_value = True
        shell.recv.side_effect = [b"cmd\r\n{ <cr>|ont-type<K> }: ", b"\r\nMA5800(config)#"]
        response = HuaweiAdapter().read_prompt(shell)
        self.assertIn("MA5800(config)#", response)
        shell.sendall.assert_called_once_with("\n")

    def test_closed_channel_is_failure(self):
        shell = MagicMock()
        shell.recv_ready.return_value = False
        shell.closed = True
        with self.assertRaises(AdapterError):
            HuaweiAdapter().read_prompt(shell)
