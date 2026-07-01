from aiogram.fsm.state import State, StatesGroup


class NodeDomainFSM(StatesGroup):
    waiting_ip = State()
    waiting_domain = State()
    waiting_overwrite_confirm = State()
    waiting_ssh_login = State()
    waiting_auth = State()
    waiting_key_passphrase = State()
