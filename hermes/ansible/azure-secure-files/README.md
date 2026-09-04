# Azure Secure Files для Hermes deployment

Этот каталог содержит только безопасные примеры. Рабочие файлы без суффикса
`.example` игнорируются Git и загружаются в Azure DevOps вручную.

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

## Подготовка SSH key

Не копируйте placeholder private key из примера. Создайте отдельную пару без
интерактивной passphrase — Azure хранит private key как защищённый Secure File:

```bash
ssh-keygen -t ed25519 \
  -f hermes-vps-ssh-key \
  -C azure-hermes-deploy \
  -N ''
chmod 600 hermes-vps-ssh-key
```

Добавьте содержимое `hermes-vps-ssh-key.pub` в `authorized_keys` пользователя,
указанного как `ansible_user`. Public key в Azure загружать не нужно.

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

## Проверка и загрузка

Проверьте файлы, не выводя секреты:

```bash
ansible-vault view \
  --vault-password-file hermes-vault-password \
  ../group_vars/all/vault.yml >/dev/null
ssh-keygen -y -P '' -f hermes-vps-ssh-key >/dev/null
test -s hermes-vps-known-hosts
```

В **Azure DevOps → Pipelines → Library → Secure files** загрузите ровно четыре
рабочих файла:

- `vault.yml` — загрузите напрямую `../group_vars/all/vault.yml`
- `hermes-vault-password`
- `hermes-vps-ssh-key`
- `hermes-vps-known-hosts`

Не загружайте `.example` и `.pub`. Для каждого Secure File разрешите только
Hermes deployment pipeline и добавьте owner approval вместе с Branch control
для `refs/heads/main`.
