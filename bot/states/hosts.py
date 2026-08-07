from aiogram.fsm.state import State, StatesGroup


class HostsFSM(StatesGroup):
    choosing_panel = State()
    choosing_tag = State()
    choosing_top_mode = State()       # "Указать вручную" / "Использовать Наборы IP"
    waiting_address = State()         # plain text input → same IP for all hosts
    choosing_source = State()         # auto (Pingachock) / manual (from set)
    choosing_distribution = State()   # same for all / each their own
    choosing_ip_set = State()         # which IP set to use
    choosing_single_ip = State()      # paginated bare-IP picker (manual + same)
    confirming_manual_bulk = State()  # preview host→IP (manual + each)
    confirming_auto = State()         # Pingachock results preview (auto)
