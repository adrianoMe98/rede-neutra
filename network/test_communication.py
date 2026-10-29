from contextlib import contextmanager
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import paramiko
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .adapters import AdapterError, HuaweiAdapter, SimulatedAdapter
from .communication import check_olt_connection
from .models import OLT, ONU, DiscoveryScan, OLTConnectionTest, Operation


@override_settings(ENABLE_LIVE_OLT=False)
class ReadOnlySSHTest(SimpleTestCase):
    def setUp(self):
        self.olt = SimpleNamespace(
            host="192.0.2.1", ssh_port=22, username="test-user", model="MA5800-X7",
            firmware="MA5800V100R022C11", get_password=lambda: "test-only-password",
        )
        self.adapter = HuaweiAdapter()

    @contextmanager
    def ssh(self):
        with patch("paramiko.SSHClient") as factory, patch.object(self.adapter, "read_prompt", return_value="MA5800>"), \
             patch.object(self.adapter, "command", return_value=self.olt.firmware) as command:
            client = factory.return_value
            client.get_host_keys.return_value.lookup.return_value = {"ssh-ed25519": MagicMock()}
            yield client, command

    def test_connection_checks_firmware_without_enabling_provisioning(self):
        with self.ssh() as (client, command):
            detail = self.adapter.test_connection(self.olt)
        self.assertIn("Comunicação SSH confirmada", detail)
        self.assertIsInstance(client.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)
        self.assertEqual([call.args[1] for call in command.call_args_list], ["display version"])
        client.close.assert_called_once()

    def test_provisioning_gate_is_preserved(self):
        with self.assertRaisesMessage(AdapterError, "Provisionamento SSH desabilitado"):
            self.adapter.preflight(self.olt)

    def test_missing_or_unknown_host_key_never_auto_accepts(self):
        for missing_file in (True, False):
            with self.subTest(missing_file=missing_file), self.ssh() as (client, command):
                if missing_file:
                    client.load_host_keys.side_effect = FileNotFoundError("private-context")
                else:
                    client.get_host_keys.return_value.lookup.return_value = None
                with self.assertRaises(AdapterError) as raised:
                    self.adapter.test_connection(self.olt)
                self.assertEqual(raised.exception.code, "host_key")
                self.assertNotIn("private-context", str(raised.exception))
                client.connect.assert_not_called()
                client.close.assert_called_once()

    def test_nonstandard_ssh_port_uses_matching_known_hosts_entry(self):
        self.olt.ssh_port = 2222
        with self.ssh() as (client, command):
            self.adapter.test_connection(self.olt)
        client.get_host_keys.return_value.lookup.assert_called_once_with("[192.0.2.1]:2222")

    def test_connection_failure_classification_and_redaction(self):
        failures = [
            (paramiko.AuthenticationException("sensitive-marker"), "authentication"),
            (paramiko.BadHostKeyException("sensitive-marker", MagicMock(), MagicMock()), "host_key"),
            (socket.gaierror("sensitive-marker"), "network"),
            (TimeoutError("sensitive-marker"), "network"),
            (ConnectionRefusedError("sensitive-marker"), "network"),
            (paramiko.SSHException("sensitive-marker"), "ssh"),
        ]
        for error, code in failures:
            with self.subTest(error=type(error).__name__), self.ssh() as (client, command):
                client.connect.side_effect = error
                with self.assertRaises(AdapterError) as raised:
                    self.adapter.test_connection(self.olt)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("sensitive-marker", str(raised.exception))
                client.close.assert_called_once()

    def test_bad_stored_credential_is_reported_without_connecting(self):
        self.olt.get_password = MagicMock(side_effect=ValidationError("sensitive-marker"))
        with self.ssh() as (client, command):
            with self.assertRaises(AdapterError) as raised:
                self.adapter.test_connection(self.olt)
            self.assertEqual(raised.exception.code, "authentication")
            client.connect.assert_not_called()

    def test_wrong_firmware_is_a_failed_test_after_ssh_response(self):
        with self.ssh() as (client, command):
            command.return_value = "MA5800V100R019C10"
            with self.assertRaises(AdapterError) as raised:
                self.adapter.test_connection(self.olt)
            self.assertEqual(raised.exception.code, "firmware")
            self.assertIn("respondeu por SSH", str(raised.exception))
            client.close.assert_called_once()

    def test_discovery_uses_a_read_only_session(self):
        with patch.object(self.adapter, "session") as session, patch.object(self.adapter, "command", return_value="The number of GPON autofind ONT is 0"):
            self.assertEqual(self.adapter.discover(self.olt), [])
        session.assert_called_once_with(self.olt, read_only=True)

    def test_access_denied_is_not_mistaken_for_an_empty_search(self):
        output = "The number of GPON autofind ONT is 0\nError: access denied\nMA5800(config)#"
        with patch.object(self.adapter, "read_prompt", return_value=output):
            with self.assertRaises(AdapterError):
                self.adapter.command(MagicMock(), "display ont autofind all")


class AutofindResponseReviewTest(SimpleTestCase):
    def test_colored_output_and_common_label_variants_are_supported(self):
        output = "\x1b[32mF/S/P : 0/1/0\x1b[0m\nOnt SN (HEX) : 48575443ABCD1234\nOnt Vendor ID : HWTC\nOnt Equipment ID : HG8245H"
        found = HuaweiAdapter.parse_discovery(output)
        self.assertEqual(found[0].serial, "HWTCABCD1234")
        self.assertEqual(found[0].vendor, "HWTC")
        self.assertEqual(found[0].equipment, "HG8245H")

    def test_duplicate_same_position_with_missing_metadata_is_merged(self):
        output = "F/S/P : 0/1/0\nOnt SN : HWTCABCD1234\nOnt EquipmentID : HG8245H\nF/S/P : 0/1/0\nOnt SN : HWTCABCD1234"
        found = HuaweiAdapter.parse_discovery(output)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].equipment, "HG8245H")

    def test_pagination_marker_split_across_packets_requests_next_page(self):
        channel = MagicMock()
        channel.closed = False
        channel.exit_status_ready.return_value = False
        channel.recv_ready.return_value = True
        channel.recv.side_effect = [b"F/S/P : 0/1/0\n---- Mo", b"re (Press 'Q' to break) ----", b"\nOnt SN : HWTCABCD1234\n\x1b[32mMA5800(config)#\x1b[0m"]
        output = HuaweiAdapter().read_prompt(channel)
        channel.sendall.assert_called_once_with(" ")
        self.assertEqual(len(HuaweiAdapter.parse_discovery(output)), 1)

    def test_vrp_style_prompt_and_indented_prompt_are_accepted(self):
        for prompt in (b"\n<HUAWEI>", b"\n  MA5800(config)# "):
            with self.subTest(prompt=prompt):
                channel = MagicMock()
                channel.recv_ready.return_value = True
                channel.recv.return_value = prompt
                self.assertIn("HUAWEI" if b"HUAWEI" in prompt else "MA5800", HuaweiAdapter().read_prompt(channel))


@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                             "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class CommunicationWorkflowTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = get_user_model().objects.create_user("operador-comunicacao", is_staff=True)
        cls.viewer = get_user_model().objects.create_user("consulta-comunicacao")
        cls.olt = OLT.objects.create(name="OLT de teste", model="MA5800-X7", firmware="MA5800V100R022C11")

    def setUp(self):
        self.client.force_login(self.operator)

    def test_get_and_catalog_do_not_open_network_connections(self):
        with patch.object(SimulatedAdapter, "test_connection") as connect:
            for url in (reverse("olt_communication", args=[self.olt.pk]), "/cadastros/olts/", f"/cadastros/olts/{self.olt.pk}/"):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Testar comunicação")
            connect.assert_not_called()
        self.assertFalse(OLTConnectionTest.objects.exists())

    @patch("paramiko.SSHClient")
    def test_simulation_records_actor_result_and_duration_without_network(self, ssh):
        with patch("network.communication.time.perf_counter", side_effect=[10, 10.25]):
            result = check_olt_connection(self.olt, self.operator)
        self.assertTrue(result.success)
        self.assertTrue(result.simulation)
        self.assertEqual(result.duration_ms, 250)
        self.assertEqual(result.actor, self.operator)
        ssh.assert_not_called()
        self.assertFalse(DiscoveryScan.objects.exists())
        self.assertFalse(ONU.objects.exists())
        self.assertFalse(Operation.objects.exists())

    def test_failure_is_recorded_with_stage(self):
        with patch.object(SimulatedAdapter, "test_connection", side_effect=AdapterError("Autenticação recusada", code="authentication")):
            result = check_olt_connection(self.olt, self.operator)
        self.assertFalse(result.success)
        self.assertEqual(result.stage, "authentication")
        self.assertIn("Autenticação recusada", result.detail)

    def test_unexpected_exception_details_are_not_exposed(self):
        with patch.object(SimulatedAdapter, "test_connection", side_effect=RuntimeError("secret-marker")):
            result = check_olt_connection(self.olt, self.operator)
        self.assertEqual(result.stage, "unexpected")
        self.assertNotIn("secret-marker", result.detail)

    def test_post_runs_test_and_redirects_without_repeating_on_reload(self):
        url = reverse("olt_communication", args=[self.olt.pk])
        response = self.client.post(url)
        self.assertRedirects(response, url)
        self.client.get(url)
        self.assertEqual(OLTConnectionTest.objects.count(), 1)
        self.assertContains(self.client.get(url), "Teste de comunicação simulado")
        self.assertContains(self.client.get("/cadastros/olts/"), "Teste simulado")

    def test_test_button_in_discovery_does_not_run_autofind(self):
        with patch.object(SimulatedAdapter, "discover") as discover:
            response = self.client.post(reverse("onu_discovery"), {"olt": self.olt.pk, "action": "test_connection"})
        self.assertContains(response, "Teste de comunicação simulado")
        discover.assert_not_called()
        self.assertEqual(OLTConnectionTest.objects.count(), 1)
        self.assertFalse(DiscoveryScan.objects.exists())

    def test_operator_and_csrf_are_required_for_test(self):
        url = reverse("olt_communication", args=[self.olt.pk])
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(url).status_code, 403)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.operator)
        self.assertEqual(csrf.post(url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertFalse(OLTConnectionTest.objects.exists())
