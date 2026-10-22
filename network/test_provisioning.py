import io
from pathlib import Path
import time
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .adapters import AdapterError, DiscoveredONT, HuaweiAdapter, SimulatedAdapter
from .discovery import TOKEN_SALT, load_discovery, scan_olt
from .forms import ONUForm, ProvisioningProfileForm, VlanForm
from .models import CTO, OLT, ONU, DiscoveryScan, Operation, Provider, ProvisioningProfile, Vlan
from .serials import normalize_serial, serial_aliases
from .services import provision

AUTOFIND = (Path(__file__).parent / "fixtures" / "huawei_autofind.txt").read_text()
EMPTY = "Failure: The automatically found ONTs do not exist\nMA5800(config)#"


class HuaweiAutofindTest(SimpleTestCase):
    def test_parse_huawei_records_and_normalize_serials(self):
        found = HuaweiAdapter.parse_discovery(AUTOFIND)
        self.assertEqual([(ont.serial, ont.pon_address) for ont in found], [("HWTCABCD1234", "0/1/0"), ("ZTEG00000002", "0/2/3")])
        self.assertEqual(found[0].equipment, "HG8245H")
        self.assertEqual(found[1].vendor, "ZTEG")
        self.assertNotIn("Password", repr(found))

    def test_no_ont_is_successful_empty_result(self):
        self.assertEqual(HuaweiAdapter.parse_discovery(EMPTY), [])
        self.assertEqual(HuaweiAdapter.parse_discovery("The number of GPON autofind ONT is 0\nMA5800(config)#"), [])

    def test_unknown_or_incomplete_output_is_not_empty_success(self):
        for response in ["MA5800(config)#", "Error: insufficient privilege", "F/S/P : 0/1/0", "Ont SN : HWTC00000001"]:
            with self.subTest(response=response), self.assertRaises(AdapterError):
                HuaweiAdapter.parse_discovery(response)

    def test_invalid_or_conflicting_records_are_rejected(self):
        for response in ["F/S/P : 0/50/0\nOnt SN : HWTC00000001", "F/S/P : 0/1/0\nOnt SN : INVALID",
                         "F/S/P : 0/1/0\nOnt SN : HWTC00000001\nF/S/P : 0/2/0\nOnt SN : HWTC00000001"]:
            with self.subTest(response=response), self.assertRaises(AdapterError):
                HuaweiAdapter.parse_discovery(response)

    def test_duplicate_identical_records_are_deduplicated(self):
        block = "F/S/P : 0/1/0\nOnt SN : HWTC00000001\n"
        self.assertEqual(len(HuaweiAdapter.parse_discovery(block + block)), 1)

    def test_discovery_sends_only_read_commands(self):
        adapter = HuaweiAdapter()
        with patch.object(adapter, "session") as session, patch.object(adapter, "command") as command:
            command.side_effect = lambda channel, cmd: AUTOFIND if cmd == "display ont autofind all" else "MA5800(config)#"
            result = adapter.discover(MagicMock())
        self.assertEqual(len(result), 2)
        self.assertEqual([call.args[1] for call in command.call_args_list], ["enable", "config", "display ont autofind all"])

    def test_empty_autofind_cli_failure_is_handled_only_for_search(self):
        adapter = HuaweiAdapter()
        channel = MagicMock()
        with patch.object(adapter, "read_prompt", return_value=EMPTY):
            self.assertEqual(adapter.command(channel, "display ont autofind all"), EMPTY)
            with self.assertRaises(AdapterError):
                adapter.command(channel, "ont add 0 0")

    def test_serial_aliases_cover_huawei_ascii_and_hex(self):
        self.assertEqual(normalize_serial("48575443abcd1234"), "HWTCABCD1234")
        self.assertEqual(normalize_serial("hwtc-abcd1234"), "HWTCABCD1234")
        self.assertEqual(serial_aliases("HWTCABCD1234"), {"HWTCABCD1234", "48575443ABCD1234"})


@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                             "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class ProvisioningWorkflowTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = get_user_model().objects.create_user("teste-provisionamento", password="test-only-user!", is_staff=True)
        cls.other_operator = get_user_model().objects.create_user("outro-operador", is_staff=True)
        cls.viewer = get_user_model().objects.create_user("leitor")
        cls.provider = Provider.objects.create(name="acert")
        cls.other_provider = Provider.objects.create(name="Outro provedor")
        cls.vlan = Vlan.objects.create(number=100, provider=cls.provider)
        cls.other_vlan = Vlan.objects.create(number=200, provider=cls.other_provider)
        cls.cto = CTO.objects.create(name="CTO-001", total_ports=16)
        cls.olt = OLT.objects.create(name="OLT-Centro", model="MA5800-X7", firmware="MA5800V100R022C11")
        cls.profile = ProvisioningProfile.objects.create(name="Acert residencial", provider=cls.provider, olt=cls.olt,
                                                         vlan=cls.vlan, line_profile=20, service_profile=30, gem_port=2, user_vlan=400)

    def setUp(self):
        self.client.force_login(self.operator)

    def onu_data(self, **changes):
        return {"profile": self.profile.pk, "provider": self.provider.pk, "subscriber": "adriano", "serial": "HWTCABCD1234",
                "vlan": self.vlan.pk, "cto": self.cto.pk, "port": 1, "olt": self.olt.pk,
                "frame": 0, "slot": 1, "pon": 0, "ont_id": 1, "service_port": 1000} | changes

    def create_onu(self, **changes):
        form = ONUForm(self.onu_data(**changes))
        self.assertTrue(form.is_valid(), form.errors)
        return form.save()

    def test_profile_catalog_and_edit_pages_render(self):
        for url in ["/cadastros/perfis/", "/cadastros/perfis/novo/", f"/cadastros/perfis/{self.profile.pk}/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_buttons_are_visible_to_operators(self):
        response = self.client.get("/onus/")
        self.assertContains(response, reverse("onu_discovery"))
        self.assertContains(response, "/cadastros/perfis/")

    def test_profile_snapshot_drives_cli_and_is_stable_after_profile_edit(self):
        onu = self.create_onu()
        self.profile.line_profile = 55
        self.profile.name = "Perfil atualizado"
        self.profile.save()
        onu.refresh_from_db()
        plan = HuaweiAdapter().plan(onu)
        self.assertIn("ont-lineprofile-id 20 ont-srvprofile-id 30", plan[3])
        self.assertIn("gemport 2 multi-service user-vlan 400", plan[-1])
        self.assertEqual(onu.profile_snapshot["name"], "Acert residencial")

    def test_profile_without_user_vlan_uses_service_vlan(self):
        self.profile.user_vlan = None
        self.profile.save()
        self.assertEqual(self.create_onu().provisioning_parameters["user_vlan"], 100)

    def test_legacy_onu_uses_olt_defaults(self):
        onu = self.create_onu(profile="")
        self.assertEqual(onu.provisioning_parameters["line_profile"], 10)
        self.assertIn("user-vlan 100", HuaweiAdapter().plan(onu)[-1])

    def test_invalid_profile_parameters_never_become_cli(self):
        onu = self.create_onu()
        onu.profile_snapshot["line_profile"] = "20\nquit"
        with self.assertRaises(AdapterError):
            HuaweiAdapter().plan(onu)

    def test_profile_must_match_provider_vlan_and_olt(self):
        other_olt = OLT.objects.create(name="Outra OLT")
        for changes in [{"provider": self.other_provider.pk, "vlan": self.other_vlan.pk}, {"olt": other_olt.pk}, {"vlan": self.other_vlan.pk}]:
            with self.subTest(changes=changes):
                self.assertFalse(ONUForm(self.onu_data(**changes)).is_valid())

    def test_inactive_profile_cannot_be_used_for_new_onu(self):
        self.profile.active = False
        self.profile.save()
        self.assertFalse(ONUForm(self.onu_data()).is_valid())

    def test_profile_cannot_use_vlan_from_another_provider(self):
        form = ProvisioningProfileForm({"name": "Inválido", "provider": self.provider.pk, "vlan": self.other_vlan.pk,
                                        "olt": self.olt.pk, "line_profile": 1, "service_profile": 1, "gem_port": 1, "active": True})
        self.assertFalse(form.is_valid())
        self.assertIn("vlan", form.errors)

    def test_profile_identity_and_vlan_ownership_are_protected(self):
        self.create_onu()
        self.profile.olt = OLT.objects.create(name="Outra OLT")
        with self.assertRaises(ValidationError):
            self.profile.full_clean()
        self.assertFalse(VlanForm({"number": 100, "provider": self.other_provider.pk}, instance=self.vlan).is_valid())

    def test_get_discovery_does_not_contact_olt(self):
        with patch.object(SimulatedAdapter, "discover") as discovery:
            self.assertEqual(self.client.get(reverse("onu_discovery")).status_code, 200)
            discovery.assert_not_called()
        self.assertFalse(DiscoveryScan.objects.exists())

    def test_discovery_is_post_csrf_protected_and_operator_only(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.operator)
        self.assertEqual(csrf_client.post(reverse("onu_discovery"), {"olt": self.olt.pk}).status_code, 403)
        self.client.force_login(self.viewer)
        with patch.object(SimulatedAdapter, "discover") as discovery:
            self.assertEqual(self.client.post(reverse("onu_discovery"), {"olt": self.olt.pk}).status_code, 403)
            discovery.assert_not_called()

    @patch("paramiko.SSHClient")
    def test_discovery_simulation_and_provisioning_end_to_end(self, ssh):
        response = self.client.post(reverse("onu_discovery"), {"olt": self.olt.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SIMULAÇÃO")
        row = response.context["rows"][0]
        token = row["token"]
        item = row["item"]
        initial_page = self.client.get(reverse("onu_create"), {"discovery": token})
        self.assertEqual(initial_page.context["form"].initial["serial"], item.serial)
        response = self.client.post(reverse("onu_create"), self.onu_data(
            serial=item.serial, frame=item.frame, slot=item.slot, pon=item.pon, discovery_token=token))
        self.assertEqual(response.status_code, 302)
        onu = ONU.objects.get(serial=item.serial)
        self.assertIsNotNone(onu.discovered_at)
        self.assertEqual(onu.profile_snapshot["line_profile"], 20)
        self.client.post(reverse("onu_action", args=[onu.pk, "provision"]), {"confirmed": "yes"})
        onu.refresh_from_db()
        self.assertEqual(onu.status, ONU.Status.ACTIVE)
        self.assertEqual(Operation.objects.get().actor, self.operator)
        ssh.assert_not_called()
        _, rows = scan_olt(self.olt, self.operator)
        self.assertNotIn(item.serial, [row["item"].serial for row in rows])

    def test_signed_discovery_selection_cannot_be_changed(self):
        _, rows = scan_olt(self.olt, self.operator)
        token = rows[0]["token"]
        item = rows[0]["item"]
        response = self.client.post(reverse("onu_create"), self.onu_data(serial="HWTC00000099", discovery_token=token,
                                   frame=item.frame, slot=item.slot, pon=item.pon))
        self.assertEqual(response.status_code, 200)
        self.assertIn("serial", response.context["form"].errors)
        self.assertFalse(ONU.objects.exists())

    def test_discovery_token_owner_expiry_and_signature(self):
        _, rows = scan_olt(self.olt, self.operator)
        token = rows[0]["token"]
        for value, actor in [(token, self.other_operator), ("invalid-token", self.operator)]:
            with self.assertRaises(AdapterError):
                load_discovery(value, actor)
        with patch("django.core.signing.time.time", return_value=time.time() + 901):
            with self.assertRaises(AdapterError):
                load_discovery(token, self.operator)
        response = self.client.get(reverse("onu_create"), {"discovery": "invalid-token"})
        self.assertRedirects(response, reverse("onu_discovery"))

    def test_malformed_signed_payload_is_rejected(self):
        token = signing.dumps({"scan": 1}, salt=TOKEN_SALT)
        with self.assertRaises(AdapterError):
            load_discovery(token, self.operator)

    def test_existing_draft_is_linked_using_hex_alias(self):
        onu = self.create_onu()
        ONU.objects.filter(pk=onu.pk).update(serial="48575443ABCD1234")
        with patch.object(SimulatedAdapter, "discover", return_value=[DiscoveredONT("HWTCABCD1234", 0, 1, 0)]):
            _, rows = scan_olt(self.olt, self.operator)
        self.assertEqual(rows[0]["existing"].pk, onu.pk)
        self.assertTrue(rows[0]["token"])
        self.assertFalse(ONUForm(self.onu_data(port=2, ont_id=2, service_port=1001)).is_valid())

    def test_existing_draft_continued_from_search_retains_discovery_validation(self):
        onu = self.create_onu()
        with patch.object(SimulatedAdapter, "discover", return_value=[DiscoveredONT(onu.serial, 0, 1, 0)]):
            _, rows = scan_olt(self.olt, self.operator)
        token = rows[0]["token"]
        response = self.client.get(reverse("onu_detail", args=[onu.pk]), {"discovery": token})
        self.assertContains(response, 'name="discovery_token"')
        self.client.post(reverse("onu_action", args=[onu.pk, "provision"]), {"confirmed": "yes", "discovery_token": token})
        onu.refresh_from_db()
        self.assertEqual(onu.status, ONU.Status.ACTIVE)
        self.assertIsNotNone(onu.discovered_at)

    def test_same_serial_at_different_location_requires_manual_review(self):
        onu = self.create_onu()
        with patch.object(SimulatedAdapter, "discover", return_value=[DiscoveredONT(onu.serial, 0, 2, 0)]):
            _, rows = scan_olt(self.olt, self.operator)
        self.assertFalse(rows[0]["position_matches"])
        self.assertEqual(rows[0]["token"], "")

    def test_failed_discovery_is_recorded_without_creating_an_onu(self):
        with patch.object(SimulatedAdapter, "discover", side_effect=AdapterError("A conexão falhou")):
            response = self.client.post(reverse("onu_discovery"), {"olt": self.olt.pk})
        self.assertContains(response, "A conexão falhou")
        self.assertEqual(DiscoveryScan.objects.get().outcome, "failed")
        self.assertFalse(ONU.objects.exists())

    def test_empty_results_render_successfully(self):
        with patch.object(SimulatedAdapter, "discover", return_value=[]):
            response = self.client.post(reverse("onu_discovery"), {"olt": self.olt.pk})
        self.assertContains(response, "Nenhuma ONT disponível")
        self.assertEqual(DiscoveryScan.objects.get().outcome, "success")

    def test_discovered_ont_must_still_be_on_same_pon_before_huawei_mutation(self):
        onu = self.create_onu()
        onu.discovered_at = timezone.now()
        adapter = HuaweiAdapter()
        for output in [EMPTY, AUTOFIND.replace("0/1/0", "0/3/2")]:
            with self.subTest(output=output), patch.object(adapter, "session"), patch.object(adapter, "command") as command:
                command.side_effect = lambda channel, cmd: output if cmd == "display ont autofind all" else "MA5800(config)#"
                with self.assertRaises(AdapterError):
                    adapter.provision(onu)
                self.assertNotIn("ont add", " ".join(call.args[1] for call in command.call_args_list))

    def test_discovered_ont_provisions_with_profile_when_still_available(self):
        onu = self.create_onu()
        onu.discovered_at = timezone.now()
        adapter = HuaweiAdapter()
        with patch.object(adapter, "session"), patch.object(adapter, "command") as command:
            command.side_effect = lambda channel, cmd: AUTOFIND if cmd == "display ont autofind all" else "MA5800(config)#"
            adapter.provision(onu)
        sent = [call.args[1] for call in command.call_args_list]
        self.assertEqual(sent[2], "display ont autofind all")
        self.assertTrue(any("ont-lineprofile-id 20" in cmd for cmd in sent))

    def test_form_edit_saves_profile_snapshot(self):
        onu = self.create_onu(profile="")
        response = self.client.post(reverse("onu_edit", args=[onu.pk]), self.onu_data())
        self.assertEqual(response.status_code, 302)
        onu.refresh_from_db()
        self.assertEqual(onu.profile_id, self.profile.pk)
        self.assertEqual(onu.profile_snapshot["service_profile"], 30)
