import base64
import datetime
import io

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round, format_amount, format_date

from ..tools import engine

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None


class NjLoanPrepayWizard(models.TransientModel):
    _name = 'nj.loan.prepay.wizard'
    _description = "Досрочное погашение"

    loan_id = fields.Many2one('nj.loan', required=True, readonly=True)
    currency_id = fields.Many2one(related='loan_id.currency_id')
    date = fields.Date("Дата погашения", required=True, default=fields.Date.context_today)
    amount = fields.Monetary("Сумма досрочного погашения", required=True)
    mode = fields.Selection([('term', "Сокращение срока"), ('payment', "Уменьшение платежа"),
                             ('bank', "Новый график банка (загружу файл)")], "Пересчёт", required=True)
    note = fields.Char("Комментарий")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        loan = self.env['nj.loan'].browse(res.get('loan_id') or self.env.context.get('active_id'))
        if loan:
            res.setdefault('loan_id', loan.id)
            res.setdefault('mode', loan.prepay_mode)
        return res

    def action_apply(self):
        self.ensure_one()
        loan = self.loan_id
        loan._apply_prepayment(self.date, self.amount, mode=self.mode, note=self.note, source='manual')
        if self.mode == 'bank':
            return {'type': 'ir.actions.act_window', 'res_model': 'nj.loan.schedule.wizard', 'view_mode': 'form',
                    'target': 'new', 'context': {'default_loan_id': loan.id,
                                                 'default_date_from': self.date + datetime.timedelta(days=1)}}
        return {'type': 'ir.actions.act_window_close'}


class NjLoanScheduleWizard(models.TransientModel):
    _name = 'nj.loan.schedule.wizard'
    _description = "Загрузка графика банка"

    loan_id = fields.Many2one('nj.loan', required=True)
    date_from = fields.Date("Заменить график с даты", required=True, default=fields.Date.context_today)
    file = fields.Binary("Файл Excel", required=True)
    filename = fields.Char()

    def _read_rows(self):
        if openpyxl is None:
            raise UserError("Библиотека openpyxl не установлена.")
        try:
            wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(self.file)), read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001
            raise UserError("Не удалось открыть файл: %s" % e) from e
        rows = []
        for row in wb.worksheets[0].iter_rows(values_only=True):
            if not row:
                continue
            d = row[0]
            if isinstance(d, datetime.datetime):
                d = d.date()
            if not isinstance(d, datetime.date):
                continue
            nums = [float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0 for x in row[1:4]]
            nums += [0.0] * (3 - len(nums))
            rows.append((d, nums[0], nums[1], nums[2]))
        if not rows:
            raise UserError("В файле нет строк с датой в колонке A. Колонки: Дата | Погашение долга | Проценты | Комиссия.")
        return rows

    def action_apply(self):
        self.ensure_one()
        rows = self._read_rows()
        loan = self.loan_id
        Line = self.env['nj.loan.line'].with_context(nj_no_refresh=True)
        loan.line_ids.filtered(lambda l: l.kind in ('principal', 'interest', 'fee') and l.date >= self.date_from) \
            .with_context(nj_no_refresh=True).unlink()
        vals = []
        for d, principal, interest, fee in rows:
            if d < self.date_from:
                continue
            for kind, amount in (('principal', principal), ('interest', interest), ('fee', fee)):
                if abs(amount) >= loan.currency_id.rounding:
                    vals.append({'loan_id': loan.id, 'date': d, 'kind': kind, 'amount': amount, 'source': 'bank'})
        Line.create(vals)
        # the bank schedule confirms the open early repayments: final gain
        for pre in loan.prepayment_ids.filtered(lambda r: r.state == 'estimated' and r.date < self.date_from):
            pre.write({'interest_after': loan._future_interest(pre.date), 'state': 'bank'})
        loan._refresh_status()
        loan.message_post(body="Загружен график банка «%s» с %s: строк %s." % (
            self.filename or '', format_date(self.env, self.date_from), len(vals)))
        return {'type': 'ir.actions.act_window_close'}


class NjLoanGenerateWizard(models.TransientModel):
    _name = 'nj.loan.generate.wizard'
    _description = "График нового кредита"

    loan_id = fields.Many2one('nj.loan', required=True)
    currency_id = fields.Many2one(related='loan_id.currency_id')
    amount = fields.Monetary("Сумма кредита", required=True)
    date_disb = fields.Date("Дата выдачи", required=True, default=fields.Date.context_today)
    rate = fields.Float("Ставка, % годовых", required=True, digits=(16, 4))
    basis = fields.Selection([('360', "360"), ('365', "365")], "База", required=True, default='360')
    method = fields.Selection([('equal', "Равные доли долга"), ('bullet', "Весь долг в конце")],
                              "Погашение долга", required=True, default='equal')
    first_principal_date = fields.Date("Первое погашение долга", required=True)
    principal_count = fields.Integer("Количество погашений", required=True, default=12)
    principal_interval = fields.Integer("Интервал погашений, мес.", required=True, default=1)
    first_interest_date = fields.Date("Первая выплата %", required=True)
    interest_interval = fields.Integer("Интервал выплаты %, мес.", required=True, default=1)

    def action_generate(self):
        self.ensure_one()
        loan = self.loan_id
        cur = loan.currency_id
        if self.amount <= 0 or self.principal_count <= 0:
            raise UserError("Сумма и количество погашений должны быть больше нуля.")
        if self.first_principal_date <= self.date_disb or self.first_interest_date <= self.date_disb:
            raise UserError("Даты погашений должны быть позже даты выдачи.")
        if loan.line_ids.filtered(lambda l: l.kind != 'adj'):
            raise UserError("У кредита уже есть график. Для изменения используйте досрочное погашение или график банка.")
        Line = self.env['nj.loan.line'].with_context(nj_no_refresh=True)
        if not loan.rate_ids.filtered(lambda r: r.date_from <= self.date_disb):
            self.env['nj.loan.rate'].create({'loan_id': loan.id, 'date_from': self.date_disb,
                                             'rate': self.rate, 'basis': self.basis})
        Line.create({'loan_id': loan.id, 'date': self.date_disb, 'kind': 'disb', 'amount': self.amount,
                     'source': 'generated'})
        dates = [engine.add_months(self.first_principal_date, i * self.principal_interval,
                                   self.first_principal_date.day) for i in range(self.principal_count)]
        if self.method == 'bullet':
            parts = [0.0] * (len(dates) - 1) + [self.amount]
        else:
            share = float_round(self.amount / len(dates), precision_rounding=cur.rounding)
            parts = [share] * (len(dates) - 1) + [float_round(self.amount - share * (len(dates) - 1),
                                                              precision_rounding=cur.rounding)]
        Line.create([{'loan_id': loan.id, 'date': d, 'kind': 'principal', 'amount': a, 'source': 'generated'}
                     for d, a in zip(dates, parts) if a])
        last = dates[-1]
        acc = engine.accrual_map(loan._daily())
        pay_dates = []
        i = 0
        while True:
            d = engine.add_months(self.first_interest_date, i * self.interest_interval, self.first_interest_date.day)
            if d >= last:
                break
            pay_dates.append(d)
            i += 1
        pay_dates.append(last)
        prev = self.date_disb
        vals = []
        for d in pay_dates:
            amount = float_round(loan._interest_for_payment(acc, d, prev), precision_rounding=cur.rounding)
            prev = d
            if amount >= cur.rounding:
                vals.append({'loan_id': loan.id, 'date': d, 'kind': 'interest', 'amount': amount, 'source': 'generated'})
        Line.create(vals)
        loan.state = 'active'
        loan._refresh_status()
        loan.message_post(body="Сформирован график: погашений долга %s, выплат процентов %s." % (
            len([a for a in parts if a]), len(vals)))
        return {'type': 'ir.actions.act_window_close'}


class NjLoanAssignWizard(models.TransientModel):
    _name = 'nj.loan.assign.wizard'
    _description = "Привязать строки выписки к кредиту"

    move_line_ids = fields.Many2many('account.move.line', string="Строки выписки", required=True)
    loan_id = fields.Many2one('nj.loan', "Кредит", required=True)

    def action_assign(self):
        self.ensure_one()
        loan = self.loan_id
        vals = []
        for line in self.move_line_ids:
            if line.nj_loan_payment_ids:
                continue
            if line.account_id not in (loan.principal_account_id | loan.interest_account_id):
                raise UserError("Счёт строки %s не совпадает со счетами кредита %s." % (line.account_id.display_name, loan.name))
            vals.append({'loan_id': loan.id, 'move_line_id': line.id, 'match_method': 'manual',
                         'kind': 'principal' if line.account_id == loan.principal_account_id else 'interest',
                         'amount': self.env['nj.loan']._line_amount(line, loan)})
        self.env['nj.loan.payment'].create(vals)
        return {'type': 'ir.actions.act_window_close'}
