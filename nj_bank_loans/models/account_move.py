import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        self._nj_loan_sync(posted)
        return posted

    def button_draft(self):
        self._nj_loan_drop_facts()
        return super().button_draft()

    def button_cancel(self):
        self._nj_loan_drop_facts()
        return super().button_cancel()

    @api.model
    def _nj_loan_sync(self, moves):
        """A bank statement was posted (1C import or manually): link its 7811 / 6921 lines to the loans and
        refresh balances and early repayments right away (the nightly cron stays as a safety net).
        Cheap filters first: active loans of the companies -> only lines on the loan accounts.
        It must never block posting: any error is logged and rolled back to a savepoint."""
        if not moves or self.env.context.get('nj_loan_no_sync'):
            return
        try:
            with self.env.cr.savepoint():
                Loan = self.env['nj.loan'].sudo()
                loans = Loan.search([('state', 'in', ('active', 'closed')),
                                     ('company_id', 'in', moves.company_id.ids)])
                if not loans:
                    return
                accounts = loans.principal_account_id | loans.interest_account_id
                lines = self.env['account.move.line'].sudo().search([
                    ('move_id', 'in', moves.ids), ('account_id', 'in', accounts.ids),
                    ('debit', '>', 0), ('nj_loan_payment_ids', '=', False)])
                if lines:
                    Loan._nj_autoset_fact_start()
                    Loan.match_statement_lines(lines)
        except Exception:  # noqa: BLE001 - accounting must not fail because of the loan registry
            _logger.exception("nj_bank_loans: automatic matching of posted entries failed")

    def _nj_loan_drop_facts(self):
        """An entry returns to draft / is cancelled: it is no longer a fact, drop its loan links
        (they are rebuilt when the entry is posted again)."""
        try:
            with self.env.cr.savepoint():
                payments = self.env['nj.loan.payment'].sudo().search([('move_line_id.move_id', 'in', self.ids)])
                if payments:
                    loans = payments.loan_id
                    payments.unlink()
                    loans._refresh_status()
        except Exception:  # noqa: BLE001
            _logger.exception("nj_bank_loans: dropping loan facts of unposted entries failed")
