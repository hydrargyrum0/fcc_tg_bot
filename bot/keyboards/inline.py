from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Развёртывание", callback_data="menu:deploy"),
            InlineKeyboardButton(text="🖥 Панели", callback_data="menu:panels"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings"),
            InlineKeyboardButton(text="🌐 Домены", callback_data="menu:domains"),
        ],
    ])


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


def panel_detail_kb(panel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Изменить URL", callback_data=f"rwp:eurl:{panel_id}"),
            InlineKeyboardButton(text="Изменить Тег", callback_data=f"rwp:etag:{panel_id}"),
        ],
        [
            InlineKeyboardButton(text="Изменить Токен", callback_data=f"rwp:etok:{panel_id}"),
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
