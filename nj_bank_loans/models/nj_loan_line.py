from odoo import api, fields, models
from odoo.exceptions import ValidationError

KINDS = [
    ('disb', "Получен займ"),
    ('principal', "Погашен осн. долг"),
    ('interest', "Погашен %"),
    ('fee', "Комиссия / БГ"),
    ('adj', "Корректировка начисления"),
]


class NjLoanLine(models.Model):
    _name = 'nj.loan.line'
    _description = "Строка графика кредита"
    _order = 'date, loan_id, id'

    loan_id = fields.Many2one('nj.loan', "Кредит", required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='loan_id.company_id', store=True, index=True)
    bank_id = fields.Many2one(related='loan_id.bank_id', store=True, string="Банк")
    currency_id = fields.Many2one(related='loan_id.currency_id')
    date = fields.Date("Дата", required=True, index=True)
    kind = fields.Selection(KINDS, "Вид", required=True)
    amount = fields.Monetary("Сумма", required=True)
    source = fields.Selection([('excel', "Excel"), ('manual', "Вручную"), ('recalc', "Пересчёт"),
                               ('bank', "График банка"), ('generated', "Сформирован")],
                              "Источник", default='manual', required=True)
    note = fields.Char("Комментарий")

    @api.constrains('amount', 'kind')
    def _check_amount(self):
        for line in self:
            if line.kind in ('disb', 'interest', 'fee') and line.amount < 0:
                raise ValidationError("Сумма «%s» не может быть отрицательной." % dict(KINDS)[line.kind])

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        if not self.env.context.get('nj_no_refresh'):
            lines.loan_id._refresh_status()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if not self.env.context.get('nj_no_refresh') and {'date', 'amount', 'kind'} & set(vals):
            self.loan_id._refresh_status()
        return res

    def unlink(self):
        loans = self.loan_id
        res = super().unlink()
        if not self.env.context.get('nj_no_refresh'):
            loans.exists()._refresh_status()
        return res


class NjLoanRate(models.Model):
    _name = 'nj.loan.rate'
    _description = "Ставка кредита"
    _order = 'loan_id, date_from'

    loan_id = fields.Many2one('nj.loan', "Кредит", required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='loan_id.company_id', store=True)
    date_from = fields.Date("С даты", required=True)
    rate = fields.Float("Ставка, % годовых", required=True, digits=(16, 6))
    basis = fields.Selection([('360', "360"), ('365', "365")], "База", required=True, default='360')

    _date_uniq = models.Constraint('unique(loan_id, date_from)', "На одну дату может быть только одна ставка.")

    @api.constrains('rate')
    def _check_rate(self):
        for r in self:
            if r.rate < 0 or r.rate > 100:
                raise ValidationError("Ставка должна быть от 0 до 100%.")


class NjLoanPayment(models.Model):
    _name = 'nj.loan.payment'
    _description = "Факт: платёж банку по кредиту"
    _order = 'date desc, id desc'

    loan_id = fields.Many2one('nj.loan', "Кредит", required=True, ondelete='cascade', index=True)
    move_line_id = fields.Many2one('account.move.line', "Строка выписки", required=True,
                                   ondelete='cascade', index=True)
    move_id = fields.Many2one(related='move_line_id.move_id', string="Документ")
    company_id = fields.Many2one(related='loan_id.company_id', store=True, index=True)
    bank_id = fields.Many2one(related='loan_id.bank_id', store=True, string="Банк")
    currency_id = fields.Many2one(related='loan_id.currency_id')
    date = fields.Date(related='move_line_id.date', store=True, string="Дата")
    label = fields.Char(related='move_line_id.name', string="Назначение")
    kind = fields.Selection([('principal', "Долг"), ('interest', "Проценты"), ('penalty', "Пеня / штраф")],
                            "Вид", required=True)
    amount = fields.Monetary("Сумма", required=True)
    match_method = fields.Selection([('key', "По ключу договора"), ('single', "Единственный кредит банка"),
                                     ('amount', "По сумме и дате"), ('overdue', "Пеня: кредит с просрочкой"),
                                     ('manual', "Вручную")],
                                    "Как привязано", required=True, default='manual')

    _line_uniq = models.Constraint('unique(move_line_id)', "Строка выписки уже привязана к кредиту.")


class NjLoanPrepayment(models.Model):
    """One early repayment and its effect: interest of the schedule after the date before and after
    the recalculation.  Gain = interest saved."""
    _name = 'nj.loan.prepayment'
    _description = "Досрочное погашение и экономия"
    _order = 'date desc, id desc'

    loan_id = fields.Many2one('nj.loan', "Кредит", required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='loan_id.company_id', store=True, index=True)
    bank_id = fields.Many2one(related='loan_id.bank_id', store=True, string="Банк")
    currency_id = fields.Many2one(related='loan_id.currency_id')
    date = fields.Date("Дата", required=True)
    amount = fields.Monetary("Сумма досрочно", required=True)
    mode = fields.Selection([('term', "Сокращение срока"), ('payment', "Уменьшение платежа"),
                             ('bank', "Новый график банка")], "Пересчёт", required=True)
    source = fields.Selection([('auto', "Из выписки (авто)"), ('manual', "Вручную")], "Источник",
                              required=True, default='manual')
    payment_id = fields.Many2one('nj.loan.payment', "Платёж в выписке", ondelete='set null')
    interest_before = fields.Monetary("% по графику до", readonly=True)
    interest_after = fields.Monetary("% по графику после", readonly=True)
    gain = fields.Monetary("Экономия %", compute='_compute_gain', store=True)
    state = fields.Selection([('estimated', "Расчётный (ждём график банка)"), ('bank', "По графику банка")],
                             "Статус", default='estimated', required=True)

    @api.depends('interest_before', 'interest_after', 'state', 'mode')
    def _compute_gain(self):
        for rec in self:
            known = rec.mode != 'bank' or rec.state == 'bank'
            rec.gain = (rec.interest_before - rec.interest_after) if known else 0.0


class NjLoanNotice(models.Model):
    """One row per pop-up already shown to a user: a reminder is given once, not every day (R23)."""
    _name = 'nj.loan.notice'
    _description = "Показанное уведомление по кредитам"
    _rec_name = 'key'
    _order = 'id desc'

    user_id = fields.Many2one('res.users', "Пользователь", required=True, ondelete='cascade', index=True)
    key = fields.Char("Событие", required=True, index=True)
