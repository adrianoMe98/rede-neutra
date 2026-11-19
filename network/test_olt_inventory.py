from contextlib import contextmanager
from io import BytesIO
from unittest.mock import Mock, patch
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .adapters import AdapterError, HuaweiAdapter, HuaweiTelnetAdapter, TelnetTerminal
from .models import CTO, OLT, ONU, Provider, ProvisioningProfile, Vlan
from .olt_inventory import parse_mac_table, parse_ont_configuration, parse_service_ports, provider_targets


SERVICE_PORTS = """
  1454 1141 common gpon 0/1 /1  7  100 vlan 100 - - down
  1455 1141 common gpon 0 /1 /1  7  101 vlan 101 - - up
  1456 1141 common eth  0 /8 /0  -  -   vlan 100 - - up
  Total: 3  (Up/Down : 2/1)
"""
MAC_TABLE = """
 SRV-P BUNDLE TYPE MAC            MAC TYPE F /S /P  VPI  VCI  VLAN ID
 1454  -      gpon 0011-2233-4455 dynamic  0 /1 /1   7   100  1141
 1455  -      gpon 0011-2233-4466 dynamic  0 /1 /1   7   101  1141
 1456  -      eth  0011-2233-4477 static   0 /8 /0   -   -    1141
 Total: 3
"""
ONT_CONFIGURATION = """
 [gpon]
 interface gpon 0/1
 ont add 1 7 sn-auth "49544253E87309FE" omci ont-lineprofile-id 100
ont-srvprofile-id 100 desc "=ONT de teste"
 ont port native-vlan 1 7 eth 1 vlan 100 priority 0
 #
 [bbs-config]
 service-port 1454 vlan 1141 gpon 0/1/1 ont 7 gemport 100 multi-service
 return
"""


class InventoryParserTest(SimpleTestCase):
    def test_extracts_ont_position_and_groups_service_ports(self):
        self.assertEqual(parse_service_ports(SERVICE_PORTS, 1141), {(0, 1, 1, 7): {1454, 1455}})
        self.assertEqual(parse_mac_table(MAC_TABLE)[1454], {"00:11:22:33:44:55"})
        self.assertEqual(parse_service_ports("Failure: No service virtual ports exist.", 1141), {})
        self.assertEqual(parse_mac_table("Total: 0"), {})

    def test_rejects_mismatched_vlan_and_incomplete_tables(self):
        with self.assertRaises(AdapterError):
            parse_service_ports(SERVICE_PORTS, 100)
        with self.assertRaises(AdapterError):
            parse_service_ports(SERVICE_PORTS.replace("Total: 3", "Total: 4"), 1141)
        with self.assertRaises(AdapterError):
            parse_mac_table("Original command output missing")

    def test_pagination_marker_preserves_first_service_port_on_next_page(self):
        adapter = HuaweiAdapter()
        channel = Mock()
        channel.recv_ready.return_value = True
        channel.recv.side_effect = [
            b" 10 2300 common gpon 0/0 /0  42  100 vlan 100 - - up\r\n---- Mo",
            b"re ( Press 'Q' to break ) ----",
            b"\r          11 2300 common gpon 0/0 /0  43  100 vlan 100 - - up\r\n"
            b" Total: 2  (Up/Down : 2/0)\r\nOLT(config)#",
        ]
        output = adapter.command(channel, "display service-port vlan 2300")
        self.assertEqual(parse_service_ports(output, 2300), {(0, 0, 0, 42): {10}, (0, 0, 0, 43): {11}})
        self.assertEqual([call.args[0] for call in channel.sendall.call_args_list],
                         ["display service-port vlan 2300\n", " "])

    def test_ont_configuration_waits_for_the_device_prompt_between_commands(self):
        adapter = HuaweiTelnetAdapter()
        connection = Mock()
        connection.read_some.side_effect = [
            b">>User name:",
            b">>User password:",
            b"\r\nOLT-TESTE>",
            ONT_CONFIGURATION.split(" service-port", 1)[0].strip().encode() + b"\r\n  <bbs-config>",
            b"\r\n service-port 1454 vlan 1141 gpon 0/1/1 ont 7 gemport 100 multi-service"
            b"\r\n #\r\n return\r\nOLT-TESTE(config)#",
            SERVICE_PORTS.encode() + b"\nOLT-TESTE(config)#",
        ]
        channel = TelnetTerminal(connection)
        adapter.login(channel, "operador-teste", "senha-teste")
        output = adapter.command(channel, "display current-configuration ont 0/1/1 7")
        self.assertIn("service-port 1454", output)
        self.assertTrue(output.endswith("OLT-TESTE(config)#"))
        output = adapter.command(channel, "display service-port vlan 1141")
        self.assertEqual(parse_service_ports(output, 1141), {(0, 1, 1, 7): {1454, 1455}})

    def test_distinguishes_serial_from_mac_and_reads_wrapped_description(self):
        ont = parse_ont_configuration(ONT_CONFIGURATION, (0, 1, 1, 7))
        self.assertEqual((ont.description, ont.serial, ont.mac), ("=ONT de teste", "49544253E87309FE", ""))
        mac_auth = ONT_CONFIGURATION.replace('sn-auth "49544253E87309FE"', "mac-auth 0011-2233-4455")
        ont = parse_ont_configuration(mac_auth, (0, 1, 1, 7))
        self.assertEqual((ont.serial, ont.mac), ("", "00:11:22:33:44:55"))
        with self.assertRaises(AdapterError):
            parse_ont_configuration(ONT_CONFIGURATION, (0, 1, 1, 8))

    def test_configuration_can_contain_failure_in_description(self):
        adapter = HuaweiAdapter()
        output = ONT_CONFIGURATION.replace("=ONT de teste", "Failure Labs")
        with patch.object(adapter, "read_prompt", return_value=output):
            self.assertEqual(adapter.command(Mock(), "display current-configuration ont 0/1/1 7"), output)


class InventoryReportTest(TestCase):
    def setUp(self):
        self.provider = Provider.objects.create(name="{=1+1}")
        self.vlan = Vlan.objects.create(number=1141, provider=self.provider)
        self.olt = OLT.objects.create(name="OLT A", mode="ssh")
        self.second_olt = OLT.objects.create(name="OLT B", mode="ssh")
        for name in ("Perfil A", "Perfil duplicado"):
            ProvisioningProfile.objects.create(
                name=name, provider=self.provider, vlan=self.vlan, olt=self.olt,
                line_profile=1, service_profile=1, gem_port=1,
            )
        cto = CTO.objects.create(name="CTO de teste")
        ONU.objects.create(
            subscriber="Uso legado", serial="HWTC12345678", provider=self.provider, vlan=self.vlan,
            olt=self.second_olt, cto=cto, port=1, service_port=1,
        )
        other = Provider.objects.create(name="Outro provedor")
        ProvisioningProfile.objects.create(
            name="Perfil externo", provider=other, vlan=Vlan.objects.create(number=1142, provider=other),
            olt=OLT.objects.create(name="OLT externa", mode="ssh"), line_profile=1, service_profile=1, gem_port=1,
        )
        self.user = get_user_model().objects.create_user("operador", is_staff=True)
        self.url = reverse("olt_inventory")

    def test_vlan_group_targets_all_matching_numbers_on_the_profiles_olt(self):
        Vlan.objects.filter(pk=self.vlan.pk).update(name="Uplink")
        Vlan.objects.create(number=1143, provider=self.provider, name="Uplink")
        Vlan.objects.filter(number=1142).update(name="Uplink")
        ProvisioningProfile.objects.create(
            name="Perfil Uplink", provider=self.provider, olt=self.olt, vlan_description="Uplink",
            line_profile=1, service_profile=1,
        )
        self.assertEqual(provider_targets(self.provider), [(self.olt, [1141, 1143]), (self.second_olt, [1141])])

    def test_requires_operator_and_post_to_open_ssh(self):
        with patch.object(HuaweiAdapter, "session") as ssh:
            self.assertEqual(self.client.get(self.url).status_code, 302)
            user = get_user_model().objects.create_user("consulta")
            self.client.force_login(user)
            self.assertEqual(self.client.post(self.url, {"provider": self.provider.pk}).status_code, 403)
            self.client.force_login(self.user)
            self.assertContains(self.client.get(self.url), "Consultar e baixar XLSX")
            self.assertContains(self.client.get(reverse("reports")), self.url)
            ssh.assert_not_called()

    def test_exports_scoped_onts_and_reports_partial_failure(self):
        sessions = []

        @contextmanager
        def session(adapter, olt, *, read_only):
            sessions.append((olt.pk, read_only))
            if olt == self.second_olt:
                raise AdapterError("Sem conexão com a OLT.")
            yield object()

        self.client.force_login(self.user)
        with patch.object(HuaweiAdapter, "session", session), patch.object(HuaweiAdapter, "command", side_effect=[
            "", "", SERVICE_PORTS, MAC_TABLE, ONT_CONFIGURATION,
        ]) as commands:
            response = self.client.post(self.url, {"provider": self.provider.pk})
            self.assertEqual(response.status_code, 200)
            self.assertIn(".xlsx", response["Content-Disposition"])
            with ZipFile(BytesIO(b"".join(response.streaming_content))) as xlsx:
                shared = xlsx.read("xl/sharedStrings.xml").decode()
                self.assertIn("=ONT de teste", shared)
                self.assertIn("{=1+1}", shared)
                self.assertIn("49544253E87309FE", shared)
                self.assertIn("00:11:22:33:44:55, 00:11:22:33:44:66", shared)
                self.assertNotIn("00:11:22:33:44:77", shared)
                self.assertIn("Sem conexão com a OLT.", shared)
                self.assertNotIn("<f", xlsx.read("xl/worksheets/sheet1.xml").decode())
        self.assertEqual(sessions, [(self.olt.pk, True), (self.second_olt.pk, True)])
        self.assertEqual([call.args[1] for call in commands.call_args_list], [
            "enable", "config", "display service-port vlan 1141", "display mac-address vlan 1141",
            "display current-configuration ont 0/1/1 7",
        ])
