from aiogram.fsm.state import State, StatesGroup


class DeployRemnanode(StatesGroup):
    choosing_panel = State()
    waiting_ip = State()
    waiting_login = State()
    waiting_auth = State()
    waiting_key_passphrase = State()
    waiting_secret_key = State()
    waiting_port = State()
    choosing_config_profile = State()
