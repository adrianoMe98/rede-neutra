from socketserver import StreamRequestHandler, TCPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .adapters import AdapterError, HuaweiTelnetAdapter, get_adapter
from .forms import OLTForm
from .models import OLT, OLTConnectionTest


@override_settings(ENABLE_LIVE_OLT=False)
class HuaweiTelnetTest(SimpleTestCase):
    def setUp(self):
        self.olt = SimpleNamespace(
            vendor="huawei", mode="telnet", model="MA5683T", firmware="MA5600V800R017C10",
            host="127.0.0.1", telnet_port=23, username="olt-teste", get_password=lambda: "senha-teste",
        )
        self.adapter = get_adapter(self.olt)

    def test_discovery_through_telnet_negotiation_and_login(self):
        received = []
        replies = (
            b"Password: ",
            b"\r\nOLT-TESTE>",
            b"VERSION : MA5600V800R017C10\r\nOLT-TESTE>",
            b"\r\nOLT-TESTE#",
            b"\r\nOLT-TESTE(config)#",
            b"\r\nThe number of GPON autofind ONT is 0\r\nOLT-TESTE(config)#",
        )

        class Handler(StreamRequestHandler):
            timeout = 3

            def handle(self):
                self.wfile.write(b"\xff\xfb\x01\r\nUser name: ")
                for reply in replies:
                    line = self.rfile.readline()
                    received.append(line)
                    if not line:
                        return
                    self.wfile.write(reply)

        with TCPServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = 3
            self.olt.telnet_port = server.server_address[1]
            thread = Thread(target=server.handle_request, daemon=True)
            thread.start()
            with patch("paramiko.SSHClient") as ssh:
                self.assertEqual(self.adapter.discover(self.olt), [])
                ssh.assert_not_called()
            thread.join(timeout=3)
        self.assertTrue(received[0].endswith(b"olt-teste\r\n"))
        self.assertEqual(received[1:], [b"senha-teste\r\n", b"display version\r\n", b"enable\r\n", b"config\r\n",
                                        b"display ont autofind all\r\n"])

    @patch("network.adapters.TelnetConnection")
    def test_login_rejection_is_redacted_and_connection_closed(self, telnet):
        conn = telnet.return_value
        conn.read_some.side_effect = [b"User ", b"name: ", b"Password: ", b"\r\nInvalid password\r\nUser name:"]
        with self.assertRaises(AdapterError) as error:
            self.adapter.test_connection(self.olt)
        self.assertEqual(error.exception.code, "authentication")
        self.assertNotIn("senha-teste", str(error.exception))
        self.assertEqual([call.args[0] for call in conn.write.call_args_list], [b"olt-teste\r\n", b"senha-teste\r\n"])
        conn.close.assert_called_once()

    @patch("network.adapters.TelnetConnection")
    def test_firmware_and_provision_gate(self, telnet):
        with self.assertRaisesMessage(AdapterError, "Provisionamento Telnet desabilitado"):
            with self.adapter.session(self.olt):
                self.fail("A sessão de escrita exige ENABLE_LIVE_OLT.")
        telnet.assert_not_called()
        conn = telnet.return_value
        conn.read_some.side_effect = [b"Password: ", b"\r\nOLT-TESTE>", b"VERSAO DIFERENTE\r\nOLT-TESTE>"]
        with self.assertRaises(AdapterError) as error:
            self.adapter.test_connection(self.olt)
        self.assertEqual(error.exception.code, "firmware")
        conn.close.assert_called_once()


class TelnetConfigurationTest(TestCase):
    def test_legacy_olt_form_post_keeps_default_telnet_port(self):
        form = OLTForm({
            "name": "OLT simulada", "vendor": "huawei", "mode": "simulation", "ssh_port": 2222,
            "line_profile": 10, "service_profile": 10, "gem_port": 1,
        })
        self.assertTrue(form.is_valid(), form.errors)
        olt = form.save()
        self.assertEqual((olt.ssh_port, olt.telnet_port), (2222, 23))

    @patch.object(HuaweiTelnetAdapter, "test_connection", return_value="Comunicação Telnet confirmada.")
    def test_communication_view_uses_configured_telnet_port(self, check):
        user = get_user_model().objects.create_user("operador-telnet", is_staff=True)
        self.client.force_login(user)
        olt = OLT.objects.create(name="OLT Telnet", mode="telnet", host="192.0.2.20", telnet_port=2323)
        url = reverse("olt_communication", args=[olt.pk])
        self.assertContains(self.client.get(url), "192.0.2.20:2323")
        check.assert_not_called()
        self.assertRedirects(self.client.post(url), url)
        self.assertEqual(OLTConnectionTest.objects.get().target, "192.0.2.20:2323")
