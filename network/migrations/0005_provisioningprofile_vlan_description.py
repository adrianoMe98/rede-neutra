import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("network", "0004_olt_telnet")]

    operations = [
        migrations.AddField(
            model_name="provisioningprofile",
            name="vlan_description",
            field=models.CharField(blank=True, max_length=120, verbose_name="grupo de VLANs de serviço"),
        ),
        migrations.AlterField(
            model_name="provisioningprofile",
            name="vlan",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="provisioning_profiles", to="network.vlan", verbose_name="VLAN de serviço",
            ),
        ),
        migrations.AddConstraint(
            model_name="provisioningprofile",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(vlan__isnull=False, vlan_description="")
                    | (models.Q(vlan__isnull=True) & ~models.Q(vlan_description=""))
                ),
                name="profile_has_one_service_vlan_selection",
            ),
        ),
    ]
