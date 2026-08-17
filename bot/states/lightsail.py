from aiogram.fsm.state import State, StatesGroup


class LightsailFSM(StatesGroup):
    """FSM for Lightsail IP search settings."""
    selecting_nodes = State()  # toggle Pingachock nodes for this config
    editing_target = State()   # enter new target_count
    editing_recheck = State()  # enter new recheck_minutes
