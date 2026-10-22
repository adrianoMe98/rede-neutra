# Rede Neutra

ERP web para cadastro e provisionamento de ONTs/ONUs em uma rede neutra. Interface em português, Django 5.2, SQLite persistente e Docker. Primeira versão funcional; a integração com equipamentos físicos ainda precisa ser homologada em laboratório.

## O que funciona

- Login, usuários de consulta e operadores (`is_staff`); superusuários gerenciam contas em `/admin/`.
- Cadastro e edição de provedores, VLANs, CTOs, OLTs e ONTs/ONUs.
- Descrição automática `provedor - cliente`, por exemplo, `acert - adriano`.
- VLAN exclusiva por número nesta rede, vinculada a um provedor. VLANs compartilhadas ou reutilizadas por domínio não fazem parte deste MVP.
- Reserva exclusiva de porta na CTO, ONT ID por PON e service-port por OLT; validação da capacidade da CTO e do provedor da VLAN.
- Plano de provisionamento, confirmação pelo operador, execução e histórico com usuário/data/resultado.
- Consulta pontual de TX/RX **na ONU**, em dBm, com data da leitura. O estado “Provisionada” indica sucesso do cadastro na integração, e não monitoração online/offline.
- Relatórios com filtros por descrição/serial, provedor, situação, VLAN, CTO e OLT, e exportação CSV compatível com Excel. Leituras simuladas são identificadas na tela e no CSV.
- Simulação de todo o fluxo sem conexão com equipamentos; adaptadores separados por fabricante.

## Iniciar com Docker

Pré-requisitos: Docker Engine com Compose v2 e Python 3.12 para gerar a configuração inicial. Não há credenciais padrão nem banco demonstrativo carregado automaticamente.

```sh
cd /workspace/rede-neutra
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/init_env.py
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```

A aplicação escuta na porta **8000**, publicada somente na interface local da máquina. O container executa migrações e coleta os arquivos estáticos antes de iniciar o Gunicorn como usuário sem privilégios. O healthcheck verifica `/health/` e o acesso ao banco. Ajuste `ERP_PORT` se a porta já estiver ocupada.

Dados ficam no volume `erp_data`. `docker compose down` preserva esse volume; `down -v` o apaga. Não use múltiplas réplicas com esta configuração SQLite. Para uma operação maior, a próxima etapa é PostgreSQL e uma fila de provisionamento.

Se o build Docker não consegue resolver os registries, mas o host tem acesso ao PyPI, use o build sem rede abaixo em um host Linux com Python 3.12 e a mesma arquitetura da imagem. O download mantém TLS e a verificação de integridade do PyPI; o build instala os mesmos pacotes fixados sem consultar a rede. Os wheels são locais e não entram no Git.

```sh
.venv/bin/pip download --only-binary=:all: -r requirements.txt -d .wheelhouse
docker compose -f compose.yaml -f compose.offline.yaml build
docker compose up -d --no-build
```

Nesta máquina de nuvem, o diretório padrão de configuração do Docker é somente leitura. Se necessário, use `mkdir -p /tmp/rede-neutra-docker` e prefixe os comandos Docker com `DOCKER_CONFIG=/tmp/rede-neutra-docker`.

Para uma demonstração **em base vazia**, sem acessar OLTs:

```sh
docker compose exec web python manage.py seed_demo
```

O comando cria as ONUs ainda não provisionadas; selecione `acert - adriano`, confira o plano, simule o provisionamento e clique em **Atualizar TX/RX**. A senha do administrador é a definida em `createsuperuser`.

## Desenvolvimento sem Docker

```sh
cd /workspace/rede-neutra
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/init_env.py
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py seed_demo  # opcional, somente base vazia
.venv/bin/gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 180
```

`init_env.py` gera `.env` com permissões restritas e chaves exclusivas, sem sobrescrever configuração existente. `.env`, bancos e credenciais não são versionados. Guarde `DJANGO_SECRET_KEY`, `OLT_CREDENTIAL_KEY` e backups do banco em local seguro; perder a chave de criptografia torna as senhas de OLT armazenadas ilegíveis.

Em cada tarefa na nuvem use o checkout existente: o ambiente já é isolado, não é necessário criar worktrees.

## Huawei: alvos informados

| Modelo | Firmware esperado | Integração |
| --- | --- | --- |
| MA5800-X7 | MA5800V100R022C11 | Adaptador SSH experimental |
| MA5683T | MA5600V800R017C10 | Adaptador SSH experimental |
| ZTE, FiberHome, outros | A definir | Simulação; adaptador físico ainda não implementado |

O adaptador Huawei gera `ont add` por SN/OMCI e `service-port` com tradução de VLAN, e consulta `display ont optical-info`. O mapeamento atual é uma VLAN de usuário igual à VLAN de serviço e um GEM port por perfil. **VLAN, line profile, service profile e mapeamento GEM devem já existir na OLT.** Cadastrar VLAN no ERP não a cria no equipamento. Configuração WAN, PPPoE, portas Ethernet, TR-069 e perfis de bridge/router não são configurados neste MVP.

O firmware é comparado com `display version` antes de enviar comandos. A compatibilidade efetiva da CLI depende do hardware, do patch de firmware, dos perfis e das permissões da conta; os testes automatizados usam respostas simuladas, não OLTs físicas. Não há alegação de homologação real. **Telnet não foi implementado**: os equipamentos informados podem usar SSH.

Para homologar SSH em laboratório:

1. Cadastre uma OLT em simulação e confira a topologia e os IDs. Use outra OLT/cadastro para a conexão física; cadastros com ONUs provisionadas não podem trocar de identidade ou modo.
2. Garanta que a máquina/container alcança o endereço de gerenciamento. Libere apenas o destino necessário nas configurações de rede do ambiente. Uma VPN deve ser configurada pelo ambiente, sem cliente VPN dentro do container.
3. Cadastre o modelo e o firmware exatos, usuário dedicado, senha, perfis existentes e GEM port. A senha é criptografada no banco com Fernet; não é exibida novamente. A rotação da chave exige recadastrar as senhas ou fazer migração própria.
4. Instale em `data/known_hosts` (ou `OLT_KNOWN_HOSTS`) a chave SSH da OLT, conferindo a impressão digital por um canal confiável com o administrador. Em Docker o arquivo deve estar dentro do volume `/app/data`, legível pelo UID 10001. Chaves desconhecidas ou alteradas são recusadas. Não use aceitação automática nem habilite algoritmos legados sem avaliação específica.
5. Defina `ENABLE_LIVE_OLT=1` no `.env` **depois de revisar a CLI e homologar os perfis**, e recrie o container: `docker compose up -d --force-recreate`. O padrão é `0` e bloqueia conexão real. Configure a OLT como SSH somente nessa etapa.
6. Reserve uma porta CTO, posição GPON/ONT e service-port livres, confira o plano e teste com uma ONU de laboratório. Confirme na CLI a configuração e a leitura óptica. Não forneça senhas no chat nem no repositório.

O ERP não executa `save`, reboot, exclusão remota ou rollback automático. Siga a política operacional da rede para persistir a configuração da OLT. Uma falha após `ont add` pode deixar configuração parcial; o ERP bloqueia o reenvio e marca **Conferir na OLT**. Uma interrupção do processo também pode deixar **Em provisionamento**; confira o estado antes de reconciliar. Não repita comandos manualmente sem verificar a OLT.

Após conferência manual, um administrador com acesso ao terminal pode reconciliar o estado, registrando o motivo:

```sh
docker compose exec web python manage.py reconcile_onu HWTC00000001 \
  --state active --verified-on-olt \
  --reason 'ONT e service-port confirmados na CLI pelo operador responsável'
```

Use `--state draft` apenas quando confirmar que **não há** cadastro nem configuração parcial na OLT. Pare/aguarde qualquer operação em andamento antes de reconciliar. O comando não altera o equipamento. Para adicionar fabricantes, implemente `plan`, `provision` e `optics` em `network/adapters.py`, registre o adaptador e crie testes com transcrições reais sanitizadas.

## Validação

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test --verbosity 2
docker compose config --quiet
docker compose exec web python manage.py test
```

Os testes exercitam restrições de cadastro, permissões, CSRF, fluxo completo de simulação, CSV, criptografia, geração de CLI Huawei, parser óptico, falhas parciais, bloqueio de reenvio e reconciliação. A homologação física fica separada desses testes.

## Publicação e operação

Para acesso externo, coloque um proxy com HTTPS à frente da aplicação, ajuste `DJANGO_ALLOWED_HOSTS` e `CSRF_TRUSTED_ORIGINS` para o domínio exato e defina `COOKIE_SECURE=1`. A configuração padrão é local e mantém `DJANGO_DEBUG=0`. A aplicação não assume cabeçalhos de proxy confiáveis; configure essa relação explicitamente na sua infraestrutura.

Crie usuários de consulta sem `is_staff`; operadores possuem `is_staff`; apenas superusuários devem administrar contas. Os operadores desta versão têm acesso à rede inteira. Isolamento de acesso por provedor, faturamento, SNMP, coleta periódica, descoberta automática de ONUs, desprovisionamento e suporte físico a outros fabricantes ficam para próximas etapas.

Faça backup consistente de SQLite (API de backup ou serviço parado), do arquivo de chaves e de `known_hosts`, protegendo esses materiais. Nunca copie um banco em escrita sem garantir consistência. As chaves de configuração e as senhas não fazem parte dos arquivos distribuídos no repositório.
