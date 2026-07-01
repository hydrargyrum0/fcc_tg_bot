from aiogram.fsm.state import State, StatesGroup


class AddRemnawave(StatesGroup):
    waiting_url = State()
    waiting_token = State()
    waiting_node_secret = State()
    waiting_node_port = State()
    waiting_tag = State()


class EditRemnawave(StatesGroup):
    waiting_url = State()
    waiting_token = State()
    waiting_node_secret = State()
    waiting_node_port = State()
    waiting_tag = State()
