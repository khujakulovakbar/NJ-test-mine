# -*- coding: utf-8 -*-
"""Parser of the treasury Excel file «Банковские кредиты NJ действующие.xlsx».

Pure Python (openpyxl only) so that it can be tested outside Odoo.

Layout of the file (first sheet):
    row N-2 : bank label above each loan block      ("OFB - 8 млн")
    row N-1 : contract above each loan block        ("Договор № … от 01.03.2023")
    row N   : headers: Дата | ДН | Проценты | Получен займ | [БГ] | Погашен % | Погашен осн.долг | Остаток | …
    rows    : one row per calendar day, column A = date; a few label rows ("2026 год") in between.

Interest formula of a day (column «Проценты»):  =<previous balance>*8%/360
Interest paid (column «Погашен %») is often =SUM(<range of «Проценты»>) – the range tells
whether the paid period ends on the payment day (window "end") or the day before ("start").
"""
import datetime
import io
import re

from . import engine

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

HEADER_DATE = 'дата'
COLS = {
    'проценты': 'interest',
    'получен займ': 'disb',
    'бг': 'fee',
    'погашен %': 'interest_paid',
    'погашен осн.долг': 'principal',
    'погашен осн. долг': 'principal',
    'остаток': 'balance',
}
RATE_RE = re.compile(r'\*\s*([0-9]+(?:[.,][0-9]+)?)\s*%\s*/\s*(360|365)')
SUM_RE = re.compile(r'^=\s*SUM\(\s*\$?[A-Z]{1,3}\$?(\d+)\s*:\s*\$?[A-Z]{1,3}\$?(\d+)\s*\)\s*$', re.I)
CONTRACT_DATE_RE = re.compile(r'от\s+(\d{2})\.(\d{2})\.(\d{4})')
KEY_RE = re.compile(r'\d{2,4}/[0-9A-Za-zА-Яа-яЁё]+')
MAX_EMPTY_ROWS = 400


class ExcelLoanError(Exception):
    pass


def _norm(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip().lower()


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return None


def suggest_keys(contract):
    """Keys that identify the loan in bank statement texts: contract number and date."""
    keys = []
    m = KEY_RE.search(contract or '')
    if m:
        keys.append(m.group(0))
    d = CONTRACT_DATE_RE.search(contract or '')
    if d:
        keys.append('%s.%s.%s' % d.groups())
    return ', '.join(keys)


def contract_date(contract):
    d = CONTRACT_DATE_RE.search(contract or '')
    if not d:
        return None
    day, month, year = (int(x) for x in d.groups())
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def parse_loans_xlsx(data):
    """Return {'blocks': [...], 'first_date', 'last_date', 'rows'} from the file bytes."""
    if openpyxl is None:
        raise ExcelLoanError("Библиотека openpyxl не установлена.")
    try:
        wb_f = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        wb_v = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 - any broken file
        raise ExcelLoanError("Не удалось открыть файл Excel: %s" % e) from e
    ws_f, ws_v = wb_f.worksheets[0], wb_v.worksheets[0]

    head = list(ws_f.iter_rows(min_row=1, max_row=30, values_only=True))
    header_row = None
    for idx, row in enumerate(head, start=1):
        if row and _norm(row[0]) == HEADER_DATE:
            header_row = idx
            break
    if not header_row or header_row < 3:
        raise ExcelLoanError("Не найдена строка заголовков («Дата» в колонке A).")
    labels, contracts, headers = head[header_row - 3], head[header_row - 2], head[header_row - 1]
    width = len(headers)

    blocks = []
    starts = [j for j, h in enumerate(headers) if _norm(h) == 'проценты']
    if not starts:
        raise ExcelLoanError("Не найдено ни одного блока кредита (колонка «Проценты»).")
    for n, j in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else width
        cols = {}
        for k in range(j, end):
            key = COLS.get(_norm(headers[k]))
            if key and key not in cols:
                cols[key] = k
            if key == 'balance':
                break
        if 'balance' not in cols or 'principal' not in cols:
            raise ExcelLoanError("Блок в колонке %s: нет колонок «Погашен осн.долг» / «Остаток»." % (j + 1))
        label = str(labels[j] or '').strip() if j < len(labels) else ''
        contract = str(contracts[j] or '').strip() if j < len(contracts) else ''
        blocks.append({
            'index': n + 1, 'column': j + 1, 'label': label, 'contract': contract,
            'cols': cols, 'events': [], 'rates': [], 'basis_votes': {},
            'window_votes': {'start': 0, 'end': 0}, 'month_end': {}, 'last_balance': 0.0,
            'missing_values': 0, 'interest_cache': {},
        })
    occurrence = {}
    for b in blocks:
        key = (b['label'], b['contract'])
        occurrence[key] = occurrence.get(key, 0) + 1
        b['occurrence'] = occurrence[key]
        b['excel_key'] = '%s|%s|%s' % (b['contract'], b['label'], b['occurrence'])
        b['contract_date'] = contract_date(b['contract'])
        b['keys'] = suggest_keys(b['contract'])

    row_date = {}
    first = last = None
    empty = 0
    prev_date = None
    rows_f = ws_f.iter_rows(min_row=header_row + 1, max_col=width, values_only=True)
    rows_v = ws_v.iter_rows(min_row=header_row + 1, max_col=width, values_only=True)
    for rownum, (rf, rv) in enumerate(zip(rows_f, rows_v), start=header_row + 1):
        d = _as_date(rf[0] if rf else None) or _as_date(rv[0] if rv else None)
        if not d:
            empty += 1
            if first and empty > MAX_EMPTY_ROWS:
                break
            continue
        empty = 0
        if prev_date and d <= prev_date:
            raise ExcelLoanError("Даты в колонке A не идут по порядку (строка %s)." % rownum)
        prev_date = d
        row_date[rownum] = d
        first = first or d
        last = d
        for b in blocks:
            c = b['cols']
            f_int = rf[c['interest']] if c['interest'] < len(rf) else None
            v_int = rv[c['interest']] if c['interest'] < len(rv) else None
            if isinstance(v_int, (int, float)) and not isinstance(v_int, bool):
                b['interest_cache'][d] = float(v_int)
            elif isinstance(f_int, str) and f_int.startswith('='):
                b['interest_missing'] = True
            if isinstance(f_int, str):
                m = RATE_RE.search(f_int)
                if m:
                    rate = float(m.group(1).replace(',', '.'))
                    basis = int(m.group(2))
                    if not b['rates'] or (b['rates'][-1][1], b['rates'][-1][2]) != (rate, basis):
                        b['rates'].append((d, rate, basis))
                    b['basis_votes'][basis] = b['basis_votes'].get(basis, 0) + 1
            for key in ('disb', 'principal', 'interest_paid', 'fee'):
                if key not in c:
                    continue
                fv = rf[c[key]] if c[key] < len(rf) else None
                vv = rv[c[key]] if c[key] < len(rv) else None
                if isinstance(fv, str) and fv.startswith('=') and vv is None:
                    b['missing_values'] += 1
                    continue
                if isinstance(vv, (int, float)) and not isinstance(vv, bool) and abs(vv) > 1e-9:
                    kind = {'disb': 'disb', 'principal': 'principal',
                            'interest_paid': 'interest', 'fee': 'fee'}[key]
                    b['events'].append((d, kind, float(vv)))
                if key == 'interest_paid' and isinstance(fv, str):
                    sm = SUM_RE.match(fv)
                    if sm:
                        end_row = int(sm.group(2))
                        if row_date.get(end_row) == d:
                            b['window_votes']['end'] += 1
                        else:
                            b['window_votes']['start'] += 1
            bal = rv[c['balance']] if c['balance'] < len(rv) else None
            if isinstance(bal, (int, float)) and not isinstance(bal, bool):
                b['last_balance'] = float(bal)
                nxt = d + datetime.timedelta(days=1)
                if nxt.day == 1:
                    b['month_end'][d] = float(bal)
            elif isinstance(rf[c['balance']] if c['balance'] < len(rf) else None, str):
                b['missing_values'] += 1
        if first is None:
            continue
    if not first:
        raise ExcelLoanError("В колонке A не найдено ни одной даты.")
    for b in blocks:
        if b['missing_values']:
            raise ExcelLoanError(
                "В файле нет рассчитанных значений формул (блок «%s»). Откройте файл в Excel, "
                "сохраните и загрузите снова." % b['label'])
        b['basis'] = max(b['basis_votes'], key=b['basis_votes'].get) if b['basis_votes'] else 360
        b['window'] = 'end' if b['window_votes']['end'] > b['window_votes']['start'] else 'start'
        b['rates'] = [(r[0], r[1], r[2]) for r in b['rates']]
        b['month_end'][last] = b['last_balance']
        _fit_accrual(b, first, last)
    wb_f.close()
    wb_v.close()
    return {'blocks': blocks, 'first_date': first, 'last_date': last}


def _fit_accrual(block, first, last):
    """Choose the accrual base that reproduces the Excel «Проценты» column best and turn the
    remaining day differences into 'adj' events, so that the daily view equals the file."""
    cache = block.pop('interest_cache')
    block['adjustments'] = []
    if block.pop('interest_missing', False):
        # no calculated values of «Проценты» in the file: keep the formula rule, no corrections
        block['base'] = 'prev'
        return
    best = None
    for base in ('prev', 'same'):
        rows = engine.daily(block['events'], block['rates'], first, last, base=base)
        diffs = []
        for r in rows:
            x = cache.get(r['date'])
            if x is not None and abs(x - r['interest']) > 0.005:
                diffs.append((r['date'], x - r['interest'], x))
        if best is None or len(diffs) < len(best[1]):
            best = (base, diffs)
    block['base'] = best[0]
    disb_days = {e[0] for e in block['events'] if e[1] == 'disb'}
    for d, diff, excel_value in best[1]:
        if abs(excel_value) < 0.005 and d.month == 1 and d.day == 1:
            note = "Excel: 0 за 1 января (формула ссылается на строку года)"
        elif d in disb_days:
            note = "Excel: проценты за день выдачи"
        else:
            note = "Excel: корректировка начисления"
        block['adjustments'].append((d, round(diff, 6), note))
        block['events'].append((d, 'adj', round(diff, 6)))
