# Azure Secure Files для Hermes deployment

Этот каталог содержит только безопасные примеры. Рабочие файлы без суффикса
`.example` игнорируются Git. В Azure DevOps загружаются только два файла,
перечисленные ниже; пароль Vault и CI-ключ задаются secret variables.

## Подготовка Vault

Создайте рабочие копии и замените placeholder пароля через локальный редактор.
**Если `../group_vars/all/vault.yml` уже существует — это ваш рабочий Vault с
настоящими секретами; не перезатирайте его этим `cp`.** Команда ниже
специально откажется работать, если файл уже есть:

```bash
cd hermes/ansible/azure-secure-files
if [ -e ../group_vars/all/vault.yml ]; then
  echo "../group_vars/all/vault.yml уже существует — не трогаем." >&2
  exit 1
fi
cp vault.yml.example ../group_vars/all/vault.yml
cp hermes-vault-password.example hermes-vault-password
chmod 600 ../group_vars/all/vault.yml hermes-vault-password
nano hermes-vault-password
```

Зашифруйте Vault до добавления настоящих значений, затем редактируйте только
через `ansible-vault edit`:

```bash
ansible-vault encrypt \
  --vault-password-file hermes-vault-password \
  ../group_vars/all/vault.yml

EDITOR=nano ansible-vault edit \
  --vault-password-file hermes-vault-password \
  ../group_vars/all/vault.yml
```

Первая строка готового файла должна начинаться с `$ANSIBLE_VAULT;`. Никогда не
загружайте и не коммитьте его расшифрованную версию.

Уже заданный `hermes_secret_env.GITHUB_TOKEN` используйте без дублирования:
отдельного top-level `GITHUB_TOKEN` или GitHub-токена для этого pipeline не нужно.
Для подключённого VPS `tailscale_auth_key` в Vault не требуется. CI-ключ
`HERMES_TAILSCALE_AUTH_KEY` задаётся отдельно в защищённой группе переменных
`hermes-deploy-secrets` (см. ниже). Локальный `hermes-vault-password` нужен для
команд подготовки Vault; в Secure Files он не загружается.

## Вход через Tailscale SSH без private key

На VPS должен быть включён Tailscale SSH. CI-узел подключается к tailnet через
`HERMES_TAILSCALE_AUTH_KEY`, а SSH policy разрешает тегу `tag:hermes-deploy`
вход пользователем из `ansible_user` без browser check. Отдельные private/public
SSH keys и правки `authorized_keys` не нужны. Обычный OpenSSH этим pipeline
не поддерживается; локальный способ deployment не меняется.

Старый Secure File `hermes-vps-ssh-key` больше не используется после обновления
pipeline в `main`. Его можно убрать из ADO, если он не нужен другим pipelines.
Полная настройка и миграция: [tailscale-deploy.md](../../../azure-ci/tailscale-deploy.md).

## Подготовка known_hosts

Создайте `hermes-vps-known-hosts` на доверенном компьютере. Полученный SSH
fingerprint обязательно сравните с fingerprint из консоли VPS-провайдера;
`ssh-keyscan` сам по себе не доказывает подлинность сервера. Для стандартного
SSH-порта команда выглядит так:

```bash
printf 'VPS host: '
IFS= read -r HERMES_VPS_HOST
ssh-keyscan -H "${HERMES_VPS_HOST}" > hermes-vps-known-hosts
unset HERMES_VPS_HOST
chmod 600 hermes-vps-known-hosts
```

Для нестандартного порта добавьте `-p PORT`; запись должна соответствовать
значению `ansible_port` в Vault.
Для Tailscale SSH используйте его host key и адрес из Vault: ключ публичного
OpenSSH может отличаться.

## Проверка и загрузка

Проверьте файлы, не выводя секреты:

```bash
ansible-vault view \
  --vault-password-file hermes-vault-password \
  ../group_vars/all/vault.yml >/dev/null
test -s hermes-vps-known-hosts
```

В **Azure DevOps → Pipelines → Library → Secure files** загрузите два
рабочих файла:

- `vault.yml` — загрузите напрямую `../group_vars/all/vault.yml`
- `hermes-vps-known-hosts`

Не загружайте `.example` и `.pub`. Для каждого Secure File разрешите только
Hermes deployment pipeline и добавьте owner approval вместе с Branch control
для `refs/heads/main`.

## Secret variables вместо двух дополнительных файлов

В **Library → Variable groups** создайте группу `hermes-deploy-secrets`:

| Имя | Значение |
|---|---|
| `HERMES_VAULT_PASSWORD` | Пароль, которым зашифрован ваш `vault.yml` |
| `HERMES_TAILSCALE_AUTH_KEY` | Отдельный CI auth key из [инструкции Tailscale](../../../azure-ci/tailscale-deploy.md) |

Обе переменные пометьте **Keep this value secret**. Вводите сами значения
одной строкой, без обрамляющих кавычек или Base64. Для группы разрешите только
deployment pipeline; добавьте owner approval и Branch control `refs/heads/main`.
Не включайте Open access. Подключение группы уже задано в YAML, только на
stage `DeployProduction`, после фиксации SHA.

При ротации обновляйте значение в группе, не создавайте Secure File.
Pipeline создаёт временные файлы `0600` в каталоге `0700` на hosted agent,
передаёт потребителям пути и удаляет эти файлы отдельным шагом `always()`.
Ни пароль, ни CI-ключ не добавляйте в Git или `hermes_secret_env`.
