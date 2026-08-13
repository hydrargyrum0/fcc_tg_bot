# Managed IP Pool — Design Spec

## Goal

Добавить управляемую базу IP-адресов: фоновый скоринг адресов из пользовательских наборов через Pingachock (ping + TLS + VLESS speedtest), с которой работает автоматизация замены адресов в хостах.

## Architecture

Два независимых слоя:

1. **Managed Pool** — фоновый скорер: непрерывно поддерживает оценённый пул IP из пользовательских наборов.
2. **Availability Monitor** — использует пул как источник замен вместо поиска в реальном времени через сырые наборы.

Оба слоя существуют параллельно: группы автоматизации могут работать в старом режиме (сырые наборы) или в новом (управляемый пул).

## Tech Stack

Python 3.12, aiogram v3, SQLAlchemy async, PostgreSQL, asyncio, Pingachock API v1.

---

## Data Layer

### Новая таблица `managed_pools`

| Поле | Тип | Описание |
|------|-----|----------|
| `id` | int PK | |
| `org_id` | int FK → organizations CASCADE | |
| `name` | str(200) | Название пула |
| `host_tag` | str(200) | Тег хостов Remnawave для VLESS-конфига |
| `ip_set_ids` | JSON (list[int]) | IpSet ID из которых тянутся IP |
| `score_threshold` | float | Минимальный балл для `is_approved` (default 60.0) |
| `check_interval_minutes` | int | Как часто пересканировать весь пул |
| `last_scanned_at` | datetime nullable | Когда последний раз прогоняли |
| `enabled` | bool | default True |
| `created_at` | datetime | server_default=func.now() |

### Новая таблица `managed_ips`

| Поле | Тип | Описание |
|------|-----|----------|
| `id` | int PK | |
| `pool_id` | int FK → managed_pools CASCADE | |
| `ip` | str(45) | Bare IP address |
| `score` | float | 0–100, итоговая оценка |
| `is_approved` | bool | score ≥ pool.score_threshold |
| `ping_rtt_ms` | float nullable | |
| `ping_loss_pct` | float nullable | 0.0–1.0 |
| `tls_ok` | bool nullable | |
| `tls_handshake_ms` | float nullable | |
| `vless_ok` | bool nullable | |
| `vless_speed_mbps` | float nullable | |
| `last_checked_at` | datetime nullable | None = ещё не проверялся |
| `created_at` | datetime | server_default=func.now() |

Unique constraint: `(pool_id, ip)`.

### Изменения в `automation_groups`

Добавить nullable колонку:
- `managed_pool_id` — int FK → managed_pools SET NULL

Семантика:
- `managed_pool_id IS NOT NULL` → новый режим: замены берутся из управляемого пула
- `managed_pool_id IS NULL` → старый режим: `ip_set_ids` + поиск через Pingachock в реальном времени

---

## IP Normalization

Новая функция `normalize_addresses(text: str) -> list[str]` в `services/ip_check_service.py`:

1. Regex-ом извлекает из произвольного текста все IPv4-паттерны (`\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b`) и IPv6
2. Для CIDR-нотации — раскрывает через `ipaddress.ip_network` (как в `expand_addresses`)
3. Дедуплицирует, возвращает `list[str]` чистых bare IP

Применяется:
- При создании/редактировании `IpSet` — вместо текущего хранения текста как есть
- В скорере при сборке пула из наборов

---

## Scoring Engine

### Файл `services/ip_pool_scorer.py`

Фоновая задача `run_ip_pool_scorer(bot, session_factory)` — запускается в `main.py` рядом с `run_availability_monitor`.

Цикл: каждые 60 секунд проверяет пулы у которых подошёл `check_interval`. Для каждого: `asyncio.create_task(_score_pool(...))`.

### `_score_pool(pool_id, session_factory)`:

1. Загружает пул и его наборы из БД
2. Разворачивает все наборы через `normalize_addresses` → дедуплицирует
3. Удаляет из `managed_ips` IP которых больше нет ни в одном наборе
4. Вставляет новые IP с `last_checked_at = None`
5. Берёт IP требующих проверки (новые или просроченные)
6. Для каждого IP запускает три Pingachock-чека:
   - `ping` — `distributed_ping_check([ip])` → rtt + loss
   - `tls` — `create_check(type="tls", target=ip, params={"port": 443, "sni": "kremnezar.online", "count": 3})`
   - `vless` — получает конфиг у сервисного юзера (Telegram ID 9636) для `pool.host_tag`, подставляет `ip`, отправляет в Pingachock
7. Считает score, обновляет запись `managed_ips`, выставляет `is_approved`
8. Обновляет `pool.last_scanned_at`

### Формула оценки (0–100)

```python
if not ping_reachable:
    score = 0.0

elif not vless_ok:
    # VPN не работает — частичный балл только за сетевое качество
    ping_score = _ping_score(rtt_ms, loss_pct)   # 0–15
    tls_score  = _tls_score(tls_ms, tls_ok)      # 0–5
    score = ping_score + tls_score  # max 20

else:
    ping_score  = _ping_score(rtt_ms, loss_pct)   # 0–30
    tls_score   = _tls_score(tls_ms, tls_ok)      # 0–20
    vless_score = _vless_score(speed_mbps)         # 0–50
    score = ping_score + tls_score + vless_score   # max 100

is_approved = score >= pool.score_threshold
```

**`_ping_score(rtt_ms, loss_pct) → 0–30`:**
- loss > 50% → 0
- loss > 25% → 5
- rtt > 300ms → 5
- rtt > 150ms → 15
- rtt > 80ms → 22
- rtt ≤ 80ms, loss = 0% → 30
- потери снижают: -1 за каждые 5%

**`_tls_score(tls_ms, tls_ok) → 0–20`:**
- tls_ok = False → 0
- tls_ms > 1000ms → 5
- tls_ms > 500ms → 10
- tls_ms > 200ms → 15
- tls_ms ≤ 200ms → 20

**`_vless_score(speed_mbps) → 0–50`:**
- speed < 1 Мбит/с → 5
- speed < 5 → 15
- speed < 15 → 30
- speed < 30 → 40
- speed ≥ 30 → 50

### VLESS-конфиг из Remnawave

Новый метод в `services/remnawave_api_service.py`:
```python
async def get_vless_config_for_tag(
    panel_url: str, api_token: str,
    service_tg_id: int,   # 9636
    host_tag: str,
) -> dict | None:
    """Fetch client VLESS config for service user, filtered to host_tag."""
```

Логика:
1. Найти Remnawave-пользователя у которого `telegramId == 9636`
2. Получить его subscription/конфиг
3. Из конфига взять outbound для хоста с тегом `host_tag`
4. Вернуть JSON готовый к отправке в Pingachock (с placeholder IP для замены)

При отправке в Pingachock: подставить тестируемый IP в поле `address` outbound'а.

---

## Availability Monitor Integration

### `_do_process_group` (изменения)

```python
if group.managed_pool_id:
    pool_ids_list = None  # не нужен
    # новый путь
else:
    # существующий путь: строим pool list из ip_set_ids
```

### Новая функция `_find_replacement_from_pool`

```python
async def _find_replacement_from_pool(
    session_factory, pool_id: int, group_id: int,
    exclude_ips: frozenset[str],
) -> tuple[str, float | None, float | None, float | None] | tuple[None, None, None, None]:
    # возвращает (ip, loss_pct, rtt_ms, speed_mbps) или (None,...)
```

1. Запрашивает из БД:
   `SELECT * FROM managed_ips WHERE pool_id=X AND is_approved=True ORDER BY score DESC`
2. Фильтрует `exclude_ips`
3. Берёт кандидатов порциями по 5, делает быстрый `distributed_ping_check` (финальная проверка — IP ещё жив?)
4. Возвращает первый прошедший финальный пинг + его метрики из `managed_ips`
5. Если одобренные кончились → `(None, None, None, None)`, логирует предупреждение

### Сообщение об успехе (managed pool режим)

```
✅ Адрес заменён

Хост/группа: premium
1.2.3.4 → 5.6.7.8
задержка 45мс, потери 0%, скорость 38Мбит/с

Время поиска: 12с
```

Метрики берутся из `managed_ips`, не из нового Pingachock-чека.

---

## UI Flow

### Главное меню → Наборы IP

Вместо прямого списка — промежуточный экран:
```
[📋 Пользовательские списки]
[⚙️ Модерируемые пулы]
```

### Пользовательские списки

Существующий функционал. Единственное изменение: при создании/редактировании набора входящий текст/файл проходит через `normalize_addresses`.

### Модерируемые пулы

**Список пулов:**
```
● Пул "Premium TM" — 847 IP, 312 одобрено ✅  (последняя проверка: 14 мин назад)
● Пул "Standard"   — 1200 IP, 89 одобрено ✅  (последняя проверка: 2 ч назад)
[+ Создать пул]
```

**Создание пула (FSM, 5 шагов):**
1. Название
2. Выбор пользовательских наборов (мультивыбор)
3. Выбор тега хостов (для VLESS-конфига)
4. Порог оценки (default: 60)
5. Интервал пересканирования в минутах

**Карточка пула:**
```
⚙️ Пул "Premium TM"

Источники: Premium-RU, Premium-EU
Тег хостов: premium
Порог: 60 | Интервал: 120 мин

📊 Всего: 847 | Одобрено: 312 (37%) | Отклонено: 535

[🔄 Запустить проверку]
[⚙️ Настройки]  [🗑 Удалить]
```

### Автоматизация — выбор источника

При создании/редактировании группы добавляется шаг выбора источника IP:
```
Источник IP для замен:
[📋 Пользовательские наборы]   ← текущий режим
[⚙️ Модерируемый пул]          ← новый
```

При выборе пула — вместо выбора наборов показывается список доступных пулов организации.

---

## Files Created / Modified

**Новые файлы:**
- `db/models/managed_pool.py` — модели `ManagedPool`, `ManagedIp`
- `services/ip_pool_scorer.py` — фоновый скорер
- `services/managed_pool_service.py` — CRUD для пулов и IP
- `bot/handlers/managed_pool_fsm.py` — FSM создания/редактирования пулов
- `alembic/versions/XXXX_managed_pools.py` — миграция

**Изменённые файлы:**
- `db/models/automation_group.py` — `managed_pool_id` FK
- `services/ip_check_service.py` — `normalize_addresses()`
- `services/remnawave_api_service.py` — `get_vless_config_for_tag()`
- `services/availability_monitor.py` — `_find_replacement_from_pool()`, ветка в `_do_process_group`
- `bot/handlers/ip_sets.py` — нормализация при вводе
- `bot/handlers/automation_fsm.py` — выбор источника (наборы vs пул)
- `bot/keyboards/inline.py` — новые клавиатуры
- `main.py` — запуск `run_ip_pool_scorer`
