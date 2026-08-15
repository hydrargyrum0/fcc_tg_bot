from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Развёртывание", callback_data="menu:deploy"),
            InlineKeyboardButton(text="🖥 Ноды", callback_data="menu:nodes"),
        ],
        [
            InlineKeyboardButton(text="📡 Хосты", callback_data="menu:hosts"),
            InlineKeyboardButton(text="🌐 Домены", callback_data="menu:domains"),
        ],
        [
            InlineKeyboardButton(text="📋 Наборы IP", callback_data="menu:ip_sets"),
            InlineKeyboardButton(text="🤖 Автоматизации", callback_data="menu:automations"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings"),
        ],
        [
            InlineKeyboardButton(text="🔄 Сменить организацию", callback_data="org:switch"),
        ],
    ])


def ip_sets_menu_kb(sets: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=s.tag, callback_data=f"ipset:view:{s.id}")]
        for s in sets
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="ipset:add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ip_set_detail_kb(set_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить набор", callback_data=f"ipset:delete:{set_id}")],
        [InlineKeyboardButton(text="◀️ Назад к наборам", callback_data="ipset:back")],
    ])


def ip_set_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ipset:cancel")],
    ])


def org_select_kb(orgs: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=org.name, callback_data=f"org:select:{org.id}")]
        for org in orgs
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Remnawave", callback_data="settings:remnawave"),
            InlineKeyboardButton(text="Cloudflare", callback_data="settings:cloudflare"),
        ],
        [
            InlineKeyboardButton(text="AmazonWS", callback_data="settings:amazonws"),
            InlineKeyboardButton(text="🔍 Pingachock", callback_data="settings:pingachock"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back"),
        ],
    ])


# ── Pingachock ─────────────────────────────────────────────────────────────────

def pingachock_not_configured_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Подключить", callback_data="pc:connect")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:settings")],
    ])


def pingachock_configured_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Изменить URL", callback_data="pc:edit_url"),
            InlineKeyboardButton(text="🔑 Изменить ключ", callback_data="pc:edit_key"),
        ],
        [InlineKeyboardButton(text="🔄 Проверить соединение", callback_data="pc:test")],
        [InlineKeyboardButton(text="🗑 Отключить", callback_data="pc:delete")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:settings")],
    ])


def pingachock_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="pc:cancel")],
    ])


def remnawave_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data="remnawave:add"),
            InlineKeyboardButton(text="🔧 Управлять", callback_data="remnawave:manage"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="menu:settings"),
        ],
    ])


def cancel_input_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="remnawave:add_cancel")],
    ])


def manage_panels_kb(panels: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=p.tag, callback_data=f"rwp:{p.id}")]
        for p in panels
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings:remnawave")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def panel_detail_kb(panel_id: int, monitoring_enabled: bool = True) -> InlineKeyboardMarkup:
    mon_text = "🔔 Мониторинг нод: вкл" if monitoring_enabled else "🔕 Мониторинг нод: выкл"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Изменить URL", callback_data=f"rwp:eurl:{panel_id}"),
            InlineKeyboardButton(text="Изменить Тег", callback_data=f"rwp:etag:{panel_id}"),
        ],
        [
            InlineKeyboardButton(text="Изменить Токен", callback_data=f"rwp:etok:{panel_id}"),
        ],
        [
            InlineKeyboardButton(text="Изменить Node Secret", callback_data=f"rwp:ensec:{panel_id}"),
            InlineKeyboardButton(text="Изменить Node Port", callback_data=f"rwp:enport:{panel_id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"rwp:delete:{panel_id}"),
        ],
        [
            InlineKeyboardButton(text=mon_text, callback_data=f"rwp:monitoring:{panel_id}"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="rwp:back"),
        ],
    ])


def cancel_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="rwp:cancel_edit")],
    ])


def cloudflare_kb(email: str | None, has_api_key: bool) -> InlineKeyboardMarkup:
    email_text = email if email else "-----"
    token_text = "*****" if has_api_key else "-----"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📧 {email_text}", callback_data="cf:edit_email")],
        [InlineKeyboardButton(text=f"🔑 {token_text}", callback_data="cf:edit_token")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:settings")],
    ])


def cancel_cf_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="cf:cancel")],
    ])


def nodes_panels_kb(panels_stats: list[tuple[int, str, str]]) -> InlineKeyboardMarkup:
    """panels_stats: list of (panel_id, tag, stats_string)"""
    rows = [
        [InlineKeyboardButton(text=f"{tag} — {stats}", callback_data=f"nodes:panel:{panel_id}")]
        for panel_id, tag, stats in panels_stats
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def nodes_panel_detail_kb(panel_id: int, nodes: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=n["name"], callback_data=f"nodes:node:{panel_id}:{n['uuid']}")]
        for n in nodes
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:nodes")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def nodes_node_detail_kb(panel_id: int, node_uuid: str, is_disabled: bool = False) -> InlineKeyboardMarkup:
    toggle_text = "✅ Включить" if is_disabled else "⛔ Выключить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Перезапустить ноду", callback_data=f"nodes:restart:{panel_id}:{node_uuid}"),
            InlineKeyboardButton(text=toggle_text, callback_data=f"nodes:toggle:{panel_id}:{node_uuid}"),
        ],
        [InlineKeyboardButton(text="⬆️ Обновить версию ноды", callback_data="nodes:wip")],
        [InlineKeyboardButton(text="⬆️ Обновить версию XRAY", callback_data="nodes:wip")],
        [InlineKeyboardButton(text="🌐 Привязать домен", callback_data=f"nodes:domain:{panel_id}:{node_uuid}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"nodes:panel:{panel_id}")],
    ])


def back_to_node_kb(panel_id: int, node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к ноде", callback_data=f"nodes:node:{panel_id}:{node_uuid}")],
    ])


def nd_cancel_kb(panel_id: int, node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"nd:cancel:{panel_id}:{node_uuid}")],
    ])


def nd_overwrite_kb(panel_id: int, node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, перезаписать", callback_data="nd:overwrite:yes"),
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"nd:cancel:{panel_id}:{node_uuid}"),
        ],
    ])


def nodes_restart_type_kb(panel_id: int, node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Плавно", callback_data=f"nodes:restart_type:{panel_id}:{node_uuid}:0"),
            InlineKeyboardButton(text="⚡ Принудительно", callback_data=f"nodes:restart_type:{panel_id}:{node_uuid}:1"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"nodes:node:{panel_id}:{node_uuid}")],
    ])


def nodes_restart_confirm_kb(panel_id: int, node_uuid: str, force: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Да, перезапустить",
                callback_data=f"nodes:restart_conf:{panel_id}:{node_uuid}:{force}",
            ),
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"nodes:node:{panel_id}:{node_uuid}"),
        ],
    ])


def deploy_presets_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стабильный Remnanode", callback_data="deploy:preset:remnanode_stable")],
        [InlineKeyboardButton(text="Remnanode + Hysteria + TLS", callback_data="deploy:preset:remnanode_hysteria_tls")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
    ])


def deploy_select_panel_kb(panels: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=p.tag, callback_data=f"deploy:panel:{p.id}")]
        for p in panels
    ]
    rows.append([InlineKeyboardButton(text="Не подключать к Remnawave", callback_data="deploy:panel:none")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="deploy:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def become_method_kb(has_password: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_password:
        rows.append([InlineKeyboardButton(
            text="🔑 Тот же пароль что SSH",
            callback_data="deploy:become:same_pass",
        )])
    rows.append([InlineKeyboardButton(
        text="🔓 Без пароля (NOPASSWD sudo)",
        callback_data="deploy:become:nopass",
    )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="deploy:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deploy_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="deploy:cancel")],
    ])


def config_profile_select_kb(profiles: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=p["name"], callback_data=f"deploy:cfprofile:{p['uuid']}")]
        for p in profiles
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def amazonws_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data="aws:add"),
            InlineKeyboardButton(text="🔧 Управлять", callback_data="aws:manage"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:settings")],
    ])


def cancel_aws_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="aws:add_cancel")],
    ])


def aws_manage_kb(accounts: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=a.tag, callback_data=f"aws:{a.id}")]
        for a in accounts
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings:amazonws")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def aws_detail_kb(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Изменить Тег", callback_data=f"aws:etag:{account_id}"),
            InlineKeyboardButton(text="Изменить Access Key ID", callback_data=f"aws:ekey:{account_id}"),
        ],
        [
            InlineKeyboardButton(text="Изменить Secret Key", callback_data=f"aws:esec:{account_id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"aws:delete:{account_id}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="aws:back")],
    ])


def cancel_aws_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="aws:cancel_edit")],
    ])


# ── Hosts ──────────────────────────────────────────────────────────────────────

def hosts_panels_kb(panels: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=p.tag, callback_data=f"hosts:panel:{p.id}")]
        for p in panels
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hosts_tags_kb(tags: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=tag, callback_data=f"hosts:tag:{i}")]
        for i, tag in enumerate(tags)
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="hosts:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hosts_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="hosts:back")],
    ])


def hosts_top_mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Указать вручную", callback_data="hosts:top:manual")],
        [InlineKeyboardButton(text="📋 Использовать Наборы IP", callback_data="hosts:top:ipsets")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="hosts:back")],
    ])


def hosts_source_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Автоматически", callback_data="hosts:source:auto")],
        [InlineKeyboardButton(text="📋 Вручную", callback_data="hosts:source:manual")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="hosts:back")],
    ])


def hosts_distribution_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔢 Всем одинаковый IP", callback_data="hosts:dist:same")],
        [InlineKeyboardButton(text="🔀 Каждому свой IP", callback_data="hosts:dist:each")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="hosts:back")],
    ])


def hosts_ip_sets_kb(sets: list) -> InlineKeyboardMarkup:
    rows = []
    for s in sets:
        count = len(s.addresses.splitlines())
        rows.append([InlineKeyboardButton(
            text=f"📋 {s.tag}  ({count:,} записей)",
            callback_data=f"hosts:ipset:{s.id}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="hosts:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hosts_ip_page_kb(
    ips: list[str],
    page: int,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=ip, callback_data=f"hosts:ip:{ip}")] for ip in ips]
    nav: list[InlineKeyboardButton] = []
    if has_prev:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"hosts:ippage:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"hosts:ippage:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="hosts:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hosts_confirm_bulk_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Применить", callback_data="hosts:bulk:confirm")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="hosts:back")],
    ])


def hosts_auto_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Применить", callback_data="hosts:auto:confirm")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="hosts:back")],
    ])


# ── Domains ────────────────────────────────────────────────────────────────────

_DOM_PAGE = 7


def domains_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Отобразить список", callback_data="dom:list:0")],
        [InlineKeyboardButton(text="🔍 Найти вручную", callback_data="dom:manual")],
        [InlineKeyboardButton(text="🔽 Назад", callback_data="menu:back")],
    ])


def _dom_pagination(prev_cb: str, page: int, next_cb: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text="◀️", callback_data=prev_cb),
        InlineKeyboardButton(text=str(page + 1), callback_data="dom:noop"),
        InlineKeyboardButton(text="▶️", callback_data=next_cb),
    ]


def domains_zones_kb(zones: list, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=z["name"], callback_data=f"dom:zone:{z['id']}:0")]
        for z in zones
    ]
    if total_pages > 1:
        prev_cb = f"dom:list:{page - 1}" if page > 0 else "dom:noop"
        next_cb = f"dom:list:{page + 1}" if page < total_pages - 1 else "dom:noop"
        rows.append(_dom_pagination(prev_cb, page, next_cb))
    rows.append([InlineKeyboardButton(text="🔽 Назад", callback_data="menu:domains")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def domains_records_kb(records: list, zone_id: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    offset = page * _DOM_PAGE
    rows = []
    for i, rec in enumerate(records):
        ridx = offset + i
        name = rec["name"]
        if len(name) > 32:
            name = name[:29] + "..."
        rows.append([InlineKeyboardButton(
            text=f"{rec['type']} {name}",
            callback_data=f"dom:rec:{zone_id}:{ridx}",
        )])
    rows.append([InlineKeyboardButton(text="➕ Добавить A-запись", callback_data=f"dom:adda:{zone_id}")])
    if total_pages > 1:
        prev_cb = f"dom:zone:{zone_id}:{page - 1}" if page > 0 else "dom:noop"
        next_cb = f"dom:zone:{zone_id}:{page + 1}" if page < total_pages - 1 else "dom:noop"
        rows.append(_dom_pagination(prev_cb, page, next_cb))
    rows.append([InlineKeyboardButton(text="🔽 Назад", callback_data="menu:domains")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def domains_record_detail_kb(zone_id: str, ridx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"dom:edit:{zone_id}:{ridx}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"dom:del:{zone_id}:{ridx}"),
        ],
        [InlineKeyboardButton(text="🔽 Назад", callback_data=f"dom:zone:{zone_id}:0")],
    ])


def domains_delete_confirm_kb(zone_id: str, ridx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Удалить", callback_data=f"dom:delok:{zone_id}:{ridx}"),
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"dom:rec:{zone_id}:{ridx}"),
        ],
    ])


def domains_cancel_kb(zone_id: str | None = None) -> InlineKeyboardMarkup:
    cb = f"dom:cancel:{zone_id}" if zone_id else "dom:cancel"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=cb)],
    ])


# ── Automations: Availability groups ──────────────────────────────────────────

_AVAIL_INTERVALS = [5, 10, 15, 30, 60]


def avail_groups_kb(groups: list, panels_by_id: dict) -> InlineKeyboardMarkup:
    """List of existing automation groups.

    panels_by_id: dict[int, RemnaWavePanel] — for showing panel tag.
    """
    rows = []
    for g in groups:
        panel = panels_by_id.get(g.panel_id)
        panel_label = panel.tag if panel else "?"
        status = "✅" if g.enabled else "❌"
        interval_label = f"{g.interval_minutes}мин"
        dist_label = "≡" if g.distribution == "same" else "⊞"
        rows.append([InlineKeyboardButton(
            text=f"{status} {g.host_tag} ({panel_label}) | {interval_label} {dist_label}",
            callback_data=f"avail:group:{g.id}",
        )])
    rows.append([InlineKeyboardButton(text="➕ Добавить группу", callback_data="avail:add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def avail_group_detail_kb(group_id: int, enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "⏸ Приостановить" if enabled else "▶️ Возобновить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=toggle_text, callback_data=f"avail:toggle:{group_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"avail:delete_confirm:{group_id}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="avail:list")],
    ])


def avail_delete_confirm_kb(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"avail:delete:{group_id}"),
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"avail:group:{group_id}"),
        ],
    ])


def avail_panels_kb(panels: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=p.tag, callback_data=f"avail:panel:{p.id}")]
        for p in panels
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="avail:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def avail_tags_kb(tags: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=tag, callback_data=f"avail:tag:{i}")]
        for i, tag in enumerate(tags)
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="avail:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def avail_ip_sets_kb(sets_info: list[dict], selected_ids: list[int]) -> InlineKeyboardMarkup:
    """Multi-select IP sets.  sets_info: list of {id, tag, count}."""
    rows = []
    for s in sets_info:
        mark = "✅" if s["id"] in selected_ids else "☐"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {s['tag']} ({s['count']:,} записей)",
            callback_data=f"avail:toggle_set:{s['id']}",
        )])
    if selected_ids:
        rows.append([InlineKeyboardButton(
            text="✅ Подтвердить выбор",
            callback_data="avail:confirm_sets",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="avail:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def avail_distribution_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔢 Всем одинаковый IP", callback_data="avail:dist:same")],
        [InlineKeyboardButton(text="🔀 Каждому свой IP", callback_data="avail:dist:each")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="avail:back")],
    ])


def avail_interval_kb(selected: int | None = None) -> InlineKeyboardMarkup:
    def _btn(n: int) -> InlineKeyboardButton:
        mark = "✅ " if n == selected else ""
        return InlineKeyboardButton(
            text=f"{mark}{n}мин",
            callback_data=f"avail:interval:{n}",
        )
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(5), _btn(10), _btn(15)],
        [_btn(30), _btn(60)],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="avail:back")],
    ])


def avail_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать", callback_data="avail:confirm_create")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="avail:back")],
    ])


# ── IP Sets section split ──────────────────────────────────────────────────────

def ip_sets_section_kb() -> InlineKeyboardMarkup:
    """Entry screen: choose between user lists and managed pools."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Пользовательские списки", callback_data="ipset:user_lists")],
        [InlineKeyboardButton(text="⚙️ Модерируемые пулы", callback_data="mpool:list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
    ])


# ── Managed Pool keyboards ─────────────────────────────────────────────────────

def mpool_list_kb(pools: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⚙️ {p.name}", callback_data=f"mpool:detail:{p.id}")]
        for p in pools
    ]
    rows.append([InlineKeyboardButton(text="➕ Создать пул", callback_data="mpool:add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:ip_sets")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mpool_detail_kb(pool_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Запустить проверку", callback_data=f"mpool:scan:{pool_id}")],
        [InlineKeyboardButton(text="📋 Список адресов", callback_data=f"mpool:ips:{pool_id}")],
        [InlineKeyboardButton(text="🧹 Очистить пул", callback_data=f"mpool:clear_confirm:{pool_id}")],
        [InlineKeyboardButton(text="⚡ Применить к автоматизациям", callback_data=f"mpool:force_apply:{pool_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"mpool:delete_confirm:{pool_id}")],
        [InlineKeyboardButton(text="◀️ К списку пулов", callback_data="mpool:list")],
    ])


def mpool_delete_confirm_kb(pool_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"mpool:delete:{pool_id}")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"mpool:detail:{pool_id}")],
    ])


def mpool_clear_confirm_kb(pool_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, очистить", callback_data=f"mpool:clear:{pool_id}")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"mpool:detail:{pool_id}")],
    ])


def mpool_ip_sets_kb(sets: list, selected: list[int]) -> InlineKeyboardMarkup:
    """Multi-select keyboard for choosing source IP sets for a pool."""
    rows = []
    for s in sets:
        mark = "✅ " if s.id in selected else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{s.tag}",
            callback_data=f"mpool:toggle_set:{s.id}",
        )])
    rows.append([InlineKeyboardButton(text="✔️ Подтвердить выбор", callback_data="mpool:confirm_sets")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="mpool:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mpool_tags_kb(tags: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=tag, callback_data=f"mpool:tag:{i}")]
        for i, tag in enumerate(tags)
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="mpool:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mpool_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="mpool:back")],
    ])


def mpool_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать пул", callback_data="mpool:confirm_create")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="mpool:back")],
    ])


# ── Automation group source-type keyboards ─────────────────────────────────────

def avail_source_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Пользовательские наборы", callback_data="avail:source:sets")],
        [InlineKeyboardButton(text="⚙️ Модерируемый пул", callback_data="avail:source:pool")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="avail:back")],
    ])


def avail_pools_kb(pools: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⚙️ {p.name}", callback_data=f"avail:pool:{p.id}")]
        for p in pools
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="avail:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
