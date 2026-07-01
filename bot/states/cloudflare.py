from aiogram.fsm.state import State, StatesGroup


class EditCloudflare(StatesGroup):
    waiting_email = State()
    waiting_api_key = State()
