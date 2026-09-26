# AI/IT digest для Hermes cron

`ai_digest` — skill для сохранённой cron-задачи Hermes. Задача, созданная через
бота, задаёт имя, расписание и `deliver` в Telegram-топик; skill задаёт
процедуру подготовки результата. Ту же задачу можно запустить вручную командой
`/cron run <job_id>`. Отдельная команда `/ai_digest` и отдельный сервис не нужны.

## Режимы и результат

`daily` — выпуск релизов, новостей, моделей и практических заметок за 24 часа
(до 5 материалов). `weekly` — выпуск исследований, подкастов и бенчмарков за
168 часов (до 7 материалов). Недельный отбор включает по материалу из каждой
из трёх категорий, если в ней есть свежие данные. Режим указывается в prompt
задачи: `daily` по умолчанию или `--mode weekly`. Имя, время и адрес доставки
остаются в созданной через бота cron-задаче, не в `sources.json`.

1. `scripts/collect_news.py` читает ограниченный объём данных из источников,
   фильтрует по времени, объединяет дубли, ранжирует и собирает проверяемый
   JSON. Для выбранных обычных статей он пытается получить текст, для HN/Reddit
   — верхние комментарии. Частичные сбои попадают в `source_issues`.
2. Агент пишет по-русски объяснения для Junior, Senior и Manager. Факты
   ограничены собранными данными и ссылками; интерпретации помечаются. Выпуск
   показывает область доказательства: статью, аннотацию, описание подкаста,
   метаданные модели или результат бенчмарка. Описание подкаста не считается
   прослушанной записью, наблюдение Trending — датой релиза.
3. `scripts/finalize_digest.py` проверяет структуру и ссылки, сохраняет новый
   Markdown-файл. Агент возвращает сводку и `MEDIA:<путь>`; cron отправляет
   ответ и вложение в свой `deliver` и записывает историю доставки. Каждая
   сборка получает новый `run_id`; архив называется `digest-<run_id>.md`, так
   что повторный запуск создаёт отдельный файл. Существующий архив не заменяется.

JSON и журнал сборщика находятся в `$AI_DIGEST_STATE_DIR` (на VPS по умолчанию
`~/.hermes/ops/news/`), архив — в `$AI_DIGEST_OUTPUT_DIR` (на VPS
`~/workspace/digests/`). Историю cron можно смотреть через `hermes cron runs
<job_id>` и `hermes cron doctor`. Содержимое источников считается недоверенным
вводом; сетевые URL проверяются на публичный адрес, соединение закрепляется за
проверенным IP против DNS rebinding: `collect_news.py` получает адреса через
`_public_addresses()`, и каждый HTTP(S) handler подключается к одному из тех же
проверенных `sockaddr`; повторного DNS lookup при connect нет. Для HTTPS TLS
проверка сохраняет исходное имя хоста. Сборщик подключается напрямую: `http_fetch`
использует `ProxyHandler({})`, поэтому `HTTP_PROXY`/`HTTPS_PROXY` не применяются.
IP-адреса берутся из DNS системы и проверяются на публичную маршрутизируемость;
это предотвращает подключение к private/loopback адресам и DNS rebinding между
проверкой и connect, но не является статическим allowlist источников или защитой
от скомпрометированного системного DNS.
Реализация находится в `scripts/collect_news.py`; тест
`test_http_fetch_connects_to_the_validated_address_without_resolving_again` в
`scripts/test_collect_news.py` проверяет подключение по закреплённому адресу без
повторного DNS lookup.

## Покрытие источников

Ежедневно используются исходные IT-ленты, OpenAI News, Google AI, DeepMind,
Hugging Face Papers/Trending, блог Simon Willison и поиск по Anthropic News,
Meta AI Blog, TLDR AI и X-публикациям Karpathy, Simon Willison и swyx. Поиск
X, LinkedIn и названных сайтов зависит от уже установленного SearXNG и наличия
даты в его результате; пропуски отображаются в отчёте.

Для AI digest задайте `vps_web.searxng_url` как доступный извне HTTP(S) endpoint.
Коллектор перед запросом проверяет, что URL разрешается только в публичные IP;
локальные и приватные адреса отклоняются. Эта проверка выполняется при запуске,
поэтому локальный `127.0.0.1` endpoint, подходящий для Hermes `web_search`, не
подходит для digest.

Недельный выпуск использует RSS Import AI, Interconnects, Latent Space и
Dwarkesh Podcast, OpenAI/DeepMind и Hugging Face Papers, поиск по The Batch и
alphaXiv, а также опубликованный JSON SWE-bench Verified. LMArena, Artificial
Analysis и Hugging Face Open LLM Leaderboard просматриваются через датированные
результаты поиска: живые баллы и их изменения этим способом не сравниваются.
SWE-bench даёт новые датированные результаты отправленных агентов, а не
изменение позиции без предыдущего снимка. Для подкастов анализируется описание
в RSS; расшифровка аудио не выполняется.

Правьте `scripts/sources.json` в репозитории и применяйте обычный Ansible
deploy. Поддержаны RSS/Atom, Hacker News, Reddit, arXiv, GitHub Trending,
открытые Hugging Face API, опубликованный JSON SWE-bench и SearXNG. Пустой
или недоступный источник отображается в выпуске. Новые ключи, платные API,
Python-зависимости и внешние сервисы не требуются.

Параметры `window_hours` (1–720), `limit` (1–20) и `topic` можно уточнить в
prompt сохранённой задачи. `request_timeout_s`, размеры ответов и текстов
ограничивают сетевую работу и расход токенов. GitHub Trending даёт дату
наблюдения, а не публикации репозитория; fallback через GitHub Search
помечается как деградация.

## Локальная проверка без Telegram

```bash
python3 -m unittest hermes/skills/ai_digest/scripts/test_collect_news.py hermes/skills/ai_digest/scripts/test_finalize_digest.py
python3 hermes/skills/ai_digest/scripts/collect_news.py --state-dir /tmp/ai-digest-check --limit 2
python3 hermes/skills/ai_digest/scripts/collect_news.py --state-dir /tmp/ai-digest-check --mode weekly --limit 3
```

Последние две команды делают реальные сетевые запросы и печатают путь к JSON.
Код `3` означает отсутствие пригодных материалов; `2` — ошибку конфигурации
или сохранения. Отправка в Telegram проверяется только запуском на VPS после
deploy; локальные тесты не подтверждают доставку.
