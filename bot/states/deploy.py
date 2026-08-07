from aiogram.fsm.state import State, StatesGroup


class DeployRemnanode(StatesGroup):
    choosing_panel = State()
    waiting_ip = State()
    waiting_login = State()
    waiting_auth = State()
    waiting_key_passphrase = State()
    waiting_become_method = State()
    waiting_secret_key = State()
    waiting_port = State()
    choosing_config_profile = State()


class DeployHysteria(StatesGroup):
    # Phase 1 — deploy Remnanode
    choosing_panel = State()
    waiting_ip = State()
    waiting_login = State()
    waiting_auth = State()
    waiting_key_passphrase = State()
    waiting_become_method = State()
    choosing_config_profile = State()
    # Phase 2 — domain + TLS
    choosing_zone_method = State()
    waiting_manual_zone = State()
    waiting_subdomain = State()
