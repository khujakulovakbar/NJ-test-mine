from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    nj_remind_days = fields.Char(
        "Напоминать за (дней)", default="5,1",
        help="Через запятую: за сколько дней до платежа создавать напоминание.")
    nj_urgent_days = fields.Integer("Срочно, дней", default=3)
    nj_soon_days = fields.Integer("Скоро, дней", default=7)
    nj_fact_start_date = fields.Date(
        "Факт из выписок с",
        help="С этой даты выписки банка загружены полностью: строки сопоставляются с кредитами, "
             "а плановый платёж без факта считается просроченным.")
    nj_cfo_user_id = fields.Many2one('res.users', "Контроль просрочки (CFO)")
    nj_match_days = fields.Integer("Окно сопоставления, дней", default=10)
    nj_match_tolerance = fields.Float("Допуск по сумме, %", default=1.0)
    nj_penalty_keywords = fields.Char(
        "Слова пени в выписке", default="пеня,пени,штраф,неустойк,jarima,penya,peni,penalty",
        help="Через запятую. Списание с банковского счёта в пользу банка-кредитора с таким словом "
             "в назначении считается пеней по кредиту.")
    nj_prepay_window = fields.Integer(
        "Окно досрочного, дней", default=31,
        help="Оплата долга сверх графика на ближайшие N дней считается досрочным погашением "
             "(раньше срока в пределах окна — просто ранняя оплата).")
    nj_notify_due_user_ids = fields.Many2many(
        'res.users', 'nj_company_notify_due_rel', 'company_id', 'user_id', "Платёж скоро / сегодня")
    nj_notify_overdue_user_ids = fields.Many2many(
        'res.users', 'nj_company_notify_overdue_rel', 'company_id', 'user_id', "Просрочка")
    nj_notify_penalty_user_ids = fields.Many2many(
        'res.users', 'nj_company_notify_penalty_rel', 'company_id', 'user_id', "Списана пеня")
    nj_notify_prepay_user_ids = fields.Many2many(
        'res.users', 'nj_company_notify_prepay_rel', 'company_id', 'user_id', "Досрочное погашение")
    nj_notify_paid_user_ids = fields.Many2many(
        'res.users', 'nj_company_notify_paid_rel', 'company_id', 'user_id', "Оплачено вовремя / кредит закрыт")

    def _nj_recipients(self, event):
        """Users for a notification event; empty list → the CFO only."""
        self.ensure_one()
        users = self['nj_notify_%s_user_ids' % event]
        return users or self.nj_cfo_user_id


class ResUsers(models.Model):
    _inherit = 'res.users'

    nj_reminder_date = fields.Date("Напоминание о кредитах показано", copy=False)
