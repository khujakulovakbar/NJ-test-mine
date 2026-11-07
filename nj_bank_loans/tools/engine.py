# -*- coding: utf-8 -*-
"""Daily interest engine, identical to the treasury Excel:

    interest(t) = balance(t-1) * rate(t) / 100 / basis(t)
    balance(t)  = balance(t-1) + disbursed(t) - principal(t)

`rate(t)` is the last rate whose date_from <= t.  Pure Python, used by the
recalculation wizards and by the tests (the SQL view nj_loan_day uses the same rule).
"""
import datetime
from collections import defaultdict

ONE_DAY = datetime.timedelta(days=1)


def daily(events, rates, date_from=None, date_to=None, base='prev'):
    """events: iterable of (date, kind, amount), kind in disb/principal/interest/fee/adj.
    rates: iterable of (date_from, rate_percent, basis).
    base 'prev': interest of day t on the balance at the end of day t-1;
    base 'same': on the balance at the end of day t (after the day's flows).
    kind 'adj' is a manual correction of the day's accrued interest.
    Returns list of dicts: date, interest, disb, principal, interest_paid, balance."""
    flows = defaultdict(lambda: [0.0, 0.0, 0.0])
    adj = defaultdict(float)
    for d, kind, amount in events:
        if kind == 'disb':
            flows[d][0] += amount
        elif kind == 'principal':
            flows[d][1] += amount
        elif kind == 'adj':
            adj[d] += amount
            flows.setdefault(d, [0.0, 0.0, 0.0])
        else:
            flows[d][2] += amount
    if not flows and not date_from:
        return []
    start = date_from or min(flows)
    end = date_to or max(flows)
    rates = sorted(rates)
    ri = -1
    balance = 0.0
    # balance before the window
    for d in sorted(flows):
        if d < start:
            balance += flows[d][0] - flows[d][1]
    out = []
    d = start
    while d <= end:
        while ri + 1 < len(rates) and rates[ri + 1][0] <= d:
            ri += 1
        rate, basis = (rates[ri][1], rates[ri][2]) if ri >= 0 else (0.0, 360)
        disb, principal, paid = flows.get(d, (0.0, 0.0, 0.0))
        before = balance
        balance = balance + disb - principal
        interest = (balance if base == 'same' else before) * rate / 100.0 / basis + adj.get(d, 0.0)
        out.append({'date': d, 'interest': interest, 'disb': disb, 'principal': principal,
                    'interest_paid': paid, 'balance': balance})
        d += ONE_DAY
    return out


def accrual_map(rows):
    return {r['date']: r['interest'] for r in rows}


def window_sum(acc, prev, day, window):
    """Accrued interest paid on `day` after the previous payment `prev`.
    window 'start': days prev .. day-1 ; window 'end': days prev+1 .. day."""
    if window == 'end':
        a, b = prev + ONE_DAY, day
    else:
        a, b = prev, day - ONE_DAY
    total = 0.0
    d = a
    while d <= b:
        total += acc.get(d, 0.0)
        d += ONE_DAY
    return total


def add_months(d, months, day=None):
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    import calendar
    last = calendar.monthrange(y, m)[1]
    return datetime.date(y, m, min(day or d.day, last))
