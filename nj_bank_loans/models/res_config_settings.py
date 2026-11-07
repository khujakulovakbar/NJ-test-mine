from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    nj_remind_days = fields.Char(related='company_id.nj_remind_days', readonly=False)
    nj_urgent_days = fields.Integer(related='company_id.nj_urgent_days', readonly=False)
    nj_soon_days = fields.Integer(related='company_id.nj_soon_days', readonly=False)
    nj_fact_start_date = fields.Date(related='company_id.nj_fact_start_date', readonly=False)
    nj_cfo_user_id = fields.Many2one(related='company_id.nj_cfo_user_id', readonly=False)
    nj_match_days = fields.Integer(related='company_id.nj_match_days', readonly=False)
    nj_match_tolerance = fields.Float(related='company_id.nj_match_tolerance', readonly=False)
    nj_penalty_keywords = fields.Char(related='company_id.nj_penalty_keywords', readonly=False)
    nj_prepay_window = fields.Integer(related='company_id.nj_prepay_window', readonly=False)
    nj_notify_due_user_ids = fields.Many2many(related='company_id.nj_notify_due_user_ids', readonly=False)
    nj_notify_overdue_user_ids = fields.Many2many(related='company_id.nj_notify_overdue_user_ids', readonly=False)
    nj_notify_penalty_user_ids = fields.Many2many(related='company_id.nj_notify_penalty_user_ids', readonly=False)
    nj_notify_prepay_user_ids = fields.Many2many(related='company_id.nj_notify_prepay_user_ids', readonly=False)
