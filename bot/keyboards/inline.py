from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Развёртывание", callback_data="menu:deploy"),
            InlineKeyboardButton(text="🖥 Ноды", callback_data="menu:nodes"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings"),
            InlineKeyboardButton(text="🌐 Домены", callback_data="menu:domains"),
        ],
        [
            InlineKeyboardButton(text="🔄 Сменить организацию", callback_data="org:switch"),
        ],
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
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back"),
        ],
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
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="remnawave:add_cancel"),
        ],
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
        [
            InlineKeyboardButton(text="❌ Отмена", callback_data="rwp:cancel_edit"),
        ],
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
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cf:cancel")],
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
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"nd:cancel:{panel_id}:{node_uuid}")],
    ])


def nd_overwrite_kb(panel_id: int, node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, перезаписать", callback_data="nd:overwrite:yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"nd:cancel:{panel_id}:{node_uuid}"),
        ],
    ])


def nodes_restart_type_kb(panel_id: int, node_uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Плавно", callback_data=f"nodes:restart_type:{panel_id}:{node_uuid}:0"),
            InlineKeyboardButton(text="⚡ Принудительно", callback_data=f"nodes:restart_type:{panel_id}:{node_uuid}:1"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"nodes:node:{panel_id}:{node_uuid}")],
    ])


def nodes_restart_confirm_kb(panel_id: int, node_uuid: str, force: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Да, перезапустить",
                callback_data=f"nodes:restart_conf:{panel_id}:{node_uuid}:{force}",
            ),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"nodes:node:{panel_id}:{node_uuid}"),
        ],
    ])


def deploy_presets_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стабильный Remnanode", callback_data="deploy:preset:remnanode_stable")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
    ])


def deploy_select_panel_kb(panels: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=p.tag, callback_data=f"deploy:panel:{p.id}")]
        for p in panels
    ]
    rows.append([InlineKeyboardButton(text="Не подключать к Remnawave", callback_data="deploy:panel:none")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="deploy:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deploy_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="deploy:cancel")],
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
        [InlineKeyboardButton(text="❌ Отмена", callback_data="aws:add_cancel")],
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
        [InlineKeyboardButton(text="❌ Отмена", callback_data="aws:cancel_edit")],
    ])
