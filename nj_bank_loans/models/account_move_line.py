from odoo import fields, models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    nj_loan_payment_ids = fields.One2many('nj.loan.payment', 'move_line_id', "Кредит (факт)")

    def action_nj_assign(self):
        return {
            'type': 'ir.actions.act_window', 'name': "Привязать к кредиту",
            'res_model': 'nj.loan.assign.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_move_line_ids': self.ids},
        }
