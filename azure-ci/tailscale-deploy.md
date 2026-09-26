# Hermes deploy из Azure DevOps через Tailscale: полная настройка

Pipeline: `azure-ci/azure-deploy-hermes.yml`. Эта настройка относится к
одноразовому Microsoft-hosted Ubuntu agent, а не к вашему Mac или VPS.
На постоянном self-hosted agent этот lifecycle использовать нельзя: cleanup
останавливает его `tailscaled`.

Эта инструкция покрывает весь путь: Tailscale → два Secure Files → две secret
variables → разрешения и approvals → выбор ветки/режима → первый deploy.
Локальный Ansible deploy не меняется. Создание ресурсов в ADO само по себе
не означает, что доступ к VPS уже проверен.

Полезные ссылки проекта:

- [Deployment pipeline №13](https://dev.azure.com/YauheniPo/popot-bot-2.0/_build?definitionId=13)
- [ADO Secure Files](https://dev.azure.com/YauheniPo/popot-bot-2.0/_library?itemType=SecureFiles)
- [Группа hermes-deploy-secrets](https://dev.azure.com/YauheniPo/popot-bot-2.0/_library?itemType=VariableGroups&view=VariableGroupView&variableGroupId=3)
- [Pipeline YAML](azure-deploy-hermes.yml)

Перед настройкой нужны права на Library/Environments/pipeline в ADO и на
tailnet policy/auth keys в Tailscale. Сохраняйте доступ к консоли VPS-провайдера
на случай ошибки в SSH policy. Секреты не отправляйте в чат и не добавляйте в Git.

## 1. Права в tailnet

Создайте `tag:hermes-deploy` для CI и `tag:hermes-vps` для целевого VPS.
В форме **Create tag** укажите имя `hermes-deploy` (итоговый тег —
`tag:hermes-deploy`), owner `autogroup:admin` и note
`Ephemeral Azure DevOps agents for Hermes deployment`. Аналогично создайте
`hermes-vps`. Тег `hermes-deploy` не назначайте самому VPS.
Владельцами тегов назначьте администраторов, не CI. Ниже **фрагмент** политики:
объедините его с существующей, не заменяйте весь policy file.

```json
{
  "tagOwners": {
    "tag:hermes-deploy": ["autogroup:admin"],
    "tag:hermes-vps": ["autogroup:admin"]
  },
  "grants": [
    {"src": ["tag:hermes-deploy"], "dst": ["tag:hermes-vps"], "ip": ["tcp:22"]}
  ],
  "ssh": [
    {"action": "accept", "src": ["tag:hermes-deploy"], "dst": ["tag:hermes-vps"], "users": ["root"]}
  ]
}
```

`root` должен соответствовать `ansible_user` из Vault. Для другого пользователя
укажите именно его и обеспечьте неинтерактивный `sudo`; не используйте `users: ["*"]`.
Тег назначения присвойте только VPS, который должен деплоиться этим pipeline.

**Перед назначением тега VPS сохраните личный доступ владельца.** Tagged node
перестаёт быть user-owned: правило `autogroup:self` уже не покрывает этот VPS.
Сначала добавьте явные сетевые правила для вашей пользовательской identity к
`tag:hermes-vps` (SSH и используемые UI-порты), а также личное SSH-правило `check`
к этому тегу. Проверьте политику, держите открытыми SSH и консоль VPS-провайдера,
затем назначьте тег и проверьте **новое** личное подключение. Существующий
личный browser check отключать не требуется.

Сетевые grants/ACL складываются: старое широкое правило `* → *` может давать CI
больше доступа, чем приведённый grant. Проверьте итоговую политику, а не только
новый фрагмент. Для CI не должно быть совпадающего SSH `check`: он имеет
приоритет над `accept` и снова потребует браузер.

Основание: [Tailscale SSH](https://tailscale.com/docs/features/tailscale-ssh),
[tag identities](https://tailscale.com/docs/features/tags).

## 2. Отдельный auth key для CI

Откройте [Tailscale → Settings → Keys](https://login.tailscale.com/admin/settings/keys)
и выберите **Generate auth key** (не API token). Description: `ado-hermes-deploy`.

- Reusable: включено — каждый запуск регистрирует новый node.
- Ephemeral: включено — node не остаётся постоянным устройством.
- Pre-approved: включено, если в tailnet требуется device approval.
- Tags: только `tag:hermes-deploy`.
- Expiry: например 30 дней; запланируйте ротацию до истечения.

Нажмите **Generate key** и сохраните полученное значение `tskey-auth-…` в ADO,
как описано ниже. Это ключ регистрации временных CI-узлов, не ключ самого VPS.

При включённом Tailnet Lock используйте предусмотренное им предварительное
подписание ключа/узла; pipeline не отключает эту защиту. Ephemeral и reusable
задаются при создании ключа: по его строке pipeline не может проверить эти флаги.

В **Azure → Pipelines → Library → Variable groups** откройте группу
`hermes-deploy-secrets` (создайте, если её ещё нет):

| Variable | Значение | Secret |
|---|---|---|
| `HERMES_TAILSCALE_AUTH_KEY` | Полученный CI auth key `tskey-auth-…` | Да |
| `HERMES_VAULT_PASSWORD` | Пароль текущего Ansible Vault, который вводится при локальном `--ask-vault-pass` | Да |

Для обеих включите **Keep this value secret**, введите значения и нажмите **Save**.
Наличие переменных с пустыми значениями недостаточно для deploy.
Значения задавайте без обрамляющих кавычек и Base64. Не используйте общий ключ
VPS `tailscale_auth_key` и не добавляйте CI-ключ в Hermes `.env`, Git или чат.
Для группы разрешите только deployment pipeline, добавьте owner approval и
Branch control `refs/heads/*`, как для двух Secure Files. YAML подключает
группу только на stage `DeployProduction`, после фиксации SHA исходников.
При ротации обновляйте secret variable в группе, не загружайте новый файл.

Ключ передаётся CLI через `--auth-key=file:...`, не как значение в argv.
Pipeline создаёт временные файлы секрета с правами `0600` в каталоге `0700`
и удаляет их отдельным шагом `always()`. Значения передаются только через
окружение шага подготовки, без вывода в лог. См.
[защищённые variable groups](https://learn.microsoft.com/en-us/azure/devops/pipelines/library/variable-groups?view=azure-devops),
[auth keys](https://tailscale.com/docs/features/access-control/auth-keys)
и [tailscale up](https://tailscale.com/docs/reference/tailscale-cli/up).

## 3. Адрес и SSH

В Vault `ansible_host` должен указывать на Tailscale IP VPS или полный MagicDNS
hostname; `ansible_user` — на пользователя, разрешённого SSH policy. Для
Tailscale SSH используется порт 22. В `hermes-vps-known-hosts` нужна проверенная
запись ключа **Tailscale SSH** для того же адреса, а не ключ публичного OpenSSH.

На VPS должен быть включён **Tailscale SSH**, а не просто подключение к tailnet.
CI auth key только регистрирует временный узел; вход на VPS отдельно разрешается
SSH policy с `action: accept` для `tag:hermes-deploy` и нужного `ansible_user`.
Private SSH key и `.pub` не нужны: Tailscale SSH использует identity узла и policy
вместо `authorized_keys`. Pipeline не поддерживает обычный OpenSSH через tailnet
и не переключается на ключи/пароли автоматически. Локальный deploy не меняется.
Pipeline не запускает `ssh-keyscan` и не отключает проверку host key.

## 4. Подготовка и загрузка двух Secure Files

В **Pipelines → Library → Secure files** нужны только эти два имени
для данного pipeline (другие файлы проекта удалять не нужно):

| Имя в ADO | Что загружать | Что не подходит |
|---|---|---|
| `vault.yml` | Рабочий зашифрованный `hermes/ansible/group_vars/all/vault.yml` | Расшифрованный YAML или `.example` |
| `hermes-vps-known-hosts` | Проверенная запись публичного SSH host key целевого VPS | Приватный ключ или ключ другого SSH-сервиса |

### Vault

Используйте существующий рабочий Vault — не заменяйте его примером. Первая
строка зашифрованного файла начинается с `$ANSIBLE_VAULT;`. Внутри должны быть
актуальные `ansible_host`, `ansible_user` и необходимые секреты Hermes.
Проверить расшифровку, не выводя содержимое, можно из корня репозитория:

```bash
ansible-vault view hermes/ansible/group_vars/all/vault.yml >/dev/null
```

Пароль вводится интерактивно; тот же пароль задайте в `HERMES_VAULT_PASSWORD`.
`GITHUB_TOKEN` остаётся внутри `hermes_secret_env`, дублировать его для ADO
deployment не нужно: checkout использует GitHub connection pipeline.
Если VPS уже подключён к Tailscale, top-level `tailscale_auth_key` в Vault
не нужен. CI-ключ хранится только в группе ADO.

### Переход со старой схемы

После публикации обновлённого pipeline в `main` Secure File `hermes-vps-ssh-key`
больше не используется. Если он уже загружен, оставлять его для этого deploy
не требуется; удаляйте только после проверки, что другие pipelines не зависят
от него. Локальные `azure-ci/hermes-vps-ssh-key` и `.pub` также не нужны для
Tailscale SSH; они остаются в `.gitignore` и не удаляются автоматически.
Ничего из этой пары на VPS устанавливать не нужно. `hermes-vps-known-hosts`
сохраните: это публичный ключ сервера для проверки подлинности, а не ключ входа.

### SSH known_hosts

Это публичный ключ **сервера**, не ключ из предыдущего пункта. Возьмите запись
из локального `~/.ssh/known_hosts` после доверенного подключения к тому же
адресу, который указан в `ansible_host`. Найти её можно через `ssh-keygen -F`
(он работает и с хешированными именами). Для вашего текущего IP:

```bash
ssh-keygen -F 100.83.123.65 -f ~/.ssh/known_hosts
```

Сохраните найденную строку с ключом в `azure-ci/hermes-vps-known-hosts`.
Этот локальный путь исключён корневым `.gitignore`; на другом checkout файл
сам не появится. Если файл уже подготовлен, не перезаписывайте его вслепую.
Не загружайте весь личный `known_hosts` с записями других серверов.

Проверьте файл и fingerprint:

```bash
ssh-keygen -lf azure-ci/hermes-vps-known-hosts
git check-ignore -v azure-ci/hermes-vps-known-hosts
```

Адрес в записи должен совпадать с `ansible_host`: запись только для IP не
покрывает MagicDNS hostname. Для нестандартного порта нужна запись
`[host]:port`. Хешированный адрес допустим. Если сохранённой записи нет,
получите ключ через доверенное подключение и проверьте fingerprint независимым
каналом; один `ssh-keyscan` подлинность сервера не подтверждает.
Не берите `/etc/ssh/ssh_host_*.pub` для Tailscale SSH — это ключи другого сервиса.

Загрузите файл с точным именем **`hermes-vps-known-hosts`**, без `.txt`.
Если IDEA скрывает ignored-файл, откройте каталог через Finder или диалог
загрузки ADO; удалять правило `.gitignore` ради отображения не нужно.

Пароль Vault и Tailscale auth key **не загружаются как Secure Files**.
Шаблоны и дополнительные команды подготовки:
[azure-secure-files/README.md](../hermes/ansible/azure-secure-files/README.md).

## 5. Pipeline permissions, approvals и Environment

Для **каждого из двух Secure Files** и группы **`hermes-deploy-secrets`**:

1. Откройте **Pipeline permissions** → разрешите только `popot-bot-2.0 Deploy`
   (pipeline №13). Не включайте **Open access**.
2. В **Approvals and checks** добавьте approval владельца.
3. Добавьте **Branch control** с разрешённым шаблоном `refs/heads/*`.

В **Pipelines → Environments** создайте или откройте `hermes-vps`:

- Разрешите использование только deployment pipeline.
- Добавьте owner approval и Branch control `refs/heads/*`.
- Добавьте **Exclusive lock**; YAML использует `lockBehavior: sequential`,
  чтобы deploy не выполнялись одновременно.

У pipeline оставьте **Queue builds** только владельцу. Ограничьте доступ к
логам и артефактам доверенными пользователями. Checks и разрешения задаются
в ADO UI: одного YAML недостаточно.

**Branch control проверяет все связанные repository resources**, включая
`deploySource`, а не только ветку определения pipeline. Allowlist
`refs/heads/*` разрешает любой branch ref репозитория, но не теги. Сохраните
проверку защиты ветки, owner approval и остальные checks. Такое расширение
allowlist разрешает выкатывать в production любой branch ref, включая ещё не
проверенный; до сохранения allowlist в Azure владелец должен явно принять этот
риск, а каждый run — одобрить по конкретному SHA из Summary. Настройте защиту для всех deployable
веток, если Branch control требует, чтобы source branch была protected.
Смена Branch control с `refs/heads/main` на `refs/heads/*` — отдельное изменение
политики доступа: оформите для него change request и получите явное одобрение
владельца, записанное в change request/PR, до merge и сохранения allowlist в
Azure. Оставляйте `refs/heads/main`, пока sign-off не записан; при проблеме
верните allowlist к `refs/heads/main` и повторно проверьте Environment checks.
См. [Branch control в Azure](https://learn.microsoft.com/en-us/azure/devops/pipelines/process/approvals?view=azure-devops#branch-control).
Код выбранной ветки получит production credentials после checks и approval —
одобряйте только проверенный SHA из Summary, а не просто знакомое имя ветки.

## 6. Выбор ветки, режима и запуск

Используйте существующий pipeline №13. Если создаёте новый, выберите этот
GitHub-репозиторий и YAML `/azure-ci/azure-deploy-hermes.yml` из `main`.
Обновлённые YAML и helper `azure-ci/scripts/prepare-hermes-deploy.py` должны
быть опубликованы в `main` до запуска: локальные правки ADO не видит.

Repository resource `deploySource` использует GitHub service connection
`github.com_YauheniPo`. В **Project settings → Service connections** разрешите
этому pipeline использование подключения, если оно ещё не авторизовано;
**Open access** не требуется.

В **Run pipeline** оставьте версию самого pipeline на `main`. В параметре
**Branch to deploy** укажите короткое имя ветки, например
`feat/hermes-ai-digest-cron`; YAML добавит `refs/heads/` и передаст значение
через compile-time выражение в `resources.repositories.deploySource.ref`.
Runtime-переопределение
`resources.repositories.deploySource.refName` для этого не используется.

| Поле | Что выбрать |
|---|---|
| Branch/tag определения pipeline | `main` |
| Checkout `deploySource` | Ветка из `Branch to deploy`; по умолчанию `main` |
| Ansible deployment mode (`deployMode`) | `full`, `config-only` или `runtime-only` |

Отдельного чекбокса подтверждения production нет: ручной **Run pipeline**
запускает проверку запроса. Настроенные approvals и проверки доступа в Azure
по-прежнему обязательны перед deployment; автоматические CI/PR triggers выключены.

- `full` — установка/обновление upstream Hermes и deployment.
- `config-only` — конфигурация без обновления upstream Hermes.
- `runtime-only` — изменения runtime/services в пределах этого режима.

Режимы не обходят проверки безопасности и backup. Границы режимов описаны в
[Hermes README](../hermes/README.md#режимы-deploy).
Выбирайте ветку, а не тег: helper принимает только ref вида `refs/heads/...`
и сверяет checkout с commit SHA, переданным Azure для выбранного resource.
Смена репозитория на fork не поддерживается. Выбранная ветка должна содержать
Hermes playbook.

Дождитесь завершения `ValidateRequest`, откройте Summary с веткой, SHA и
режимом и только затем подтвердите approvals `DeployProduction`.
Изменение ветки во время ожидания не меняет зафиксированный архив. Повтор
deploy job использует прежний архив; для нового SHA запустите новый pipeline.

## 7. Что происходит при запуске

1. Pipeline проверяет `main` для своего определения и отдельно скачивает `self`
   и выбранную версию `deploySource` без сохранения credentials. Helper из
   доверенного checkout `main` сверяет SHA исходников с версией resource Azure
   и архивирует именно этот commit до approvals, не вычисляя вершину ветки заново.
2. После approvals environment, Secure Files и группы переменных подготавливает
   временные файлы секретов, устанавливает Tailscale из официального signed apt repo
   и подключает CI node `ado-hermes-<build>-<attempt>` без входящего SSH.
3. Join ограничен 75 секундами (+ до 5 секунд принудительного завершения).
4. До deployment tasks выполняет отдельную SSH/sudo-проверку `raw /bin/true`
   по тому же inventory/Vault с общим лимитом 75 секунд (+ до 5 на завершение).
   Отказ ACL, browser check, host-key mismatch и недоступный sudo останавливают
   job с диагностикой. Сам deploy не запускается.
5. Выполняет Ansible выбранного режима; проверки backup сохраняются.
6. Отдельными шагами `always()` удаляет временные файлы секретов, вызывает logout
   и останавливает Tailscale, включая сбой/отмену.
   При аварийной потере VM cleanup не гарантирован: используется одноразовый
   hosted agent, а ephemeral node удалится
   после отключения. При нормальном logout он удаляется сразу.

Первый запуск подтверждает реальную связность; локальные тесты не проверяют
ваши ACL, ключи и VPS. Убедитесь, что preflight проходит без браузера, а после
успеха/отмены node исчезает из Machines. Интерактивный личный доступ проверьте
отдельно. [Жизненный цикл ephemeral nodes](https://tailscale.com/docs/features/ephemeral-nodes).

## 8. Чеклист первого запуска и диагностика

- [ ] Изменения pipeline опубликованы в `main`; выбрана правильная source branch.
- [ ] Два Secure Files загружены под точными именами; обе secret variables заполнены.
- [ ] Для файлов, группы и environment настроены permissions и checks.
- [ ] CI auth key reusable/ephemeral, не истёк и имеет только `tag:hermes-deploy`.
- [ ] На VPS включён Tailscale SSH, согласована SSH policy; личный доступ после тегирования проверен.
- [ ] Summary содержит ожидаемые SHA и режим; approvals подтверждены.
- [ ] `JoinTailnet` и `ProbeDeploySsh` прошли без browser login.
- [ ] Ansible завершился без failed/unreachable; проверьте итоговые service checks.
- [ ] Временный CI node исчез после cleanup; личное подключение к VPS работает.

| Ошибка/симптом | Что проверить |
|---|---|
| Resource not found / not authorized до deploy | Точные имена Secure Files/группы/environment и Pipeline permissions |
| Stage ждёт approval | Approvals and checks всех защищённых ресурсов stage; это не зависание SSH |
| Ошибка подготовки secret variables | Обе переменные заполнены, без переносов строк; CI auth key имеет формат `tskey-auth-…` |
| Vault decryption failed | Пароль соответствует загруженному зашифрованному `vault.yml` |
| Pipeline требует `hermes-vps-ssh-key` | Запускается старый YAML: опубликуйте обновление в `main` и создайте новый run |
| Tailscale join failed / timeout | Срок ключа, tag owner, device approval/Tailnet Lock и исходящая сеть agent |
| SSH/sudo preflight failed / timeout | Tailscale SSH включён на VPS; `ansible_host/user`, TCP/22, SSH `accept` без совпадающего CI `check`, доступный sudo |
| Host key verification failed | Совпадение адреса/порта и актуального Tailscale SSH host key; не отключайте StrictHostKeyChecking |
| Mandatory backup failed | Первый реальный FAILED выше итогового Abort; не отключайте backup ради продолжения |

Не публикуйте Vault, ключи или полный дамп environment при диагностике.
После ротации пароля обновите `HERMES_VAULT_PASSWORD` и соответствующий
зашифрованный Vault; после ротации CI auth key — только его secret variable.
Для нового host key сначала повторно проверьте подлинность сервера.
