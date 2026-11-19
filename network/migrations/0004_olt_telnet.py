from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("network", "0003_oltconnectiontest")]

    operations = [
        migrations.AddField(
            model_name="olt",
            name="telnet_port",
            field=models.PositiveIntegerField(
                default=23, validators=[MinValueValidator(1), MaxValueValidator(65535)], verbose_name="porta Telnet",
            ),
        ),
        migrations.AlterField(
            model_name="olt",
            name="mode",
            field=models.CharField(
                choices=[
                    ("simulation", "Simulação"), ("ssh", "SSH • Huawei (experimental)"),
                    ("telnet", "Telnet • Huawei (experimental)"),
                ],
                default="simulation", max_length=20, verbose_name="integração",
            ),
        ),
        migrations.AlterField(
            model_name="olt",
            name="username",
            field=models.CharField(blank=True, max_length=80, verbose_name="usuário de gerenciamento"),
        ),
        migrations.AlterField(
            model_name="oltconnectiontest",
            name="stage",
            field=models.CharField(
                choices=[
                    ("ready", "Concluído"), ("configuration", "Configuração"), ("host_key", "Chave SSH"),
                    ("network", "Rede / porta TCP"), ("ssh", "Negociação SSH"),
                    ("authentication", "Autenticação"), ("cli", "CLI / permissões"),
                    ("firmware", "Firmware"), ("unexpected", "Falha inesperada"),
                ],
                max_length=20,
            ),
        ),
    ]
