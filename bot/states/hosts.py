from aiogram.fsm.state import State, StatesGroup


class HostsFSM(StatesGroup):
    choosing_panel = State()
    choosing_tag = State()
    waiting_address = State()
