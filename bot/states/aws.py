from aiogram.fsm.state import State, StatesGroup


class AddAWS(StatesGroup):
    waiting_access_key_id = State()
    waiting_secret_key = State()
    waiting_tag = State()


class EditAWS(StatesGroup):
    waiting_tag = State()
    waiting_access_key_id = State()
    waiting_secret_key = State()
