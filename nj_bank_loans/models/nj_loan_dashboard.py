import datetime
from collections import defaultdict

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.tools import format_amount, format_date


class NjLoanDashboard(models.TransientModel):
    _name = 'nj.loan.dashboard'
    _description = "Кредиты: обзор"

    date = fields.Date("Дата отчёта", required=True, default=fields.Date.context_today)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    currency_id = fields.Many2one('res.currency', compute='_compute_kpi')
    debt = fields.Monetary("Долг на дату", compute='_compute_kpi')
    pay_30 = fields.Monetary("Платежи за 30 дней", compute='_compute_kpi')
    pay_year = fields.Monetary("Платежи до конца года", compute='_compute_kpi')
    current_portion = fields.Monetary("Погасить в 12 мес.", compute='_compute_kpi')
    avg_rate = fields.Float("Средневзв. ставка, %", compute='_compute_kpi', digits=(16, 2))
    loan_count = fields.Integer("Кредитов", compute='_compute_kpi')
    penalty_year = fields.Monetary("Потери: пени за год", compute='_compute_kpi')
    gain_year = fields.Monetary("Экономия от досрочных за год", compute='_compute_kpi')
    overdue_total = fields.Monetary("Просрочено сейчас", compute='_compute_kpi')
    delay_cost_day = fields.Monetary("Стоимость дня просрочки", compute='_compute_kpi')
    unmatched_count = fields.Integer("Не сопоставлено строк выписки", compute='_compute_kpi')
    calendar_html = fields.Html("Платёжный календарь", compute='_compute_kpi', sanitize=False)
    bank_html = fields.Html("По банкам", compute='_compute_kpi', sanitize=False)

    @api.depends('date')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "Обзор кредитов"

    @api.model
    def action_open(self):
        rec = self.create({})
        return {'type': 'ir.actions.act_window', 'name': "Обзор кредитов", 'res_model': self._name,
                'res_id': rec.id, 'view_mode': 'form', 'target': 'current',
                'context': {'form_view_initial_mode': 'edit'}}

    def action_refresh(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'current'}

    @api.depends('date', 'company_id')
    def _compute_kpi(self):
        for rec in self:
            day = rec.date or fields.Date.context_today(rec)
            loans = self.env['nj.loan'].search([('company_id', '=', rec.company_id.id),
                                                ('state', 'in', ('active', 'closed'))])
            currency = loans[:1].currency_id or rec.company_id.currency_id
            rec.currency_id = currency
            lines = loans.line_ids
            def bal(loan, d):
                return sum(l.amount for l in loan.line_ids if l.kind == 'disb' and l.date <= d) \
                    - sum(l.amount for l in loan.line_ids if l.kind == 'principal' and l.date <= d)
            balances = {loan: bal(loan, day) for loan in loans}
            debt = sum(balances.values())
            pay = lines.filtered(lambda l: l.kind in ('principal', 'interest', 'fee') and l.date > day)
            year_end = datetime.date(day.year, 12, 31)
            rec.debt = debt
            rec.pay_30 = sum(pay.filtered(lambda l: l.date <= day + datetime.timedelta(days=30)).mapped('amount'))
            rec.pay_year = sum(pay.filtered(lambda l: l.date <= year_end).mapped('amount'))
            rec.current_portion = sum(pay.filtered(
                lambda l: l.kind == 'principal' and l.date <= day + datetime.timedelta(days=365)).mapped('amount'))
            rec.avg_rate = (sum(b * loan.current_rate(day).rate for loan, b in balances.items()) / debt) if debt else 0.0
            rec.loan_count = len(loans.filtered(lambda l: abs(balances[l]) >= 0.005))
            y0, y1 = datetime.date(day.year, 1, 1), datetime.date(day.year, 12, 31)
            pens = self.env['nj.loan.payment'].search([('loan_id', 'in', loans.ids), ('kind', '=', 'penalty'),
                                                       ('date', '>=', y0), ('date', '<=', y1)])
            pres = self.env['nj.loan.prepayment'].search([('loan_id', 'in', loans.ids),
                                                          ('date', '>=', y0), ('date', '<=', y1)])
            rec.penalty_year = sum(pens.mapped('amount'))
            rec.gain_year = sum(pres.mapped('gain'))
            rec.overdue_total = sum(loans.mapped('overdue_amount'))
            rec.delay_cost_day = sum(loans.mapped('delay_cost_day'))
            rec.unmatched_count = self.env['account.move.line'].search_count(
                self.env['nj.loan']._unmatched_domain(loans)) if loans else 0
            rec.calendar_html = rec._calendar_html(pay, day, currency)
            rec.bank_html = rec._bank_html(loans, balances, pay, day, currency)

    def _year_domain(self):
        return [('company_id', '=', self.company_id.id), ('date', '>=', datetime.date(self.date.year, 1, 1)),
                ('date', '<=', datetime.date(self.date.year, 12, 31))]

    def action_open_penalties(self):
        return {'type': 'ir.actions.act_window', 'name': "Пени, %s" % self.date.year, 'res_model': 'nj.loan.payment',
                'view_mode': 'list,pivot', 'domain': self._year_domain() + [('kind', '=', 'penalty')],
                'context': {'create': False}}

    def action_open_gains(self):
        return {'type': 'ir.actions.act_window', 'name': "Досрочные погашения, %s" % self.date.year,
                'res_model': 'nj.loan.prepayment', 'view_mode': 'list,pivot', 'domain': self._year_domain(),
                'context': {'create': False}}

    def action_open_overdue(self):
        action = self.env['ir.actions.act_window']._for_xml_id('nj_bank_loans.action_nj_loan')
        action['domain'] = [('company_id', '=', self.company_id.id), ('urgency', '=', 'overdue')]
        action['context'] = {}
        return action

    def action_open_unmatched(self):
        return self.env['nj.loan'].action_open_unmatched()

    def action_import(self):
        return self.env['ir.actions.act_window']._for_xml_id('nj_bank_loans.action_nj_loan_import_wizard')

    def action_match(self):
        n = self.env['nj.loan'].match_statement_lines()
        self.env['nj.loan'].search([('company_id', '=', self.company_id.id),
                                    ('state', 'in', ('active', 'closed'))])._refresh_status()
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': "Сопоставление выписок", 'type': 'info', 'sticky': False,
                           'message': "Привязано строк выписки: %s" % n,
                           'next': self.action_refresh()}}

    def _fmt(self, amount, currency):
        return format_amount(self.env, amount, currency)

    def _calendar_html(self, pay, day, currency):
        groups = defaultdict(lambda: self.env['nj.loan.line'])
        for l in pay.filtered(lambda l: l.date <= day + datetime.timedelta(days=60)):
            groups[(l.date, l.loan_id.bank_id)] |= l
        rows = []
        for (d, bank), ls in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1].display_name or '')):
            days = (d - day).days
            color = '#991B1B' if days <= (self.company_id.nj_urgent_days or 0) else \
                '#92400E' if days <= (self.company_id.nj_soon_days or 0) else '#1F2937'
            principal = sum(ls.filtered(lambda l: l.kind == 'principal').mapped('amount'))
            interest = sum(ls.filtered(lambda l: l.kind != 'principal').mapped('amount'))
            rows.append(Markup(
                "<tr><td style='color:%s;font-weight:600'>%s</td><td style='text-align:right'>%s</td>"
                "<td>%s</td><td>%s</td><td style='text-align:right'>%s</td><td style='text-align:right'>%s</td>"
                "<td style='text-align:right;font-weight:600'>%s</td></tr>") % (
                color, format_date(self.env, d), days, bank.display_name, ", ".join(ls.loan_id.mapped('name')),
                self._fmt(principal, currency), self._fmt(interest, currency), self._fmt(principal + interest, currency)))
        if not rows:
            return Markup("<p>Платежей в ближайшие 60 дней нет.</p>")
        head = Markup("<table class='table table-sm'><thead><tr><th>Дата</th><th style='text-align:right'>Дней</th>"
                      "<th>Банк</th><th>Кредиты</th><th style='text-align:right'>Долг</th>"
                      "<th style='text-align:right'>Проценты</th><th style='text-align:right'>Всего</th></tr></thead><tbody>")
        return head + Markup('').join(rows) + Markup("</tbody></table>")

    def _bank_html(self, loans, balances, pay, day, currency):
        year_end = datetime.date(day.year, 12, 31)
        rows = []
        total = [0.0, 0.0, 0.0]
        for bank in loans.bank_id.sorted('display_name'):
            bl = loans.filtered(lambda l: l.bank_id == bank)
            b = sum(balances[l] for l in bl)
            p = pay.filtered(lambda l: l.loan_id in bl and l.date <= year_end)
            pr = sum(p.filtered(lambda l: l.kind == 'principal').mapped('amount'))
            it = sum(p.filtered(lambda l: l.kind != 'principal').mapped('amount'))
            total = [total[0] + b, total[1] + pr, total[2] + it]
            rows.append(Markup("<tr><td>%s</td><td style='text-align:right'>%s</td><td style='text-align:right'>%s</td>"
                               "<td style='text-align:right'>%s</td><td style='text-align:right'>%s</td></tr>") % (
                bank.display_name, len(bl), self._fmt(b, currency), self._fmt(pr, currency), self._fmt(it, currency)))
        head = Markup("<table class='table table-sm'><thead><tr><th>Банк</th><th style='text-align:right'>Кредитов</th>"
                      "<th style='text-align:right'>Остаток</th><th style='text-align:right'>Долг до конца года</th>"
                      "<th style='text-align:right'>% до конца года</th></tr></thead><tbody>")
        foot = Markup("<tr style='font-weight:700'><td>Итого</td><td></td><td style='text-align:right'>%s</td>"
                      "<td style='text-align:right'>%s</td><td style='text-align:right'>%s</td></tr>") % (
            self._fmt(total[0], currency), self._fmt(total[1], currency), self._fmt(total[2], currency))
        return head + Markup('').join(rows) + foot + Markup("</tbody></table>")
