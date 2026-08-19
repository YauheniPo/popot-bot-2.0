# Ansible Vault: секреты Hermes

Этот каталог содержит единственный рабочий файл секретов для VPS:
`vault.yml`. Он не попадает в Git и должен быть зашифрован Ansible Vault.
Шаблон структуры и названий полей находится в
[`vault.yml.example`](vault.yml.example).

Пароль Ansible Vault — отдельный секрет. Он **не совпадает** с root-паролем
VPS, паролем панели провайдера или паролем SSH. Если этот пароль утрачен,
расшифровать существующий `vault.yml` невозможно: создайте новый Vault и
заново внесите ключи.

## Первое создание

Выполните на Mac из корня репозитория:

```bash
cd hermes/ansible
cp group_vars/all/vault.yml.example group_vars/all/vault.yml
chmod 600 group_vars/all/vault.yml
ansible-vault encrypt group_vars/all/vault.yml
```

Сразу сохраните пароль Vault в password manager. Проверить, что файл
зашифрован, можно без чтения его содержимого:

```bash
head -n 1 group_vars/all/vault.yml
```

Ожидаемый результат начинается с `$ANSIBLE_VAULT;`.

## Безопасное редактирование

Редактируйте только через `ansible-vault edit`: команда создаёт временный
файл, а после сохранения автоматически записывает `vault.yml` обратно в
зашифрованном виде.

```bash
cd hermes/ansible
EDITOR=nano ansible-vault edit group_vars/all/vault.yml
```

На macOS можно заменить `nano` на предпочитаемый текстовый редактор. Внесите
значения в `hermes_secret_env` и при необходимости в `hermes_llm_config`, затем
сохраните файл и выйдите из редактора.

Не используйте для обычного редактирования `ansible-vault decrypt`: он оставит
секреты в открытом виде на диске. Если файл уже был расшифрован осознанно,
зашифруйте его до следующего deploy:

```bash
cd hermes/ansible
ansible-vault encrypt group_vars/all/vault.yml
```

Никогда не отправляйте содержимое `vault.yml`, вывод `ansible-vault view`,
Vault password или API keys в чат, Git, issue либо shell history.

## Типичная конфигурация

Минимальный набор для LLM — `OPENROUTER_API_KEY` в `hermes_secret_env` и
модель в `hermes_llm_config`. Дополнительно можно добавить:

- `FIRECRAWL_API_KEY` — чтение HTML/PDF и веб-страниц;
- `BRAVE_SEARCH_API_KEY` — поиск через Brave;
- `TELEGRAM_BOT_TOKEN` вместе с `TELEGRAM_ALLOWED_USERS` — запуск Telegram
  gateway;
- `hermes_grafana_admin_password` — пароль администратора Grafana;
- `tailscale_auth_key` — временный ключ подключения Tailscale.

При применении playbook Hermes получает в своём `workspace/AGENTS.md` только
отсортированные **названия** ключей из `hermes_secret_env`. Это помогает ему
выбрать нужный инструмент (например, GitHub или Firecrawl), но значения не
попадают в этот документ: они остаются в `.hermes/.env` с правами `0600`.
Название ключа не подтверждает, что токен ещё действителен или имеет нужные
права — Hermes должен проверить это безопасной операцией, не читая и не
печатая `.env`.

Это правило поведения, а не техническая изоляция: terminal Hermes работает
тем же Unix-пользователем `hermes`, поэтому при нарушении инструкции он может
прочитать собственный `.env`. Ограничение `0600` защищает ключи только от
других непривилегированных пользователей VPS. Для строгой границы нужны
отдельный runtime без terminal/sudo и внешний secret manager или egress proxy;
подробное ограничение модели описано в
[`../../../SECRETS-CHECKLIST.md`](../../../SECRETS-CHECKLIST.md).

Не добавляйте `AGENT_BROWSER_ARGS`: безопасное для данного Ubuntu VPS значение
управляется playbook автоматически и не является секретом.

Минимальный пример для редактирования (замените placeholders внутри
`ansible-vault edit`, но не публикуйте реальные значения):

```yaml
hermes_manage_secret_env: true
hermes_secret_env:
  OPENROUTER_API_KEY: "replace-inside-ansible-vault"
  FIRECRAWL_API_KEY: "replace-inside-ansible-vault"
  TELEGRAM_BOT_TOKEN: "replace-inside-ansible-vault"
  TELEGRAM_ALLOWED_USERS: "123456789"

hermes_llm_config:
  model:
    provider: "openrouter"
    default: "openrouter/free"
    max_tokens: 4096
```

## Применить изменения

После каждого изменения Vault запустите playbook из корня репозитория:

```bash
ansible-playbook -i hermes/ansible/inventory.ini \
  hermes/ansible/playbook.yml \
  --ask-vault-pass --ask-pass
```

Сначала будет запрошен текущий SSH/root-пароль VPS, затем пароль Ansible Vault.
При `hermes_manage_secret_env: true` файл
`/home/hermes/.hermes/.env` на VPS полностью управляется Vault и его ручные
изменения будут перезаписаны при следующем запуске playbook.
