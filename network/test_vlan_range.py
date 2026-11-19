from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import CTO, OLT, ONU, Provider, ProvisioningProfile, Vlan


class VlanRangeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = get_user_model().objects.create_user("operador-vlans", is_staff=True)
        cls.reader = get_user_model().objects.create_user("consulta-vlans")
        cls.provider = Provider.objects.create(name="Provedor de teste")
        cls.other_provider = Provider.objects.create(name="Outro provedor")

    def setUp(self):
        self.client.force_login(self.operator)
        self.url = reverse("vlan_range_create")
        self.catalog = reverse("catalog", kwargs={"kind": "vlans"})
        self.payload = {"first": 1140, "last": 1156, "provider": self.provider.pk, "name": "VLAN de serviço"}

    def test_creates_inclusive_range_from_catalog(self):
        self.assertContains(self.client.get(self.catalog), self.url)
        response = self.client.post(self.url, self.payload, follow=True)
        self.assertRedirects(response, self.catalog)
        self.assertContains(response, "17 VLANs cadastradas.")
        rows = Vlan.objects.values_list("number", "provider_id", "name")
        self.assertEqual(list(rows), [(number, self.provider.pk, "VLAN de serviço") for number in range(1140, 1157)])

    def test_repeating_range_keeps_existing_description_and_fills_missing(self):
        existing = Vlan.objects.create(number=1141, provider=self.provider, name="Descrição original")
        response = self.client.post(self.url, self.payload, follow=True)
        self.assertContains(response, "16 VLANs cadastradas.")
        self.assertContains(response, "Já existia 1 para o provedor.")
        response = self.client.post(self.url, self.payload, follow=True)
        self.assertContains(response, "0 VLANs cadastradas.")
        existing.refresh_from_db()
        self.assertEqual(existing.name, "Descrição original")
        self.assertEqual(Vlan.objects.count(), 17)

    def test_other_provider_conflict_rejects_entire_range(self):
        Vlan.objects.create(number=1141, provider=self.other_provider)
        response = self.client.post(self.url, self.payload)
        self.assertContains(response, "outro provedor na faixa (1141)")
        self.assertEqual(list(Vlan.objects.values_list("number", "provider_id")), [(1141, self.other_provider.pk)])

    def test_validates_range_and_accepts_single_vlan_at_upper_bound(self):
        for limits in ({"first": 0}, {"last": 4095}, {"first": 1157}):
            with self.subTest(limits=limits):
                response = self.client.post(self.url, {**self.payload, **limits})
                self.assertTrue(response.context["form"].errors)
        self.assertFalse(Vlan.objects.exists())
        response = self.client.post(self.url, {**self.payload, "first": 4094, "last": 4094})
        self.assertRedirects(response, self.catalog)
        self.assertEqual(Vlan.objects.get().number, 4094)

    def test_requires_operator_and_csrf(self):
        self.client.force_login(self.reader)
        self.assertNotContains(self.client.get(self.catalog), self.url)
        self.assertEqual(self.client.post(self.url, self.payload).status_code, 403)
        protected_client = Client(enforce_csrf_checks=True)
        protected_client.force_login(self.operator)
        self.assertEqual(protected_client.post(self.url, self.payload).status_code, 403)
        self.assertFalse(Vlan.objects.exists())


class VlanRangeRemovalTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.operator = get_user_model().objects.create_user("operador-remocao", is_staff=True)
        cls.provider = Provider.objects.create(name="Provedor A")
        cls.other_provider = Provider.objects.create(name="Provedor B")
        cls.first = Vlan.objects.create(number=1140, provider=cls.provider)
        cls.last = Vlan.objects.create(number=1156, provider=cls.provider)
        Vlan.objects.create(number=1141, provider=cls.other_provider)
        Vlan.objects.create(number=1157, provider=cls.provider)

    def setUp(self):
        self.client.force_login(self.operator)
        self.url = reverse("vlan_range_remove")
        self.catalog = reverse("catalog", kwargs={"kind": "vlans"})
        self.payload = {"first": 1140, "last": 1156, "provider": self.provider.pk}

    def add_profile(self, vlan):
        return ProvisioningProfile.objects.create(
            name="Perfil em uso", provider=self.provider, vlan=vlan, olt=OLT.objects.create(name="OLT de teste"),
            line_profile=1, service_profile=1, gem_port=1,
        )

    def test_preview_only_selected_provider_and_remove_inclusive_range(self):
        self.assertContains(self.client.get(self.catalog), self.url)
        preview = self.client.post(self.url, self.payload)
        self.assertEqual([vlan.number for vlan in preview.context["vlans"]], [1140, 1156])
        self.assertEqual(Vlan.objects.count(), 4)
        response = self.client.post(self.url, {
            **self.payload, "action": "remove", "confirmation": preview.context["confirmation"],
        }, follow=True)
        self.assertRedirects(response, self.catalog)
        self.assertContains(response, "2 VLANs removidas do ERP.")
        self.assertEqual(list(Vlan.objects.values_list("number", flat=True)), [1141, 1157])

    def test_removal_requires_matching_signed_preview(self):
        response = self.client.post(self.url, {**self.payload, "action": "remove"})
        self.assertContains(response, "A confirmação é inválida.")
        confirmation = response.context["confirmation"]
        Vlan.objects.create(number=1142, provider=self.provider)
        response = self.client.post(self.url, {**self.payload, "action": "remove", "confirmation": confirmation})
        self.assertContains(response, "A seleção mudou.")
        self.assertEqual(Vlan.objects.count(), 5)

    def test_preview_shows_and_blocks_onu_and_profile_dependencies(self):
        profile = self.add_profile(self.last)
        ONU.objects.create(
            provider=self.provider, olt=profile.olt, vlan=self.first, cto=CTO.objects.create(name="CTO teste"),
            subscriber="Assinante teste", serial="HWTC12345678", port=1, service_port=1,
        )
        response = self.client.post(self.url, self.payload)
        first, last = response.context["vlans"]
        self.assertTrue(first.has_onu)
        self.assertTrue(last.has_profile)
        self.assertFalse(response.context["confirmation"])
        self.assertEqual(Vlan.objects.count(), 4)

    def test_dependency_added_after_preview_rolls_back_all_delete_batches(self):
        first, last = 1, 501
        Vlan.objects.bulk_create([Vlan(number=number, provider=self.provider) for number in range(first, last + 1)])
        payload = {**self.payload, "first": first, "last": last}
        preview = self.client.post(self.url, payload)
        self.add_profile(Vlan.objects.get(number=last))
        response = self.client.post(self.url, {
            **payload, "action": "remove", "confirmation": preview.context["confirmation"],
        })
        self.assertContains(response, "vinculadas a ONUs ou perfis de provisionamento")
        self.assertFalse(response.context["confirmation"])
        self.assertEqual(Vlan.objects.filter(number__range=(first, last)).count(), 501)

    def test_requires_operator_and_csrf(self):
        self.client.force_login(get_user_model().objects.create_user("consulta-remocao"))
        self.assertNotContains(self.client.get(self.catalog), self.url)
        self.assertEqual(self.client.post(self.url, self.payload).status_code, 403)
        protected_client = Client(enforce_csrf_checks=True)
        protected_client.force_login(self.operator)
        self.assertEqual(protected_client.post(self.url, self.payload).status_code, 403)
        self.assertEqual(Vlan.objects.count(), 4)
