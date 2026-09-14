"""aiogram FSM states for the lead collection flow."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class LeadForm(StatesGroup):
    # No state == Idle.
    Collecting = State()
    Reviewing = State()
    EditingField = State()
    ConfirmingDuplicate = State()
    ManualEntry = State()
