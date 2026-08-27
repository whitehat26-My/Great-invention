"""Simulated payroll.

Returns hours worked from the roster the scheduling agent produced, with the
small over-runs real service produces, so labour cost is not simply the roster
read back verbatim.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from restaurant_ai.db.base import session_scope
from restaurant_ai.db.models import Shift, ShiftAssignment, Staff
from restaurant_ai.integrations.base import StaffHours


class FakePayroll:
    provider = "fake_payroll"

    def __init__(self, seed: int | None = None) -> None:
        self._seed = seed

    def fetch_hours(self, business_date: date) -> list[StaffHours]:
        seed = self._seed if self._seed is not None else int(business_date.strftime("%Y%m%d"))
        rng = random.Random(seed + 13)

        with session_scope() as session:
            rows = list(
                session.execute(
                    select(ShiftAssignment, Shift, Staff)
                    .join(Shift, ShiftAssignment.shift_id == Shift.id)
                    .join(Staff, ShiftAssignment.staff_id == Staff.id)
                    .where(Shift.business_date == business_date)
                )
            )

        hours: list[StaffHours] = []
        for _assignment, shift, staff in rows:
            scheduled = shift.hours
            # Service rarely ends on the hour; a little over-run is normal.
            actual = scheduled + Decimal(str(round(rng.uniform(-0.15, 0.5), 2)))
            hours.append(
                StaffHours(
                    employee_code=staff.employee_code,
                    business_date=business_date,
                    hours=max(actual, Decimal("0")).quantize(Decimal("0.01")),
                    hourly_rate=staff.hourly_rate,
                )
            )
        return hours
