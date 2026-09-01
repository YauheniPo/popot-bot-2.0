# Данные и секреты для Hermes на VPS

Этот файл — инвентаризация, а не место для настоящих значений. Отмечайте
готовность флажками, но никогда не вставляйте сюда API keys, bot tokens,
пароли, OAuth JSON, recovery codes или private keys.

Основное место для environment-secrets — локальный зашифрованный
`ansible/group_vars/all/vault.yml`. При deploy Ansible создаёт на VPS
`/home/hermes/.hermes/.env` с правами `0600`. Настоящий `vault.yml`, пароль
Ansible Vault и `.env` исключены из Git.

Все шаги и команды для создания, шифрования, безопасного редактирования и
применения Vault находятся рядом с ним в
[`ansible/group_vars/all/VAULT.md`](ansible/group_vars/all/VAULT.md).

## Минимум для выбранной конфигурации

| Готово | Данные | Имя/формат | Где получить | Где хранить |
|---|---|---|---|---|
| [ ] | OpenRouter API key | `OPENROUTER_API_KEY` | [OpenRouter Keys](https://openrouter.ai/keys) | `hermes_secret_env` в Ansible Vault |
| [ ] | OpenRouter model policy | `vps_hermes.config.managed_overlay` | каталог моделей OpenRouter или `hermes model` | versioned `config/vps-defaults.yml`; не хранить в Vault |
| [ ] | Brave Search API key | `BRAVE_SEARCH_API_KEY` | кабинет Brave Search API | `hermes_secret_env` в Ansible Vault; нужен для Brave `web_search` |
| [ ] | Firecrawl API key | `FIRECRAWL_API_KEY` | Firecrawl dashboard | `hermes_secret_env` в Ansible Vault; извлечение HTML/PDF и browser-backed web |
| [ ] | Telegram bot token | `TELEGRAM_BOT_TOKEN` | создать бота у `@BotFather` | `hermes_secret_env` в Ansible Vault |
| [ ] | Разрешённый Telegram user ID | `TELEGRAM_ALLOWED_USERS="123..."` | ID личного аккаунта, не username | Vault/`.env`; это allowlist, не пароль |

В типовом Ansible-профиле основной provider — OpenRouter. Задайте
`OPENROUTER_API_KEY`; repository-owned non-secret policy задайте в
`vps_hermes.config.managed_overlay`. Выбранную через `/model_global` модель не
добавляйте в authoritative overlay, если она должна переживать deploy. Минимальный фрагмент и правила
замены placeholders находятся в
[`ansible/group_vars/all/VAULT.md`](ansible/group_vars/all/VAULT.md).

## Web search и браузер

Для Brave `web_search` задайте `BRAVE_SEARCH_API_KEY`: название backend в
Hermes — `brave-free`, но это не означает отсутствие ключа. Brave умеет только
искать; для извлечения страниц используйте браузер либо отдельно настройте
Nous Tool Gateway, Firecrawl, Tavily, Exa или Parallel. Если Brave key не
задан, Hermes не принуждается к неработающему backend и выбирает доступный
настроенный backend автоматически. При наличии ключа deploy явно выбирает
Brave для поиска, не меняя backend извлечения страниц.

Для нормального чтения страниц добавьте `FIRECRAWL_API_KEY`. Playbook выберет
`firecrawl` как `web.extract_backend`, сохранив Brave для поиска. Локальный
`agent-browser` с отдельным Chrome for Testing устанавливается и проверяется
при каждом deploy: он нужен для переходов по ссылкам, JavaScript-страниц и
форм. Firecrawl не заменяет вход в приватный Google Drive: для него нужен
Google OAuth.

## Telegram и alerts

Обязательный безопасный минимум:

```dotenv
TELEGRAM_BOT_TOKEN=<secret>
TELEGRAM_ALLOWED_USERS=<numeric-user-id>
```

Дополнительные данные задаются только при необходимости:

| Переменная | Когда нужна | Секрет |
|---|---|---|
| `TELEGRAM_HOME_CHANNEL` | доставка cron и alerts в конкретный chat/channel | Нет, но не публиковать без причины |
| `TELEGRAM_CRON_THREAD_ID` | отдельная forum topic для cron | Нет |
| `TELEGRAM_GROUP_ALLOWED_USERS` | разрешённые отправители только в группах | Нет |
| `TELEGRAM_GROUP_ALLOWED_CHATS` | разрешённые групповые chat IDs | Нет |
| `TELEGRAM_WEBHOOK_URL` | только если вместо polling используется публичный webhook | Нет |
| `TELEGRAM_WEBHOOK_SECRET` | обязательно вместе с webhook URL | Да; создать `openssl rand -hex 32` |

Не задавайте `TELEGRAM_ALLOW_ALL_USERS=true` агенту с terminal и доступом к
почте. Для обычного VPS оставьте Telegram polling: он не требует публичного
порта и webhook secret.

## GitHub и Git

Выберите один способ доступа к private repositories:

| Готово | Данные | Рекомендация | Хранение |
|---|---|---|---|
| [ ] | Fine-grained token `GITHUB_TOKEN` | Предпочтительно для управляемого VPS и bundled GitHub skills | Ansible Vault → managed `.env` `0600` |
| [ ] | GitHub OAuth через `gh auth login` | Для неуправляемой интерактивной установки | credential store пользователя `hermes` |
| [ ] | GitHub deploy key | Для одного repository и SSH clone/push | private key на VPS mode `0600`; public часть в GitHub |
| [ ] | `vps_github.git_identity` | Имя/email автора commit | `config/vps-defaults.yml`; не credential |

Минимальные fine-grained permissions для полного PR workflow: Metadata read,
Contents read/write, Pull requests read/write, Issues read/write и Actions
read. Workflows read/write требуется только для изменения workflow-файлов.
`GITHUB_TOKEN` также понимают bundled Hermes GitHub skills; managed `gh`
wrapper использует его без второго plaintext credential store. Для GitHub
Copilot inference существуют отдельные credentials. Не выдавайте classic или
organization-wide token, если достаточно selected repositories. Никогда не
используйте SSH private key входа на VPS как GitHub deploy key.

## Gmail, Calendar и Google Workspace

Google Workspace использует OAuth, а не простой API key. Нужно подготовить:

| Готово | Данные | Где появляются | Где хранить |
|---|---|---|---|
| [ ] | Google Cloud project ID/name | Google Cloud Console | Можно записать в operational docs |
| [ ] | Включённые Gmail, Calendar, Drive, Sheets, Docs и People APIs | API Library проекта | Не секрет |
| [ ] | OAuth consent screen и Google test user | Google Auth Platform | Не секрет, но содержит account email |
| [ ] | Desktop OAuth client JSON | скачивается из Google Cloud | `~/.hermes/google_client_secret.json`, mode `0600` |
| [ ] | OAuth refresh/access token | создаётся после browser consent | `~/.hermes/google_token.json`, mode `0600` |

Настройка запускается фразой Hermes: `Настрой Google Workspace для Gmail и
Calendar`. После OAuth проверьте, что оба JSON-файла принадлежат `hermes` и
имеют mode `0600`. Они не входят в `hermes_secret_env`, но входят в полный
локальный Hermes backup.

## VPS, SSH, Tailscale и Ansible

| Готово | Данные | Секрет | Правильное место |
|---|---|---|---|
| [ ] | Public IP или Tailscale IP/MagicDNS | Да | зашифрованный `ansible/group_vars/all/vault.yml` (`ansible_host`) |
| [ ] | SSH username | Да | зашифрованный `ansible/group_vars/all/vault.yml` (`ansible_user`) |
| [ ] | SSH public key | Нет | cloud-init/VPS `authorized_keys` |
| [ ] | SSH private key для входа на VPS | Да | Только управляющий компьютер; на VPS не копировать |
| [ ] | Пароль Ansible Vault | Да | Password manager, отдельно от encrypted `vault.yml` |
| [ ] | Tailscale auth key | Да | top-level `tailscale_auth_key` в Ansible Vault |
| [ ] | Tailscale ACL/tag | Нет | Tailscale admin console/IaC |

Для Tailscale лучше использовать одноразовый или короткоживущий tagged auth
key. После первого успешного входа проверьте Tailscale SSH из второй сессии и
только затем закрывайте public TCP/22.

## Backups

Hermes создаёт local backup автоматически и не требует отдельных backup
credentials. Следите за диском VPS и периодически проверяйте восстановление
архива.

## Дополнительные сервисы — только если включены

| Возможность | Credentials |
|---|---|
| Image generation | `FAL_KEY` или `KREA_API_KEY` |
| Premium TTS | `ELEVENLABS_API_KEY` |
| Speech-to-text | `GROQ_API_KEY`, если выбран Groq Whisper |
| External memory | один из `HONCHO_API_KEY`, `HINDSIGHT_API_KEY`, `MEM0_API_KEY`, `OPENVIKING_API_KEY` |
| Notion skill | `NOTION_API_KEY` |
| Linear skill | `LINEAR_API_KEY` |
| Airtable skill | `AIRTABLE_API_KEY` |
| Langfuse | `HERMES_LANGFUSE_PUBLIC_KEY`, `HERMES_LANGFUSE_SECRET_KEY`, optional base URL |
| MCP | отдельный минимально-привилегированный token для каждого реально включённого MCP |

Не добавляйте эти ключи заранее: неиспользуемая интеграция увеличивает расходы
и последствия утечки.

## Где должен находиться каждый тип данных

| Тип | Source of truth | Не хранить |
|---|---|---|
| Hermes API keys и Telegram token | encrypted Ansible `vault.yml` → Hermes `.env` `0600` | README, Git, shell history, Telegram |
| Google OAuth JSON/token | Hermes home `0600` + encrypted full backup | `.env`, Git, сообщения |
| GitHub PAT | encrypted Ansible Vault → managed Hermes `.env` `0600` | README, Git, shell history, отдельный plaintext `gh` store |
| VPS SSH private key | защищённый управляющий компьютер/password manager | VPS, Hermes home, Git |
| Ansible Vault password | отдельный password manager | repository, VPS, `vault.yml` |
| Provider/model policy и endpoints | `vps_hermes.config.managed_overlay` → `config.yaml`; live model через `/model_global` | Inline `api_key`, README, shell history |
| User IDs и public URLs | Public Ansible vars, inventory или Vault по policy | Не смешивать с secret values без необходимости |

Важно: `.env` защищает ключи от других непривилегированных Unix users, но сам
Hermes работает пользователем `hermes` и технически может прочитать собственный
`.env`. Для более строгой production-модели используйте Hermes egress proxy или
внешний secret manager с короткоживущими scoped credentials.

## Проверка без вывода секретов

```bash
# Hermes видит provider config и наличие credentials
sudo -u hermes -H /home/hermes/.local/bin/hermes config check
sudo -u hermes -H /home/hermes/.local/bin/hermes doctor
sudo -u hermes -H /home/hermes/.local/bin/hermes status

# Проверить только permissions, не содержимое
sudo stat -c '%a %U:%G %n' \
  /home/hermes/.hermes/.env \
  /home/hermes/.hermes/google_client_secret.json \
  /home/hermes/.hermes/google_token.json

# GitHub login и private-repository access без печати token
sudo -u hermes -H /home/hermes/.local/bin/gh auth status
sudo -u hermes -H /home/hermes/.local/bin/gh repo view YauheniPo/popot-bot-2.0
```

Не используйте `cat`, `env`, `set`, `printenv`, `hermes dump --show-keys` или
`ansible-vault view` в логируемом terminal/чате. Для проверки достаточно
статусов `set/not set`, permissions и тестового запроса без чувствительных
данных.

## Официальные справочники

- [Hermes environment variables](https://hermes-agent.nousresearch.com/docs/reference/environment-variables)
- [Hermes inference providers](https://hermes-agent.nousresearch.com/docs/integrations/providers)
- [Hermes Google Workspace skill](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/skills/google-workspace.md)
- [Ansible Vault](https://docs.ansible.com/projects/ansible/latest/vault_guide/index.html)
