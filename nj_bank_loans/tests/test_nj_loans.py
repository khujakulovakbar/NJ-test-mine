import base64
import datetime
import io

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged

from ..tools import engine

D = datetime.date


@tagged('post_install', '-at_install', 'nj_loans')
class TestNjLoans(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref('nj_bank_loans.group_nj_loan_manager')
        cls.cur = cls.env.company.currency_id
        cls.acc_principal = cls.env['account.account'].create(
            {'code': '781199', 'name': "Долгосрочные кредиты в валюте", 'account_type': 'liability_non_current'})
        cls.acc_interest = cls.env['account.account'].create(
            {'code': '692199', 'name': "Начисленные проценты", 'account_type': 'liability_current'})
        cls.bank_a = cls.env['res.partner'].create({'name': "Тест Банк А", 'is_company': True})
        cls.bank_b = cls.env['res.partner'].create({'name': "Тест Банк Б", 'is_company': True})
        cls.journal = cls.company_data['default_journal_bank']
        cls.env.company.write({'nj_urgent_days': 3, 'nj_soon_days': 14, 'nj_match_days': 10,
                               'nj_match_tolerance': 1.0, 'nj_remind_days': '5,1'})

    # ------------------------------------------------------------------ helpers
    def _loan(self, name, bank, lines, rates=((D(2026, 1, 1), 12.0, '360'),), **kw):
        vals = {
            'name': name, 'bank_id': bank.id, 'contract': "Договор %s" % name, 'currency_id': self.cur.id,
            'principal_account_id': self.acc_principal.id, 'interest_account_id': self.acc_interest.id,
            'state': 'active',
            'rate_ids': [(0, 0, {'date_from': d, 'rate': r, 'basis': b}) for d, r, b in rates],
            'line_ids': [(0, 0, {'date': d, 'kind': k, 'amount': a}) for d, k, a in lines],
        }
        vals.update(kw)
        return self.env['nj.loan'].create(vals)

    def _bank_line(self, bank, account, date, amount, text):
        move = self.env['account.move'].create({
            'move_type': 'entry', 'journal_id': self.journal.id, 'date': date,
            'line_ids': [
                (0, 0, {'account_id': account.id, 'partner_id': bank.id, 'debit': amount, 'name': text}),
                (0, 0, {'account_id': self.journal.default_account_id.id, 'credit': amount, 'name': text}),
            ]})
        move.action_post()
        return move.line_ids.filtered(lambda l: l.account_id == account)

    # ------------------------------------------------------------------ engine and daily view
    def test_01_daily_view_equals_engine(self):
        loan = self._loan('T-01', self.bank_a, [
            (D(2026, 1, 1), 'disb', 1_000_000.0),
            (D(2026, 2, 1), 'interest', 10_333.33),
            (D(2026, 3, 1), 'principal', 400_000.0),
            (D(2026, 3, 15), 'adj', -50.0),
            (D(2026, 6, 30), 'principal', 600_000.0),
        ], rates=((D(2026, 1, 1), 12.0, '360'), (D(2026, 4, 1), 10.0, '365')))
        self.env.flush_all()
        days = self.env['nj.loan.day'].search([('loan_id', '=', loan.id)], order='date')
        ref = engine.daily(loan._engine_events(), loan._engine_rates())
        self.assertEqual(len(days), len(ref))
        for row, exp in zip(days, ref):
            self.assertEqual(row.date, exp['date'])
            self.assertAlmostEqual(row.balance, exp['balance'], places=2)
            self.assertAlmostEqual(row.interest, exp['interest'], places=4)
        # 1 000 000 × 12% / 360 from the second day (balance of the previous day)
        jan2 = days.filtered(lambda r: r.date == D(2026, 1, 2))
        self.assertAlmostEqual(jan2.interest, 333.33, places=2)
        self.assertEqual(days[0].interest, 0.0)
        self.assertEqual(days.filtered(lambda r: r.date == D(2026, 1, 5)).weekday, 'ПН')
        # the correction is added to the day's interest
        mar15 = days.filtered(lambda r: r.date == D(2026, 3, 15))
        self.assertAlmostEqual(mar15.interest, 600_000 * 0.12 / 360 - 50.0, places=4)
        # rate change: 10% / 365 from 1 April
        apr2 = days.filtered(lambda r: r.date == D(2026, 4, 2))
        self.assertAlmostEqual(apr2.interest, 600_000 * 0.10 / 365, places=4)
        months = self.env['nj.loan.month'].search([('loan_id', '=', loan.id)], order='month')
        self.assertEqual(months[0].balance_end, 1_000_000.0)
        self.assertEqual(months.filtered(lambda m: m.month == D(2026, 6, 1)).balance_end, 0.0)
        self.assertAlmostEqual(sum(months.mapped('interest_accrued')), sum(r['interest'] for r in ref), places=2)

    def test_02_accrual_same_day(self):
        loan = self._loan('T-02', self.bank_a, [(D(2026, 1, 1), 'disb', 365_000.0),
                                                (D(2026, 1, 10), 'principal', 365_000.0)],
                          rates=((D(2026, 1, 1), 10.0, '365'),), accrual_base='same')
        self.env.flush_all()
        day1 = self.env['nj.loan.day'].search([('loan_id', '=', loan.id), ('date', '=', D(2026, 1, 1))])
        self.assertAlmostEqual(day1.interest, 100.0, places=4)
        last = self.env['nj.loan.day'].search([('loan_id', '=', loan.id), ('date', '=', D(2026, 1, 10))])
        self.assertEqual(last.interest, 0.0)

    # ------------------------------------------------------------------ status
    def test_03_status_urgency_and_close(self):
        loan = self._loan('T-03', self.bank_a, [(D(2026, 1, 1), 'disb', 100.0), (D(2026, 1, 20), 'interest', 5.0),
                                                (D(2026, 1, 31), 'principal', 100.0)])
        loan._refresh_status(today=D(2026, 1, 18))
        self.assertEqual((loan.next_payment_date, loan.days_to_next, loan.urgency), (D(2026, 1, 20), 2, 'urgent'))
        self.assertEqual(loan.balance, 100.0)
        loan._refresh_status(today=D(2026, 1, 21))
        self.assertEqual((loan.urgency, loan.next_payment_amount), ('soon', 100.0))
        loan._refresh_status(today=D(2026, 2, 1))
        self.assertEqual((loan.state, loan.urgency, loan.balance), ('closed', 'none', 0.0))

    # ------------------------------------------------------------------ bank statement matching and plan-fact
    def test_04_matching_and_planfact(self):
        self.env.company.nj_fact_start_date = D(2026, 9, 1)
        k1 = self._loan('T-K1', self.bank_a, [(D(2026, 1, 1), 'disb', 1_000_000.0),
                                              (D(2026, 9, 19), 'principal', 228_556.80),
                                              (D(2026, 9, 19), 'interest', 63_197.89)], match_keys="1647/CIF, 12.03.2025")
        k2 = self._loan('T-K2', self.bank_a, [(D(2026, 1, 1), 'disb', 500_000.0),
                                              (D(2026, 9, 10), 'interest', 41_800.0)])
        k3 = self._loan('T-K3', self.bank_a, [(D(2026, 1, 1), 'disb', 500_000.0),
                                              (D(2026, 9, 10), 'interest', 20_838.89)])
        k4 = self._loan('T-K4', self.bank_b, [(D(2026, 1, 1), 'disb', 300_000.0),
                                              (D(2026, 9, 2), 'principal', 20_000.0)])
        l_key1 = self._bank_line(self.bank_a, self.acc_principal, D(2026, 9, 8), 128_556.80,
                                 "DEAL 1647/CIF1091/UZB-MFA DD12.03.2025 PMNT")
        l_key2 = self._bank_line(self.bank_a, self.acc_principal, D(2026, 9, 9), 117_846.57,
                                 "DEAL 1647/CIF1091/UZB-MFA DD12.03.2025 PMNT")
        l_key3 = self._bank_line(self.bank_a, self.acc_interest, D(2026, 9, 9), 45_351.32,
                                 "DEAL 1647/CIF1091/UZB-MFA DD12.03.2025 INTEREST")
        l_amt1 = self._bank_line(self.bank_a, self.acc_interest, D(2026, 9, 10), 41_760.28,
                                 "Погашение процентов в иностранной валюте")
        l_amt2 = self._bank_line(self.bank_a, self.acc_interest, D(2026, 9, 10), 20_838.89,
                                 "Погашение процентов в иностранной валюте")
        l_single = self._bank_line(self.bank_b, self.acc_principal, D(2026, 9, 2), 20_000.0, "досрочно")
        l_none = self._bank_line(self.bank_a, self.acc_interest, D(2026, 9, 12), 777.0, "неизвестный платёж")
        n = self.env['nj.loan'].match_statement_lines()
        self.assertEqual(n, 6)
        self.assertEqual(l_key1.nj_loan_payment_ids.loan_id, k1)
        self.assertEqual(l_key1.nj_loan_payment_ids.match_method, 'key')
        self.assertEqual(l_key3.nj_loan_payment_ids.kind, 'interest')
        self.assertEqual((l_amt1.nj_loan_payment_ids.loan_id, l_amt1.nj_loan_payment_ids.match_method), (k2, 'amount'))
        self.assertEqual(l_amt2.nj_loan_payment_ids.loan_id, k3)
        self.assertEqual((l_single.nj_loan_payment_ids.loan_id, l_single.nj_loan_payment_ids.match_method), (k4, 'single'))
        self.assertFalse(l_none.nj_loan_payment_ids)
        unmatched = self.env['account.move.line'].search(self.env['nj.loan']._unmatched_domain())
        self.assertIn(l_none, unmatched)
        self.assertNotIn(l_key1, unmatched)
        # second run is idempotent
        self.assertEqual(self.env['nj.loan'].match_statement_lines(), 0)
        self.env.flush_all()
        PF = self.env['nj.loan.planfact']
        pf1 = PF.search([('loan_id', '=', k1.id), ('month', '=', D(2026, 9, 1))])
        principal = pf1.filtered(lambda r: r.kind == 'principal')
        interest = pf1.filtered(lambda r: r.kind == 'interest')
        self.assertAlmostEqual(principal.diff, 17_846.57, places=2)
        self.assertAlmostEqual(interest.diff, -17_846.57, places=2)
        self.assertEqual((principal.diff_type, interest.diff_type), ('account', 'account'))
        self.assertTrue(principal.fact_loaded and principal.has_diff)
        pf2 = PF.search([('loan_id', '=', k2.id), ('month', '=', D(2026, 9, 1))])
        self.assertEqual((pf2.diff_type, pf2.status), ('amount', 'diff'))
        pf3 = PF.search([('loan_id', '=', k3.id), ('month', '=', D(2026, 9, 1))])
        self.assertEqual(pf3.status, 'ok')
        # manual assignment of the unmatched line
        wiz = self.env['nj.loan.assign.wizard'].create({'move_line_ids': [(6, 0, l_none.ids)], 'loan_id': k2.id})
        wiz.action_assign()
        self.assertEqual(l_none.nj_loan_payment_ids.match_method, 'manual')

    # ------------------------------------------------------------------ prepayment
    def _schedule_loan(self, mode):
        lines = [(D(2026, 1, 1), 'disb', 1_200_000.0)]
        for m in range(1, 13):
            d = D(2026, m, 28)
            lines.append((d, 'principal', 100_000.0))
        loan = self._loan('T-P' + mode, self.bank_a, lines, prepay_mode=mode)
        acc = engine.accrual_map(loan._daily())
        prev = D(2026, 1, 1)
        for m in range(1, 13):
            d = D(2026, m, 28)
            self.env['nj.loan.line'].create({'loan_id': loan.id, 'date': d, 'kind': 'interest',
                                             'amount': round(engine.window_sum(acc, prev, d, 'start'), 2)})
            prev = d
        return loan

    def test_05_prepay_term(self):
        loan = self._schedule_loan('term')
        before = sum(loan.line_ids.filtered(lambda l: l.kind == 'interest').mapped('amount'))
        wiz = self.env['nj.loan.prepay.wizard'].with_context(active_id=loan.id).create(
            {'loan_id': loan.id, 'date': D(2026, 6, 10), 'amount': 250_000.0})
        self.assertEqual(wiz.mode, 'term')
        wiz.action_apply()
        principal = loan.line_ids.filtered(lambda l: l.kind == 'principal').sorted('date')
        self.assertAlmostEqual(sum(principal.mapped('amount')), 1_200_000.0, places=2)
        self.assertEqual(principal[-1].date, D(2026, 10, 28))     # Nov and Dec paid off, Oct reduced
        self.assertAlmostEqual(principal[-1].amount, 50_000.0, places=2)
        after = sum(loan.line_ids.filtered(lambda l: l.kind == 'interest').mapped('amount'))
        self.assertLess(after, before)
        # interest of every future payment equals the accrual of its window
        acc = engine.accrual_map(loan._daily())
        ints = loan.line_ids.filtered(lambda l: l.kind == 'interest').sorted('date')
        prev = D(2026, 1, 1)
        for line in ints:
            if line.date > D(2026, 6, 10):
                self.assertAlmostEqual(line.amount, engine.window_sum(acc, prev, line.date, 'start'), delta=0.02)
            prev = line.date
        # after the last repayment only the accrual of 28.10 (balance of 27.10) is left for 28.11
        tail = ints.filtered(lambda l: l.date > D(2026, 10, 28))
        self.assertEqual(tail.mapped('date'), [D(2026, 11, 28)])
        self.assertAlmostEqual(tail.amount, round(50_000 * 0.12 / 360, 2), places=2)

    def test_06_prepay_payment_and_errors(self):
        loan = self._schedule_loan('payment')
        wiz = self.env['nj.loan.prepay.wizard'].create(
            {'loan_id': loan.id, 'date': D(2026, 6, 10), 'amount': 300_000.0, 'mode': 'payment'})
        wiz.action_apply()
        future = loan.line_ids.filtered(lambda l: l.kind == 'principal' and l.date > D(2026, 6, 10))
        self.assertEqual(len(future), 7)
        self.assertAlmostEqual(sum(future.mapped('amount')), 400_000.0, places=2)
        self.assertAlmostEqual(future.sorted('date')[0].amount, 57_142.86, places=2)
        with self.assertRaises(UserError):
            self.env['nj.loan.prepay.wizard'].create(
                {'loan_id': loan.id, 'date': D(2026, 7, 1), 'amount': 10_000_000.0, 'mode': 'term'}).action_apply()

    # ------------------------------------------------------------------ new loan
    def test_07_generate_schedule(self):
        loan = self.env['nj.loan'].create({'name': 'T-G', 'bank_id': self.bank_a.id, 'contract': "Новый",
                                           'currency_id': self.cur.id})
        self.assertEqual(loan.state, 'draft')
        wiz = self.env['nj.loan.generate.wizard'].create({
            'loan_id': loan.id, 'amount': 1_000_000.0, 'date_disb': D(2026, 11, 1), 'rate': 11.0, 'basis': '360',
            'method': 'equal', 'first_principal_date': D(2027, 1, 10), 'principal_count': 3, 'principal_interval': 1,
            'first_interest_date': D(2026, 12, 10), 'interest_interval': 1})
        wiz.action_generate()
        self.assertEqual(loan.state, 'active')
        principal = loan.line_ids.filtered(lambda l: l.kind == 'principal')
        self.assertEqual(len(principal), 3)
        self.assertAlmostEqual(sum(principal.mapped('amount')), 1_000_000.0, places=2)
        first_int = loan.line_ids.filtered(lambda l: l.kind == 'interest').sorted('date')[0]
        self.assertEqual(first_int.date, D(2026, 12, 10))
        self.assertAlmostEqual(first_int.amount, round(1_000_000 * 0.11 / 360 * 38, 2), places=2)
        self.assertEqual(loan.date_end, D(2027, 3, 10))
        with self.assertRaises(UserError):
            wiz.action_generate()

    # ------------------------------------------------------------------ Excel import
    def _xlsx_rows(self, rows):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Дата', 'Долг', 'Проценты', 'Комиссия'])
        for d, a, b, c in rows:
            ws.append([datetime.datetime.combine(d, datetime.time()), a, b, c])
        buf = io.BytesIO()
        wb.save(buf)
        return base64.b64encode(buf.getvalue())

    def _xlsx(self, blocks_rows):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws['A3'], ws['C3'], ws['I3'] = None, 'Банк Тест - 1 млн', 'Хамкорбанк 2'
        ws['C4'], ws['I4'] = 'Договор № 2025/77-Ю от 01.02.2026', 'Договор № 13 от 29.07.2025'
        heads = ['Дата', 'ДН', 'Проценты', 'Получен займ', 'БГ', 'Погашен %', 'Погашен осн.долг', 'Остаток',
                 'Проценты', 'Получен займ', 'Погашен %', 'Погашен осн.долг', 'Остаток']
        for j, h in enumerate(heads, 1):
            ws.cell(row=5, column=j, value=h)
        ws.cell(row=6, column=1, value='2026 год')
        r = 7
        bal1 = bal2 = 0.0
        for d, e1, e2 in blocks_rows:
            ws.cell(row=r, column=1, value=datetime.datetime.combine(d, datetime.time()))
            ws.cell(row=r, column=3, value='=H%s*8%%/360' % (r - 1))
            ws.cell(row=r, column=9, value='=M%s*13%%/365' % (r - 1))
            disb1, pct1, pr1 = e1
            disb2, pct2, pr2 = e2
            bal1 += disb1 - pr1
            bal2 += disb2 - pr2
            for col, v in ((4, disb1), (6, pct1), (7, pr1), (10, disb2), (11, pct2), (12, pr2)):
                if v:
                    ws.cell(row=r, column=col, value=v)
            ws.cell(row=r, column=8, value=bal1)
            ws.cell(row=r, column=13, value=bal2)
            r += 1
        buf = io.BytesIO()
        wb.save(buf)
        return base64.b64encode(buf.getvalue())

    def test_08_import_excel(self):
        rows = []
        d = D(2026, 1, 1)
        for i in range(120):
            e1 = (100_000.0 if i == 0 else 0, 2_000.0 if d.day == 28 else 0, 10_000.0 if d.day == 28 else 0)
            e2 = (50_000.0 if i == 2 else 0, 0, 25_000.0 if i in (60, 90) else 0)
            rows.append((d, e1, e2))
            d += datetime.timedelta(days=1)
        self.env['res.partner'].create({'name': 'HAMKORBANK ATB', 'is_company': True})
        wiz = self.env['nj.loan.import.wizard'].create({'file': self._xlsx(rows), 'filename': 't.xlsx'})
        wiz.action_parse()
        self.assertEqual(len(wiz.line_ids), 2)
        l1, l2 = wiz.line_ids.sorted('index')
        self.assertEqual((l1.basis, l2.basis), ('360', '365'))
        self.assertEqual(l2.partner_id.name, 'HAMKORBANK ATB')       # transliteration Хамкор → hamkor
        l1.partner_id = self.bank_a
        wiz.action_import()
        self.assertEqual(wiz.state, 'done')
        loans = self.env['nj.loan'].search([('excel_key', '!=', False)], order='name')
        self.assertEqual(len(loans), 2)
        k1 = loans.filtered(lambda l: l.bank_id == self.bank_a)
        self.assertEqual(k1.match_keys, '2025/77, 01.02.2026')
        self.assertEqual(k1.contract_date, D(2026, 2, 1))
        self.assertEqual(k1.amount, 100_000.0)
        self.assertEqual(k1.rate, 8.0)
        self.assertEqual(len(k1.line_ids.filtered(lambda l: l.kind == 'principal')), 4)
        self.assertIn("совпадает", k1.message_ids[0].body)
        # re-import updates the same loans
        n_lines = len(k1.line_ids)
        wiz2 = self.env['nj.loan.import.wizard'].create({'file': self._xlsx(rows), 'filename': 't.xlsx'})
        wiz2.action_parse()
        self.assertEqual(wiz2.line_ids.sorted('index')[0].loan_id, k1)
        wiz2.action_import()
        self.assertEqual(self.env['nj.loan'].search_count([('excel_key', '!=', False)]), 2)
        self.assertEqual(len(k1.line_ids), n_lines)
        with self.assertRaises(UserError):
            bad = self.env['nj.loan.import.wizard'].create({'file': base64.b64encode(b'not an excel'), 'filename': 'x'})
            bad.action_parse()

    # ------------------------------------------------------------------ reminders
    def test_09_reminders(self):
        today = D(2026, 11, 5)
        loan = self._loan('T-R', self.bank_a, [(D(2026, 1, 1), 'disb', 100_000.0),
                                               (D(2026, 11, 10), 'interest', 900.0),
                                               (D(2026, 12, 10), 'principal', 100_000.0)],
                          user_id=self.env.user.id)
        n = self.env['nj.loan']._create_reminder_activities(today)
        self.assertEqual(n, 1)
        act = loan.activity_ids
        self.assertEqual((act.date_deadline, act.user_id), (D(2026, 11, 10), self.env.user))
        self.assertEqual(self.env['nj.loan']._create_reminder_activities(today), 0)
        # overdue: plan in the loaded statement period without fact
        self.env.company.nj_fact_start_date = D(2026, 11, 1)
        self.env.company.nj_cfo_user_id = self.env.user
        # without statements after the due date nothing is overdue
        self.assertEqual(self.env['nj.loan']._create_reminder_activities(D(2026, 11, 13)), 0)
        self._cash_out(self.bank_b, D(2026, 11, 12), 10.0, "оплата")     # statements loaded up to 12.11
        self.assertEqual(self.env['nj.loan']._create_reminder_activities(D(2026, 11, 13)), 1)
        self.assertTrue(loan.activity_ids.filtered(lambda a: a.summary.startswith("Нет оплаты")))
        # pop-up once a day
        self.env.user.group_ids |= self.env.ref('nj_bank_loans.group_nj_loan_user')
        self.env.user.nj_reminder_date = False
        items = self.env['nj.loan'].with_context(tz='UTC').nj_popup_reminders()
        self.assertTrue(items)
        self.assertEqual(self.env['nj.loan'].nj_popup_reminders(), [])

    # ------------------------------------------------------------------ access rights
    def test_10_access_rights(self):
        viewer = self.env['res.users'].create({
            'name': 'Viewer', 'login': 'nj_viewer',
            'group_ids': [(6, 0, [self.env.ref('nj_bank_loans.group_nj_loan_user').id])]})
        treasurer = self.env['res.users'].create({
            'name': 'Treasurer', 'login': 'nj_treasurer',
            'group_ids': [(6, 0, [self.env.ref('nj_bank_loans.group_nj_loan_treasurer').id])]})
        loan = self._loan('T-A', self.bank_a, [(D(2026, 1, 1), 'disb', 1.0)])
        self.assertEqual(loan.with_user(viewer).name, 'T-A')
        with self.assertRaises(AccessError):
            self.env['nj.loan'].with_user(viewer).create({'name': 'X', 'bank_id': self.bank_a.id, 'contract': 'X'})
        new = self.env['nj.loan'].with_user(treasurer).create({'name': 'X', 'bank_id': self.bank_a.id, 'contract': 'X'})
        with self.assertRaises(AccessError):
            new.with_user(treasurer).unlink()
        self.assertTrue(self.env['nj.loan.day'].with_user(viewer).search_count([]))

    # ------------------------------------------------------------------ dashboard
    def test_11_dashboard(self):
        self._loan('T-D1', self.bank_a, [(D(2026, 1, 1), 'disb', 1_000_000.0), (D(2026, 11, 10), 'interest', 9_000.0),
                                         (D(2027, 6, 1), 'principal', 1_000_000.0)])
        self._loan('T-D2', self.bank_b, [(D(2026, 1, 1), 'disb', 1_000_000.0), (D(2026, 12, 20), 'principal', 500_000.0),
                                         (D(2027, 12, 20), 'principal', 500_000.0)],
                   rates=((D(2026, 1, 1), 8.0, '360'),))
        dash = self.env['nj.loan.dashboard'].create({'date': D(2026, 10, 27)})
        self.assertEqual(dash.debt, 2_000_000.0)
        self.assertEqual(dash.pay_30, 9_000.0)
        self.assertEqual(dash.pay_year, 509_000.0)
        self.assertEqual(dash.current_portion, 1_500_000.0)
        self.assertAlmostEqual(dash.avg_rate, 10.0, places=4)
        self.assertIn('Тест Банк А', dash.calendar_html)
        action = self.env['nj.loan.dashboard'].action_open()
        self.assertEqual(action['res_model'], 'nj.loan.dashboard')

    def test_12_menu_placement(self):
        root = self.env.ref('nj_bank_loans.menu_nj_loan_root')
        self.env['nj.loan']._nj_place_menu()
        self.assertEqual(root.parent_id, self.env.ref('account.menu_finance_entries'))
        self.assertFalse(root.child_id.child_id, "items must be flat (section -> items)")
        assets = self.env.ref('accountant.account_assets_liabilities_menu', raise_if_not_found=False)
        if assets:
            self.assertEqual(root.sequence, assets.sequence + 1)

    # ------------------------------------------------------------------ phase 2: penalties, auto prepayment, recipients
    def _cash_out(self, bank, date, amount, text, expense=None):
        """Bank account outflow (how 1C books a penalty debited by the bank)."""
        expense = expense or self.company_data['default_account_expense']
        move = self.env['account.move'].create({
            'move_type': 'entry', 'journal_id': self.journal.id, 'date': date,
            'line_ids': [
                (0, 0, {'account_id': expense.id, 'partner_id': bank.id, 'debit': amount, 'name': text}),
                (0, 0, {'account_id': self.journal.default_account_id.id, 'partner_id': bank.id,
                        'credit': amount, 'name': text}),
            ]})
        move.action_post()
        return move.line_ids.filtered(lambda l: l.account_id == self.journal.default_account_id)

    def test_13_penalty_and_overdue(self):
        self.env.company.nj_fact_start_date = D(2026, 9, 1)
        late = self._loan('T-L1', self.bank_a, [(D(2026, 1, 1), 'disb', 400_000.0),
                                                (D(2026, 9, 10), 'principal', 100_000.0)], penalty_rate=0.05)
        ok = self._loan('T-L2', self.bank_a, [(D(2026, 1, 1), 'disb', 300_000.0),
                                              (D(2026, 9, 12), 'principal', 50_000.0)])
        self._bank_line(self.bank_a, self.acc_principal, D(2026, 9, 12), 50_000.0, "Погашение кредита")
        pen = self._cash_out(self.bank_a, D(2026, 9, 18), 412.0, "Пеня за просрочку погашения кредита")
        other = self._cash_out(self.bank_a, D(2026, 9, 18), 55.0, "Комиссия за обслуживание счёта")
        self.env['nj.loan'].match_statement_lines()
        self.assertEqual(pen.nj_loan_payment_ids.kind, 'penalty')
        self.assertEqual(pen.nj_loan_payment_ids.loan_id, late)            # the loan with the overdue payment
        self.assertEqual(pen.nj_loan_payment_ids.match_method, 'overdue')
        self.assertFalse(other.nj_loan_payment_ids)                        # no penalty word
        self.assertEqual(self.env['nj.loan'].match_penalties(), 0)         # idempotent
        (late | ok)._refresh_status(today=D(2026, 9, 25))
        self.assertEqual((late.urgency, late.overdue_amount, late.overdue_since), ('overdue', 100_000.0, D(2026, 9, 10)))
        self.assertAlmostEqual(late.delay_cost_day, 50.0, places=2)        # 100 000 × 0.05 %
        self.assertNotEqual(ok.urgency, 'overdue')
        self.assertEqual(late.penalty_total, 412.0)
        # the penalty is not in plan-fact (no false interest difference)
        self.env.flush_all()
        pf = self.env['nj.loan.planfact'].search([('loan_id', '=', late.id), ('kind', '=', 'interest')])
        self.assertFalse(pf)
        dash = self.env['nj.loan.dashboard'].create({'date': D(2026, 9, 25)})
        self.assertEqual(dash.penalty_year, 412.0)
        self.assertEqual(dash.overdue_total, 100_000.0)

    def test_14_auto_prepayment_and_gain(self):
        loan = self._schedule_loan('term')
        self.env.company.nj_fact_start_date = D(2026, 6, 1)
        interest_before = sum(loan.line_ids.filtered(lambda l: l.kind == 'interest').mapped('amount'))
        # regular 28.06 payment made early on 10.06 together with 150 000 extra
        pay = self._bank_line(self.bank_a, self.acc_principal, D(2026, 6, 10), 250_000.0, "Погашение кредита")
        self.env['nj.loan'].match_statement_lines()
        pre = loan.prepayment_ids
        self.assertEqual(len(pre), 1)
        self.assertEqual((pre.source, pre.mode, pre.date, pre.amount), ('auto', 'term', D(2026, 6, 10), 150_000.0))
        self.assertEqual(pre.payment_id, pay.nj_loan_payment_ids)
        interest_after = sum(loan.line_ids.filtered(lambda l: l.kind == 'interest').mapped('amount'))
        self.assertAlmostEqual(pre.gain, interest_before - interest_after, delta=0.05)
        self.assertGreater(pre.gain, 0)
        self.assertAlmostEqual(sum(loan.line_ids.filtered(lambda l: l.kind == 'principal').mapped('amount')),
                               1_200_000.0, places=2)
        self.assertEqual(self.env['nj.loan'].match_statement_lines(), 0)
        self.assertEqual(len(loan.prepayment_ids), 1)                      # idempotent
        # the 28.06 instalment was paid early: not overdue (28.06 interest paid on time)
        int_jun = loan.line_ids.filtered(lambda l: l.kind == 'interest' and l.date == D(2026, 6, 28))
        self._bank_line(self.bank_a, self.acc_interest, D(2026, 6, 28), int_jun.amount, "Проценты")
        self.env['nj.loan'].match_statement_lines()
        self.assertEqual(len(loan.prepayment_ids), 1)
        loan._refresh_status(today=D(2026, 7, 5))
        self.assertNotEqual(loan.urgency, 'overdue')
        dash = self.env['nj.loan.dashboard'].create({'date': D(2026, 7, 5)})
        self.assertAlmostEqual(dash.gain_year, pre.gain, places=2)
        # bank schedule confirms the prepayment: final gain from the bank's interest
        wiz = self.env['nj.loan.schedule.wizard'].create({
            'loan_id': loan.id, 'date_from': D(2026, 6, 11),
            'file': self._xlsx_rows([(D(2026, 6, 28), 100_000.0, 5_000.0, 0), (D(2026, 7, 28), 950_000.0, 4_000.0, 0)])})
        wiz.action_apply()
        self.assertEqual(pre.state, 'bank')
        self.assertAlmostEqual(pre.interest_after, 9_000.0, places=2)

    def test_15_recipients(self):
        other = self.env['res.users'].create({'name': 'Treasurer', 'login': 'nj_treas',
                                              'group_ids': [(6, 0, [self.env.ref('nj_bank_loans.group_nj_loan_user').id])]})
        self.env.user.group_ids |= self.env.ref('nj_bank_loans.group_nj_loan_user')
        due = datetime.date.today() + datetime.timedelta(days=2)
        self._loan('T-N', self.bank_a, [(due - datetime.timedelta(days=30), 'disb', 1000.0), (due, 'principal', 1000.0)])
        co = self.env.company
        co.nj_cfo_user_id = False
        co.nj_notify_due_user_ids = other
        self.env.user.nj_reminder_date = False
        self.assertEqual(self.env['nj.loan'].nj_popup_reminders(), [])     # not ticked → nothing
        other_items = self.env['nj.loan'].with_user(other).nj_popup_reminders()
        self.assertTrue(other_items and other_items[0]['title'].startswith("Платёж по кредиту"))
        co.nj_notify_due_user_ids = False
        co.nj_cfo_user_id = self.env.user                                  # empty list → CFO
        self.env.user.nj_reminder_date = False
        self.assertTrue(self.env['nj.loan'].nj_popup_reminders())

    def test_16_excel_does_not_overwrite_odoo_changes(self):
        rows = []
        d = D(2026, 1, 1)
        for i in range(120):
            e1 = (100_000.0 if i == 0 else 0, 2_000.0 if d.day == 28 else 0, 10_000.0 if d.day == 28 else 0)
            e2 = (50_000.0 if i == 2 else 0, 0, 25_000.0 if i in (60, 90) else 0)
            rows.append((d, e1, e2))
            d += datetime.timedelta(days=1)
        wiz = self.env['nj.loan.import.wizard'].create({'file': self._xlsx(rows), 'filename': 't.xlsx'})
        wiz.action_parse()
        wiz.line_ids.write({'partner_id': self.bank_a.id})
        wiz.action_import()
        loan = self.env['nj.loan'].search([('excel_key', '!=', False)], order='name', limit=1)
        loan._apply_prepayment(D(2026, 2, 1), 20_000.0, mode='term')
        wiz2 = self.env['nj.loan.import.wizard'].create({'file': self._xlsx(rows), 'filename': 't.xlsx'})
        wiz2.action_parse()
        line = wiz2.line_ids.filtered(lambda l: l.loan_id == loan)
        self.assertFalse(line.do_import)
        self.assertTrue(line.warning)

    def test_17_prepayment_not_reduced_by_future_interest(self):
        """K-03 case: interest of the previous period paid, next interest not due yet → full excess."""
        loan = self._schedule_loan('term')
        self.env.company.nj_fact_start_date = D(2026, 5, 1)
        int_may = loan.line_ids.filtered(lambda l: l.kind == 'interest' and l.date == D(2026, 5, 28))
        self._bank_line(self.bank_a, self.acc_principal, D(2026, 5, 28), 100_000.0, "Погашение")
        self._bank_line(self.bank_a, self.acc_interest, D(2026, 5, 28), int_may.amount, "Проценты")
        self._bank_line(self.bank_a, self.acc_principal, D(2026, 6, 10), 250_000.0, "Погашение")
        self.env['nj.loan'].match_statement_lines()
        self.assertEqual(loan.prepayment_ids.mapped('amount'), [150_000.0])

    def test_18_penalty_not_spread_over_statement_siblings(self):
        """Production case: a day of the bank statement is ONE move with many lines.
        A penalty word on one line must not turn salary / supplier lines into penalties."""
        self.env.company.nj_fact_start_date = D(2026, 9, 1)
        loan = self._loan('T-S1', self.bank_a, [(D(2026, 1, 1), 'disb', 400_000.0),
                                                (D(2026, 9, 10), 'principal', 100_000.0)])
        cash = self.journal.default_account_id
        expense = self.company_data['default_account_expense']
        texts = [("Пеня за просрочку погашения кредита", 412.0, self.bank_a.id),
                 ("зар.плата за август 2026 года на пластиковые карточки", 150_000.0, False),
                 ("Предоставление фин. займа по Договору № 26/12-ФЗ", 200_000.0, False),
                 ("Возврат фин. займа по Договору № 17/03-ФЗ", 90_000.0, False)]
        lines = []
        for text, amount, partner in texts:
            lines.append((0, 0, {'account_id': expense.id, 'partner_id': partner,
                                 'debit': amount, 'name': text}))
            lines.append((0, 0, {'account_id': cash.id, 'partner_id': partner,
                                 'credit': amount, 'name': text}))
        move = self.env['account.move'].create({
            'move_type': 'entry', 'journal_id': self.journal.id,
            'date': D(2026, 9, 18), 'ref': "Выписка банка 00000000842", 'line_ids': lines})
        move.action_post()
        self.assertGreater(len(move.line_ids), self.env['nj.loan'].SIBLING_LIMIT)
        self.env['nj.loan'].match_statement_lines()
        paid = move.line_ids.filtered(lambda l: l.account_id == cash and l.nj_loan_payment_ids)
        self.assertEqual(len(paid), 1)
        self.assertEqual(paid.name, texts[0][0])
        self.assertEqual(paid.nj_loan_payment_ids.kind, 'penalty')
        self.assertEqual(loan.penalty_total, 412.0)

    def test_19_overdue_is_the_remainder_and_nets_principal_with_interest(self):
        """K-02 / K-03 case on the production copy: the whole planned line was reported as overdue."""
        co = self.env.company
        co.nj_fact_start_date = D(2026, 9, 1)
        bank_c = self.env['res.partner'].create({'name': "Тест Банк В", 'is_company': True})
        # A — 1C books part of the interest to the loan account: the split differs, the money is there
        a = self._loan('T-O1', self.bank_a, [(D(2026, 1, 1), 'disb', 1_000_000.0),
                                             (D(2026, 9, 19), 'principal', 228_556.80),
                                             (D(2026, 9, 19), 'interest', 63_197.89)])
        self._bank_line(self.bank_a, self.acc_principal, D(2026, 9, 9), 246_403.37, "Погашение")
        self._bank_line(self.bank_a, self.acc_interest, D(2026, 9, 9), 45_351.32, "Проценты")
        # B — interest short-paid well above the tolerance
        b = self._loan('T-O2', self.bank_b, [(D(2026, 1, 1), 'disb', 500_000.0),
                                             (D(2026, 9, 2), 'principal', 100_000.0),
                                             (D(2026, 9, 20), 'interest', 33_650.41)])
        self._bank_line(self.bank_b, self.acc_principal, D(2026, 9, 2), 100_000.0, "Погашение")
        self._bank_line(self.bank_b, self.acc_interest, D(2026, 9, 21), 20_000.0, "Проценты")
        # C — exchange-rate rounding: below 1 % of the largest scheduled payment, not a debt
        c = self._loan('T-O3', bank_c, [(D(2026, 1, 1), 'disb', 300_000.0),
                                        (D(2026, 9, 2), 'principal', 100_000.0)])
        self._bank_line(bank_c, self.acc_principal, D(2026, 9, 2), 99_800.0, "Погашение")
        self._cash_out(self.bank_a, D(2026, 9, 30), 10.0, "комиссия")     # statements loaded up to 30.09
        self.env['nj.loan'].match_statement_lines()
        (a | b | c)._refresh_status(today=D(2026, 10, 5))
        self.assertEqual((a.overdue_amount, a.urgency != 'overdue'), (0.0, True))
        self.assertAlmostEqual(b.overdue_amount, 13_650.41, places=2)
        self.assertEqual((b.urgency, b.overdue_since), ('overdue', D(2026, 9, 20)))
        self.assertEqual(c.overdue_amount, 0.0)

    def test_20_popup_colours_and_one_time(self):
        """Yellow inside «Скоро» (7 дн), red inside «Срочно» (3 дн), green for a prepayment, each once."""
        co = self.env.company
        co.nj_cfo_user_id = self.env.user
        co.nj_urgent_days, co.nj_soon_days = 3, 7
        self.env.user.group_ids |= self.env.ref('nj_bank_loans.group_nj_loan_user')
        today = datetime.date.today()
        far = today + datetime.timedelta(days=5)
        near = today + datetime.timedelta(days=2)
        self._loan('T-Y', self.bank_a, [(today - datetime.timedelta(days=60), 'disb', 50_000.0),
                                        (far, 'principal', 20_000.0)])
        self._loan('T-R', self.bank_b, [(today - datetime.timedelta(days=60), 'disb', 50_000.0),
                                        (near, 'principal', 30_000.0)])
        Loan = self.env['nj.loan'].with_context(tz='UTC')
        items = Loan.nj_popup_reminders()
        self.assertEqual(sorted(i['type'] for i in items), ['danger', 'warning'])
        self.assertEqual(Loan.nj_popup_reminders(), [])                    # one time is enough
        # a payment beyond «Скоро» stays silent
        self._loan('T-L', self.bank_a, [(today - datetime.timedelta(days=60), 'disb', 10_000.0),
                                        (today + datetime.timedelta(days=20), 'principal', 10_000.0)])
        self.assertEqual(Loan.nj_popup_reminders(), [])
        # a prepayment is green and also shown once
        loan = self._schedule_loan('term')
        loan._apply_prepayment(today, 20_000.0, mode='term')
        green = Loan.nj_popup_reminders()
        self.assertIn('success', [i['type'] for i in green])
        self.assertTrue(any("Досрочное погашение" in i['title'] for i in green))
        self.assertEqual(Loan.nj_popup_reminders(), [])

    def test_21_bank_guarantee_fee_separate_from_interest(self):
        """БГ is its own column (as in Excel): not inside «Погашен %», but inside the total payment."""
        loan = self._loan('T-BG', self.bank_a, [(D(2026, 1, 1), 'disb', 100_000.0),
                                                (D(2026, 2, 10), 'interest', 1_000.0),
                                                (D(2026, 2, 10), 'fee', 250.0),
                                                (D(2026, 2, 20), 'principal', 10_000.0)])
        self.env.flush_all()
        m = self.env['nj.loan.month'].search([('loan_id', '=', loan.id), ('month', '=', D(2026, 2, 1))])
        self.assertEqual((m.interest_paid, m.fee, m.principal, m.payment), (1_000.0, 250.0, 10_000.0, 11_250.0))
        day = self.env['nj.loan.day'].search([('loan_id', '=', loan.id), ('date', '=', D(2026, 2, 10))])
        self.assertEqual((day.interest_paid, day.fee), (1_000.0, 250.0))
